import re
from dataclasses import dataclass, field
from typing import Dict, Optional


# The most one response carries. A browser playing a video asks for
# the rest as it needs it; a response never holds a whole film.
CHUNK_BYTES = 4 * 1024 * 1024

_RANGE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")


class Unsatisfiable(ValueError):
    """
    The Range header asked for bytes the file does not have.
    """


@dataclass
class ByteResponse:
    """
    An HTTP answer for media bytes: a status, its headers, and a body
    no larger than CHUNK_BYTES.
    """

    status: int
    body: bytes = b""
    headers: Dict[str, str] = field(default_factory=dict)
    content_type: str = "text/plain; charset=utf-8"


def parse_range(header: Optional[str], size: int):
    """
    The (first, last) byte positions a Range header asks for, both
    inclusive, or None when there is no header.

    Only one range is accepted. An open range ("bytes=100-") and a
    suffix ("bytes=-500") are understood. Raises Unsatisfiable for a
    range that starts past the end, and ValueError for one that cannot
    be read.
    """
    if header is None or not str(header).strip():
        return None

    found = _RANGE.match(str(header).strip().replace(" ", ""))

    if found is None:
        raise ValueError("Only a single byte range is supported.")

    first, last = found.groups()

    if not first and not last:
        raise ValueError("That range is empty.")

    if size <= 0:
        raise Unsatisfiable("The file is empty.")

    if not first:
        length = int(last)

        if length <= 0:
            raise Unsatisfiable("A suffix range must ask for at least one byte.")

        return max(size - length, 0), size - 1

    first = int(first)
    last = int(last) if last else size - 1

    if first >= size:
        raise Unsatisfiable("That range starts past the end of the file.")

    if last < first:
        raise ValueError("That range ends before it starts.")

    return first, min(last, size - 1)


def plan_response(header, size, chunk=CHUNK_BYTES):
    """
    Decide what to send for a request: (status, first, last).

    With no Range header the whole file is sent as 200 only when it
    fits in one chunk; a larger file is never sent as a 200 that would
    have to be cut short, so the request is refused with 416 and the
    file's size, which tells a client to ask for ranges. A range is
    answered with 206 and at most `chunk` bytes from where it starts;
    a client asks again for the rest.
    """
    wanted = parse_range(header, size)

    if wanted is None:
        if size <= chunk:
            return 200, 0, size - 1

        raise Unsatisfiable("This file is sent in ranges; ask for one.")

    first, last = wanted

    return 206, first, min(last, first + chunk - 1)


def range_headers(status, first, last, size) -> Dict[str, str]:
    headers = {"Accept-Ranges": "bytes"}

    if status == 206:
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"

    return headers


def slice_whole(body: bytes, header, content_type) -> ByteResponse:
    """
    Answer a request from a whole file already held in memory, which
    is never larger than one chunk.
    """
    size = len(body)

    try:
        status, first, last = plan_response(header, size)
    except Unsatisfiable as exc:
        return unsatisfiable(size, str(exc))
    except ValueError as exc:
        return ByteResponse(400, str(exc).encode("utf-8"))

    return ByteResponse(
        status, body[first:last + 1], range_headers(status, first, last, size), content_type
    )


def unsatisfiable(size: Optional[int], reason: str) -> ByteResponse:
    headers = {"Accept-Ranges": "bytes"}

    if size is not None:
        headers["Content-Range"] = f"bytes */{size}"

    return ByteResponse(416, reason.encode("utf-8"), headers)
