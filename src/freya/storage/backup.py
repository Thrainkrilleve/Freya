"""
Copy Freya's databases and settings to another drive.

A backup beside the data it protects is lost with it, so this refuses
a target on the same drive. It copies; it never moves or deletes, and
it never reads what the databases say, only copies them.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from freya.storage.placement import (
    DATA_DIR,
    HDD,
    SSD,
    drive_kinds,
    drive_of,
)


BACKUP_FOLDER_NAME = "FreyaBackups"

# Copied with SQLite's own backup, which gives a consistent copy even
# of a database that is in use.
DATABASES = ("freya_memory.db", "freya_control.db", "freya_usage.db")

# Small files copied as they are.
FILES = (
    "freya_settings.json",
    "cloud_settings.json",
    "screening_corrections.json",
    "sent_briefs.jsonl",
    "model_record.json",
    "voice_profiles.json",
    "avatar_settings.json",
    "avatar_library.json",
    "storage_locations.json",
)

# Set to 0 to stop the backup Freya makes when she closes.
AUTOMATIC_VARIABLE = "FREYA_BACKUP"
# Where to keep backups, instead of a folder on another drive.
TARGET_VARIABLE = "FREYA_BACKUP_DIR"
# A backup newer than this makes another one unnecessary.
MINIMUM_HOURS = 1
STAMP_FORMAT = "%Y-%m-%d_%H%M%S"


class BackupRefused(RuntimeError):
    """The backup was not made. The message says why."""


def default_target(data_dir=DATA_DIR, drives: Optional[Dict[str, str]] = None):
    """
    A folder on a different drive from the data: a hard disk if there
    is one, otherwise another SSD. None when there is no second drive.
    """
    drives = drive_kinds() if drives is None else drives
    home = drive_of(data_dir)

    for wanted in (HDD, SSD):
        for letter in sorted(drives):
            if letter != home and drives[letter] == wanted:
                return Path(f"{letter}:/") / BACKUP_FOLDER_NAME

    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_database(source: Path, target: Path) -> None:
    # Opened read-only: a backup must never change what it copies.
    original = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            original.backup(copy)
            # WAL is a property of the copy too; a backup should be
            # one plain file that can be picked up and carried.
            copy.execute("PRAGMA journal_mode = DELETE")
            verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copy.close()
    finally:
        original.close()

    if verdict != "ok":
        raise BackupRefused(f"The copy of {source.name} did not pass its check.")


def back_up(
    data_dir=DATA_DIR,
    target_root=None,
    now: Optional[datetime] = None,
    allow_same_drive: bool = False,
) -> Path:
    """
    Make one dated backup and return its folder.

    Each run makes a new folder and leaves earlier ones alone. A
    manifest in the folder lists what was copied with its size and
    checksum, so a later check can tell whether a copy has changed.
    """
    data_dir = Path(data_dir)
    target_root = (
        Path(target_root) if target_root is not None else default_target(data_dir)
    )

    if target_root is None:
        raise BackupRefused("There is no second drive to keep a backup on.")
    if not allow_same_drive and drive_of(target_root) == drive_of(data_dir):
        raise BackupRefused(
            "That is the same drive as the data. A backup there would be "
            "lost with it."
        )

    stamp = (now or datetime.now()).strftime(STAMP_FORMAT)
    folder = target_root / stamp

    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BackupRefused("A backup with this time already exists.") from exc

    copied: List[dict] = []

    try:
        for name in DATABASES:
            source = data_dir / name
            if source.is_file():
                _copy_database(source, folder / name)
                copied.append({"name": name, "kind": "database"})

        for name in FILES:
            source = data_dir / name
            if source.is_file():
                shutil.copy2(source, folder / name)
                copied.append({"name": name, "kind": "file"})
    except (OSError, sqlite3.Error) as exc:
        raise BackupRefused(f"The backup could not be completed: {exc}") from exc

    if not copied:
        folder.rmdir()
        raise BackupRefused("There was nothing to back up.")

    for entry in copied:
        path = folder / entry["name"]
        entry["bytes"] = path.stat().st_size
        entry["sha256"] = _sha256(path)

    temporary = folder / "manifest.tmp"
    temporary.write_text(
        json.dumps(
            {"made": stamp, "from": str(data_dir), "copied": copied}, indent=2,
        ),
        encoding="utf-8",
    )

    os.replace(temporary, folder / "manifest.json")

    return folder


def _completed(folder: Path) -> bool:
    # The manifest is published last. Older backups use the same schema.
    try:
        manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
        if not isinstance(manifest, dict) or manifest.get("made") != folder.name:
            return False
        entries = manifest.get("copied")
        if not isinstance(entries, list) or not entries:
            return False
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            name = entry.get("name")
            if not isinstance(name, str) or name not in DATABASES + FILES or name in seen:
                return False
            seen.add(name)
            expected_kind = "database" if name in DATABASES else "file"
            size, digest = entry.get("bytes"), entry.get("sha256")
            if entry.get("kind") != expected_kind or type(size) is not int or size < 0:
                return False
            if not isinstance(digest, str) or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                return False
            copied = folder / name
            if not copied.is_file() or copied.stat().st_size != size:
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def latest(target_root) -> Optional[datetime]:
    """When the newest completed backup was made; ignore partial attempts."""
    newest = None

    try:
        entries = list(Path(target_root).iterdir())
    except OSError:
        return None

    for entry in entries:
        try:
            made = datetime.strptime(entry.name, STAMP_FORMAT)
        except ValueError:
            continue
        if entry.is_dir() and _completed(entry) and (newest is None or made > newest):
            newest = made

    return newest


STATUS_NAME = "backup_status.json"
# A newest backup older than this is worth telling the owner about.
STALE_DAYS = 3


def _note(data_dir, moment, ok, reason="", target=None) -> None:
    """Remember how the last attempt went, for the next start to read."""
    try:
        path = Path(data_dir) / STATUS_NAME
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({
                "at": moment.strftime(STAMP_FORMAT),
                "ok": bool(ok),
                "reason": str(reason)[:300],
                "target": str(target) if target is not None else None,
            }, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass


def back_up_on_close(data_dir=DATA_DIR, now: Optional[datetime] = None, environ=None):
    """
    The backup Freya makes as she closes. Returns the new folder, or
    None when none was made, which is never an error: closing must
    not fail or wait because a backup could not be made. How it went
    is noted, so that the next start can tell the owner of a failure.
    """
    environ = os.environ if environ is None else environ

    if environ.get(AUTOMATIC_VARIABLE) == "0":
        return None

    moment = now or datetime.now()
    target = None

    try:
        named = environ.get(TARGET_VARIABLE)
        target = Path(named) if named else default_target(data_dir)

        if target is None:
            _note(data_dir, moment, False, "There is no second drive to keep a backup on.")
            return None

        newest = latest(target)

        if newest is not None and (moment - newest).total_seconds() < (
            MINIMUM_HOURS * 3600
        ):
            return None

        folder = back_up(data_dir, target, moment)
        _note(data_dir, moment, True, target=target)
        return folder
    except Exception as failure:
        _note(data_dir, moment, False, failure, target)
        return None


def startup_warning(data_dir=DATA_DIR, now: Optional[datetime] = None, environ=None) -> List[str]:
    """
    What the owner should be told about backups as Freya starts: the
    last one failed, or the newest is old. It reads only the note the
    last attempt left; it starts no program and touches no drive.
    """
    environ = os.environ if environ is None else environ

    if environ.get(AUTOMATIC_VARIABLE) == "0":
        return []

    try:
        status = json.loads(
            (Path(data_dir) / STATUS_NAME).read_text(encoding="utf-8")
        )
        made = datetime.strptime(status["at"], STAMP_FORMAT)
    except (OSError, ValueError, KeyError, TypeError):
        # No attempt has been noted yet; the first close will make one.
        return []

    if status.get("ok") is not True:
        reason = str(status.get("reason") or "no reason was recorded")[:300]
        return [
            "The backup Freya tried to make when she last closed did not "
            f"work: {reason} Your databases have no recent copy on another "
            "drive until one succeeds."
        ]

    age = (now or datetime.now()) - made

    if age.days >= STALE_DAYS:
        return [
            f"The newest backup of Freya's databases is {age.days} days old. "
            "One is made each time she closes."
        ]

    return []


def check(folder) -> List[str]:
    """
    Problems with a backup folder: a missing file, a changed file, or
    a database that fails its own check. An empty list means sound.
    """
    folder = Path(folder)
    problems = []

    try:
        manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
        entries = manifest["copied"]
    except (OSError, ValueError, KeyError, TypeError):
        return ["The manifest is missing or unreadable."]

    for entry in entries:
        path = folder / entry["name"]

        if not path.is_file():
            problems.append(f"{entry['name']} is missing.")
            continue
        if _sha256(path) != entry["sha256"]:
            problems.append(f"{entry['name']} has changed since it was copied.")
            continue
        if entry["kind"] == "database":
            copy = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
            try:
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    problems.append(f"{entry['name']} fails its own check.")
            except sqlite3.Error:
                problems.append(f"{entry['name']} cannot be opened.")
            finally:
                copy.close()

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy Freya's databases and settings to another drive.",
    )
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--to", type=Path, help="Folder to keep backups in. Default: a folder on another drive.")
    parser.add_argument("--check", type=Path, help="Check an existing backup folder instead of making one.")
    args = parser.parse_args(argv)

    if args.check:
        problems = check(args.check)
        print("\n".join(problems) if problems else "The backup is sound.")
        return 1 if problems else 0

    try:
        folder = back_up(args.data, args.to)
    except BackupRefused as refused:
        print(f"No backup was made: {refused}", file=sys.stderr)
        return 1

    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
    print(f"Backup made in {folder}")
    for entry in manifest["copied"]:
        print(f"  {entry['name']}: {entry['bytes']:,} bytes")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
