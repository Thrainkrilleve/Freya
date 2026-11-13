"""
Where each kind of Freya's long-term data belongs, by what a drive is
good at. This works out a plan and reports it. It moves nothing.
"""
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from freya.child_environment import child_environment
from freya.storage.locations import store_path


DATA_DIR = Path(__file__).resolve().parents[3] / "data"

SSD = "ssd"
HDD = "hdd"
UNKNOWN = "unknown"

# What a kind of data needs from the drive it lives on.
FAST = "fast"   # looked up or written during a turn: wants an SSD
BULK = "bulk"   # large, read rarely or front to back: fine on a hard disk
COPY = "copy"   # a backup: belongs on a different drive from its source


@dataclass(frozen=True)
class DataKind:
    name: str
    need: str
    what: str
    personal: bool = False


# Every store under data/. A store added later must be added here; a
# test checks the names against what the code writes.
DATA_KINDS = (
    DataKind("freya_memory.db", FAST, "Saved memories and every conversation", True),
    DataKind("freya_control.db", FAST, "Policy rules and the audit log"),
    DataKind("freya_usage.db", FAST, "What each model route has used"),
    DataKind("freya_settings.json", FAST, "Names Freya answers to and her volume", True),
    DataKind("storage_locations.json", FAST, "Where bulk data is kept"),
    DataKind("backup_status.json", FAST, "How the last backup attempt went"),
    DataKind("cloud_settings.json", FAST, "The owner's cloud choices"),
    DataKind("screening_corrections.json", FAST, "The not-private and always-private lists", True),
    DataKind("sent_briefs.jsonl", FAST, "The record of what was sent to cloud models", True),
    DataKind("model_record.json", FAST, "What each model measured"),
    DataKind("avatar_settings.json", FAST, "Avatar size, place and limits"),
    DataKind("avatar_library.json", FAST, "The list of saved avatars"),
    DataKind("voice_profiles.json", FAST, "Saved voices"),
    DataKind("window_profile", FAST, "The window's browser profile"),
    DataKind("speech_models", FAST, "Voice model files, loaded when a voice is used"),
    DataKind("speech_envs", FAST, "The voice engines' own Python environments"),
    DataKind("avatar_models", BULK, "Avatar model files"),
    DataKind("avatar_previews", BULK, "Avatar thumbnails"),
    DataKind("avatar_work", BULK, "Leftovers from converting avatars"),
    DataKind("avatar_build", BULK, "Leftovers from converting avatars"),
    DataKind("images", BULK, "Generated images", True),
    DataKind("media", FAST, "The media library: a database and its files", True),
    DataKind("voice_clips", BULK, "Generated speech clips", True),
    DataKind("voice_references", BULK, "Recordings voices were made from", True),
    DataKind("voice_benchmarks", BULK, "Voice engine comparisons"),
    DataKind("backups", COPY, "Copies of the databases", True),
    DataKind("freya.log", BULK, "Freya's log"),
    DataKind("comfyui_server.log", BULK, "The image server's log"),
    DataKind("avatar_overlay.log", BULK, "The avatar window's log"),
    DataKind("speech_chatterbox.log", BULK, "A voice engine's log"),
    DataKind("speech_qwen3-tts.log", BULK, "A voice engine's log"),
)

_QUERY = (
    "$disks = @{}; Get-PhysicalDisk | ForEach-Object { "
    "$disks[[string]$_.DeviceId] = [string]$_.MediaType }; "
    "Get-Partition | Where-Object DriveLetter | ForEach-Object { "
    "[pscustomobject]@{ letter = [string]$_.DriveLetter; "
    "kind = $disks[[string]$_.DiskNumber] } } | ConvertTo-Json -Compress"
)


