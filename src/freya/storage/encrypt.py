"""
Migrate plaintext databases to SQLCipher, once, on purpose.

New stores are encrypted when they are created. This command is for a
database that already exists as ordinary SQLite. It takes a verified
backup first, checks integrity and row counts, writes an encrypted
sibling, and only then replaces the plaintext file. A failed check
leaves the original bytes where they were. Running it again is safe:
an encrypted store is opened and left alone.

The old plaintext file is overwritten with random bytes and removed,
and so are its ``-journal``, ``-wal``, and ``-shm`` files. The
conversation search index lives inside ``freya_memory.db``, so it is
covered by that file.

    python -m freya.storage.encrypt --backup-dir D:\\FreyaBackups
    python -m freya.storage.encrypt --show-recovery-key

The passphrase is typed at the prompt. It is not an argument and it is
not read from the environment. Key rotation is not this command.
"""

import argparse
import os
import sys
from pathlib import Path

from freya.memory.deletion_ledger import ProtectionSetupError, ledger_path_for
from freya.storage.backup import BackupRefused, back_up, check
from freya.storage.dbkey import (
    StoreLocked,
    acknowledge_recovery_key,
    ensure_unlocked,
    reveal_recovery_key,
)
from freya.storage.placement import DATA_DIR
from freya.storage.sqlite import (
    LIVE_DATABASES,
    StoreError,
    connect,
    counts,
    is_plaintext_sqlite,
    open_legacy,
    wipe_file,
    wipe_sidecars,
)
from freya.storage.sqlite import install_encrypted


def store_paths(data, environ=None):
    """Every live database Freya keeps for this data directory, if the file exists."""
    data = Path(data)
    found = []

    for name in LIVE_DATABASES:
        path = data / name

        if path.is_file():
            found.append(path)

    conversations = data / "backups" / "freya_memory-before-conversations.db"

    if conversations.is_file():
        found.append(conversations)

    try:
        ledger = ledger_path_for(data, environ)
    except (OSError, ProtectionSetupError, StoreLocked):
        ledger = None

    if ledger is not None and ledger.is_file():
        found.append(ledger)

        for copy in ledger.parent.glob("before-protection*.db"):
            if copy.is_file():
                found.append(copy)

    return found


def _checkpoint_plaintext(path: Path) -> None:
    """Fold a plaintext journal into the main file so the sidecar can be removed."""
    connection = open_legacy(path, read_only=False)

    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()


def migrate_file(path) -> str:
    """
    Encrypt one plaintext database, or accept one that is already encrypted.

    Returns ``already`` or ``migrated``. A failure leaves the original
    file in place and removes the unfinished encrypted sibling.
    """
    file = Path(path)
    aside = file.with_name(file.name + ".plain-aside")
    sibling = file.with_name(file.name + ".encrypting")

    if aside.is_file() and not file.exists():
        os.replace(aside, file)

    if not file.is_file():
        raise StoreError(f"{file.name} is not a database. Nothing was changed.")

    if not is_plaintext_sqlite(file):
        checking = connect(file)

        try:
            if checking.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreError(
                    f"{file.name} did not pass its check. Nothing was changed."
                )
        finally:
            checking.close()

        wipe_file(aside)
        wipe_file(sibling)
        wipe_sidecars(sibling)
        return "already"

    before_connection = open_legacy(file, read_only=True)

    try:
        if before_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise StoreError(
                f"{file.name} did not pass its check. Nothing was changed."
            )

        before = counts(before_connection)
    finally:
        before_connection.close()

    wipe_file(sibling)
    wipe_sidecars(sibling)

    try:
        install_encrypted(file, sibling)
        after_connection = connect(sibling)

        try:
            if after_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreError(
                    f"The encrypted copy of {file.name} did not pass its check. "
                    "The original was left unchanged."
                )

            after = counts(after_connection)
        finally:
            after_connection.close()

        if before != after:
            raise StoreError(
                f"The encrypted copy of {file.name} does not match the original. "
                "The original was left unchanged."
            )
    except BaseException:
        wipe_file(sibling)
        wipe_sidecars(sibling)
        raise

    # The encrypted sibling matches. Fold the plaintext journal, then
    # swap. A crash after the checkpoint and before the swap leaves a
    # plaintext file, and running this again migrates it.
    try:
        _checkpoint_plaintext(file)
    except Exception:
        wipe_file(sibling)
        wipe_sidecars(sibling)
        raise

    wipe_sidecars(file)

    try:
        os.replace(file, aside)
        os.replace(sibling, file)
    except OSError as exc:
        if not file.exists() and aside.exists():
            os.replace(aside, file)

        wipe_file(sibling)
        raise StoreError(
            f"{file.name} could not be replaced. The original was left in place."
        ) from exc

    wipe_file(aside)
    wipe_sidecars(aside)
    return "migrated"


