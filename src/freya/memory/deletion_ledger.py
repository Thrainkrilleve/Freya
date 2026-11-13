"""
Deletions that survive a restore.

Deleting a row removes it from the memory database, but a copy of that
database made earlier still holds it, and putting the copy back would
bring it back. In protected mode every row that is deleted because the
owner asked is first written down in a second, separate database, the
deletion ledger, as a tombstone: a random identifier, the kind of record,
and the time. Nothing of what was deleted is kept there, not even a hash
of it. Each time the memory database is opened in protected mode the
tombstones are applied to it again before anything is read from it, so a
restored copy loses the same rows.

This only helps while the ledger is kept somewhere a restore of the
memory database does not also put back. Nothing here can tell what the
owner's backups cover: rolling both files back together cannot be
detected. It is not physical erasure, and it reaches only rows in the
memory database this code opens, not other copies of it.

Protected mode is chosen by whoever opens the store, by naming the
ledger. It is never read off the database, because a restored copy from
before `initialize_protection` would then switch it off.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path


# The kind of a deleted record, and the table it was in.
TABLES = {
    "memory": "memories",
    "history": "memory_history",
    "conversation": "conversation",
    "turn": "conversation_turn",
}

# What a database must already have before it can be protected.
REQUIRED_COLUMNS = {
    "memories": {"id", "content"},
    "memory_history": {"history_id", "memory_id", "content"},
    "conversation": {"id", "summary", "summarized_turns"},
    "conversation_turn": {"id", "conversation_id", "content"},
}
SEARCH_TABLE = "conversation_search"

# An identifier is 32 lower-case hexadecimal digits.
MALFORMED = (
    "({0} IS NULL OR typeof({0}) != 'text' OR length({0}) != 32 "
    "OR {0} GLOB '*[^0-9a-f]*')"
)
NEW_ID = "lower(hex(randomblob(16)))"

LEDGER_SCHEMA = (
    "CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE tombstone ("
    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "artifact_id TEXT NOT NULL UNIQUE, "
    "kind TEXT NOT NULL CHECK (kind IN ('memory', 'history', 'conversation', 'turn')), "
    "deleted_at TEXT NOT NULL)",
    "CREATE TRIGGER tombstone_kept BEFORE DELETE ON tombstone "
    "BEGIN SELECT RAISE(ABORT, 'tombstones are never removed'); END",
    "CREATE TRIGGER tombstone_fixed BEFORE UPDATE ON tombstone "
    "BEGIN SELECT RAISE(ABORT, 'tombstones are never changed'); END",
)


class StorageQuarantined(RuntimeError):
    """
    The memory database cannot be served: it or its deletion ledger
    failed a check, or a recorded deletion could not be carried out.
    Its connection is closed, so nothing can be read from it until it
    is opened, checked, and reconciled again.
    """


class ProtectionSetupError(RuntimeError):
    """
    `initialize_protection` could not finish. The memory database is as
    it was.
    """


# -- small helpers -------------------------------------------------------------


def _has(connection, kind, name) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?", (kind, name)
        ).fetchone()
        is not None
    )


def _columns(connection, table) -> set:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _marker(connection):
    """
    The store's identity and how far it has applied the ledger, or
    None when the database was never protected.
    """
    if not _has(connection, "table", "storage_meta"):
        return None

    return dict(connection.execute("SELECT key, value FROM storage_meta").fetchall())


def is_protected(connection) -> bool:
    return _marker(connection) is not None


def _well_formed(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def _same(first: Path, second: Path) -> bool:
    if os.path.normcase(first) == os.path.normcase(second):
        return True

    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _beside(path: Path, database: Path) -> bool:
    """
    Whether `path` is in the memory database's folder or below it,
    which is what a backup of that folder would take with it.
    """
    return path.is_relative_to(database.parent)


def _database_path(connection):
    row = connection.execute("PRAGMA database_list").fetchone()

    return Path(row[2]).resolve() if row and row[2] else None


def _roll_back(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        pass


def _identity_statements(table) -> tuple:
    """
    What gives every new row of a table its own identifier. A row
    cannot bring one with it, so imported rows cannot claim another
    row's identifier or one that was already deleted, and once given
    it cannot be changed.
    """
    extra = (
        ", memory_artifact_id = (SELECT artifact_id FROM memories WHERE id = NEW.memory_id)"
        if table == "memory_history"
        else ""
    )

    return (
        f"CREATE UNIQUE INDEX {table}_artifact_id ON {table} (artifact_id)",
        f"CREATE TRIGGER {table}_artifact_id_assign AFTER INSERT ON {table} BEGIN "
        "SELECT RAISE(ABORT, 'artifact_id is assigned by the store') "
        "WHERE NEW.artifact_id IS NOT NULL; "
        f"UPDATE {table} SET artifact_id = {NEW_ID}{extra} WHERE rowid = NEW.rowid; END",
        f"CREATE TRIGGER {table}_artifact_id_fixed BEFORE UPDATE OF artifact_id ON {table} "
        "WHEN OLD.artifact_id IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'artifact_id cannot be changed'); END",
    )


# -- removing what the ledger names ----------------------------------------------


def _purge(connection, tombstones, keep=None, plan=None) -> None:
    """
    Remove every row named by `tombstones` (pairs of identifier and
    kind) and what was derived from it. Rows that are already gone are
    passed over, so the same tombstones can be applied any number of
    times. Rows are found by their identifiers only, never by their
    numbers, which a restored database gives out again.

    `keep` is the conversation in progress, which stays even when its
    last message goes. `plan` is the number and words of the one memory
    being forgotten; without it, a saved migration plan is cleared
    whole, because its lines hold copies of memories and carry no
    identifiers.
    """
    named = {kind: [] for kind in TABLES}

    for artifact_id, kind in tombstones:
        named[kind].append(artifact_id)

    removed = 0

    for kind in ("memory", "history"):
        for artifact_id in named[kind]:
            removed += connection.execute(
                f"DELETE FROM {TABLES[kind]} WHERE artifact_id = ?", (artifact_id,)
            ).rowcount

    if removed and _has(connection, "table", "migration_plan"):
        if plan is None:
            connection.execute("DELETE FROM migration_plan")
        else:
            connection.execute(
                "DELETE FROM migration_plan "
                "WHERE memory_id = ? OR LOWER(original_content) = LOWER(?)",
                plan,
            )

    turns = []
    affected = set()

    for artifact_id in named["turn"]:
        row = connection.execute(
            "SELECT id, conversation_id FROM conversation_turn WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()

        if row is not None:
            turns.append(row[0])
            affected.add(row[1])

    for artifact_id in named["conversation"]:
        row = connection.execute(
            "SELECT id FROM conversation WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()

        if row is None:
            continue

        # Its messages were recorded with it. Any still here would be
        # left without a conversation, so they go as well.
        turns += [
            found[0]
            for found in connection.execute(
                "SELECT id FROM conversation_turn WHERE conversation_id = ?", (row[0],)
            ).fetchall()
        ]
        connection.execute("DELETE FROM conversation WHERE id = ?", (row[0],))

    for turn_id in set(turns):
        connection.execute("DELETE FROM conversation_search WHERE rowid = ?", (turn_id,))
        connection.execute("DELETE FROM conversation_turn WHERE id = ?", (turn_id,))

    for conversation_id in affected:
        left = connection.execute(
            "SELECT COUNT(*) FROM conversation_turn WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]

        if left == 0 and conversation_id != keep:
            connection.execute("DELETE FROM conversation WHERE id = ?", (conversation_id,))
        else:
            # Its summary may repeat what was removed.
            connection.execute(
                "UPDATE conversation SET summary = '', summarized_turns = 0 WHERE id = ?",
                (conversation_id,),
            )

    if turns:
        # The index keeps a deleted message's words in its older parts
        # until those are merged. Merging them now drops the words.
        connection.execute(
            "INSERT INTO conversation_search (conversation_search) VALUES ('optimize')"
        )


# -- the ledger ------------------------------------------------------------------


class DeletionLedger:
    """
    The record of what was deleted from one memory database, in a
    database of its own. It holds the store's identity and one
    tombstone per deleted row, and is only ever added to.

    Deleting is done in two steps. The tombstones are committed to the
    ledger first. Then, in one transaction, the rows are removed from
    the memory database and it notes how far into the ledger it has
    got. If the first step fails nothing has changed. If the second
    fails, the deletion is on record but the rows are still there, so
    the memory database's connection is closed at once: everything
    sharing it stops, and the next protected open finishes the job.
    """

    def __init__(self, connection, store_id):
        self.connection = connection
        self.store_id = store_id
        self.quarantined = False

    @classmethod
    def open(cls, path, source) -> "DeletionLedger":
        """
        Open the existing ledger at `path` for the memory database on
        `source`, check that the two belong together, and apply every
        tombstone. Call this before anything else touches `source`.

        A ledger is never created here. A missing, empty, damaged, or
        foreign ledger, one that is behind the memory database, or a
        memory database that was never protected (a copy from before
        `initialize_protection`, say) raises StorageQuarantined, and
        neither file has been changed. The caller closes `source`.
        """
        path = Path(path).resolve()

        try:
            database = _database_path(source)
        except sqlite3.Error as error:
            raise StorageQuarantined("The memory database could not be read.") from error

        if database is None:
            raise StorageQuarantined("Protected deletion needs a memory database in a file.")

        if _same(path, database) or _beside(path, database):
            raise StorageQuarantined(
                "The deletion ledger must be kept outside the memory database's folder."
            )

        if not path.is_file():
            raise StorageQuarantined("The deletion ledger is missing.")

        try:
            # mode=rw: an existing file is opened, never a new one made.
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=rw",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as error:
            raise StorageQuarantined("The deletion ledger could not be opened.") from error

        ledger = cls(connection, None)

        try:
            connection.execute("PRAGMA synchronous = FULL")
            rows = ledger._verify(source)
        except sqlite3.Error as error:
            connection.close()
            raise StorageQuarantined(
                "The deletion ledger or the memory database could not be checked."
            ) from error
        except BaseException:
            connection.close()
            raise

        ledger._apply(source, [row[1:] for row in rows], rows[-1][0] if rows else 0)

        return ledger

    def _verify(self, source) -> list:
        """
        Check the ledger, then the memory database against it, reading
        only. Returns the tombstones as (sequence, identifier, kind).
        """
        ledger = self.connection

        if ledger.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise StorageQuarantined("The deletion ledger is damaged.")

        if not (_has(ledger, "table", "ledger_meta") and _has(ledger, "table", "tombstone")):
            raise StorageQuarantined("The deletion ledger has not been set up.")

        found = ledger.execute(
            "SELECT value FROM ledger_meta WHERE key = 'store_id'"
        ).fetchone()
        self.store_id = found[0] if found else None

        if not _well_formed(self.store_id):
            raise StorageQuarantined("The deletion ledger has not been set up.")

        kinds = ", ".join(f"'{kind}'" for kind in TABLES)

        if ledger.execute(
            f"SELECT COUNT(*) FROM tombstone WHERE {MALFORMED.format('artifact_id')} "
            f"OR kind NOT IN ({kinds}) OR typeof(seq) != 'integer' OR seq < 1"
        ).fetchone()[0]:
            raise StorageQuarantined("The deletion ledger holds a malformed tombstone.")

        rows = ledger.execute(
            "SELECT seq, artifact_id, kind FROM tombstone ORDER BY seq"
        ).fetchall()
        head = rows[-1][0] if rows else 0

        marker = _marker(source)

        if marker is None:
            raise StorageQuarantined(
                "The memory database has not been set up for protected deletion. "
                "It may be a copy made before that was done."
            )

        if marker.get("store_id") != self.store_id:
            raise StorageQuarantined(
                "The deletion ledger belongs to a different memory database."
            )

        applied = str(marker.get("ledger_seq", ""))

        if not (applied.isascii() and applied.isdigit()):
            raise StorageQuarantined("The memory database's ledger position is malformed.")

        if head < int(applied):
            raise StorageQuarantined(
                "The deletion ledger is older than the memory database: "
                "deletions it should hold are missing from it."
            )

        for table in TABLES.values():
            wanted = {"artifact_id"} | (
                {"memory_artifact_id"} if table == "memory_history" else set()
            )
            index = source.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (f"{table}_artifact_id",),
            ).fetchone()

            if (
                not _has(source, "table", table)
                or not wanted <= _columns(source, table)
                or index is None
                or not str(index[0]).upper().startswith("CREATE UNIQUE INDEX")
                or not _has(source, "trigger", f"{table}_artifact_id_assign")
                or not _has(source, "trigger", f"{table}_artifact_id_fixed")
            ):
                raise StorageQuarantined(
                    f"The memory database's {table} table is not set up for protected deletion."
                )

            if source.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {MALFORMED.format('artifact_id')}"
            ).fetchone()[0]:
                raise StorageQuarantined(
                    f"A row of the memory database's {table} table has no valid identifier."
                )

        return rows

    def head(self) -> int:
        return self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM tombstone"
        ).fetchone()[0]

    def forget(self, source, tombstones, keep=None, plan=None) -> None:
        """
        Delete the rows named by `tombstones` (pairs of identifier and
        kind) from the memory database on `source`, for good. See
        `_purge` for `keep` and `plan`.

        An error raised before the ledger has the tombstones leaves
        the memory database unchanged and in use. After that point a
        failure raises StorageQuarantined and closes `source`.
        """
        if self.quarantined:
            raise StorageQuarantined("The memory database is quarantined.")

        tombstones = list(tombstones)

        if source.in_transaction:
            raise sqlite3.OperationalError(
                "Finish the current memory transaction before recording a deletion."
            )

        head = self._record(source, tombstones)
        self._apply(source, tombstones, head, keep, plan)

    def _record(self, source, tombstones) -> int:
        moment = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ledger = self.connection
        committing = False

        try:
            ledger.execute("BEGIN IMMEDIATE")

            for artifact_id, kind in tombstones:
                ledger.execute(
                    "INSERT INTO tombstone (artifact_id, kind, deleted_at) "
                    "SELECT ?, ?, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM tombstone WHERE artifact_id = ?)",
                    (artifact_id, kind, moment, artifact_id),
                )

            head = self.head()
            committing = True
            ledger.execute("COMMIT")
        except BaseException as error:
            _roll_back(ledger)
            if committing:
                # A commit error does not prove the deletion was not
                # persisted. Refuse access until a fresh open reconciles.
                self._quarantine(source)
                if isinstance(error, Exception):
                    raise StorageQuarantined(
                        "The deletion ledger commit is uncertain. Reopen memory "
                        "storage to reconcile before serving it."
                    ) from error
            raise

        return head

    def _apply(self, source, tombstones, head, keep=None, plan=None) -> None:
        """
        Remove tombstoned rows from the memory database and note the
        ledger position reached, together or not at all.
        """
        try:
            source.execute("BEGIN IMMEDIATE")
            _purge(source, tombstones, keep, plan)
            source.execute(
                "UPDATE storage_meta SET value = ? "
                "WHERE key = 'ledger_seq' AND CAST(value AS INTEGER) < ?",
                (str(head), head),
            )
            source.commit()
        except BaseException as error:
            self._quarantine(source)

            if isinstance(error, Exception):
                raise StorageQuarantined(
                    "A recorded deletion could not be carried out. The memory "
                    "database is closed until it is opened and reconciled again."
                ) from error

            raise

    def _quarantine(self, source) -> None:
        self.quarantined = True
        _roll_back(source)

        for connection in (source, self.connection):
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        self.connection.close()


# -- setting a database up ---------------------------------------------------------


def _same_contents(first, second) -> bool:
    """
    Whether two databases have the same schema and the same rows in
    every table, the search index's own storage included.
    """
    schema = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    entries = first.execute(schema).fetchall()

    if entries != second.execute(schema).fetchall():
        return False

    for kind, name, _table, _sql in entries:
        if kind != "table":
            continue

        query = 'SELECT * FROM "{}"'.format(name.replace('"', '""'))

        for ours, theirs in zip_longest(first.execute(query), second.execute(query)):
            if ours != theirs:
                return False

    return True


def _back_up(database: Path, backup: Path) -> None:
    """
    Copy the memory database to a new file and check the copy: it must
    be sound and hold exactly what the database holds. The caller holds
    the database's write lock, so nothing changes meanwhile.
    """
    # Claims the name, and fails if anything is already there.
    backup.open("xb").close()
    reader = copy = None

    try:
        reader = sqlite3.connect(database)
        copy = sqlite3.connect(backup)
        reader.backup(copy)
        copy.close()

        copy = sqlite3.connect(backup)

        if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ProtectionSetupError("The backup copy is damaged.")

        if not _same_contents(reader, copy):
            raise ProtectionSetupError("The backup copy does not match the memory database.")
    except BaseException:
        for connection in (reader, copy):
            if connection is not None:
                connection.close()

        backup.unlink(missing_ok=True)
        raise

    reader.close()
    copy.close()


def _create_ledger(path: Path, store_id: str) -> None:
    connection = sqlite3.connect(path, isolation_level=None)

    try:
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("BEGIN")

        for statement in LEDGER_SCHEMA:
            connection.execute(statement)

        connection.execute(
            "INSERT INTO ledger_meta (key, value) VALUES ('store_id', ?)", (store_id,)
        )
        connection.execute("COMMIT")
    finally:
        connection.close()


def _check_ready(source) -> None:
    if _marker(source) is not None:
        raise ProtectionSetupError("This memory database is already protected.")

    for table, needed in REQUIRED_COLUMNS.items():
        columns = _columns(source, table)

        if not needed <= columns:
            raise ProtectionSetupError(
                f"The memory database has no usable {table} table. Open it "
                "with Freya once before protecting it."
            )

        if columns & {"artifact_id", "memory_artifact_id"}:
            raise ProtectionSetupError(f"The {table} table already has identifiers.")

    if not _has(source, "table", SEARCH_TABLE):
        raise ProtectionSetupError(f"The memory database has no {SEARCH_TABLE} table.")


def _add_identities(source, store_id) -> None:
    for table in TABLES.values():
        source.execute(f"ALTER TABLE {table} ADD COLUMN artifact_id TEXT")

        for (rowid,) in source.execute(f"SELECT rowid FROM {table}").fetchall():
            source.execute(
                f"UPDATE {table} SET artifact_id = ? WHERE rowid = ?",
                (uuid.uuid4().hex, rowid),
            )

    # An archived version remembers which memory it was a version of,
    # so that forgetting a memory that is no longer live (one removed
    # as a duplicate) also covers copies in which it still is.
    source.execute("ALTER TABLE memory_history ADD COLUMN memory_artifact_id TEXT")
    source.execute(
        "UPDATE memory_history SET memory_artifact_id = "
        "(SELECT artifact_id FROM memories WHERE id = memory_history.memory_id)"
    )

    for table in TABLES.values():
        for statement in _identity_statements(table):
            source.execute(statement)

    source.execute("CREATE TABLE storage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    source.execute(
        "INSERT INTO storage_meta (key, value) VALUES ('store_id', ?), ('ledger_seq', '0')",
        (store_id,),
    )


def initialize_protection(database_path, ledger_path, backup_path) -> str:
    """
    Protect an existing memory database, once, as a deliberate act:
    back it up, create its deletion ledger, and give every row of its
    memories, their history, and the conversations an identifier.
    Returns the identity the two files now share.

    The backup is made and checked before anything is changed; if it
    cannot be, nothing is changed. It is a copy from before protection,
    so a protected open will refuse it. The database must not be in use,
    and must already have Freya's tables. The ledger and the backup are
    new files in folders that exist; nothing is ever overwritten. The
    ledger must be outside the memory database's folder, and should be
    somewhere a restore of the memory database does not also put back:
    that cannot be checked here.

    On failure ProtectionSetupError is raised, the memory database is as
    it was, and no ledger is left behind. A backup that was completed
    and checked is kept.
    """
    database, ledger, backup = (
        Path(path).resolve() for path in (database_path, ledger_path, backup_path)
    )

    if not database.is_file():
        raise ProtectionSetupError("There is no memory database at that path.")

    if _same(ledger, database) or _beside(ledger, database):
        raise ProtectionSetupError(
            "The deletion ledger must be kept outside the memory database's folder."
        )

    sidecars = [database] + [
        database.with_name(database.name + suffix) for suffix in ("-journal", "-wal", "-shm")
    ]

    if _same(backup, ledger) or any(_same(backup, path) for path in sidecars):
        raise ProtectionSetupError(
            "The backup must be a file of its own, apart from the memory "
            "database and the ledger."
        )

    for path in (ledger, backup):
        if os.path.lexists(path):
            raise ProtectionSetupError(f"{path.name} already exists and will not be overwritten.")

    made_ledger = False
    source = sqlite3.connect(database, isolation_level=None)

    try:
        source.execute("PRAGMA secure_delete = ON")
        # Nothing else can write from here until the end.
        source.execute("BEGIN IMMEDIATE")
        _check_ready(source)
        _back_up(database, backup)

        store_id = uuid.uuid4().hex
        ledger.open("xb").close()
        made_ledger = True
        _create_ledger(ledger, store_id)

        _add_identities(source, store_id)
        source.execute("COMMIT")
    except BaseException as error:
        _roll_back(source)

        try:
            unchanged = _marker(source) is None
        except sqlite3.Error:
            unchanged = False

        if made_ledger and unchanged:
            ledger.unlink(missing_ok=True)

        if isinstance(error, (sqlite3.Error, OSError)):
            raise ProtectionSetupError(
                f"The memory database could not be protected: {error}"
            ) from error

        raise
    finally:
        source.close()

    return store_id
