"""
The only opener for Freya's live SQLite stores.

Every file-backed store is a SQLCipher 4 database. ``connect`` applies
the unlocked data key and the cipher settings below. A missing unlock
refuses. A plaintext file refuses and names the migration command.
Nothing else under ``src/freya`` may import the standard-library
``sqlite3`` module or ``sqlcipher3``.

Binding
-------
``sqlcipher3`` 0.6.5 or newer (coleifer/sqlcipher3). That release
publishes Python 3.12 wheels for Windows (win32, amd64, arm64), Linux
(manylinux and musllinux, x86_64 and aarch64), and macOS (universal2,
x86_64, and arm64), and the import name is ``sqlcipher3``.

``sqlcipher3-binary`` 0.6.0 publishes a Linux x86_64 wheel only, so it
cannot be the one binding. ``sqlcipher3-wheels`` is the unofficial fork
that first built those wheels; the official package has since absorbed
the Windows, Linux, and macOS wheels, so Freya depends on the official
package rather than the fork.

Cipher settings
---------------
The data key is 32 bytes from ``storage/dbkey.py``. It is handed to
SQLCipher as a raw key, which is SQLCipher 4's example 2: a 64-character
hex blob. That form is the key material itself. SQLCipher does not run
PBKDF2 over it, so opening a store does not pay 256,000 iterations.
The key id recorded in the key bundle is not mixed into this key. A
later rotation can generate a new data key, run ``PRAGMA rekey`` (see
``rekey``), and record the new key id. ``rekey`` copies the store,
re-encrypts the copy, checks it, and only then replaces the original.

Pinned on every open, after the key and before the first read, so a
later SQLCipher default does not open these files differently:

- ``cipher_page_size = 4096``
- ``cipher_use_hmac = ON``
- ``cipher_hmac_algorithm = HMAC_SHA512``
- ``cipher_kdf_algorithm = PBKDF2_HMAC_SHA512`` (recorded; the raw key
  does not run it)
- ``cipher_plaintext_header_size = 0`` so the file does not start with
  the SQLite magic and the first page is not a plaintext header
- ``cipher_memory_security = ON`` so SQLCipher wipes the allocations it
  makes. This is a runtime setting, not part of the page format.
- ``temp_store = MEMORY`` so temporary tables, sorts, and indices stay
  in process memory. SQLCipher does not encrypt SQLite's temporary
  files, which is why they are not allowed on disk.
- ``secure_delete = ON`` on a writable connection, so a deleted page is
  overwritten inside the encrypted file.

SQLCipher 4 encrypts the main database, the rollback journal, and the
WAL frames. The ``-shm`` file is the WAL index (page numbers and
locks), not page contents. The first 16 bytes of a database file are
SQLCipher's random salt, not the data key and not a row. A test writes
a known marker and checks that none of those files contain it.

The full-text indexes (the conversation search index, and any other
FTS5 table) live inside the same database file. There is no separate
plaintext index file. The index's own ``secure-delete`` option, set by
the conversation store, still overwrites deleted words inside that
encrypted file.

Stores
------
Opened through this module, one data key for all of them:

- ``freya_memory.db`` — memories, history, conversations, and the FTS5
  conversation index
- ``freya_control.db`` — policy and the audit log
- ``freya_usage.db`` — the usage ledger
- ``freya_tasks.db`` — the local task journal
- ``media/freya_media.db`` — the media catalog
- the deletion ledger outside ``data/``
- the pre-protection copy and the conversation-schema copy, when those
  copies are made from an encrypted store

A knowledge store is not built (ADR-006). There is no separate database
for it to encrypt.

Backup copies are still the authenticated files from ``storage/crypto.py``.
Making one exports a plaintext SQLite image in a private temporary file,
seals that image, then overwrites and removes the temporary file. The
published backup does not contain a SQLCipher page. Restoring imports
that image back into a SQLCipher file under the current data key, so a
later rotation of the live key does not require the old page key to
read an old backup. The backup's own data key is unchanged.
"""

