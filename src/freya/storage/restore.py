"""
Put a dated backup back.

Databases and settings are copied out of the backup folder. The
deletion ledger is not rolled backwards: a backup taken before a
forgetting holds the forgotten row and not the tombstone. When the
live ledger already records that tombstone it is kept, and the
tombstones are applied to the restored memory database before this
returns. A missing live ledger is taken from the backup. If that is
the only copy, a forgetting made after the backup comes back.
"""

import json
import os
import shutil
import sqlite3
from pathlib import Path

from freya.memory.deletion_ledger import (
    DeletionLedger,
    StorageQuarantined,
    is_protected,
    ledger_path_for,
)
from freya.storage.backup import LEDGER_COPY_NAME, BackupRefused, check, copy_path


def _read_ledger(path: Path):
    """The store identity and the tombstones, oldest first."""
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )

    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupRefused("The deletion ledger fails its own check.")

        found = connection.execute(
            "SELECT value FROM ledger_meta WHERE key = 'store_id'"
        ).fetchone()
        rows = connection.execute(
            "SELECT artifact_id, kind, deleted_at FROM tombstone ORDER BY seq"
        ).fetchall()
    except sqlite3.Error as error:
        raise BackupRefused("The deletion ledger could not be read.") from error
    finally:
        connection.close()

    if found is None:
        raise BackupRefused("The deletion ledger has not been set up.")

    return found[0], rows


def _compatible(backup_file: Path, live: Path) -> None:
    """
    Refuse before anything is written when the two ledgers are not
    the same chain. A shorter one may be a prefix of a longer one.
    """
    if not live.is_file():
        return

    backup_id, backup_rows = _read_ledger(backup_file)
    live_id, live_rows = _read_ledger(live)

    if live_id != backup_id:
        raise BackupRefused(
            "The backup's deletion ledger belongs to a different memory database."
        )

    live_ids = [row[0] for row in live_rows]
    backup_ids = [row[0] for row in backup_rows]

    if (
        live_ids[: len(backup_ids)] == backup_ids
        or backup_ids[: len(live_ids)] == live_ids
    ):
        return

    raise BackupRefused(
        "The deletion ledger in the backup does not continue the one on this machine."
    )


def _replace(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_name(dest.name + ".restore")
    shutil.copy2(source, temporary)

    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(dest) + suffix).unlink(missing_ok=True)

    os.replace(temporary, dest)


def _append_tombstones(live: Path, rows) -> None:
    connection = sqlite3.connect(live, isolation_level=None)

    try:
        connection.execute("BEGIN IMMEDIATE")

        for artifact_id, kind, deleted_at in rows:
            connection.execute(
                "INSERT INTO tombstone (artifact_id, kind, deleted_at) "
                "SELECT ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM tombstone WHERE artifact_id = ?)",
                (artifact_id, kind, deleted_at, artifact_id),
            )

        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

        raise
    finally:
        connection.close()


def _apply_ledger(backup_file: Path, live: Path) -> None:
    if not live.is_file():
        live.parent.mkdir(parents=True, exist_ok=True)
        _replace(backup_file, live)
        return

    _, backup_rows = _read_ledger(backup_file)
    _, live_rows = _read_ledger(live)
    live_ids = [row[0] for row in live_rows]
    backup_ids = [row[0] for row in backup_rows]

    if live_ids[: len(backup_ids)] == backup_ids:
        # The live ledger already holds every tombstone in the backup,
        # and any forgotten since.
        return

    _append_tombstones(live, backup_rows[len(live_ids) :])


def _replay(database: Path, ledger: Path) -> None:
    """Apply the ledger to a protected memory database, if both exist."""
    if not database.is_file() or not ledger.is_file():
        return

    connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
    )
    opened = None

    try:
        if not is_protected(connection):
            return

        connection.execute("PRAGMA secure_delete = ON")
        opened = DeletionLedger.open(ledger, connection)
    except StorageQuarantined as error:
        raise BackupRefused(str(error)) from error
    finally:
        if opened is not None:
            opened.close()

        try:
            connection.close()
        except sqlite3.Error:
            pass


def restore(folder, data_dir, environ=None) -> Path:
    """
    Copy ``folder`` back into ``data_dir`` and return the ledger path.

    The backup is checked first. A ledger that does not continue the
    live one refuses the restore with nothing written.
    """
    folder = Path(folder)
    data_dir = Path(data_dir)
    problems = check(folder)

    if problems:
        raise BackupRefused("The backup is not sound: " + " ".join(problems))

    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["copied"]
    ledger = ledger_path_for(data_dir, environ)
    names = [entry["name"] for entry in entries]

    if LEDGER_COPY_NAME in names:
        ledger_copy = copy_path(folder, LEDGER_COPY_NAME)

        if ledger_copy is None:
            raise BackupRefused("The backup names a file it should not.")

        _compatible(ledger_copy, ledger)

    for entry in entries:
        name = entry["name"]
        source = copy_path(folder, name)

        if source is None:
            raise BackupRefused("The backup names a file it should not.")

        if name == LEDGER_COPY_NAME:
            _apply_ledger(source, ledger)
            continue

        _replace(source, data_dir / name)

    if LEDGER_COPY_NAME in names or ledger.is_file():
        _replay(data_dir / "freya_memory.db", ledger)

    return ledger
