"""Display remote answer media in an opaque, script-free frame; fetch nothing here."""
import html
import ipaddress
import re
from urllib.parse import urlsplit


def public_https(value):
    if not isinstance(value, str) or len(value) > 4096 or any(
        ord(character) <= 32 or character == "\\" for character in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        # A trailing dot names the same host to a browser; it is
        # removed before the names below are looked for.
        host = (parsed.hostname or "").rstrip(".")
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                return None
            # A name whose every label is a number ("127.1", "10.1",
            # "0177.0.0.1") is an address in a form browsers accept
            # and `ip_address` does not, so it is never public.
            if all(re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)", label, re.I) for label in host.split(".")):
                return None
        return value
    except ValueError:
        return None


def media_document(kind, url):
    """A narrow template, never provider HTML. Session checks happen in Gateway."""
    url = public_https(url)
    if url is None or kind not in ("image", "video"):
        return None
    if kind == "video" and not urlsplit(url).path.lower().endswith((".mp4", ".webm", ".ogg")):
        return None
    escaped = html.escape(url, quote=True)
    if kind == "image":
        media = f'<img src="{escaped}" alt="Picture from the source answer" referrerpolicy="no-referrer">'
        policy = "img-src https:; media-src 'none';"
    else:
        media = f'<video src="{escaped}" controls preload="none" playsinline></video>'
        policy = "img-src 'none'; media-src https:;"
    return (
        '<!doctype html><html><head><meta name="viewport" content="width=device-width">'
        '<title>Answer media</title><style>html,body{margin:0;background:#111827;color:white}'
        'img,video{display:block;max-width:100%;max-height:100vh;margin:auto}</style>'
        '</head><body>' + media + '</body></html>',
        "default-src 'none'; " + policy + " script-src 'none'; style-src 'unsafe-inline'; "
        "connect-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'self'; sandbox",
    )
