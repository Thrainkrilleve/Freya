"""
Rotate and revoke the wraps on Freya's live data key.

The database key stays the same when the passphrase or the recovery key
changes. Only a data-key rotation re-encrypts the stores, and only after
a verified backup. The old wraps are kept beside the bundle until the
new bundle has been checked. Nothing here writes a passphrase, a recovery
key, or a data key as a plain file, and nothing here puts them in an
exception.

    python -m freya.storage.keys status
    python -m freya.storage.keys add-passphrase
    python -m freya.storage.keys rotate-passphrase
    python -m freya.storage.keys rotate-recovery
    python -m freya.storage.keys rotate-dek --backup-dir D:\\FreyaBackups
    python -m freya.storage.keys rotate-dek --rollback
    python -m freya.storage.keys revoke --which passphrase
    python -m freya.storage.keys revoke --which recovery
    python -m freya.storage.keys show-recovery-key

A rotation that stops halfway is recorded outside the data directory.
Running the same command again resumes it. ``--rollback`` puts the
stores back on the previous data key and removes that record. The
passphrase stretch is unchanged: 256 MiB, 3 iterations, parallelism 4.
"""

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from freya.storage.backup import BackupRefused, back_up, check
from freya.storage.crypto import METHOD_OS, METHOD_PASSPHRASE, METHOD_RECOVERY, BundleFile, Wrap
from freya.storage.custody import (
    PURPOSE_DEK,
    PURPOSE_RECOVERY,
    BackupRefused as CustodyRefused,
    accept_new_passphrase,
    default_custody,
    format_recovery_key,
    make_recovery_body,
    parse_recovery_key,
    prompt_passphrase,
    unwrap_data_key,
    unwrap_os,
    wrap_under_stretch,
)
from freya.storage.dbkey import (
    StoreLocked,
    _bind,
    bundle_path_for,
    current,
    read_bundle,
    replace_bundle,
    require_unlocked,
)
from freya.storage.encrypt import store_paths
from freya.storage.placement import DATA_DIR
from freya.storage.sqlite import (
    StoreError,
    is_plaintext_sqlite,
    rekey_copy,
    wipe_file,
    wipe_sidecars,
)


JOURNAL_VERSION = 1
_hook = None


class RotationStopped(StoreError):
    """A rotation step was interrupted. The journal is still on disk."""


def set_rotation_hook(hook) -> None:
    """Test hook called after a journalled step. ``None`` clears it."""
    global _hook
    _hook = hook


def _step(kind: str, path: Path) -> None:
    if _hook is not None:
        _hook(kind, path)


def _journal_path(data_dir, environ=None) -> Path:
    bundle = bundle_path_for(data_dir, environ)
    return bundle.with_name(bundle.name + ".rotation")


def _sibling(path: Path) -> Path:
    return path.with_name(path.name + ".rekey-new")


def _aside(path: Path) -> Path:
    return path.with_name(path.name + ".rekey-old")