import os
import sqlite3 as _stdlib
import tempfile
from pathlib import Path
from typing import Optional

import sqlcipher3 as _driver

from freya.storage.dbkey import StoreLocked, require_unlocked


# Re-exported so a store can say ``except sqlite3.Error`` and mean the
# driver that actually opened the file. These are sqlcipher3's classes.
# They are not subclasses of the standard-library ones.
Connection = _driver.Connection
Cursor = _driver.Cursor
Error = _driver.Error
DatabaseError = _driver.DatabaseError
DataError = _driver.DataError
IntegrityError = _driver.IntegrityError
InterfaceError = _driver.InterfaceError
InternalError = _driver.InternalError
NotSupportedError = _driver.NotSupportedError
OperationalError = _driver.OperationalError
ProgrammingError = _driver.ProgrammingError
Warning = _driver.Warning
Row = _driver.Row
complete_statement = _driver.complete_statement

SQLITE_HEADER = b"SQLite format 3\x00"

# Relative to the data directory. The deletion ledger is not in this
# tuple: it lives outside that directory.
LIVE_DATABASES = (
    "freya_memory.db",
    "freya_control.db",
    "freya_usage.db",
    "freya_tasks.db",
    "media/freya_media.db",
)

# Page format. Set on every open, after the key, before the first read.
CIPHER_PRAGMAS = (
    "PRAGMA cipher_page_size = 4096",
    "PRAGMA cipher_use_hmac = ON",
    "PRAGMA cipher_hmac_algorithm = HMAC_SHA512",
    "PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512",
    "PRAGMA cipher_plaintext_header_size = 0",
    "PRAGMA cipher_memory_security = ON",
    "PRAGMA temp_store = MEMORY",
)

# sqlcipher3 4.20 reports this. Recorded so a test can show which build
# opened the file. The page format above is what a different build must
# match, not this string.
BINDING = "sqlcipher3"


class StoreError(RuntimeError):
    """A store could not be copied or migrated. The message says why."""


def is_db_error(error) -> bool:
    """True for an error from SQLCipher or from the standard-library sqlite3."""
    return isinstance(error, (_driver.Error, _stdlib.Error))


def is_plaintext_sqlite(path) -> bool:
    """True when the file starts with the ordinary SQLite header."""
    file = Path(path)

    try:
        with file.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def wipe_file(path) -> None:
    """
    Overwrite a file with random bytes, then remove it.

    A symlink is removed without being followed. A missing file is
    left missing. This is the handling for a plaintext database that
    has already been copied into an encrypted one, and for the
    temporary plaintext image a backup seals.
    """
    file = Path(path)

    try:
        if file.is_symlink():
            file.unlink(missing_ok=True)
            return

        if not file.is_file():
            return

        size = file.stat().st_size

        with file.open("r+b") as handle:
            remaining = size

            while remaining:
                chunk = os.urandom(min(remaining, 1024 * 1024))
                handle.write(chunk)
                remaining -= len(chunk)

            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass

    file.unlink(missing_ok=True)


def wipe_plaintext_tree(root) -> None:
    """Overwrite ordinary SQLite files under ``root`` before the tree is removed."""
    folder = Path(root)

    if not folder.exists():
        return

    for path in folder.rglob("*"):
        if path.is_file() and is_plaintext_sqlite(path):
            wipe_file(path)
            wipe_sidecars(path)


def wipe_sidecars(path) -> None:
    """Remove a database's journal, WAL, and WAL-index files, overwritten first."""
    for suffix in ("-journal", "-wal", "-shm"):
        wipe_file(Path(str(path) + suffix))


def _quote_path(path: Path) -> str:
    text = Path(path).resolve().as_posix()

    if "'" in text or "\x00" in text:
        raise StoreError("The database path cannot be used.")

    return text


