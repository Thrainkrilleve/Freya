import json
import re
import urllib.parse
from typing import Optional

from freya.media.ranges import (
    CHUNK_BYTES,
    ByteResponse,
    Unsatisfiable,
    parse_range,
    slice_whole,
    unsatisfiable,
)
from freya.media.transport import http_request


MAX_PAGE = 100
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SEARCH = 100

# What may be asked for, and what the window can play itself.
ITEM_TYPES = "Movie,Episode,Video,MusicVideo,Audio"
WINDOW_CONTAINERS = {"mp4", "m4v", "webm", "mp3", "m4a", "ogg", "opus", "wav", "flac", "aac"}

_ITEM_ID = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class JellyfinError(RuntimeError):
    """
    Something went wrong talking to Jellyfin. The message never holds
    the access token.
    """


def item_id(value) -> Optional[str]:
    """
    A Jellyfin item ID in its plain form (32 lowercase hex digits), or
    None if `value` is not one.
    """
    if not isinstance(value, str) or not _ITEM_ID.match(value.strip()):
        return None

    return value.strip().replace("-", "").lower()


def check_base_url(url) -> str:
    """
    The configured server address, checked: http or https, a host, no
    user name or password, no query. A path is kept, for a server
    behind a reverse proxy.
    """
    try:
        parts = urllib.parse.urlsplit(str(url).strip())
        parts.port
    except ValueError as exc:
        raise JellyfinError("FREYA_JELLYFIN_URL is not a valid address.") from exc

    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise JellyfinError(
            "FREYA_JELLYFIN_URL must be an http or https address of the "
            "server, such as http://192.168.1.20:8096, with nothing after it."
        )

    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class JellyfinClient:
    """
    Talks to one configured Jellyfin server with an API key.

    The key stays here: it is sent only in a request header to that
    server, never put in an address, and never returned. Requests go to
    fixed API paths built here, so this is not a way to fetch anything
    else.
    """

    def __init__(self, base_url, token, user_id=None, transport=http_request):
        if not token:
            raise JellyfinError("FREYA_JELLYFIN_TOKEN is not set.")

        self.base_url = check_base_url(base_url)
        self._token = str(token)
        self._user_id = item_id(user_id) if user_id else None
        self._transport = transport

        if user_id and self._user_id is None:
            raise JellyfinError("FREYA_JELLYFIN_USER_ID is not a Jellyfin user ID.")

    def __repr__(self):
        return f"JellyfinClient({self.base_url!r})"

    def _headers(self, extra=None):
        headers = {
            "Authorization": (
                'MediaBrowser Client="Freya", Device="Freya", '
                f'DeviceId="freya-media", Version="1", Token="{self._token}"'
            ),
            "X-Emby-Token": self._token,
            "Accept": "application/json",
        }
        headers.update(extra or {})
        return headers

    def _redact(self, text) -> str:
        return str(text).replace(self._token, "[redacted]")

    def _url(self, path, query=None) -> str:
        url = self.base_url + path

        if query:
            url += "?" + urllib.parse.urlencode(
                {name: value for name, value in query.items() if value is not None}
            )

        return url

    def _get_json(self, path, query=None):
        try:
            answer = self._transport(
                "GET", self._url(path, query), self._headers(), max_bytes=MAX_JSON_BYTES
            )
        except OSError as exc:
            raise JellyfinError(
                "Jellyfin could not be reached: " + self._redact(exc)
            ) from None

        if answer.status in (401, 403):
            raise JellyfinError(
                "Jellyfin refused the access token. Check FREYA_JELLYFIN_TOKEN."
            )

        if 300 <= answer.status < 400:
            raise JellyfinError(
                "Jellyfin answered with a redirect, which Freya does not "
                "follow. Check FREYA_JELLYFIN_URL."
            )

        if answer.status != 200:
            raise JellyfinError(f"Jellyfin answered with an error ({answer.status}).")

        if answer.truncated:
            raise JellyfinError("Jellyfin's answer was too large.")

        try:
            return json.loads(answer.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise JellyfinError("Jellyfin's answer could not be read.") from None

    # -- browsing ------------------------------------------------------

    def test(self) -> dict:
        info = self._get_json("/System/Info")

        if not isinstance(info, dict):
            raise JellyfinError("Jellyfin's answer could not be read.")

        return {
            "server": str(info.get("ServerName") or "Jellyfin")[:120],
            "version": str(info.get("Version") or "")[:40],
        }

    def libraries(self) -> list:
        if self._user_id:
            data = self._get_json("/UserViews", {"userId": self._user_id})
        else:
            data = self._get_json("/Library/MediaFolders")

        return [
            {
                "id": item_id(entry.get("Id")),
                "name": str(entry.get("Name") or "")[:200],
                "type": str(entry.get("CollectionType") or "")[:40],
            }
            for entry in _items(data)
            if item_id(entry.get("Id"))
        ]

    def items(self, parent_id=None, search=None, start=0, limit=50) -> dict:
        """
        One page of playable items: in a library, matching a search,
        or both.
        """
        if parent_id is not None and item_id(parent_id) is None:
            raise JellyfinError("That is not a Jellyfin library.")

        if search is not None:
            search = str(search).strip()[:MAX_SEARCH] or None

        start = _bounded(start, 0, 1_000_000)
        limit = _bounded(limit, 1, MAX_PAGE)

        data = self._get_json(
            "/Items",
            {
                "userId": self._user_id,
                "parentId": item_id(parent_id) if parent_id else None,
                "searchTerm": search,
                "recursive": "true",
                "includeItemTypes": ITEM_TYPES,
                "fields": "MediaSources,ProductionYear",
                "sortBy": "SortName",
                "startIndex": str(start),
                "limit": str(limit),
            },
        )

        total = data.get("TotalRecordCount") if isinstance(data, dict) else None

        return {
            "items": [describe_item(entry) for entry in _items(data) if item_id(entry.get("Id"))],
            "start": start,
            "total": int(total) if isinstance(total, int) and total >= 0 else None,
        }

    def item(self, identifier) -> dict:
        identifier = item_id(identifier)

        if identifier is None:
            raise JellyfinError("That is not a Jellyfin item.")

        data = self._get_json(
            "/Items",
            {"userId": self._user_id, "ids": identifier, "fields": "MediaSources,ProductionYear"},
        )
        found = [entry for entry in _items(data) if item_id(entry.get("Id")) == identifier]

        if not found:
            raise JellyfinError("Jellyfin has no such item.")

        return describe_item(found[0])

    def web_url(self, identifier) -> str:
        """
        Where the owner can play an item in Jellyfin's own web client,
        signing in there. Holds no credential.
        """
        return f"{self.base_url}/web/#/details?id={item_id(identifier)}"

    # -- playing -------------------------------------------------------

    def stream(self, identifier, kind, range_header) -> ByteResponse:
        """
        One bounded piece of an item's original file, for the window's
        player. Jellyfin is asked for a byte range no larger than
        CHUNK_BYTES, and anything else it sends is refused.
        """
        identifier = item_id(identifier)

        if identifier is None:
            return ByteResponse(404, b"Not found.")

        try:
            wanted = parse_range(range_header, 1 << 62)
        except Unsatisfiable as exc:
            return unsatisfiable(None, str(exc))
        except ValueError as exc:
            return ByteResponse(400, str(exc).encode("utf-8"))

        if wanted is None:
            first, upstream_range = 0, f"bytes=0-{CHUNK_BYTES - 1}"
        elif str(range_header).strip().replace(" ", "").startswith("bytes=-"):
            first = None
            length = min(int(str(range_header).split("-", 1)[1]), CHUNK_BYTES)
            upstream_range = f"bytes=-{length}"
        else:
            first = wanted[0]
            last = min(wanted[1], first + CHUNK_BYTES - 1)
            upstream_range = f"bytes={first}-{last}"

        path = "/Audio" if kind == "audio" else "/Videos"

        try:
            answer = self._transport(
                "GET",
                self._url(f"{path}/{identifier}/stream", {"static": "true"}),
                self._headers({"Range": upstream_range, "Accept": "*/*"}),
                max_bytes=CHUNK_BYTES,
            )
        except OSError:
            return ByteResponse(502, b"Jellyfin could not be reached.")

        content_type = answer.headers.get("content-type", "").split(";")[0].strip().lower()

        if not re.match(r"^(video|audio)/[a-z0-9.+-]{1,60}$", content_type):
            return ByteResponse(502, b"Jellyfin did not send a playable file.")

        if answer.status == 416:
            return unsatisfiable(None, "That range is past the end of the file.")

        if answer.status == 200:
            # The server sent the whole file. That is only acceptable
            # when all of it fitted.
            if answer.truncated:
                return ByteResponse(
                    502,
                    b"Jellyfin did not send a byte range. Use Open in Jellyfin instead.",
                )

            return slice_whole(answer.body, range_header, content_type)

        if answer.status != 206:
            return ByteResponse(502, f"Jellyfin answered with an error ({answer.status}).".encode())

        found = _CONTENT_RANGE.match(answer.headers.get("content-range", ""))

        if found is None:
            return ByteResponse(502, b"Jellyfin's answer could not be read.")

        got_first, got_last, size = (int(value) for value in found.groups())

        if (
            got_last < got_first
            or got_last >= size
            or (first is not None and got_first != first)
            or (first is not None and got_last > int(upstream_range.split("-", 1)[1]))
            or (first is None and got_last != size - 1)
            or (first is None and got_first != max(0, size - length))
            or got_last - got_first + 1 != len(answer.body)
            or answer.truncated
        ):
            return ByteResponse(502, b"Jellyfin's answer did not match what was asked for.")

        if wanted is None and size == len(answer.body):
            return ByteResponse(200, answer.body, {"Accept-Ranges": "bytes"}, content_type)

        if wanted is None:
            return unsatisfiable(size, "This file is sent in ranges; ask for one.")

        return ByteResponse(
            206,
            answer.body,
            {"Accept-Ranges": "bytes", "Content-Range": f"bytes {got_first}-{got_last}/{size}"},
            content_type,
        )


def _items(data) -> list:
    if isinstance(data, dict):
        data = data.get("Items")

    if not isinstance(data, list):
        return []

    return [entry for entry in data[:MAX_PAGE] if isinstance(entry, dict)]


def _bounded(value, low, high) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = low

    return max(low, min(value, high))


def describe_item(entry: dict) -> dict:
    """
    What the window is told about an item: plain fields, bounded, and
    no address that carries a credential.
    """
    ticks = entry.get("RunTimeTicks")
    duration = ticks / 10_000_000 if isinstance(ticks, int) and 0 < ticks < 10**14 else None
    sources = entry.get("MediaSources") if isinstance(entry.get("MediaSources"), list) else []
    container = ""

    if sources and isinstance(sources[0], dict):
        container = str(sources[0].get("Container") or "").lower()[:40]

    kind = "audio" if entry.get("Type") == "Audio" or entry.get("MediaType") == "Audio" else "video"
    containers = {part.strip() for part in container.split(",") if part.strip()}

    return {
        "id": item_id(entry.get("Id")),
        "name": str(entry.get("Name") or "Untitled")[:300],
        "type": str(entry.get("Type") or "")[:40],
        "series": str(entry.get("SeriesName") or "")[:300] or None,
        "year": entry.get("ProductionYear") if isinstance(entry.get("ProductionYear"), int) else None,
        "duration": duration,
        "container": container,
        "kind": kind,
        # A guess from the container: the codecs inside decide.
        "window_playable": bool(containers & WINDOW_CONTAINERS),
    }
