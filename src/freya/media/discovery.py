import html
import json
import urllib.parse

from freya.media.transport import http_request
from freya.media.youtube import external_search_url, video_id, watch_url


SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
MAX_RESULTS = 25
MAX_QUERY = 200


class DiscoveryError(RuntimeError):
    """
    A search could not be made. The message never holds the API key.
    """


def _text(value, limit):
    # The API escapes titles as HTML; the window shows text as text.
    return html.unescape(str(value or "")).strip()[:limit]


class YouTubeDiscovery:
    """
    Finding videos to bookmark.

    With an API key (FREYA_YOUTUBE_API_KEY), YouTube's official Data
    API search is used; the key travels in a request header, never in
    an address. Without one, Freya offers a link to YouTube's own
    search page for the owner's browser, labelled as external; she
    never reads or scrapes that page.
    """

    def __init__(self, api_key=None, transport=http_request):
        self._key = api_key or None
        self._transport = transport

    @property
    def official(self) -> bool:
        return self._key is not None

    def search(self, query, limit=10) -> dict:
        query = _text(query, MAX_QUERY)

        if not query:
            raise DiscoveryError("Type something to search for.")

        fallback = {
            "label": "Search on YouTube (opens your browser; Freya does not read the results)",
            "url": external_search_url(query),
        }

        if not self.official:
            return {"source": "external", "results": [], "external": fallback}

        try:
            limit = max(1, min(int(limit), MAX_RESULTS))
        except (TypeError, ValueError):
            limit = 10

        url = SEARCH_URL + "?" + urllib.parse.urlencode(
            {
                "part": "snippet",
                "type": "video",
                "safeSearch": "moderate",
                "maxResults": str(limit),
                "q": query,
            }
        )

        try:
            answer = self._transport(
                "GET", url, {"X-Goog-Api-Key": self._key, "Accept": "application/json"},
                max_bytes=1024 * 1024,
            )
        except OSError:
            raise DiscoveryError("YouTube's search could not be reached.") from None

        if answer.status != 200 or answer.truncated:
            raise DiscoveryError(
                f"YouTube's search answered with an error ({answer.status}). "
                "Check FREYA_YOUTUBE_API_KEY and its quota."
            )

        try:
            data = json.loads(answer.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise DiscoveryError("YouTube's answer could not be read.") from None

        results = []

        for item in (data.get("items") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue

            identifier = (item.get("id") or {}).get("videoId") if isinstance(item.get("id"), dict) else None
            identifier = video_id(watch_url(identifier)) if isinstance(identifier, str) else None
            snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}

            if identifier is None:
                continue

            results.append(
                {
                    "video_id": identifier,
                    "url": watch_url(identifier),
                    "title": _text(snippet.get("title"), 300) or "Untitled",
                    "channel": _text(snippet.get("channelTitle"), 200),
                    "published": _text(snippet.get("publishedAt"), 40),
                    "description": _text(snippet.get("description"), 500),
                }
            )

            if len(results) >= limit:
                break

        return {"source": "youtube-data-api", "results": results, "external": fallback}