def _apply_key(connection, dek: bytes) -> None:
    if not isinstance(dek, (bytes, bytearray)) or len(dek) != 32:
        raise StoreLocked("The database key is not usable.")

    # Silence SQLCipher's own stderr line on a wrong key. The message
    # Freya raises is the one the owner sees.
    connection.execute("PRAGMA cipher_log_level = NONE")
    # 64 hex characters: SQLCipher 4's raw key, no PBKDF2.
    connection.execute('PRAGMA key = "x\'' + bytes(dek).hex() + '\'"')

    for pragma in CIPHER_PRAGMAS:
        connection.execute(pragma)


def _verify_key(connection, name: str) -> None:
    try:
        connection.execute("SELECT count(*) FROM sqlite_master")
    except _driver.DatabaseError as exc:
        text = str(exc).lower()

        if "not a database" in text or "hmac" in text:
            raise StoreLocked(
                f"The database key was not accepted for {name}."
            ) from None

        raise StoreLocked(f"The database could not be opened ({name}).") from None


def connect(
    path,
    timeout=5.0,
    isolation_level="",
    check_same_thread=True,
    *,
    read_only=False,
    create=True,
):
    """
    Open one store and apply the unlocked data key.

    A missing unlock, a plaintext file, or a wrong key raises
    ``StoreLocked`` and does not create a plaintext file. ``:memory:``
    is keyed too, so the only opener has one path.
    """
    if str(path) == ":memory:":
        unlocked = require_unlocked()
        connection = _driver.connect(
            ":memory:",
            timeout=timeout,
            isolation_level=isolation_level,
            check_same_thread=check_same_thread,
        )
        _apply_key(connection, unlocked.dek)
        _verify_key(connection, ":memory:")
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    file = Path(path)
    name = file.name

    if not create and file.is_file() and file.stat().st_size == 0:
        raise StoreLocked(f"{name} could not be opened.")

    if file.exists() and is_plaintext_sqlite(file):
        raise StoreLocked(
            f"{name} is not encrypted. Run python -m freya.storage.encrypt. "
            "Nothing was opened."
        )

    unlocked = require_unlocked()

    if read_only or not create:
        mode = "ro" if read_only else "rw"
        target = f"file:{file.resolve().as_posix()}?mode={mode}"
        uri = True
    else:
        if not file.exists():
            file.parent.mkdir(parents=True, exist_ok=True)

        target = file
        uri = False

    connection = _driver.connect(
        target,
        timeout=timeout,
        isolation_level=isolation_level,
        check_same_thread=check_same_thread,
        uri=uri,
    )
    try:
        _apply_key(connection, unlocked.dek)
        _verify_key(connection, name)
    except BaseException:
        connection.close()
        raise

    if not read_only:
        connection.execute("PRAGMA secure_delete = ON")

    return connection


def _plaintext_uri(path, *, read_only: bool) -> str:
    file = Path(path)

    if not file.is_file() or not is_plaintext_sqlite(file):
        raise StoreError("There is no plaintext database to read.")

    mode = "ro" if read_only else "rw"
    return f"file:{file.resolve().as_posix()}?mode={mode}"


def open_legacy(path, *, read_only=True, isolation_level=""):
    """
    Open a plaintext SQLite file that already exists.

    This is the unsealed backup image and a database that has not been
    migrated yet, not a live store. It refuses to create a file, and it
    refuses a file that is not ordinary SQLite. The standard-library
    connection is what a backup check opens the image with.
    """
    return _stdlib.connect(
        _plaintext_uri(path, read_only=read_only),
        uri=True,
        isolation_level=isolation_level,
        check_same_thread=False,
    )


def _open_plaintext_for_export(path, *, read_only=True):
    """
    A SQLCipher connection on a plaintext file, with no key applied.

    ``sqlcipher_export`` exists only on that connection. The file must
    already be ordinary SQLite. No key is set, so this does not encrypt
    it and does not create a store.
    """
    connection = _driver.connect(
        _plaintext_uri(path, read_only=read_only),
        uri=True,
        check_same_thread=False,
    )
    connection.execute("PRAGMA cipher_log_level = NONE")

    try:
        connection.execute("SELECT count(*) FROM sqlite_master")
    except _driver.DatabaseError as exc:
        connection.close()
        raise StoreError("There is no plaintext database to read.") from exc

    return connection


