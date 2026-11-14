"""
Open the memory store in protected mode.

``main`` calls :func:`ensure_memory_protection` before it builds
``FreyaAssistant``. A database that has never been protected is
protected once: Freya's tables are created if needed, a copy from
before protection is kept beside the ledger, and every row gains an
identifier. A database that is already protected is left alone. The
ledger stays outside the data folder.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from freya.memory import memory_manager
from freya.memory.conversations import ConversationStore
from freya.memory.deletion_ledger import (
    ProtectionSetupError,
    StorageQuarantined,
    initialize_protection,
    is_protected,
    ledger_path_for,
)


def _protected_file(database: Path) -> bool:
    if not database.is_file():
        return False

    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro",
        uri=True,
    )

    try:
        return is_protected(connection)
    except sqlite3.Error as error:
        raise StorageQuarantined(
            "The memory database could not be read."
        ) from error
    finally:
        connection.close()


def _fresh_backup(folder: Path) -> Path:
    """
    A new file for the pre-protection copy. An earlier copy is kept;
    nothing here overwrites it.
    """
    plain = folder / "before-protection.db"

    if not plain.exists():
        return plain

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    named = folder / f"before-protection-{stamp}.db"

    if named.exists():
        raise ProtectionSetupError(
            f"{named.name} already exists and will not be overwritten."
        )

    return named


def ensure_memory_protection(environ=None) -> Path:
    """
    Return the deletion ledger path, protecting the memory database
    first when it has never been protected.

    The database ``MemoryManager`` would open is the one protected.
    Tests point that path at a temporary file before calling this.
    """
    database = memory_manager.DATABASE_PATH
    ledger = ledger_path_for(memory_manager.DATA_DIR, environ)

    if _protected_file(database):
        if not ledger.is_file():
            raise StorageQuarantined("The deletion ledger is missing.")

        return ledger

    memory = memory_manager.MemoryManager(None)

    try:
        ConversationStore(memory.connection)
    finally:
        memory.connection.close()

    ledger.parent.mkdir(parents=True, exist_ok=True)
    initialize_protection(database, ledger, _fresh_backup(ledger.parent))

    return ledger
