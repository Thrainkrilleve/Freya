"""Strict structural validation of model-proposed unified diffs.

Only modifications of explicitly selected existing files are supported.
This does not establish that a patch applies or that its code is safe.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")
# Git's marker after a line that has no trailing newline. It is not a line
# of either file, so it does not count against the hunk's line numbers.
NO_NEWLINE = "\\ No newline at end of file"
FORBIDDEN = ("diff --git ", "index ", "new file mode ", "deleted file mode ",
             "old mode ", "new mode ", "rename ", "copy ", "Binary files ",
             "GIT binary patch", "*** Begin Patch", "+++ /dev/null", "--- /dev/null")


def _path(header: str, prefix: str, selected: set[str]) -> str:
    if not header.startswith(prefix):
        raise ValueError("Invalid patch header")
    name = header[len(prefix):]
    if not name or "\t" in name or "\r" in name or "\0" in name:
        raise ValueError("Patch path contains unsupported metadata")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts or "\\" in name:
        raise ValueError("Unsafe patch path")
    if name not in selected:
        raise ValueError("Patch modifies an unselected path")
    return name


def _follows_content(lines: list[str], index: int) -> bool:
    return index > 0 and lines[index - 1][:1] in (" ", "-", "+") and lines[index - 1] != NO_NEWLINE


def validate_unified_diff(patch: str, selected: set[str]) -> None:
    if not patch or len(patch.encode("utf-8")) > 1_000_000:
        raise ValueError("Empty or oversized patch")
    if "\r" in patch or "\0" in patch or not patch.endswith("\n"):
        raise ValueError("Patch must be LF-terminated text")
    lines = patch.splitlines()
    index = 0
    seen: set[str] = set()
    while index < len(lines):
        if not lines[index].startswith("--- a/"):
            raise ValueError("Expected old-file header")
        old = _path(lines[index], "--- a/", selected)
        index += 1
        if index >= len(lines):
            raise ValueError("Missing new-file header")
        new = _path(lines[index], "+++ b/", selected)
        index += 1
        if old != new or old in seen:
            raise ValueError("Mismatched or repeated patch path")
        seen.add(old)
        hunk_count = 0
        while index < len(lines) and lines[index].startswith("@@ "):
            match = HUNK.fullmatch(lines[index])
            if not match:
                raise ValueError("Invalid hunk header")
            old_count = int(match[2] or "1")
            new_count = int(match[4] or "1")
            index += 1
            hunk_count += 1
            while old_count or new_count:
                if index >= len(lines):
                    raise ValueError("Truncated hunk")
                line = lines[index]
                if line == NO_NEWLINE and _follows_content(lines, index):
                    index += 1
                    continue
                if line.startswith(" "):
                    old_count -= 1
                    new_count -= 1
                elif line.startswith("-"):
                    old_count -= 1
                elif line.startswith("+"):
                    new_count -= 1
                else:
                    raise ValueError("Invalid hunk content")
                if old_count < 0 or new_count < 0:
                    raise ValueError("Hunk line counts exceeded")
                index += 1
            if index < len(lines) and lines[index] == NO_NEWLINE and _follows_content(lines, index):
                index += 1
        if not hunk_count:
            raise ValueError("Patch file has no hunks")
        if index < len(lines) and not lines[index].startswith("--- a/"):
            raise ValueError("Unexpected trailing patch content")
    if not seen:
        raise ValueError("Patch has no changes")
