"""
Live database encryption.

Keys in here are generated for the test and protect nothing. The
passphrase is made up. Nothing is written beside the temporary data
folder as a plain key.
"""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from freya.core.persistence.control_store import ControlStore
from freya.storage.custody import BACKEND_DPAPI, MemoryKeyStore, NoKeyStore
from freya.storage.dbkey import (
    StoreLocked,
    bind_memory_key,
    bundle_path_for,
    clear_unlock,
    current,
    ensure_unlocked,
    require_unlocked,
)
from freya.storage.encrypt import migrate_file, migrate_stores
from freya.storage.sqlite import (
    SQLITE_HEADER,
    StoreError,
    connect,
    is_plaintext_sqlite,
    open_with,
    rekey,
)


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src" / "freya"
MARKER = "MARKER-plain-text-4419-unique"
WHEN = datetime(2026, 11, 15, 8, 0, 0)
PASSPHRASE = "made-up-passphrase-for-this-test"
DIRECT_IMPORT = re.compile(
    r"(?m)^(?:import|from)\s+(sqlite3|sqlcipher3)\b"
)


def test_only_the_opener_imports_sqlite_or_sqlcipher():
    offenders = []

    for path in SOURCE.rglob("*.py"):
        if path.name == "sqlite.py" and path.parent.name == "storage":
            continue

        text = path.read_text(encoding="utf-8")

        if DIRECT_IMPORT.search(text):
            offenders.append(str(path.relative_to(REPO)))

    assert offenders == []


def test_a_new_store_round_trips_and_is_not_plaintext(tmp_path):
    path = tmp_path / "freya_control.db"
    store = ControlStore(path)

    try:
        store.execute("CREATE TABLE notes (body TEXT)")
        store.execute("INSERT INTO notes (body) VALUES (?)", (MARKER,))
    finally:
        store.close()

    raw = path.read_bytes()
    assert not raw.startswith(SQLITE_HEADER)
    assert MARKER.encode("utf-8") not in raw
    assert current().dek not in raw
    held = current()

    again = connect(path)

    try:
        assert again.execute("SELECT body FROM notes").fetchone()[0] == MARKER
    finally:
        again.close()

    clear_unlock()

    with pytest.raises(StoreLocked, match="locked"):
        connect(path)

    assert not path.with_name(path.name + ".key").exists()
    bind_memory_key(held.dek, held.key_id)
    restored = connect(path)

    try:
        assert restored.execute("SELECT body FROM notes").fetchone()[0] == MARKER
    finally:
        restored.close()


def test_a_wrong_key_is_refused_and_a_missing_unlock_writes_nothing(tmp_path):
    path = tmp_path / "freya_usage.db"
    first = current()
    store = ControlStore(path)
    store.close()
    before = path.read_bytes()
    bind_memory_key()

    with pytest.raises(StoreLocked, match="not accepted") as refused:
        connect(path)

    assert first.dek.hex() not in str(refused.value)
    assert path.read_bytes() == before

    clear_unlock()
    missing = tmp_path / "freya_tasks.db"

    with pytest.raises(StoreLocked, match="Nothing was opened"):
        connect(missing)

    assert not missing.exists()
    bind_memory_key(first.dek, first.key_id)


def test_wal_journal_and_temp_files_do_not_hold_the_marker(tmp_path):
    path = tmp_path / "notes.db"
    connection = connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.execute("INSERT INTO notes (body) VALUES (?)", (MARKER,))
    connection.execute(
        "CREATE VIRTUAL TABLE notes_fts USING fts5(body)"
    )
    connection.execute(
        "INSERT INTO notes_fts (body) VALUES (?)", (MARKER,)
    )
    connection.commit()
    # Leave the WAL in place. A checkpoint would fold it into the main file.
    connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    present = {}

    for suffix in ("", "-wal", "-shm", "-journal"):
        side = Path(str(path) + suffix)

        if side.is_file() and side.stat().st_size:
            present[suffix or "db"] = side.read_bytes()

    for name, blob in present.items():
        assert MARKER.encode("utf-8") not in blob, name
        assert current().dek not in blob, name

    assert "db" in present
    assert not present["db"].startswith(SQLITE_HEADER)
    # A sort large enough to spill, if temporary files were on disk.
    connection.execute(
        "CREATE TABLE wide (body TEXT)"
    )
    connection.executemany(
        "INSERT INTO wide (body) VALUES (?)",
        [(MARKER,)] * 200,
    )
    connection.execute(
        "SELECT body FROM wide ORDER BY body DESC"
    ).fetchall()
    connection.close()

    for side in tmp_path.iterdir():
        if side.is_file():
            assert MARKER.encode("utf-8") not in side.read_bytes()