def migrate_stores(
    data,
    backup_dir,
    *,
    environ=None,
    custody=None,
    passphrase=None,
    recovery_key=None,
    allow_same_drive=False,
    now=None,
) -> list:
    """
    Back up ``data``, then encrypt every plaintext store.

    The backup is checked before anything is replaced. When every store
    is already encrypted, no new backup is required and each store is
    only opened. Returns the words ``migrated`` or ``already`` for each
    file that was there.
    """
    data = Path(data)
    paths = store_paths(data, environ)
    plaintext = [path for path in paths if is_plaintext_sqlite(path)]

    if plaintext:
        if backup_dir is None:
            raise StoreError(
                "A verified backup is required before encryption. "
                "Nothing was changed."
            )

        try:
            folder = back_up(
                data,
                backup_dir,
                now,
                allow_same_drive=allow_same_drive,
                environ=environ,
                custody=custody,
                passphrase=passphrase,
                recovery_key=recovery_key,
            )
        except BackupRefused as exc:
            raise StoreError(str(exc)) from exc

        problems = check(
            folder,
            custody=custody,
            passphrase=passphrase,
            recovery_key=recovery_key,
        )

        if problems:
            raise StoreError(
                "The backup was not sound, so nothing was encrypted: "
                + " ".join(problems)
            )

    results = []

    for path in paths:
        results.append((path, migrate_file(path)))

    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Encrypt Freya's existing plaintext databases. "
            "A verified backup is taken first. Running this again is safe."
        ),
    )
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Folder on another drive for the backup taken before encryption.",
    )
    parser.add_argument(
        "--show-recovery-key",
        action="store_true",
        help="Print the database recovery key once, if it has not been shown.",
    )
    args = parser.parse_args(argv)
    data = args.data

    if args.show_recovery_key:
        if not sys.stdout.isatty():
            print(
                "The recovery key is shown only on a terminal, so it is not "
                "written to a file.",
                file=sys.stderr,
            )
            return 1

        try:
            text = reveal_recovery_key(data)
            acknowledge_recovery_key(data)
        except StoreLocked as exc:
            print(str(exc), file=sys.stderr)
            return 1

        print(text)
        print("Keep that key off this machine. It will not be shown again.")
        return 0

    try:
        created = ensure_unlocked(data, interactive=sys.stdin.isatty())
    except StoreLocked as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if created and sys.stdout.isatty():
        print(
            "Write this database recovery key down and keep it off this machine. "
            "It will not be shown again."
        )
        print(created)
        acknowledge_recovery_key(data)
    elif created:
        print(
            "A database recovery key was created and has not been shown. "
            "Run python -m freya.storage.encrypt --show-recovery-key on a terminal.",
            file=sys.stderr,
        )

    if args.backup_dir is None and any(
        is_plaintext_sqlite(path) for path in store_paths(data)
    ):
        print(
            "Name a backup folder on another drive with --backup-dir. "
            "Nothing was changed.",
            file=sys.stderr,
        )
        return 1

    try:
        results = migrate_stores(data, args.backup_dir)
    except (StoreError, StoreLocked) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not results:
        print("There was no database to encrypt.")
        return 0

    for path, state in results:
        print(f"{path.name}: {state}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