def _save_journal(path: Path, record: dict) -> None:
    blob = json.dumps(record, indent=2).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    from freya.storage.dbkey import _fsync_dir, _fsync_write

    _fsync_write(temporary, blob)
    os.replace(temporary, path)
    _fsync_dir(path.parent)

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_journal(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StoreError("The rotation record could not be read.") from exc

    if not isinstance(record, dict) or record.get("version") != JOURNAL_VERSION:
        raise StoreError("The rotation record could not be read.")

    return record


def _wipe_journal(path: Path) -> None:
    wipe_file(path)
    wipe_file(path.with_name(path.name + ".tmp"))


def stretch_of(body: bytes):
    """The stretch numbers stored on a wrap, or None when the body is short."""
    from freya.storage.custody import ARGON2_SALT_LEN
    import struct

    header = ARGON2_SALT_LEN + 9

    if not isinstance(body, (bytes, bytearray)) or len(body) < header:
        return None

    memory, time_cost, parallelism = struct.unpack(
        ">IIB", bytes(body[ARGON2_SALT_LEN:header])
    )

    return {
        "memory_kib": memory,
        "time": time_cost,
        "parallelism": parallelism,
    }


def _methods(bundle):
    found = []

    for wrap in bundle.wraps:
        if wrap.method == METHOD_OS:
            found.append("os")
        elif wrap.method == METHOD_PASSPHRASE:
            found.append("passphrase")
        elif wrap.method == METHOD_RECOVERY:
            found.append("recovery")

    return found


def status(data_dir, environ=None) -> dict:
    """
    Wrap metadata for the live data key.

    The result has the key id, which wraps exist, the stretch numbers,
    whether the recovery key has been shown, and whether a rotation is
    in progress. It has no wrap body, passphrase, recovery key, or data key.
    """
    path = bundle_path_for(data_dir, environ)
    bundle = read_bundle(data_dir, environ)
    stretch = None

    for wrap in bundle.wraps:
        if wrap.method == METHOD_PASSPHRASE:
            stretch = stretch_of(wrap.body)

    journal = _journal_path(data_dir, environ)
    rotation = None

    if journal.is_file():
        try:
            rotation = _load_journal(journal).get("phase")
        except StoreError:
            rotation = "unreadable"

    shown = "recovery" in _methods(bundle) and (
        bundle.acknowledged or bundle.recovery_seal is None
    )

    return {
        "key_id": bundle.key_id.hex(),
        "wraps": _methods(bundle),
        "stretch": stretch,
        "recovery_shown": shown,
        "rotation": rotation,
        "bundle": str(path),
    }


def _unwrap_one(bundle, custody, *, passphrase=None, recovery_key=None) -> bytes:
    try:
        return unwrap_data_key(
            bundle,
            custody,
            passphrase,
            recovery_key,
            missing_recovery="This database key has no recovery wrap.",
            missing_passphrase="This database key has no passphrase wrap.",
        )
    except (BackupRefused, CustodyRefused):
        raise StoreLocked("The unlock was not accepted. Nothing was changed.") from None


def _require_current(bundle, dek: bytes, custody, passphrase, recovery_key) -> None:
    """Each existing wrap must open onto ``dek`` before anything is written."""
    methods = _methods(bundle)

    if "passphrase" in methods:
        if passphrase is None:
            raise StoreLocked("A passphrase is required. Nothing was changed.")

        if _unwrap_one(bundle, custody, passphrase=passphrase) != dek:
            raise StoreLocked("The unlock was not accepted. Nothing was changed.")

    if "recovery" in methods:
        if recovery_key is None:
            raise StoreLocked("The recovery key is required. Nothing was changed.")

        if _unwrap_one(bundle, custody, recovery_key=recovery_key) != dek:
            raise StoreLocked("The unlock was not accepted. Nothing was changed.")

    if "os" in methods:
        try:
            opened = unwrap_os(bundle.wraps, custody, bundle.key_id, purpose=PURPOSE_DEK)
        except (BackupRefused, CustodyRefused):
            raise StoreLocked("The unlock was not accepted. Nothing was changed.") from None

        if opened != dek:
            raise StoreLocked("The unlock was not accepted. Nothing was changed.")


def _bundle_for(dek, key_id, custody, passphrase, recovery_raw, *, show_recovery: bool):
    wraps = []
    seal = None

    if custody is not None and custody.available():
        wraps.append(Wrap(
            METHOD_OS,
            custody.backend_id,
            custody.wrap(dek, key_id=key_id, purpose=PURPOSE_DEK),
        ))

        if show_recovery:
            seal = Wrap(
                METHOD_OS,
                custody.backend_id,
                custody.wrap(recovery_raw, key_id=key_id, purpose=PURPOSE_RECOVERY),
            )

    if passphrase is not None:
        wraps.append(Wrap(
            METHOD_PASSPHRASE,
            0,
            wrap_under_stretch(passphrase, dek, key_id=key_id, purpose=PURPOSE_DEK),
        ))

        if show_recovery and seal is None:
            seal = Wrap(
                METHOD_PASSPHRASE,
                0,
                wrap_under_stretch(
                    passphrase, recovery_raw, key_id=key_id, purpose=PURPOSE_RECOVERY,
                ),
            )

    if recovery_raw is not None:
        wraps.append(Wrap(
            METHOD_RECOVERY, 0, make_recovery_body(recovery_raw, dek, key_id),
        ))

    if not wraps:
        raise StoreLocked("The database key has no unlock. Nothing was changed.")

    return BundleFile(key_id, not show_recovery, wraps, seal if show_recovery else None)


def _check_opens(path, bundle, custody, passphrase, recovery_key, dek) -> None:
    opened = _unwrap_one(
        bundle,
        custody,
        passphrase=passphrase,
        recovery_key=recovery_key,
    )

    if opened != bytes(dek):
        raise StoreLocked("The new wraps did not open. The previous wraps were kept.")


def rotate_passphrase(
    data_dir, current_passphrase, new_passphrase, *, custody=None, environ=None,
) -> None:
    """
    Re-wrap the current data key under ``new_passphrase``.

    The stores are not opened and not rewritten. The previous bundle is
    kept until the new wrap unwraps. A wrong current passphrase writes
    nothing.
    """
    if custody is None:
        custody = default_custody(environ)

    try:
        accept_new_passphrase(new_passphrase)
    except CustodyRefused as exc:
        raise StoreLocked(str(exc) + " Nothing was changed.") from exc

    held = require_unlocked()
    bundle = read_bundle(data_dir, environ)

    if "passphrase" not in _methods(bundle):
        raise StoreLocked(
            "There is no passphrase wrap to replace. "
            "Run python -m freya.storage.keys add-passphrase. Nothing was changed."
        )

    if _unwrap_one(bundle, custody, passphrase=current_passphrase) != held.dek:
        raise StoreLocked("The unlock was not accepted. Nothing was changed.")

    wraps = []
    seal = bundle.recovery_seal

    # A recovery key that has not been shown yet is sealed under the
    # passphrase. Re-seal it so the new passphrase can still reveal it.
    if seal is not None and seal.method == METHOD_PASSPHRASE:
        from freya.storage.custody import open_stretch_wrap

        try:
            opened = bytearray(open_stretch_wrap(
                current_passphrase,
                seal.body,
                key_id=bundle.key_id,
                purpose=PURPOSE_RECOVERY,
            ))
        except (BackupRefused, CustodyRefused):
            raise StoreLocked("The unlock was not accepted. Nothing was changed.") from None

        try:
            seal = Wrap(
                METHOD_PASSPHRASE,
                0,
                wrap_under_stretch(
                    new_passphrase,
                    bytes(opened),
                    key_id=bundle.key_id,
                    purpose=PURPOSE_RECOVERY,
                ),
            )
        finally:
            for index in range(len(opened)):
                opened[index] = 0

    for wrap in bundle.wraps:
        if wrap.method == METHOD_PASSPHRASE:
            wraps.append(Wrap(
                METHOD_PASSPHRASE,
                0,
                wrap_under_stretch(
                    new_passphrase, held.dek, key_id=bundle.key_id, purpose=PURPOSE_DEK,
                ),
            ))
        else:
            wraps.append(wrap)

    updated = BundleFile(bundle.key_id, bundle.acknowledged, wraps, seal)
    path = bundle_path_for(data_dir, environ)

    def check():
        fresh = read_bundle(data_dir, environ)
        _check_opens(path, fresh, custody, new_passphrase, None, held.dek)

    replace_bundle(path, updated, check=check)


def _finish_add_passphrase(data_dir, journal, held, custody, passphrase, environ) -> None:
    bundle = read_bundle(data_dir, environ)
    path = bundle_path_for(data_dir, environ)

    if "passphrase" in _methods(bundle):
        if _unwrap_one(bundle, custody, passphrase=passphrase) != held.dek:
            raise StoreLocked("The unlock was not accepted. Nothing was changed.")

        _wipe_journal(journal)
        return

    wraps = list(bundle.wraps)
    wraps.append(Wrap(
        METHOD_PASSPHRASE,
        0,
        wrap_under_stretch(passphrase, held.dek, key_id=bundle.key_id, purpose=PURPOSE_DEK),
    ))
    updated = BundleFile(bundle.key_id, bundle.acknowledged, wraps, bundle.recovery_seal)

    def check():
        fresh = read_bundle(data_dir, environ)
        _check_opens(path, fresh, custody, passphrase, None, held.dek)

        if "os" in _methods(bundle) and custody.available():
            if _unwrap_one(fresh, custody) != held.dek:
                raise StoreLocked("The new wraps did not open. The previous wraps were kept.")

    replace_bundle(path, updated, check=check)
    _step("add-written", path)
    _wipe_journal(journal)


def add_passphrase(data_dir, passphrase, *, custody=None, environ=None) -> None:
    """
    Add one argon2id wrap beside the wraps that already open the data key.

    The stores are not rewritten. A passphrase wrap that is already there
    is refused. The passphrase is not written into the journal. A crash
    leaves that journal, and the same command finishes the add.
    """
    try:
        accept_new_passphrase(passphrase)
    except CustodyRefused as exc:
        raise StoreLocked(str(exc) + " Nothing was changed.") from exc

    if custody is None:
        custody = default_custody(environ)

    held = require_unlocked()
    existing, journal = _record_from_journal(data_dir, environ)

    if existing is not None:
        if existing.get("operation") != "add-passphrase":
            raise StoreLocked("A rotation is already in progress. Nothing was changed.")

        if existing.get("key_id") != held.key_id.hex():
            raise StoreLocked("The rotation record does not match this key. Nothing was changed.")

        _finish_add_passphrase(data_dir, journal, held, custody, passphrase, environ)
        return

    bundle = read_bundle(data_dir, environ)

    if "passphrase" in _methods(bundle):
        raise StoreLocked(
            "A passphrase wrap already exists. "
            "Run python -m freya.storage.keys rotate-passphrase. Nothing was changed."
        )

    record = {
        "version": JOURNAL_VERSION,
        "operation": "add-passphrase",
        "phase": "writing",
        "key_id": bundle.key_id.hex(),
    }
    _save_journal(journal, record)
    _step("add-prepared", journal)
    _finish_add_passphrase(data_dir, journal, held, custody, passphrase, environ)


def rotate_recovery(
    data_dir, *, custody=None, passphrase=None, recovery_key=None, environ=None,
) -> str:
    """
    Mint a recovery key, wrap the current data key with it, and drop the old wrap.

    The returned text is the new recovery key. The caller shows it once.
    This function does not print it. The stores are not rewritten.
    """
    if custody is None:
        custody = default_custody(environ)

    held = require_unlocked()
    bundle = read_bundle(data_dir, environ)
    _require_current(bundle, held.dek, custody, passphrase, recovery_key)
    recovery_raw = bytearray(os.urandom(32))

    try:
        wraps = [wrap for wrap in bundle.wraps if wrap.method != METHOD_RECOVERY]
        wraps.append(Wrap(
            METHOD_RECOVERY,
            0,
            make_recovery_body(bytes(recovery_raw), held.dek, bundle.key_id),
        ))
        seal = None

        if custody.available():
            seal = Wrap(
                METHOD_OS,
                custody.backend_id,
                custody.wrap(
                    bytes(recovery_raw), key_id=bundle.key_id, purpose=PURPOSE_RECOVERY,
                ),
            )
        elif passphrase is not None:
            seal = Wrap(
                METHOD_PASSPHRASE,
                0,
                wrap_under_stretch(
                    passphrase,
                    bytes(recovery_raw),
                    key_id=bundle.key_id,
                    purpose=PURPOSE_RECOVERY,
                ),
            )

        updated = BundleFile(bundle.key_id, False, wraps, seal)
        text = format_recovery_key(bytes(recovery_raw))
        path = bundle_path_for(data_dir, environ)

        def check():
            fresh = read_bundle(data_dir, environ)
            opened = _unwrap_one(fresh, custody, recovery_key=text)

            if opened != held.dek:
                raise StoreLocked("The new wraps did not open. The previous wraps were kept.")

        replace_bundle(path, updated, check=check)
        return text
    finally:
        for index in range(len(recovery_raw)):
            recovery_raw[index] = 0


def revoke(
    data_dir, which, *, custody=None, passphrase=None, recovery_key=None, environ=None,
) -> None:
    """
    Remove one wrap. The last unlock method is refused, and a remaining
    method must open the data key before the bundle is written.
    """
    if which not in ("passphrase", "recovery"):
        raise StoreLocked("Name passphrase or recovery. Nothing was changed.")

    if custody is None:
        custody = default_custody(environ)

    held = require_unlocked()
    bundle = read_bundle(data_dir, environ)
    methods = _methods(bundle)

    if which not in methods:
        raise StoreLocked("That wrap is not on the database key. Nothing was changed.")

    remaining = [name for name in methods if name != which]

    if not remaining:
        raise StoreLocked("That is the only unlock. Nothing was changed.")

    if "passphrase" in remaining:
        if _unwrap_one(bundle, custody, passphrase=passphrase) != held.dek:
            raise StoreLocked("The unlock was not accepted. Nothing was changed.")

    if "recovery" in remaining:
        if _unwrap_one(bundle, custody, recovery_key=recovery_key) != held.dek:
            raise StoreLocked("The unlock was not accepted. Nothing was changed.")

    if "os" in remaining:
        try:
            opened = unwrap_os(bundle.wraps, custody, bundle.key_id, purpose=PURPOSE_DEK)
        except (BackupRefused, CustodyRefused):
            raise StoreLocked("The unlock was not accepted. Nothing was changed.") from None

        if opened != held.dek:
            raise StoreLocked("The unlock was not accepted. Nothing was changed.")

    drop = METHOD_PASSPHRASE if which == "passphrase" else METHOD_RECOVERY
    wraps = [wrap for wrap in bundle.wraps if wrap.method != drop]
    seal = None if which == "recovery" else bundle.recovery_seal
    acknowledged = True if which == "recovery" else bundle.acknowledged
    updated = BundleFile(bundle.key_id, acknowledged, wraps, seal)
    path = bundle_path_for(data_dir, environ)

    def check():
        fresh = read_bundle(data_dir, environ)

        if which in _methods(fresh):
            raise StoreLocked("The wrap is still present. The previous wraps were kept.")

        if "passphrase" in remaining:
            _check_opens(path, fresh, custody, passphrase, None, held.dek)
        elif "recovery" in remaining:
            _check_opens(path, fresh, custody, None, recovery_key, held.dek)
        else:
            opened = unwrap_os(fresh.wraps, custody, fresh.key_id, purpose=PURPOSE_DEK)

            if opened != held.dek:
                raise StoreLocked("The new wraps did not open. The previous wraps were kept.")

    replace_bundle(path, updated, check=check)


def _verified_backup(data_dir, backup_dir, *, custody, passphrase, allow_same_drive, environ, now):
    if backup_dir is None:
        raise StoreError("A verified backup is required before the data key changes. Nothing was changed.")

    # The live recovery key opens the database wraps. A new backup has
    # its own recovery key, so it is not offered here.
    try:
        folder = back_up(
            data_dir,
            backup_dir,
            now,
            allow_same_drive=allow_same_drive,
            environ=environ,
            custody=custody,
            passphrase=passphrase,
        )
    except BackupRefused as exc:
        raise StoreError(str(exc)) from None

    problems = check(
        folder,
        custody=custody,
        passphrase=passphrase,
    )

    if problems:
        raise StoreError(
            "The backup was not sound, so the data key was not changed: " + " ".join(problems)
        )

    return folder


def _new_bundle_bytes(dek, key_id, custody, passphrase, recovery_raw) -> bytes:
    from freya.storage.crypto import dump_bundle

    bundle = _bundle_for(
        dek, key_id, custody, passphrase, recovery_raw, show_recovery=False,
    )

    return dump_bundle(bundle)


def _stores(data_dir, environ):
    paths = [path for path in store_paths(data_dir, environ) if path.is_file()]

    if any(is_plaintext_sqlite(path) for path in paths):
        raise StoreError(
            "A database is not encrypted. Run python -m freya.storage.encrypt. "
            "Nothing was changed."
        )

    if not paths:
        raise StoreError("There is no encrypted database to rotate. Nothing was changed.")

    return paths


def _record_from_journal(data_dir, environ):
    path = _journal_path(data_dir, environ)

    if not path.is_file():
        return None, path

    return _load_journal(path), path


def _decode_bundle(payload: str):
    from freya.storage.crypto import load_bundle

    try:
        return load_bundle(base64.b64decode(payload))
    except Exception as exc:
        raise StoreError("The rotation record could not be read.") from exc


def _copy_stores(record, journal: Path, old_dek, new_dek) -> None:
    for item in record["stores"]:
        path = Path(item["path"])

        if item["state"] in ("copied", "swapped"):
            continue

        dest = _sibling(path)
        rekey_copy(path, dest, old_dek, new_dek)
        item["state"] = "copied"
        _save_journal(journal, record)
        _step("copied", path)


def _swap_stores(record, journal: Path) -> None:
    record["phase"] = "swapping"
    _save_journal(journal, record)

    for item in record["stores"]:
        if item["state"] == "swapped":
            continue

        if item["state"] != "copied":
            raise StoreError("The rotation is incomplete. Nothing further was published.")

        path = Path(item["path"])
        sibling = _sibling(path)
        aside = _aside(path)

        # A crash can leave the original already moved aside and the new
        # file not yet in its place. Do not remove that aside.
        if aside.is_file() and not path.exists() and sibling.is_file():
            os.replace(sibling, path)
        elif path.exists() and sibling.is_file():
            os.replace(path, aside)
            try:
                os.replace(sibling, path)
            except OSError:
                if not path.exists() and aside.is_file():
                    os.replace(aside, path)

                raise
        elif aside.is_file() and not path.exists():
            os.replace(aside, path)
            raise StoreError("The re-encrypted copy was missing. The original was restored.")
        else:
            raise StoreError("The re-encrypted copy was missing. Nothing further was published.")

        wipe_sidecars(path)
        item["state"] = "swapped"
        _save_journal(journal, record)
        _step("swapped", path)


def _open_factor(bundle, custody, passphrase, recovery_key) -> bytes:
    if passphrase is not None and "passphrase" in _methods(bundle):
        return _unwrap_one(bundle, custody, passphrase=passphrase)

    if recovery_key is not None and "recovery" in _methods(bundle):
        return _unwrap_one(bundle, custody, recovery_key=recovery_key)

    return unwrap_os(bundle.wraps, custody, bundle.key_id, purpose=PURPOSE_DEK)


def _publish(data_dir, record, journal, custody, passphrase, recovery_key, new_dek, environ=None) -> None:
    record["phase"] = "publishing"
    _save_journal(journal, record)
    bundle = _decode_bundle(record["bundle"])
    path = bundle_path_for(data_dir, environ)

    def check():
        fresh = read_bundle(data_dir, environ)
        opened = _open_factor(fresh, custody, passphrase, recovery_key)

        if opened != bytes(new_dek):
            raise StoreLocked("The new wraps did not open. The previous wraps were kept.")

    if path.is_file():
        current_bundle = read_bundle(data_dir, environ)

        if current_bundle.key_id == bundle.key_id:
            check()
        else:
            replace_bundle(path, bundle, check=check)
    else:
        replace_bundle(path, bundle, check=check)

    for item in record["stores"]:
        store = Path(item["path"])
        wipe_file(_aside(store))
        wipe_sidecars(_aside(store))
        wipe_file(_sibling(store))
        wipe_sidecars(_sibling(store))

    _wipe_journal(journal)


def _rollback_files(record) -> None:
    for item in record["stores"]:
        path = Path(item["path"])
        aside = _aside(path)
        sibling = _sibling(path)

        if item["state"] == "swapped" and aside.is_file():
            holding = path.with_name(path.name + ".rollback")
            wipe_file(holding)

            if path.exists():
                os.replace(path, holding)

            os.replace(aside, path)
            wipe_file(holding)
            wipe_sidecars(holding)
            wipe_sidecars(path)

        wipe_file(sibling)
        wipe_sidecars(sibling)
        wipe_file(aside)
        wipe_sidecars(aside)


def rollback_dek(data_dir, environ=None) -> None:
    """
    Put a half-finished data-key rotation back.

    Stores already swapped are restored from the previous file. The
    key bundle is left as it was unless the new bundle was published,
    in which case the backed-up wraps are restored when they are still there.
    """
    record, journal = _record_from_journal(data_dir, environ)

    if record is None:
        raise StoreError("There is no rotation to roll back.")

    if record.get("phase") == "publishing":
        path = bundle_path_for(data_dir, environ)
        backup = path.with_name(path.name + ".bak")

        if backup.is_file() and path.is_file():
            current_bundle = read_bundle(data_dir, environ)

            if current_bundle.key_id.hex() == record.get("new_key_id"):
                os.replace(backup, path)

    _rollback_files(record)
    _wipe_journal(journal)


def _release(previous, new_dek, key_id) -> None:
    _bind(new_dek, key_id)

    if previous is not None and previous is not current():
        previous.wipe()


def _continue(data_dir, record, journal, custody, passphrase, recovery_key, environ=None) -> None:
    old_bundle = read_bundle(data_dir, environ)
    new_bundle = _decode_bundle(record["bundle"])

    # Once the new bundle is published, the on-disk key id is the new one
    # and the old wraps are no longer the way in.
    if old_bundle.key_id.hex() == record.get("new_key_id"):
        new_dek = _open_factor(old_bundle, custody, passphrase, recovery_key)
        _publish(
            data_dir, record, journal, custody, passphrase, recovery_key, new_dek, environ,
        )
        _release(current(), new_dek, old_bundle.key_id)
        return

    old_dek = _open_factor(old_bundle, custody, passphrase, recovery_key)
    new_dek = _open_factor(new_bundle, custody, passphrase, recovery_key)

    if record.get("phase") != "swapping" and record.get("phase") != "publishing":
        _copy_stores(record, journal, old_dek, new_dek)

    if record.get("phase") != "publishing":
        _swap_stores(record, journal)

    held = current()
    _publish(
        data_dir, record, journal, custody, passphrase, recovery_key, new_dek, environ,
    )
    _release(held, new_dek, new_bundle.key_id)


def rotate_dek(
    data_dir,
    backup_dir=None,
    *,
    custody=None,
    passphrase=None,
    recovery_key=None,
    allow_same_drive=False,
    environ=None,
    now=None,
) -> None:
    """
    Re-encrypt every live store under a new data key.

    A verified backup is required when a rotation is not already in
    progress. A journal outside the data directory records each store
    so a later call resumes. The old in-memory data key is wiped after
    the new wraps are published.
    """
    if custody is None:
        custody = default_custody(environ)

    data_dir = Path(data_dir)
    existing, journal = _record_from_journal(data_dir, environ)

    if existing is not None:
        if existing.get("operation") == "add-passphrase":
            raise StoreLocked(
                "A passphrase wrap is already being added. Nothing was changed."
            )

        _continue(
            data_dir, existing, journal, custody, passphrase, recovery_key, environ,
        )
        return

    held = require_unlocked()
    bundle = read_bundle(data_dir, environ)
    _require_current(bundle, held.dek, custody, passphrase, recovery_key)
    paths = _stores(data_dir, environ)
    folder = _verified_backup(
        data_dir,
        backup_dir,
        custody=custody,
        passphrase=passphrase,
        allow_same_drive=allow_same_drive,
        environ=environ,
        now=now,
    )
    new_dek = bytearray(os.urandom(32))
    new_id = os.urandom(16)

    try:
        recovery_raw = parse_recovery_key(recovery_key) if recovery_key is not None else None
        payload = _new_bundle_bytes(
            bytes(new_dek), new_id, custody, passphrase, recovery_raw,
        )
        record = {
            "version": JOURNAL_VERSION,
            "phase": "copying",
            "old_key_id": bundle.key_id.hex(),
            "new_key_id": new_id.hex(),
            "bundle": base64.b64encode(payload).decode("ascii"),
            "backup": str(folder),
            "stores": [{"path": str(path), "state": "pending"} for path in paths],
        }
        _save_journal(journal, record)
        _continue(
            data_dir, record, journal, custody, passphrase, recovery_key, environ,
        )
    finally:
        for index in range(len(new_dek)):
            new_dek[index] = 0


def _show(data_dir, environ=None) -> int:
    if not sys.stdout.isatty():
        print(
            "The recovery key is shown only on a terminal, so it is not written to a file.",
            file=sys.stderr,
        )
        return 1

    from freya.storage.dbkey import acknowledge_recovery_key, reveal_recovery_key

    try:
        text = reveal_recovery_key(data_dir, environ=environ)
        acknowledge_recovery_key(data_dir, environ=environ)
    except StoreLocked as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(text)
    print("Keep that key off this machine. It will not be shown again.")
    return 0


def _ask_replacement():
    return prompt_passphrase(label="New database passphrase")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rotate or revoke Freya's database key wraps.")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("show-recovery-key")
    sub.add_parser("add-passphrase")
    sub.add_parser("rotate-passphrase")
    sub.add_parser("rotate-recovery")
    dek = sub.add_parser("rotate-dek")
    dek.add_argument("--backup-dir", type=Path)
    dek.add_argument("--rollback", action="store_true")
    dek.add_argument("--allow-same-drive", action="store_true", help=argparse.SUPPRESS)
    revoke_parser = sub.add_parser("revoke")
    revoke_parser.add_argument("--which", required=True, choices=("passphrase", "recovery"))
    args = parser.parse_args(argv)

    try:
        if args.command == "status":
            report = status(args.data)

            print("key id: " + report["key_id"])
            print("wraps: " + ", ".join(report["wraps"]))

            if report["stretch"]:
                stretch = report["stretch"]
                print(
                    "passphrase stretch: "
                    f"{stretch['memory_kib']} KiB, time {stretch['time']}, "
                    f"parallelism {stretch['parallelism']}"
                )

            print("recovery key shown: " + ("yes" if report["recovery_shown"] else "no"))
            print("rotation: " + (report["rotation"] or "none"))
            return 0

        if args.command == "show-recovery-key":
            return _show(args.data)

        if args.command == "add-passphrase":
            if not sys.stdin.isatty():
                print(
                    "A passphrase is asked for only on a terminal.",
                    file=sys.stderr,
                )
                return 1

            from freya.storage.dbkey import ensure_unlocked

            if not bundle_path_for(args.data).is_file():
                print("There is no database key yet. Nothing was changed.", file=sys.stderr)
                return 1

            # Adding the wrap is what makes a required passphrase possible.
            # The setting must not block the unlock that adds it.
            unlock_environ = os.environ.copy()
            unlock_environ.pop("FREYA_REQUIRE_PASSPHRASE", None)
            ensure_unlocked(args.data, interactive=False, environ=unlock_environ)

            try:
                phrase = prompt_passphrase(label="Database passphrase")
                accept_new_passphrase(phrase)
            except CustodyRefused as exc:
                print(str(exc), file=sys.stderr)
                return 1

            add_passphrase(args.data, phrase, environ=unlock_environ)
            print("A passphrase wrap was added. The databases were not rewritten.")
            return 0

        if args.command == "rotate-passphrase":
            current_phrase = prompt_passphrase(label="Current database passphrase")
            new_phrase = _ask_replacement()
            rotate_passphrase(args.data, current_phrase, new_phrase)
            print("The passphrase wrap was replaced. The databases were not rewritten.")
            return 0

        if args.command == "rotate-recovery":
            phrase = None
            paper = None
            report = status(args.data)

            if "passphrase" in report["wraps"] and sys.stdin.isatty():
                phrase = prompt_passphrase(label="Database passphrase")

            if "recovery" in report["wraps"] and sys.stdin.isatty():
                paper = prompt_passphrase(label="Current recovery key")

            text = rotate_recovery(
                args.data,
                passphrase=phrase,
                recovery_key=paper,
            )

            if sys.stdout.isatty():
                print("Write this recovery key down and keep it off this machine.")
                print("It will not be shown again.")
                print(text)
                from freya.storage.dbkey import acknowledge_recovery_key

                acknowledge_recovery_key(args.data)
            else:
                print(
                    "A new recovery key is waiting. "
                    "Run python -m freya.storage.keys show-recovery-key on a terminal.",
                    file=sys.stderr,
                )

            return 0

        if args.command == "rotate-dek":
            if args.rollback:
                rollback_dek(args.data)
                print("The data-key rotation was rolled back.")
                return 0

            phrase = None
            paper = None
            report = status(args.data)

            if "passphrase" in report["wraps"] and sys.stdin.isatty():
                phrase = prompt_passphrase(label="Database passphrase")

            if "recovery" in report["wraps"] and sys.stdin.isatty():
                paper = prompt_passphrase(label="Database recovery key")

            rotate_dek(
                args.data,
                args.backup_dir,
                passphrase=phrase,
                recovery_key=paper,
                allow_same_drive=args.allow_same_drive,
            )
            print("The databases were re-encrypted under a new data key.")
            return 0

        if args.command == "revoke":
            phrase = None
            paper = None
            report = status(args.data)
            remaining = [name for name in report["wraps"] if name != args.which]

            if "passphrase" in remaining and sys.stdin.isatty():
                phrase = prompt_passphrase(label="Database passphrase")

            if "recovery" in remaining and sys.stdin.isatty():
                paper = prompt_passphrase(label="Database recovery key")

            revoke(args.data, args.which, passphrase=phrase, recovery_key=paper)
            print("The wrap was removed.")
            return 0
    except (StoreLocked, StoreError, RotationStopped) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("The command was not recognised.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
