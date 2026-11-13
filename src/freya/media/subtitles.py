import math
import re
from typing import List, Tuple


# A subtitle file larger than this is refused rather than read.
MAX_SUBTITLE_BYTES = 3 * 1024 * 1024
MAX_CUES = 20000

_TIME = r"(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})"
_CUE_TIME = re.compile(rf"^\s*{_TIME}\s*-->\s*{_TIME}")
_TAG = re.compile(r"<[^>]{0,200}>|\{\\[^}]{0,200}\}")


def _seconds(hours, minutes, seconds, fraction) -> float:
    value = (
        int(hours or 0) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(fraction.ljust(3, "0")[:3]) / 1000
    )

    if not math.isfinite(value):
        raise ValueError("A subtitle time could not be read.")

    return value


def parse_subtitles(text: str) -> List[Tuple[float, float, str]]:
    """
    Read SubRip (.srt) or WebVTT (.vtt) text into (start, end, text)
    cues. Styling tags are removed; the words are kept as they are,
    as untrusted text. Cues that cannot be read are skipped.
    """
    if not isinstance(text, str):
        raise ValueError("Subtitles must be text.")

    cues = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("﻿", ""))

    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]

        for index, line in enumerate(lines[:3]):
            found = _CUE_TIME.match(line)

            if found is None:
                continue

            groups = found.groups()
            start = _seconds(*groups[:4])
            end = _seconds(*groups[4:])
            words = " ".join(
                _TAG.sub("", other).strip() for other in lines[index + 1:]
            ).strip()

            if words and end >= start:
                cues.append((start, end, words))

            break

        if len(cues) >= MAX_CUES:
            break

    return cues


def parse_plain_transcript(text: str) -> List[Tuple[float, float, str]]:
    """
    A transcript pasted as plain lines, optionally starting with a time
    such as "12:34" or "1:02:03". Lines without a time follow the one
    before. Each line ends where the next begins.
    """
    stamped = re.compile(r"^\s*\[?(?:(\d{1,3}):)?(\d{1,2}):(\d{2})\]?\s*[-–:]?\s*(.*)$")
    entries = []
    last = 0.0

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        found = stamped.match(line)

        if found:
            hours, minutes, seconds, words = found.groups()
            last = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
            line = words.strip()

            if not line:
                continue

        entries.append([float(last), line])

        if len(entries) >= MAX_CUES:
            break

    cues = []

    for index, (start, words) in enumerate(entries):
        following = entries[index + 1][0] if index + 1 < len(entries) else start
        cues.append((start, max(start, following), words))

    return cues
