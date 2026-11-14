"""
Copy Freya's databases and settings to another drive.

A backup beside the data it protects is lost with it, so this refuses
a target on the same drive. It copies; it never moves or deletes, and
it never reads what the databases say, only copies them.

New backups are encrypted. Each file is sealed with one data key
(AES-256-GCM, ``storage/crypto.py``) and the data key is wrapped
(``storage/custody.py``). The wrapped keys travel in the backup. The
data key does not. A backup made before encryption is not written any
more; opening one is the explicit migration path in ``storage/restore.py``.
"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from freya.memory.deletion_ledger import (
    ProtectionSetupError,
    ledger_path_for,
)
from freya.storage.crypto import (
    FILE_MAGIC,
    FILE_VERSION,
    KEY_MAGIC,
    MAX_KEY_FILE_BYTES,
    MAX_MEMBER_BYTES,
    METHOD_OS,
    METHOD_PASSPHRASE,
    METHOD_RECOVERY,
    BackupRefused,
    BundleFile,
    KeyFile,
    Wrap,
    dump_bundle,
    dump_keys,
    header_info,
    load_bundle,
    load_keys,
    open_sealed,
    seal,
    sha256,
)
from freya.storage.custody import (
    PURPOSE_DEK,
    PURPOSE_RECOVERY,
    NoKeyStore,
    default_custody,
    format_recovery_key,
    make_recovery_body,
    open_passphrase_body,
    open_recovery_body,
    prompt_passphrase,
    seal_with_passphrase,
    unwrap_os,
)
from freya.storage.placement import (
    DATA_DIR,
    HDD,
    SSD,
    drive_kinds,
    drive_of,
)


BACKUP_FOLDER_NAME = "FreyaBackups"
KEYS_NAME = "keys.bin"
MANIFEST_NAME = "manifest.json"

# The local bundle holds wraps so the next backup can reuse the data
# key, and, until the owner has written it down, a sealed recovery key.
# It is never inside the data folder. FREYA_BACKUP_KEYS names the file.
BUNDLE_DIRECTORY = "freya_backup_keys"
BUNDLE_FILE = "bundle.bin"
BUNDLE_VARIABLE = "FREYA_BACKUP_KEYS"

UNENCRYPTED_MIGRATION = (
    "This backup is not encrypted. Restoring it needs the explicit "
    "migration flag for backups made before encryption."
)

# Copied with SQLite's own backup, which gives a consistent copy even
# of a database that is in use. The copy is sealed before it is published.
DATABASES = (
    "freya_memory.db",
    "freya_control.db",
    "freya_usage.db",
    "freya_tasks.db",
    # The media library keeps its database under data/media/.
    "media/freya_media.db",
)

# The deletion ledger lives outside data/ so a copy of that folder does
# not take it. A backup copies it under this name. Restore will not
# replace a newer ledger with this older one.
LEDGER_COPY_NAME = "deletion_ledger.db"

# Small files copied as they are, then sealed like the databases.
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
    "federation_nodes.json",
)

# Set to 0 to stop the backup Freya makes when she closes.
AUTOMATIC_VARIABLE = "FREYA_BACKUP"
# Where to keep backups, instead of a folder on another drive.
TARGET_VARIABLE = "FREYA_BACKUP_DIR"
# A backup newer than this makes another one unnecessary.
MINIMUM_HOURS = 1
STAMP_FORMAT = "%Y-%m-%d_%H%M%S"


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


def bundle_path_for(data_dir, environ=None) -> Path:
    """
    Where the wrapped backup keys for ``data_dir`` are kept. Never
    inside that folder: a copy of the data must not be the only thing
    standing between a reader and a later reveal of the recovery key.
    """
    environ = os.environ if environ is None else environ
    data = Path(data_dir).resolve()
    named = str(environ.get(BUNDLE_VARIABLE, "") or "").strip()

    if named:
        path = Path(named).expanduser().resolve()
    else:
        path = (data.parent / BUNDLE_DIRECTORY / BUNDLE_FILE).resolve()

    if path == data or data in path.parents:
        raise BackupRefused("Backup keys must be kept outside the data folder.")

    return path


def _sha256(path: Path) -> str:
    # A legacy plaintext backup is hashed from disk. A new backup hashes
    # the plaintext before it is sealed.
    digest = hashlib.sha256()

    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)

    return digest.hexdigest()


def _copy_database(source: Path, target: Path) -> None:
    # Opened read-only: a backup must never change what it copies.
    # The plaintext copy lives in a temporary directory and is removed
    # before this returns; only the sealed bytes are published.
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


def _known(name: str) -> bool:
    return name in DATABASES or name in FILES or name == LEDGER_COPY_NAME


def _kind(name: str) -> str:
    if name in DATABASES or name == LEDGER_COPY_NAME:
        return "database"

    return "file"


def _read_limited(path: Path, limit: int) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise BackupRefused("The backup file is incomplete.") from exc

    if size > limit:
        raise BackupRefused("The backup file is larger than this Freya will open.")

    try:
        return path.read_bytes()
    except OSError as exc:
        raise BackupRefused("The backup file is incomplete.") from exc


def _child(folder: Path, name: str) -> Path:
    relative = Path(name)

    if relative.is_absolute() or ".." in relative.parts:
        raise BackupRefused("The backup names a file it should not.")

    path = (Path(folder) / relative).resolve()
    root = Path(folder).resolve()

    if path != root and root not in path.parents:
        raise BackupRefused("The backup names a file it should not.")

    return path


def _publish(dest: Path, payload: bytes) -> None:
    """Write a finished payload. The temporary name is not the final file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, dest)


