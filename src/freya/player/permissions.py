"""
Where an adapter may attach.

A missing or unreadable allow-list means no. A target that is not on
the list means no. A public address means no. The one game named in
NEW-EL as out of scope means no, even when someone wrote it on the list.

The list is a JSON document the owner wrote. This module does not
choose a path under data/, and it does not open a network connection.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from freya.player.schema import PermissionRefused, _is_public_address, _names_eve


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,80}\Z")


@dataclass(frozen=True)
class AllowEntry:
    adapter_id: str
    target: str


def parse_allow_list(text) -> tuple:
    """
    Read an allow-list. A damaged document is an empty list, which
    allows nothing.
    """
    if not isinstance(text, str) or not text.strip():
        return ()
    try:
        raw = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(raw, list):
        return ()

    entries = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"adapter", "target"}:
            return ()
        adapter_id = item["adapter"]
        target = item["target"]
        if (
            not isinstance(adapter_id, str)
            or not isinstance(target, str)
            or not _LABEL.match(adapter_id)
            or not _LABEL.match(target)
        ):
            return ()
        entries.append(AllowEntry(adapter_id, target))
    return tuple(entries)


def read_allow_list(path) -> tuple:
    """Read a file the owner named. Missing or unreadable means no."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    return parse_allow_list(text)


def attachment_problem(adapter_id, target) -> str:
    """
    Why this pair is refused before the allow-list is consulted, or
    an empty string when the list may still allow it.
    """
    if _names_eve(adapter_id) or _names_eve(target):
        return "That game is out of scope."
    if _is_public_address(target) or _is_public_address(adapter_id):
        return "That target is not on this machine."
    return ""


def check_attachment(entries, adapter_id, target) -> None:
    """Raise PermissionRefused unless the owner listed this pair."""
    if entries is None:
        raise PermissionRefused("There is no allow-list.")
    problem = attachment_problem(adapter_id, target)
    if problem:
        raise PermissionRefused(problem)
    if not any(
        entry.adapter_id == adapter_id and entry.target == target
        for entry in entries
    ):
        raise PermissionRefused("That target is not on the allow-list.")
