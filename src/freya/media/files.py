import hashlib
import itertools
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional


# What may be imported, by extension, and the type it is served as.
# Matroska and QuickTime are accepted, but the window's browser may
# not be able to play every codec inside them.
MEDIA_TYPES = {
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".mkv": ("video", "video/x-matroska"),
    ".mov": ("video", "video/quicktime"),
    ".mp3": ("audio", "audio/mpeg"),
    ".m4a": ("audio", "audio/mp4"),
    ".aac": ("audio", "audio/aac"),
    ".ogg": ("audio", "audio/ogg"),
    ".opus": ("audio", "audio/ogg"),
    ".oga": ("audio", "audio/ogg"),
    ".wav": ("audio", "audio/wav"),
    ".flac": ("audio", "audio/flac"),
}

SUBTITLE_TYPES = (".srt", ".vtt")

# Files a browser or download manager is still writing. They are
# counted, never imported.
PARTIAL_SUFFIXES = (
    ".crdownload", ".part", ".partial", ".download", ".tmp",
    ".opdownload", ".!ut", ".downloading",
)

# Lists of other files or addresses, not media. Never imported.
PLAYLIST_SUFFIXES = (".m3u", ".m3u8", ".pls", ".xspf", ".asx", ".strm", ".cue")

# A file whose size or time changed this recently may still be being
# written, so it waits for a later scan.
STABLE_SECONDS = 15.0

MAX_DEPTH = 8
MAX_VISITED = 20000
MAX_FOUND = 2000
MAX_FILE_BYTES = 256 * 1024 ** 3

# The fingerprint reads this much from each end of a file, so that a
# very large film is recognised again without reading all of it.
FINGERPRINT_SAMPLE = 1024 * 1024

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class UnsafePath(ValueError):
    """
    A path that is outside the media roots, is a link, or no longer
    is the file that was imported.
    """


def _is_link(info) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)

    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE)


@dataclass
class FoundFile:
    root: str
    relative: str
    name: str
    kind: str
    media_type: str
    size: int
    modified_ns: int
    subtitles: List[str]


@dataclass
class ScanResult:
    files: List[FoundFile]
    partial: int = 0
    unstable: int = 0
    skipped_links: int = 0
    skipped_other: int = 0
    truncated: bool = False
    missing_roots: List[str] = None


def _root(path) -> Path:
    root = Path(path)

    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise UnsafePath(f"The media folder {root} is not available.") from exc


def _relative(root: Path, path: Path) -> str:
    return PurePosixPath(*path.relative_to(root).parts).as_posix()


def scan_roots(roots, now=None, stable_seconds=STABLE_SECONDS) -> ScanResult:
    """
    List the media files in the configured folders, without following
    links or junctions and without opening any file.
    """
    now = time.time() if now is None else now
    result = ScanResult(files=[], missing_roots=[])
    visited = 0

    for configured in roots:
        try:
            root = _root(configured)
        except UnsafePath:
            result.missing_roots.append(str(configured))
            continue

        pending = [(root, 0)]

        while pending:
            folder, depth = pending.pop()

            try:
                with os.scandir(folder) as listing:
                    entries = list(itertools.islice(listing, max(0, MAX_VISITED - visited) + 1))
                if len(entries) > MAX_VISITED - visited:
                    result.truncated = True
                    return result
                entries.sort(key=lambda e: e.name.lower())
            except OSError:
                continue

            names = {entry.name.lower() for entry in entries}

            for entry in entries:
                visited += 1

                if visited > MAX_VISITED or len(result.files) >= MAX_FOUND:
                    result.truncated = True
                    return result

                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                if _is_link(info):
                    result.skipped_links += 1
                    continue

                path = Path(entry.path)

                if stat.S_ISDIR(info.st_mode):
                    if depth + 1 < MAX_DEPTH and not entry.name.startswith("."):
                        pending.append((path, depth + 1))
                    continue

                if not stat.S_ISREG(info.st_mode):
                    continue

                suffix = path.suffix.lower()

                if suffix in PARTIAL_SUFFIXES:
                    result.partial += 1
                    continue

                if suffix not in MEDIA_TYPES:
                    if suffix not in SUBTITLE_TYPES:
                        result.skipped_other += 1
                    continue

                # Some download managers write the finished name next
                # to a marker while they are still working.
                if any(f"{entry.name.lower()}{marker}" in names for marker in PARTIAL_SUFFIXES):
                    result.partial += 1
                    continue

                if (
                    info.st_size <= 0
                    or info.st_size > MAX_FILE_BYTES
                    or now - info.st_mtime < stable_seconds
                ):
                    result.unstable += 1
                    continue

                kind, media_type = MEDIA_TYPES[suffix]
                stem = path.stem.lower()
                subtitles = [
                    other.name
                    for other in entries
                    if other.name.lower().endswith(SUBTITLE_TYPES)
                    and (
                        other.name.lower().rsplit(".", 1)[0] == stem
                        or other.name.lower().rsplit(".", 2)[0] == stem
                    )
                ][:5]

                result.files.append(
                    FoundFile(
                        root=str(root),
                        relative=_relative(root, path),
                        name=path.name,
                        kind=kind,
                        media_type=media_type,
                        size=info.st_size,
                        modified_ns=info.st_mtime_ns,
                        subtitles=subtitles,
                    )
                )

    return result