def counts(connection) -> dict:
    """Row counts for every table, the full-text shadow tables included."""
    found = {}

    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()

    for (name,) in rows:
        quoted = '"' + str(name).replace('"', '""') + '"'
        found[str(name)] = connection.execute(
            f"SELECT COUNT(*) FROM {quoted}"
        ).fetchone()[0]

    return found


def legacy_problem(path) -> Optional[str]:
    """
    A problem string when a plaintext database fails its own check,
    or None when it is sound. Used on an unsealed backup image.
    """
    try:
        connection = open_legacy(path, read_only=True)
    except (StoreError, _stdlib.Error):
        return "cannot be opened."

    try:
        verdict = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except _stdlib.Error:
        return "cannot be opened."
    finally:
        connection.close()

    if verdict != "ok":
        return "fails its own check."

    return None


def _snapshot_bytes(source) -> bytes:
    """
    A consistent plaintext SQLite image of ``source``.

    The image is a normal database with the delete journal, checked
    before it is returned. The temporary file is overwritten and
    removed before this returns. ``source`` has to be a standard-library
    connection: SQLCipher's backup cannot cross into that library, so
    an encrypted store is exported first.
    """
    descriptor, name = tempfile.mkstemp(prefix="freya-snap-", suffix=".db")
    os.close(descriptor)
    target = Path(name)
    copy = None

    try:
        copy = _stdlib.connect(target)
        source.backup(copy)
        copy.execute("PRAGMA journal_mode = DELETE")
        verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
        copy.close()
        copy = None

        if verdict != "ok":
            raise StoreError("The copy did not pass its check.")

        return target.read_bytes()
    except _stdlib.Error as exc:
        raise StoreError("The copy did not pass its check.") from exc
    finally:
        if copy is not None:
            copy.close()

        wipe_file(target)
        wipe_sidecars(target)


def plaintext_image(path) -> bytes:
    """
    The bytes a backup seals for one database.

    An encrypted store is exported to a plaintext image first. A store
    that has not been migrated yet is read as ordinary SQLite. Either
    way the temporary plaintext file is overwritten before this returns,
    and the source file is not written.
    """
    file = Path(path)

    if not file.is_file():
        raise StoreError("There is no database to copy.")

    if is_plaintext_sqlite(file):
        source = open_legacy(file, read_only=True)

        try:
            return _snapshot_bytes(source)
        finally:
            source.close()

    source = connect(file, read_only=True)
    descriptor, name = tempfile.mkstemp(prefix="freya-export-", suffix=".db")
    os.close(descriptor)
    exported = Path(name)

    try:
        quoted = _quote_path(exported)
        source.execute(f"ATTACH DATABASE '{quoted}' AS plain KEY ''")

        try:
            source.execute("SELECT sqlcipher_export('plain')")
        finally:
            source.execute("DETACH DATABASE plain")

        image = open_legacy(exported, read_only=True)

        try:
            return _snapshot_bytes(image)
        finally:
            image.close()
    except StoreLocked:
        raise
    except (_driver.Error, _stdlib.Error, StoreError) as exc:
        raise StoreError("The encrypted database could not be copied.") from exc
    finally:
        source.close()
        wipe_file(exported)
        wipe_sidecars(exported)