def _write_bundle(path: Path, bundle: BundleFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(dump_bundle(bundle))
    os.replace(temporary, path)

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _sources(data_dir: Path, ledger: Path):
    found = []

    for name in DATABASES:
        source = data_dir / name

        if source.is_file():
            found.append((name, "database", source))

    if ledger.is_file():
        found.append((LEDGER_COPY_NAME, "database", ledger))

    for name in FILES:
        source = data_dir / name

        if source.is_file():
            found.append((name, "file", source))

    return found


def _plain_bytes(kind: str, source: Path) -> bytes:
    if kind != "database":
        return _read_limited(source, MAX_MEMBER_BYTES)

    with tempfile.TemporaryDirectory(prefix="freya-backup-") as temp:
        copy = Path(temp) / "copy.db"
        _copy_database(source, copy)

        return _read_limited(copy, MAX_MEMBER_BYTES)


def _require_wrap(wraps, method: int, missing: str) -> Wrap:
    found = [item for item in wraps if item.method == method]

    if not found:
        raise BackupRefused(missing)

    if len(found) != 1:
        raise BackupRefused("The backup key file is not usable.")

    return found[0]


def _unwrap_saved(bundle: BundleFile, custody, passphrase, recovery_key):
    """
    Open the saved data key. The caller names one method. A failure
    does not try the next.
    """
    if recovery_key is not None:
        from freya.storage.custody import parse_recovery_key

        raw = parse_recovery_key(recovery_key)
        wrap = _require_wrap(bundle.wraps, METHOD_RECOVERY, "This backup has no recovery wrap.")

        return open_recovery_body(raw, wrap.body, bundle.key_id)

    if passphrase is not None:
        wrap = _require_wrap(
            bundle.wraps,
            METHOD_PASSPHRASE,
            "This backup has no passphrase wrap. Use the recovery key.",
        )

        return open_passphrase_body(
            passphrase, wrap.body, key_id=bundle.key_id, purpose=PURPOSE_DEK,
        )

    return unwrap_os(bundle.wraps, custody, bundle.key_id, purpose=PURPOSE_DEK)


def _new_keys(custody, passphrase):
    """
    A new data key, its wraps, and the recovery key text. The text is
    returned to the caller and is not written. A sealed copy is kept
    on the bundle until the caller acknowledges it.
    """
    if passphrase is None and not custody.available():
        raise BackupRefused(
            "No keystore is available. A passphrase is required. Nothing was written."
        )

    key_id = os.urandom(16)
    dek = os.urandom(32)
    recovery_raw = os.urandom(32)
    wraps = []
    seal = None

    if custody.available():
        wraps.append(Wrap(
            METHOD_OS,
            custody.backend_id,
            custody.wrap(dek, key_id=key_id, purpose=PURPOSE_DEK),
        ))
        seal = Wrap(
            METHOD_OS,
            custody.backend_id,
            custody.wrap(recovery_raw, key_id=key_id, purpose=PURPOSE_RECOVERY),
        )

    if passphrase is not None or seal is None:
        if passphrase is None:
            raise BackupRefused(
                "No keystore is available. A passphrase is required. Nothing was written."
            )

        dek_body, sealed_recovery = seal_with_passphrase(
            passphrase, dek, recovery_raw, key_id=key_id,
        )
        wraps.append(Wrap(METHOD_PASSPHRASE, 0, dek_body))

        if seal is None:
            seal = Wrap(METHOD_PASSPHRASE, 0, sealed_recovery)

    wraps.append(Wrap(
        METHOD_RECOVERY, 0, make_recovery_body(recovery_raw, dek, key_id),
    ))
    bundle = BundleFile(key_id, False, wraps, seal)

    return dek, bundle, format_recovery_key(recovery_raw)


def back_up(
    data_dir=DATA_DIR,
    target_root=None,
    now: Optional[datetime] = None,
    allow_same_drive: bool = False,
    environ=None,
    *,
    custody=None,
    passphrase=None,
    recovery_key=None,
    recovery_lines=None,
) -> Path:
    """
    Make one dated encrypted backup and return its folder.

    Each run makes a new folder and leaves earlier ones alone. The
    manifest lists what was copied with the plaintext size and checksum.
    It is sealed with the same data key. ``keys.bin`` holds the wraps,
    not the data key.

    A new recovery key is appended to ``recovery_lines`` when one is
    created. The automatic backup does not pass that list: the key stays
    sealed until the owner asks to see it. Nothing here prints the key.
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

    # A ledger path inside the data folder is a mistake. Refuse before
    # creating the backup folder, and before copying anything else.
    try:
        ledger = ledger_path_for(data_dir, environ)
    except ProtectionSetupError as exc:
        raise BackupRefused(str(exc)) from exc

    sources = _sources(data_dir, ledger)
    stamp = (now or datetime.now()).strftime(STAMP_FORMAT)
    folder = target_root / stamp

    if not sources:
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise BackupRefused("A backup with this time already exists.") from exc

        folder.rmdir()
        raise BackupRefused("There was nothing to back up.")

    if custody is None:
        custody = default_custody()

    bundle_path = bundle_path_for(data_dir, environ)
    existing = None

    if bundle_path.is_file():
        existing = load_bundle(_read_limited(bundle_path, MAX_KEY_FILE_BYTES))

    if existing is None:
        dek, bundle, recovery_text = _new_keys(custody, passphrase)
        _write_bundle(bundle_path, bundle)
        created = recovery_text
    else:
        dek = _unwrap_saved(existing, custody, passphrase, recovery_key)
        bundle = existing
        created = None

    key_id = bundle.key_id

    try:
        prepared = []

        for name, kind, source in sources:
            plain = _plain_bytes(kind, source)
            prepared.append((name, kind, plain, seal(plain, dek, key_id=key_id, name=name)))
    except (OSError, sqlite3.Error) as exc:
        raise BackupRefused(f"The backup could not be completed: {exc}") from exc

    try:
        folder.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BackupRefused("A backup with this time already exists.") from exc

    copied = []

    for name, kind, plain, payload in prepared:
        _publish(folder / name, payload)
        copied.append({
            "name": name,
            "kind": kind,
            "bytes": len(plain),
            "sha256": sha256(plain),
        })

    manifest_plain = json.dumps(
        {
            "made": stamp,
            "from": str(data_dir),
            "encrypted": True,
            "format": FILE_VERSION,
            "copied": copied,
        },
        indent=2,
    ).encode("utf-8")
    manifest_sealed = seal(manifest_plain, dek, key_id=key_id, name=MANIFEST_NAME)
    listed = [(name, len(payload)) for name, _kind, _plain, payload in prepared]
    listed.append((MANIFEST_NAME, len(manifest_sealed)))
    _publish(folder / KEYS_NAME, dump_keys(KeyFile(key_id, bundle.wraps, listed)))
    # The manifest is published last. A folder without it is not a backup.
    _publish(folder / MANIFEST_NAME, manifest_sealed)

    if created is not None and recovery_lines is not None:
        recovery_lines.append(created)

    return folder


def _completed_legacy(folder: Path) -> bool:
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

            if not isinstance(name, str) or not _known(name) or name in seen:
                return False

            seen.add(name)
            expected_kind = _kind(name)
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


def _completed_encrypted(folder: Path) -> bool:
    try:
        keys = load_keys(_read_limited(_child(folder, KEYS_NAME), MAX_KEY_FILE_BYTES))
    except (OSError, BackupRefused):
        return False

    seen = set()

    if len(keys.files) < 2:
        return False

    for name, size in keys.files:
        if name in seen or Path(name).is_absolute() or ".." in Path(name).parts:
            return False

        seen.add(name)

        try:
            path = _child(folder, name)
            blob = path.read_bytes()
        except (OSError, BackupRefused):
            return False

        if len(blob) != size or not path.is_file():
            return False

        try:
            _version, key_id = header_info(blob)
        except BackupRefused:
            return False

        if key_id != keys.key_id:
            return False

    return MANIFEST_NAME in seen


def _completed(folder: Path) -> bool:
    try:
        head = (folder / MANIFEST_NAME).read_bytes()[: len(FILE_MAGIC)]
    except OSError:
        return False

    if head == FILE_MAGIC:
        return _completed_encrypted(folder)

    return _completed_legacy(folder)


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

    This does not show a recovery key. The first encrypted backup seals
    one, and the next start says it has not been written down yet.
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

        folder = back_up(data_dir, target, moment, environ=environ)
        _note(data_dir, moment, True, target=target)
        return folder
    except Exception as failure:
        _note(data_dir, moment, False, failure, target)
        return None


def recovery_waiting(data_dir, environ=None) -> bool:
    """
    True when a recovery key has been sealed and not yet shown.
    A missing or damaged bundle is not waiting: there is nothing to show.
    """
    try:
        path = bundle_path_for(data_dir, environ)
        bundle = load_bundle(_read_limited(path, MAX_KEY_FILE_BYTES))
    except (OSError, BackupRefused):
        return False

    return not bundle.acknowledged


def startup_warning(data_dir=DATA_DIR, now: Optional[datetime] = None, environ=None) -> List[str]:
    """
    What the owner should be told about backups as Freya starts: the
    last one failed, the recovery key has not been written down, or the
    newest backup is old. It reads only notes. It starts no program and
    touches no drive, and it never includes a key.
    """
    environ = os.environ if environ is None else environ

    if environ.get(AUTOMATIC_VARIABLE) == "0":
        return []

    messages = []

    if recovery_waiting(data_dir, environ):
        messages.append(
            "A recovery key for backups has not been written down yet. "
            "Run python -m freya.storage.backup --show-recovery-key and keep "
            "what it prints off this machine."
        )

    try:
        status = json.loads(
            (Path(data_dir) / STATUS_NAME).read_text(encoding="utf-8")
        )
        made = datetime.strptime(status["at"], STAMP_FORMAT)
    except (OSError, ValueError, KeyError, TypeError):
        return messages

    if status.get("ok") is not True:
        reason = str(status.get("reason") or "no reason was recorded")[:300]
        messages.append(
            "The backup Freya tried to make when she last closed did not "
            f"work: {reason} Your databases have no recent copy on another "
            "drive until one succeeds."
        )
        return messages

    age = (now or datetime.now()) - made

    if age.days >= STALE_DAYS:
        messages.append(
            f"The newest backup of Freya's databases is {age.days} days old. "
            "One is made each time she closes."
        )

    return messages


def reveal_recovery_key(
    data_dir, *, custody=None, passphrase=None, environ=None,
) -> str:
    """
    Return the recovery key text while it is still sealed. The caller
    prints it and then acknowledges it. A second call refuses.
    """
    path = bundle_path_for(data_dir, environ)
    bundle = load_bundle(_read_limited(path, MAX_KEY_FILE_BYTES))

    if bundle.acknowledged or bundle.recovery_seal is None:
        raise BackupRefused(
            "The recovery key was already shown. The printed copy is the "
            "one that still opens these backups."
        )

    seal_wrap = bundle.recovery_seal

    if seal_wrap.method == METHOD_OS:
        if custody is None:
            custody = default_custody()

        raw = unwrap_os([seal_wrap], custody, bundle.key_id, purpose=PURPOSE_RECOVERY)
    elif seal_wrap.method == METHOD_PASSPHRASE:
        if passphrase is None:
            raise BackupRefused("A passphrase is required.")

        raw = open_passphrase_body(
            passphrase, seal_wrap.body, key_id=bundle.key_id, purpose=PURPOSE_RECOVERY,
        )
    else:
        raise BackupRefused("The backup key file is not usable.")

    return format_recovery_key(raw)


def acknowledge_recovery_key(data_dir, *, environ=None) -> None:
    """
    Drop the sealed recovery key. The printed copy is the one that remains.
    """
    path = bundle_path_for(data_dir, environ)
    bundle = load_bundle(_read_limited(path, MAX_KEY_FILE_BYTES))

    if bundle.acknowledged or bundle.recovery_seal is None:
        raise BackupRefused(
            "The recovery key was already shown. The printed copy is the "
            "one that still opens these backups."
        )

    bundle.acknowledged = True
    bundle.recovery_seal = None
    _write_bundle(path, bundle)


def copy_path(folder, name) -> Optional[Path]:
    """
    The file a manifest entry names, when it is one this backup may
    hold and the path stays inside the backup folder.
    """
    if not isinstance(name, str) or not _known(name):
        return None

    relative = Path(name)

    if relative.is_absolute() or ".." in relative.parts:
        return None

    path = (Path(folder) / relative).resolve()
    root = Path(folder).resolve()

    if path != root and root not in path.parents:
        return None

    return path


def _is_encrypted(folder: Path) -> bool:
    try:
        keys = (folder / KEYS_NAME).read_bytes()[: len(KEY_MAGIC)]
        manifest = (folder / MANIFEST_NAME).read_bytes()[: len(FILE_MAGIC)]
    except OSError:
        return False

    return keys == KEY_MAGIC and manifest == FILE_MAGIC


def _is_legacy(folder: Path) -> bool:
    path = folder / MANIFEST_NAME

    try:
        blob = path.read_bytes()
    except OSError:
        return False

    if blob.startswith(FILE_MAGIC):
        return False

    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeError, ValueError):
        return False

    return isinstance(parsed, dict) and isinstance(parsed.get("copied"), list)


def unlock_dek(folder, *, custody=None, passphrase=None, recovery_key=None):
    """
    The data key for one backup folder. A recovery key, a passphrase, or
    the OS keystore: whichever the caller passed, and not the others.
    """
    keys = load_keys(_read_limited(_child(folder, KEYS_NAME), MAX_KEY_FILE_BYTES))

    if recovery_key is not None:
        from freya.storage.custody import parse_recovery_key

        raw = parse_recovery_key(recovery_key)
        wrap = _require_wrap(keys.wraps, METHOD_RECOVERY, "This backup has no recovery wrap.")
        dek = open_recovery_body(raw, wrap.body, keys.key_id)
    elif passphrase is not None:
        wrap = _require_wrap(
            keys.wraps,
            METHOD_PASSPHRASE,
            "This backup has no passphrase wrap. Use the recovery key.",
        )
        dek = open_passphrase_body(
            passphrase, wrap.body, key_id=keys.key_id, purpose=PURPOSE_DEK,
        )
    else:
        if custody is None:
            custody = default_custody()

        dek = unwrap_os(keys.wraps, custody, keys.key_id, purpose=PURPOSE_DEK)

    if len(dek) != 32:
        raise BackupRefused("The backup failed its authentication check.")

    return dek, keys.key_id


def read_manifest(folder, **unlock) -> dict:
    """The manifest of an encrypted backup, after the key has opened it."""
    dek, key_id = unlock_dek(folder, **unlock)
    plain = open_sealed(
        _read_limited(_child(folder, MANIFEST_NAME), MAX_MEMBER_BYTES),
        dek,
        key_id=key_id,
        name=MANIFEST_NAME,
    )

    try:
        manifest = json.loads(plain.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BackupRefused("The manifest is missing or unreadable.") from exc

    if not isinstance(manifest, dict):
        raise BackupRefused("The manifest is missing or unreadable.")

    return manifest


def read_member(folder, name: str, **unlock) -> bytes:
    """One backup member, in full, after the authentication check."""
    dek, key_id = unlock_dek(folder, **unlock)
    path = copy_path(folder, name)

    if path is None or not path.is_file():
        raise BackupRefused("The backup names a file it should not.")

    return open_sealed(
        _read_limited(path, MAX_MEMBER_BYTES), dek, key_id=key_id, name=name,
    )


def replace_member(
    folder, name: str, plaintext: bytes, *, update_manifest: bool = True, **unlock,
) -> None:
    """
    Put new plaintext in one member and, by default, the manifest checksum
    that matches it. Both are sealed again. A test uses this when it has
    changed a copy on purpose.
    """
    if not isinstance(plaintext, (bytes, bytearray)) or not _known(name):
        raise BackupRefused("The backup names a file it should not.")

    dek, key_id = unlock_dek(folder, **unlock)
    manifest = read_manifest(folder, **unlock)
    _publish(Path(folder) / name, seal(bytes(plaintext), dek, key_id=key_id, name=name))

    if update_manifest:
        for entry in manifest.get("copied", []):
            if isinstance(entry, dict) and entry.get("name") == name:
                entry["bytes"] = len(plaintext)
                entry["sha256"] = sha256(bytes(plaintext))

        _publish_manifest(folder, manifest, dek, key_id)

    _rewrite_sizes(folder)


def _publish_manifest(folder, manifest, dek, key_id) -> None:
    payload = json.dumps(manifest, indent=2).encode("utf-8")
    sealed = seal(payload, dek, key_id=key_id, name=MANIFEST_NAME)
    _publish(Path(folder) / MANIFEST_NAME, sealed)


def _rewrite_sizes(folder) -> None:
    folder = Path(folder)
    keys = load_keys(_read_limited(_child(folder, KEYS_NAME), MAX_KEY_FILE_BYTES))
    files = []

    for name, _size in keys.files:
        path = _child(folder, name)
        files.append((name, path.stat().st_size))

    _publish(folder / KEYS_NAME, dump_keys(KeyFile(keys.key_id, keys.wraps, files)))


def materialise(folder, **unlock) -> Path:
    """
    Decrypt a backup into a new temporary directory. Every member is
    authenticated before it is written. On any failure the directory
    is removed, so a refused backup leaves no plaintext behind.
    """
    folder = Path(folder)
    root = Path(tempfile.mkdtemp(prefix="freya-restore-"))

    try:
        dek, key_id = unlock_dek(folder, **unlock)
        manifest_plain = open_sealed(
            _read_limited(_child(folder, MANIFEST_NAME), MAX_MEMBER_BYTES),
            dek,
            key_id=key_id,
            name=MANIFEST_NAME,
        )
        (root / MANIFEST_NAME).write_bytes(manifest_plain)

        try:
            manifest = json.loads(manifest_plain.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise BackupRefused("The manifest is missing or unreadable.") from exc

        for entry in manifest["copied"]:
            if not isinstance(entry, dict):
                raise BackupRefused("The backup names a file it should not.")

            name = entry.get("name")
            source = copy_path(folder, name)

            if source is None:
                raise BackupRefused("The backup names a file it should not.")

            # A known file that is not there is left out. The plaintext
            # check then reports it missing, which is the same words a
            # plaintext backup uses.
            if not source.is_file():
                continue

            plain = open_sealed(
                _read_limited(source, MAX_MEMBER_BYTES), dek, key_id=key_id, name=name,
            )
            dest = copy_path(root, name)

            if dest is None:
                raise BackupRefused("The backup names a file it should not.")

            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(plain)

        return root
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _check_plain(folder: Path) -> List[str]:
    """
    Problems with a plaintext backup tree: a missing file, a changed
    file, or a database that fails its own check. An empty list means sound.
    """
    folder = Path(folder)
    problems = []

    try:
        manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
        entries = manifest["copied"]
    except (OSError, ValueError, KeyError, TypeError):
        return ["The manifest is missing or unreadable."]

    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("The backup names a file it should not.")
            continue

        path = copy_path(folder, entry.get("name"))

        if path is None:
            problems.append("The backup names a file it should not.")
            continue

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


def check(
    folder,
    *,
    custody=None,
    passphrase=None,
    recovery_key=None,
    allow_unencrypted: bool = False,
) -> List[str]:
    """
    Problems with a backup folder. An encrypted backup is decrypted in
    a temporary directory that is removed before this returns. An old
    plaintext backup is reported, and checked only when
    ``allow_unencrypted`` is set.
    """
    folder = Path(folder)

    if _is_encrypted(folder):
        try:
            staging = materialise(
                folder,
                custody=custody,
                passphrase=passphrase,
                recovery_key=recovery_key,
            )
        except BackupRefused as exc:
            return [str(exc)]

        try:
            return _check_plain(staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    if _is_legacy(folder):
        if not allow_unencrypted:
            return [UNENCRYPTED_MIGRATION]

        return _check_plain(folder)

    return ["The manifest is missing or unreadable."]


def _require_terminal() -> None:
    if not sys.stdout.isatty():
        raise BackupRefused(
            "The recovery key is shown only on a terminal, so it is not written to a file."
        )


def _unlock_for_folder(folder: Path) -> dict:
    """How the command line should open this folder. One method, no second try."""
    keys = load_keys(_read_limited(_child(folder, KEYS_NAME), MAX_KEY_FILE_BYTES))
    custody = default_custody()

    if custody.available() and any(
        item.method == METHOD_OS and item.backend == custody.backend_id
        for item in keys.wraps
    ):
        return {"custody": custody}

    if any(item.method == METHOD_PASSPHRASE for item in keys.wraps):
        return {"passphrase": prompt_passphrase(), "custody": NoKeyStore()}

    import getpass

    return {
        "recovery_key": getpass.getpass("Recovery key: "),
        "custody": NoKeyStore(),
    }


def _unlock_for_new_backup(data_dir) -> dict:
    custody = default_custody()
    path = bundle_path_for(data_dir)

    if path.is_file():
        bundle = load_bundle(_read_limited(path, MAX_KEY_FILE_BYTES))
        has_os = custody.available() and any(
            item.method == METHOD_OS and item.backend == custody.backend_id
            for item in bundle.wraps
        )

        if has_os:
            return {"custody": custody}

        if any(item.method == METHOD_PASSPHRASE for item in bundle.wraps):
            return {"passphrase": prompt_passphrase(), "custody": NoKeyStore()}

        import getpass

        return {
            "recovery_key": getpass.getpass("Recovery key: "),
            "custody": NoKeyStore(),
        }

    if custody.available():
        return {"custody": custody}

    return {"passphrase": prompt_passphrase(), "custody": NoKeyStore()}


def _show_recovery_key(data_dir) -> int:
    _require_terminal()
    path = bundle_path_for(data_dir)
    bundle = load_bundle(_read_limited(path, MAX_KEY_FILE_BYTES))

    if bundle.recovery_seal is not None and bundle.recovery_seal.method == METHOD_PASSPHRASE:
        text = reveal_recovery_key(data_dir, passphrase=prompt_passphrase())
    else:
        text = reveal_recovery_key(data_dir)

    print("Write this recovery key down and keep it off this machine.")
    print("It will not be shown again.")
    print(text)
    acknowledge_recovery_key(data_dir)

    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Copy Freya's databases and settings to another drive. "
            "New backups are encrypted. A passphrase is asked for when "
            "this machine has no keystore; it is not a command argument."
        ),
    )
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--to", type=Path, help="Folder to keep backups in. Default: a folder on another drive.")
    parser.add_argument("--check", type=Path, help="Check an existing backup folder instead of making one.")
    parser.add_argument(
        "--allow-unencrypted",
        action="store_true",
        help="Check a backup made before encryption. Only for that migration.",
    )
    parser.add_argument(
        "--show-recovery-key",
        action="store_true",
        help="Print the recovery key once, if it has not been shown yet.",
    )
    args = parser.parse_args(argv)

    try:
        if args.show_recovery_key:
            return _show_recovery_key(args.data)

        if args.check:
            if _is_encrypted(args.check):
                problems = check(args.check, **_unlock_for_folder(args.check))
            else:
                problems = check(args.check, allow_unencrypted=args.allow_unencrypted)
        else:
            lines = []
            unlock = _unlock_for_new_backup(args.data)
            folder = back_up(
                args.data,
                args.to,
                recovery_lines=lines,
                **unlock,
            )

            if lines:
                _require_terminal()
                print("Write this recovery key down and keep it off this machine.")
                print("It will not be shown again.")
                print(lines[0])
                acknowledge_recovery_key(args.data)
                lines.clear()

            manifest = read_manifest(folder, **unlock)
            print(f"Backup made in {folder}")

            for entry in manifest["copied"]:
                print(f"  {entry['name']}: {entry['bytes']:,} bytes")

            return 0
    except BackupRefused as refused:
        print(f"No backup was made: {refused}", file=sys.stderr)
        return 1

    print("\n".join(problems) if problems else "The backup is sound.")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