def drive_kinds() -> Dict[str, str]:
    """
    Each drive letter on this PC and whether Windows says it is an SSD
    or a hard disk. Empty when that cannot be found out.
    """
    if sys.platform != "win32":
        return {}

    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            capture_output=True,
            timeout=30,
            env=child_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        found = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}

    if isinstance(found, dict):
        found = [found]
    if not isinstance(found, list):
        return {}

    kinds = {}

    for entry in found:
        if not isinstance(entry, dict):
            continue
        letter = str(entry.get("letter") or "").upper()[:1]
        if letter.isalpha():
            kinds[letter] = {
                "ssd": SSD, "hdd": HDD,
            }.get(str(entry.get("kind") or "").lower(), UNKNOWN)

    return kinds


def drive_of(path) -> str:
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return drive[:1].upper()


def size_of(path: Path) -> int:
    """Bytes under a path. Only sizes are read, never contents."""
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(
            entry.stat().st_size
            for entry in path.rglob("*")
            if entry.is_file()
        )
    except OSError:
        return 0


@dataclass(frozen=True)
class Placement:
    kind: DataKind
    present: bool
    size: int
    on_drive: str
    best_drive: Optional[str]
    reason: str

    @property
    def well_placed(self) -> bool:
        return self.best_drive is None or self.best_drive == self.on_drive


def _first(drives, wanted, avoid=None):
    for letter in sorted(drives):
        if drives[letter] == wanted and letter != avoid:
            return letter
    return None


def plan_placement(data_dir=DATA_DIR, drives=None) -> List[Placement]:
    """
    For each kind of data, the drive it would best live on.

    Fast data wants an SSD. Bulk data is fine on a hard disk and
    should not take SSD space when one is there. A backup belongs on
    a different drive from what it protects, whatever its type. Where
    Windows does not say what a drive is, nothing is recommended.
    """
    data_dir = Path(data_dir)
    drives = drive_kinds() if drives is None else drives
    plan = []

    for kind in DATA_KINDS:
        # A bulk store may already be kept somewhere else.
        path = store_path(kind.name, data_dir)
        home = drive_of(path)
        present = path.exists()
        best, reason = None, "It is where it should be."

        if not drives or drives.get(home, UNKNOWN) == UNKNOWN:
            reason = "Windows did not say what kind of drive this is."
        elif kind.need == FAST:
            if drives[home] != SSD:
                best = _first(drives, SSD)
                reason = (
                    "It is used during a turn and would be quicker on the SSD."
                    if best else "There is no SSD to move it to."
                )
        elif kind.need == BULK:
            if drives[home] == SSD:
                best = _first(drives, HDD)
                reason = (
                    "It is large or rarely read; the hard disk has the room."
                    if best else "It is where it should be."
                )
        else:
            best = _first(drives, HDD, avoid=home) or _first(drives, SSD, avoid=home)
            reason = (
                "A backup on the same drive is lost with it."
                if best else "There is no second drive to keep a backup on."
            )

        plan.append(Placement(kind, present, size_of(path) if present else 0, home, best, reason))

    return plan


def describe(plan: List[Placement], drives: Dict[str, str]) -> List[str]:
    def amount(size):
        for unit in ("bytes", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
            size /= 1024

    lines = ["Drives: " + (", ".join(
        f"{letter}: {kind}" for letter, kind in sorted(drives.items())
    ) or "unknown")]

    for move in (True, False):
        chosen = [p for p in plan if p.present and (not p.well_placed) == move]
        if not chosen:
            continue
        lines.append("")
        lines.append("Would be better elsewhere:" if move else "Well placed:")
        for placement in chosen:
            where = (
                f"{placement.on_drive}: -> {placement.best_drive}:"
                if move else f"{placement.on_drive}:"
            )
            lines.append(
                f"  {placement.kind.name} ({amount(placement.size)}), {where} "
                f"{placement.reason if move else placement.kind.what}"
            )

    lines.append("")
    lines.append("Nothing has been moved.")

    return lines


def main() -> int:
    drives = drive_kinds()
    print("\n".join(describe(plan_placement(drives=drives), drives)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