def test_no_keystore_requires_a_passphrase_and_a_wrong_one_does_not_unlock(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    clear_unlock()

    with pytest.raises(StoreLocked, match="passphrase"):
        ensure_unlocked(data, custody=NoKeyStore(), interactive=False)

    assert current() is None
    assert list(data.iterdir()) == []
    text = ensure_unlocked(
        data, custody=NoKeyStore(), passphrase=PASSPHRASE,
    )
    bundle = bundle_path_for(data)
    assert bundle.is_file()
    assert data not in bundle.parents
    blob = bundle.read_bytes()
    assert current().dek not in blob
    assert PASSPHRASE.encode("utf-8") not in blob
    assert text.encode("ascii") not in blob
    path = data / "freya_control.db"
    store = ControlStore(path)
    store.execute("CREATE TABLE notes (body TEXT)")
    store.execute("INSERT INTO notes (body) VALUES (?)", (MARKER,))
    store.close()
    clear_unlock()

    with pytest.raises(StoreLocked):
        ensure_unlocked(data, custody=NoKeyStore(), passphrase="wrong-made-up-phrase")

    assert current() is None

    with pytest.raises(StoreLocked, match="locked"):
        connect(path)

    ensure_unlocked(data, custody=NoKeyStore(), passphrase=PASSPHRASE)
    opened = connect(path)

    try:
        assert opened.execute("SELECT body FROM notes").fetchone()[0] == MARKER
    finally:
        opened.close()

    clear_unlock()
    ensure_unlocked(data, custody=NoKeyStore(), recovery_key=text)
    opened = connect(path)

    try:
        assert opened.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 1
    finally:
        opened.close()


def test_migration_is_one_way_idempotent_and_a_failure_leaves_the_original(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "freya_memory.db"
    plain = sqlite3.connect(path)
    plain.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    plain.execute("INSERT INTO notes (body) VALUES (?)", (MARKER,))
    plain.execute("INSERT INTO notes (body) VALUES (?)", ("second made-up row",))
    plain.commit()
    plain.close()
    original = path.read_bytes()
    assert original.startswith(SQLITE_HEADER)
    assert MARKER.encode("utf-8") in original

    real_counts = __import__(
        "freya.storage.encrypt", fromlist=["counts"]
    ).counts
    calls = {"n": 0}

    def fail_the_copy(connection):
        calls["n"] += 1
        found = real_counts(connection)

        if calls["n"] >= 2:
            return {}

        return found

    monkeypatch.setattr("freya.storage.encrypt.counts", fail_the_copy)

    with pytest.raises(StoreError, match="left unchanged"):
        migrate_file(path)

    assert path.read_bytes() == original
    assert is_plaintext_sqlite(path)
    monkeypatch.undo()
    store = MemoryKeyStore(BACKEND_DPAPI)
    results = migrate_stores(
        data,
        tmp_path / "backups",
        custody=store,
        allow_same_drive=True,
        now=WHEN,
        environ={},
    )
    assert results == [(path, "migrated")]
    encrypted = path.read_bytes()
    assert not encrypted.startswith(SQLITE_HEADER)
    assert MARKER.encode("utf-8") not in encrypted
    assert current().dek not in encrypted

    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(str(path) + suffix)

        if side.is_file():
            assert MARKER.encode("utf-8") not in side.read_bytes()

    opened = connect(path)

    try:
        rows = opened.execute("SELECT body FROM notes ORDER BY id").fetchall()
    finally:
        opened.close()

    assert [row[0] for row in rows] == [MARKER, "second made-up row"]
    again = migrate_stores(
        data,
        tmp_path / "backups",
        custody=store,
        allow_same_drive=True,
        now=WHEN.replace(minute=5),
        environ={},
    )
    assert again == [(path, "already")]
    assert path.read_bytes() == encrypted


def test_rekey_replaces_the_file_after_it_checks(tmp_path):
    path = tmp_path / "freya_control.db"
    connection = connect(path)
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.execute("INSERT INTO notes (body) VALUES (?)", (MARKER,))
    connection.commit()
    connection.close()
    old = bytes(require_unlocked().dek)
    new_dek = os.urandom(32)
    rekey(path, new_dek)
    assert not path.read_bytes().startswith(SQLITE_HEADER)
    assert MARKER.encode("utf-8") not in path.read_bytes()
    assert not path.with_name(path.name + ".rekey").exists()
    assert not path.with_name(path.name + ".before-rekey").exists()

    with pytest.raises(StoreLocked):
        open_with(path, old)

    bind_memory_key(new_dek, os.urandom(16))
    opened = connect(path)

    try:
        assert opened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert opened.execute("SELECT body FROM notes").fetchone()[0] == MARKER
    finally:
        opened.close()


def test_plaintext_is_refused_without_the_migration(tmp_path):
    path = tmp_path / "freya_memory.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(StoreLocked, match="not encrypted"):
        connect(path)

    assert is_plaintext_sqlite(path)