def install_encrypted(plain_path, dest) -> None:
    """
    Replace ``dest`` with a SQLCipher database holding ``plain_path``.

    ``plain_path`` is an ordinary SQLite file (an unsealed backup image).
    It is not modified. ``dest`` is written to a sibling and checked
    before it takes the name. A failure leaves ``dest`` as it was.
    """
    plain = Path(plain_path)
    target = Path(dest)
    unlocked = require_unlocked()
    target.parent.mkdir(parents=True, exist_ok=True)
    sibling = target.with_name(target.name + ".encrypting")
    wipe_file(sibling)
    wipe_sidecars(sibling)
    source = _open_plaintext_for_export(plain, read_only=True)

    try:
        quoted = _quote_path(sibling)
        source.execute(f"ATTACH DATABASE '{quoted}' AS enc KEY \"x'{unlocked.dek.hex()}'\"")

        try:
            source.execute("PRAGMA enc.cipher_page_size = 4096")
            source.execute("PRAGMA enc.cipher_use_hmac = ON")
            source.execute("PRAGMA enc.cipher_hmac_algorithm = HMAC_SHA512")
            source.execute("PRAGMA enc.cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
            source.execute("PRAGMA enc.cipher_plaintext_header_size = 0")
            source.execute("SELECT sqlcipher_export('enc')")
        finally:
            source.execute("DETACH DATABASE enc")
    except (_driver.Error, _stdlib.Error) as exc:
        wipe_file(sibling)
        wipe_sidecars(sibling)
        raise StoreError("The database could not be encrypted.") from exc
    finally:
        source.close()

    checking = None

    try:
        checking = connect(sibling)
        verdict = checking.execute("PRAGMA integrity_check").fetchone()[0]

        if verdict != "ok":
            raise StoreError("The encrypted copy did not pass its check.")

        # The key was accepted. Compare is the caller's when it has the
        # plaintext counts. Here, a database that does not open is enough
        # to refuse the swap; migrate_file compares counts itself.
    except StoreLocked:
        wipe_file(sibling)
        wipe_sidecars(sibling)
        raise
    finally:
        if checking is not None:
            checking.close()

    if is_plaintext_sqlite(sibling) or SQLITE_HEADER in sibling.read_bytes()[:16]:
        wipe_file(sibling)
        raise StoreError("The encrypted copy is still a plaintext database.")

    temporary = target.with_name(target.name + ".replacing")
    aside = target.with_name(target.name + ".plain-aside")
    wipe_file(temporary)
    replacing_plaintext = target.is_file() and is_plaintext_sqlite(target)

    try:
        # Drop a plaintext journal before the encrypted file takes the name,
        # or SQLite would try to replay it onto the new file.
        os.replace(sibling, temporary)

        if replacing_plaintext:
            os.replace(target, aside)

        wipe_sidecars(target)
        os.replace(temporary, target)
    except OSError as exc:
        if not target.exists() and aside.exists():
            os.replace(aside, target)

        wipe_file(temporary)
        raise StoreError("The encrypted database could not be put in place.") from exc

    wipe_file(aside)
    wipe_sidecars(aside)
    wipe_sidecars(target)


def copy_connection(source, target) -> None:
    """
    Copy ``source`` to a new file at ``target``.

    An encrypted connection makes an encrypted copy under the same data
    key. A plaintext connection (a test that opened its own file, or a
    legacy file this process already holds) makes a plaintext copy of
    that same connection. A live store is never opened as plaintext by
    this function: the plaintext branch runs only when the caller
    already holds a standard-library connection.
    """
    file = Path(target)
    file.parent.mkdir(parents=True, exist_ok=True)
    encrypted = isinstance(source, _driver.Connection)
    dest = connect(file) if encrypted else _stdlib.connect(file)

    try:
        source.backup(dest)

        if encrypted:
            dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        else:
            dest.execute("PRAGMA journal_mode = DELETE")
    except (_driver.Error, _stdlib.Error) as exc:
        raise StoreError("The database could not be copied.") from exc
    finally:
        dest.close()

    if encrypted:
        wipe_sidecars(file)


def open_matching(source, path, **kwargs):
    """Open ``path`` the way ``source`` was opened."""
    if isinstance(source, _driver.Connection):
        return connect(path, **kwargs)

    return _stdlib.connect(path, **kwargs)


def open_with(
    path,
    dek,
    timeout=5.0,
    isolation_level="",
    check_same_thread=True,
    *,
    read_only=False,
):
    """
    Open one existing store with this data key.

    The process unlock is not consulted. A plaintext file is refused.
    Rotation uses this for the key being installed and for the key
    being replaced. It does not create a store.
    """
    file = Path(path)
    name = file.name

    if not file.is_file() or file.stat().st_size == 0:
        raise StoreLocked(f"{name} could not be opened.")

    if is_plaintext_sqlite(file):
        raise StoreLocked(
            f"{name} is not encrypted. Run python -m freya.storage.encrypt. "
            "Nothing was opened."
        )

    connection = _driver.connect(
        file,
        timeout=timeout,
        isolation_level=isolation_level,
        check_same_thread=check_same_thread,
    )

    try:
        _apply_key(connection, dek)
        _verify_key(connection, name)
    except BaseException:
        connection.close()
        raise

    if not read_only:
        connection.execute("PRAGMA secure_delete = ON")

    return connection


def _raw_pragma(statement: str, dek) -> str:
    if statement not in ("key", "rekey"):
        raise StoreError("The database key is not usable.")

    if not isinstance(dek, (bytes, bytearray)) or len(dek) != 32:
        raise StoreError("The database key is not usable.")

    return 'PRAGMA ' + statement + ' = "x\'' + bytes(dek).hex() + '\'"'


def rekey_copy(source, destination, old_dek, new_dek) -> None:
    """
    Write a copy of ``source`` re-encrypted with ``new_dek``.

    ``source`` is checkpointed and left on ``old_dek``. The copy is
    opened with the old raw key, then ``PRAGMA rekey`` installs the new
    raw key. A fresh open with the new key must pass ``integrity_check``.
    """
    original = Path(source)
    copy = Path(destination)
    held = open_with(original, old_dek)

    try:
        held.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        held.close()

    copy.parent.mkdir(parents=True, exist_ok=True)
    wipe_file(copy)
    wipe_sidecars(copy)

    try:
        data = original.read_bytes()
        copy.write_bytes(data)
        moved = open_with(copy, old_dek)

        try:
            try:
                moved.execute(_raw_pragma("rekey", new_dek))
                verdict = moved.execute("PRAGMA integrity_check").fetchone()[0]
            except _driver.DatabaseError:
                raise StoreError(
                    f"The re-encrypted copy of {original.name} did not open."
                ) from None
        finally:
            moved.close()

        if verdict != "ok":
            raise StoreError(f"The re-encrypted copy of {original.name} did not pass its check.")

        checking = open_with(copy, new_dek)

        try:
            if checking.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StoreError(
                    f"The re-encrypted copy of {original.name} did not open."
                )
        finally:
            checking.close()
    except BaseException:
        wipe_file(copy)
        wipe_sidecars(copy)
        raise


def rekey(path, new_dek: bytes) -> None:
    """
    Re-encrypt one store with ``PRAGMA rekey``.

    The file is copied, re-encrypted, checked, and only then replaces
    the original. The process unlock is not changed: the caller binds
    the new data key before opening the store again. A failed check
    leaves the original bytes in place.
    """
    old = require_unlocked()
    file = Path(path)
    sibling = file.with_name(file.name + ".rekey")
    aside = file.with_name(file.name + ".before-rekey")
    before = file.read_bytes()

    try:
        rekey_copy(file, sibling, old.dek, new_dek)
        wipe_file(aside)
        os.replace(file, aside)

        try:
            os.replace(sibling, file)
        except OSError:
            if not file.exists() and aside.is_file():
                os.replace(aside, file)

            raise
    except BaseException as exc:
        wipe_file(sibling)
        wipe_sidecars(sibling)

        if file.is_file() and file.read_bytes() != before and aside.is_file():
            os.replace(aside, file)

        if isinstance(exc, (StoreError, StoreLocked)):
            raise

        raise StoreError(
            f"{file.name} was not re-encrypted. The original was left in place."
        ) from None

    wipe_file(aside)
    wipe_sidecars(aside)
    wipe_sidecars(file)
