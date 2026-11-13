import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict


DEFAULT_TIMEOUT = 15.0


@dataclass
class HttpAnswer:
    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    # True when the server had more to send than `max_bytes`.
    truncated: bool = False


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """
    A redirect is returned as it is, never followed: following one
    could carry a credential to another server.
    """

    def redirect_request(self, *args, **kwargs):
        return None


# No proxies from the environment, and no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())


def http_request(method, url, headers=None, max_bytes=2 * 1024 * 1024,
                 timeout=DEFAULT_TIMEOUT) -> HttpAnswer:
    """
    One HTTP request to a server the caller has already checked. At
    most `max_bytes` of the answer are read. Raises OSError when the
    server cannot be reached.
    """
    request = urllib.request.Request(url, method=method, headers=dict(headers or {}))

    try:
        response = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error

    try:
        body = response.read(max_bytes + 1) or b""
        answer_headers = {name.lower(): value for name, value in response.headers.items()}
        status = response.status if hasattr(response, "status") else response.code
    finally:
        response.close()

    return HttpAnswer(
        status=int(status),
        headers=answer_headers,
        body=body[:max_bytes],
        truncated=len(body) > max_bytes,
    )
