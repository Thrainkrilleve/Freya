import re
import urllib.parse
from typing import Optional


# The privacy-enhanced embed host. The page's content security policy
# allows frames from this host only.
EMBED_ORIGIN = "https://www.youtube-nocookie.com"

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

_WATCH_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOSTS = {"youtu.be"}
_EMBED_HOSTS = {"www.youtube-nocookie.com", "youtube-nocookie.com"}

MAX_URL_LENGTH = 2000


def video_id(url) -> Optional[str]:
    """
    The video ID in a YouTube address, or None if it is not a plain
    address of one YouTube video. Only https (or http) addresses on
    YouTube's own hosts are accepted, with no user name, password, or
    port; playlists alone, channels, and searches are not videos.
    """
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        return None

    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None

    if parts.scheme not in ("https", "http") or parts.username or parts.password or port:
        return None

    host = (parts.hostname or "").lower()
    path = [piece for piece in parts.path.split("/") if piece]
    candidate = None

    if host in _WATCH_HOSTS:
        if path == ["watch"]:
            values = urllib.parse.parse_qs(parts.query).get("v", [])
            candidate = values[0] if len(values) == 1 else None
        elif len(path) == 2 and path[0] in ("shorts", "embed", "live", "v"):
            candidate = path[1]
    elif host in _SHORT_HOSTS and len(path) == 1:
        candidate = path[0]
    elif host in _EMBED_HOSTS and len(path) == 2 and path[0] == "embed":
        candidate = path[1]

    if candidate and _VIDEO_ID.match(candidate):
        return candidate

    return None


def watch_url(identifier: str) -> str:
    return f"https://www.youtube.com/watch?v={identifier}"


def embed_url(identifier: str, start=None) -> str:
    """
    The address the page frames. YouTube's own player, controls, and
    advertisements are shown unchanged.
    """
    query = {"rel": "0", "playsinline": "1"}

    if start is not None and start > 0:
        query["start"] = str(int(start))

    return f"{EMBED_ORIGIN}/embed/{identifier}?{urllib.parse.urlencode(query)}"


def external_search_url(query: str) -> str:
    """
    YouTube's own search page, opened in the owner's browser. Freya
    does not read it.
    """
    return "https://www.youtube.com/results?" + urllib.parse.urlencode(
        {"search_query": query[:200]}
    )