def resolve_inside(roots, root, relative) -> Path:
    """
    The file at `relative` inside `root`, which must still be one of the
    configured roots. Every part of the path is checked: nothing on the
    way may be a link or a junction, and the result may not leave the
    root.
    """
    allowed = []

    for configured in roots:
        try:
            allowed.append(_root(configured))
        except UnsafePath:
            continue

    try:
        base = Path(root).resolve(strict=True)
    except OSError as exc:
        raise UnsafePath("That media folder is no longer available.") from exc

    if base not in allowed:
        raise UnsafePath("That file's folder is no longer a media folder.")

    parts = PurePosixPath(str(relative)).parts

    if (
        not parts
        or any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts)
        or PurePosixPath(str(relative)).is_absolute()
    ):
        raise UnsafePath("That is not a path inside a media folder.")

    current = base

    for part in parts:
        current = current / part

        try:
            info = os.lstat(current)
        except OSError as exc:
            raise UnsafePath("That file is no longer there.") from exc

        if _is_link(info):
            raise UnsafePath("That path goes through a link, which Freya does not follow.")

    resolved = current.resolve(strict=True)

    if not resolved.is_relative_to(base):
        raise UnsafePath("That file is outside its media folder.")

    if not stat.S_ISREG(os.lstat(resolved).st_mode):
        raise UnsafePath("That is not a file.")

    return resolved


def open_checked(path: Path, size: int, modified_ns: int):
    """
    Open a file for reading, but only if it is still the file that was
    imported (same size and modification time). The caller closes it.
    """
    handle = open(path, "rb")

    try:
        if opened_path(handle) != path.resolve(strict=True) or path.resolve(strict=True) != path:
            raise UnsafePath("That path was replaced by a link while it was opened.")
        opened = os.fstat(handle.fileno())

        if not os.path.samestat(opened, os.stat(path)):
            raise UnsafePath("That file changed while it was being opened.")

        if opened.st_size != size or opened.st_mtime_ns != modified_ns:
            raise UnsafePath(
                "That file has changed since it was imported. Scan and "
                "import it again."
            )
    except Exception:
        handle.close()
        raise

    return handle


def opened_path(handle) -> Path:
    """Resolve the actual opened handle, before reading any bytes."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint]
        function.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(msvcrt.get_osfhandle(handle.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise UnsafePath("The opened file could not be verified.")
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            name = "\\\\" + name[8:]
        elif name.startswith("\\\\?\\"):
            name = name[4:]
        return Path(name)
    if os.path.exists("/proc/self/fd"):
        return Path(os.path.realpath(f"/proc/self/fd/{handle.fileno()}"))
    raise UnsafePath("Checked media-file opening is unavailable on this platform.")


def fingerprint(path: Path, size: int, modified_ns: int) -> str:
    """
    A SHA-256 over the file's size and its first and last megabyte:
    enough to recognise the same file again, not a full content hash.
    """
    digest = hashlib.sha256(str(size).encode("ascii") + b"|")

    with open_checked(path, size, modified_ns) as handle:
        digest.update(handle.read(FINGERPRINT_SAMPLE))

        if size > FINGERPRINT_SAMPLE:
            handle.seek(max(size - FINGERPRINT_SAMPLE, FINGERPRINT_SAMPLE))
            digest.update(handle.read(FINGERPRINT_SAMPLE))

    return "sampled-sha256:" + digest.hexdigest()


def read_text_bounded(path: Path, limit: int) -> Optional[str]:
    """
    A small text file's contents, or None when it is larger than
    `limit` bytes.
    """
    info = path.stat()
    with open_checked(path, info.st_size, info.st_mtime_ns) as handle:
        data = handle.read(limit + 1)

    if len(data) > limit:
        return None

    return data.decode("utf-8-sig", errors="replace")
