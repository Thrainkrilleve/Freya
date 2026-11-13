import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from freya.paths import data_dir


logger = logging.getLogger(__name__)

DATA_DIR = data_dir()
CONTROL_DATABASE_PATH = DATA_DIR / "freya_control.db"

# Each entry is one schema version. Never edit an entry that has shipped;
# append a new one instead.
MIGRATIONS = [
    """
    CREATE TABLE policy_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        revision INTEGER NOT NULL,
        grant_epoch INTEGER NOT NULL
    );

    INSERT INTO policy_state (id, revision, grant_epoch)
    VALUES (1, 0, 1);

    CREATE TABLE policy_rule (
        rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        capability TEXT NOT NULL,
        effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (subject, capability, effect)
    );

    CREATE TABLE audit_event (
        event_id INTEGER PRIMARY KEY,
        operation_id TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        kind TEXT NOT NULL
            CHECK (kind IN ('decision', 'dispatch', 'outcome')),
        actor TEXT NOT NULL,
        client TEXT NOT NULL,
        domain TEXT NOT NULL,
        capability TEXT NOT NULL,
        provider TEXT,
        target_ref TEXT,
        decision TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
        decision_reason TEXT NOT NULL,
        policy_revision INTEGER,
        grant_epoch INTEGER,
        outcome TEXT,
        error_summary TEXT,
        arguments_digest TEXT,
        prev_hash TEXT NOT NULL,
        row_hash TEXT NOT NULL
    );

    CREATE INDEX audit_event_operation
        ON audit_event (operation_id);

    CREATE TRIGGER audit_event_no_update
        BEFORE UPDATE ON audit_event
    BEGIN
        SELECT RAISE(ABORT, 'audit_event is append-only');
    END;

    CREATE TRIGGER audit_event_no_delete
        BEFORE DELETE ON audit_event
    BEGIN
        SELECT RAISE(ABORT, 'audit_event is append-only');
    END;

    CREATE TABLE audit_checkpoint (
        checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        last_event_id INTEGER NOT NULL,
        row_hash TEXT NOT NULL,
        sink TEXT,
        sink_status TEXT
    );
    """,
    # Version 2: remember which default rule sets have been applied, so a
    # new default set can be added later without restoring rules the owner
    # removed. Stores seeded under version 1 already hold the system-tools
    # set.
    """
    CREATE TABLE policy_seed (
        name TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    );

    INSERT INTO policy_seed (name, applied_at)
    SELECT 'alpha-system-tools', strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    FROM policy_state
    WHERE revision > 0;
    """,
]


class ControlStore:
    """
    SQLite storage for Freya's policy and audit state.

    This is deliberately a separate database from the memory store.
    The durability settings (WAL journal, synchronous=FULL) are a
    provisional alpha choice.

    Another small store that wants the same handling passes its own
    migrations and path; it never shares a file with this one.
    """

    def __init__(self, path=CONTROL_DATABASE_PATH, migrations=None):
        self.path = Path(path)
        self._migrations = migrations
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            check_same_thread=False,
            timeout=5.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute("PRAGMA foreign_keys = ON")

        self._migrate()

    def _migrate(self):
        migrations = (
            self._migrations
            if self._migrations is not None
            else MIGRATIONS
        )

        with self.transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version "
                "(version INTEGER NOT NULL)"
            )
            row = connection.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            current = row[0] or 0

            if current > len(migrations):
                raise RuntimeError(
                    "Control database schema version "
                    f"{current} is newer than this code supports."
                )

            for version in range(current + 1, len(migrations) + 1):
                for statement in _split_statements(
                    migrations[version - 1]
                ):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_version (version) VALUES (?)",
                    (version,),
                )

    @property
    def schema_version(self):
        row = self.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        return row[0]

    def execute(self, sql, parameters=()):
        """
        Run one statement and return its rows, already read.

        The connection is shared between threads, so rows are fetched
        while the lock is held. Fetching later, from a cursor another
        thread's statement can disturb, returned rows of the wrong
        shape or none at all, which policy turned into spurious
        denials and the audit check could have turned into spurious
        tamper reports.
        """
        with self._lock:
            cursor = self._connection.execute(sql, parameters)
            return _Rows(cursor.fetchall(), cursor.rowcount)

    @contextmanager
    def transaction(self):
        """
        Run a write transaction. The write lock is taken up front so
        that read-then-write sequences cannot interleave.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.execute("COMMIT")
            except BaseException:
                # A failed COMMIT (busy, disk full, I/O error) can leave
                # the transaction open; without a rollback every later
                # BEGIN would fail and the store would stay unusable.
                self._rollback_quietly()
                raise

    def _rollback_quietly(self):
        # Only reached while another exception is propagating; that
        # exception is the one the caller needs to see.
        if not self._connection.in_transaction:
            return
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error as exc:
            logger.warning("Control store rollback failed: %s", exc)

    def close(self):
        with self._lock:
            self._connection.close()


def _split_statements(script):
    """
    Split a migration script into statements, keeping trigger bodies
    (which contain their own semicolons) intact.
    """
    statements = []
    buffer = ""

    for line in script.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""

    if buffer.strip():
        raise ValueError("Incomplete SQL statement in migration.")

    return statements


class _Rows:
    """
    The rows of a finished statement, with the parts of the sqlite3
    cursor interface that callers use.
    """

    def __init__(self, rows, rowcount):
        self._rows = rows
        self._position = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row

    def fetchall(self):
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())
