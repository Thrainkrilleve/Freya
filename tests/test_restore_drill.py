"""
Scratch restore drill, and the deletion ledger that a restore must not roll back.

Everything stored here is made up. The owner's data directory is not opened.
"""

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from freya.memory import memory_manager
from freya.memory.deletion_ledger import ledger_path_for
from freya.memory.memory_manager import MemoryManager
from freya.memory.protection import ProtectionSetupError, ensure_memory_protection
from freya.paths import DATA_DIR_VARIABLE
from freya.storage import backup
from freya.storage.backup import BackupRefused, back_up, read_manifest, read_member, replace_member
from freya.storage.custody import BACKEND_KEYCHAIN, MemoryKeyStore
from freya.storage.restore import restore
from freya.storage.restore_drill import SCRATCH_FACT, WHEN, run_drill


REPO = Path(__file__).resolve().parents[1]
FACT = "The drill code is 1357."


@pytest.fixture
def keystore(monkeypatch):
    """A stand-in keystore. Direct backups in this file go through it."""
    store = MemoryKeyStore(BACKEND_KEYCHAIN)
    monkeypatch.setattr(backup, "default_custody", lambda environ=None: store)
    return store


def _backup_ledger(folder, keystore, directory):
    path = directory / "backup-ledger.db"
    path.write_bytes(read_member(folder, "deletion_ledger.db", custody=keystore))
    return path


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(memory_manager, "DATA_DIR", data)
    monkeypatch.setattr(memory_manager, "DATABASE_PATH", data / "freya_memory.db")
    monkeypatch.delenv("FREYA_DELETION_LEDGER", raising=False)
    return data


def _close(memory) -> None:
    if memory.ledger is not None:
        memory.ledger.close()

    memory.connection.close()


def _contents(database: Path) -> str:
    connection = sqlite3.connect(
        f"file:{database.resolve().as_posix()}?mode=ro",
        uri=True,
    )

    try:
        rows = connection.execute("SELECT content FROM memories").fetchall()
    finally:
        connection.close()

    return " ".join(row[0] for row in rows)


def _tombstones(path: Path) -> list:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )

    try:
        return connection.execute(
            "SELECT artifact_id FROM tombstone ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()


def _insert_tombstone(path: Path, artifact_id: str) -> None:
    connection = sqlite3.connect(path, isolation_level=None)

    try:
        connection.execute(
            "INSERT INTO tombstone (artifact_id, kind, deleted_at) "
            "VALUES (?, 'memory', '2026-11-13T12:00:00+00:00')",
            (artifact_id,),
        )
    finally:
        connection.close()


def test_protection_is_applied_once(scratch):
    first = ensure_memory_protection(environ={})
    second = ensure_memory_protection(environ={})

    assert first == second
    assert first.is_file()
    assert not first.is_relative_to(scratch)
    assert list(first.parent.glob("before-protection*.db"))


def test_a_ledger_inside_the_data_folder_is_refused(scratch):
    with pytest.raises(ProtectionSetupError):
        ensure_memory_protection(
            environ={"FREYA_DELETION_LEDGER": str(scratch / "deletions.db")},
        )

    assert not (scratch / "freya_memory.db").exists()


def test_a_fact_forgotten_after_a_backup_stays_forgotten_after_restore(scratch, tmp_path, keystore):
    ledger = ensure_memory_protection(environ={})
    memory = MemoryManager(ledger)

    try:
        number = memory.add_memory(FACT, "fact", "drill")
    finally:
        _close(memory)

    folder = back_up(
        scratch,
        tmp_path / "backups",
        WHEN,
        allow_same_drive=True,
        environ={},
    )
    copied = [
        entry["name"]
        for entry in read_manifest(folder, custody=keystore)["copied"]
    ]
    assert "freya_memory.db" in copied
    assert "deletion_ledger.db" in copied
    assert _tombstones(_backup_ledger(folder, keystore, tmp_path)) == []

    memory = MemoryManager(ledger)

    try:
        assert memory.delete_memory(number) is True
        assert FACT not in _contents(scratch / "freya_memory.db")
    finally:
        _close(memory)

    restore(folder, scratch, environ={})
    assert _tombstones(ledger) != []
    assert _tombstones(_backup_ledger(folder, keystore, tmp_path)) == []
    assert FACT not in _contents(scratch / "freya_memory.db")

    memory = MemoryManager(ledger)

    try:
        assert memory.get_memory(number) is None
        assert FACT not in _contents(scratch / "freya_memory.db")
    finally:
        _close(memory)


def test_a_forgetting_comes_back_when_the_newer_ledger_is_gone(scratch, tmp_path, keystore):
    """
    The backup's ledger is the only one left, and it was taken before
    the forgetting. Restore cannot invent a tombstone it never had.
    """
    ledger = ensure_memory_protection(environ={})
    memory = MemoryManager(ledger)

    try:
        number = memory.add_memory(FACT, "fact", "drill")
    finally:
        _close(memory)

    folder = back_up(
        scratch, tmp_path / "backups", WHEN, allow_same_drive=True, environ={},
    )
    memory = MemoryManager(ledger)

    try:
        assert memory.delete_memory(number) is True
    finally:
        _close(memory)

    ledger.unlink()
    restore(folder, scratch, environ={})

    assert FACT in _contents(scratch / "freya_memory.db")
    assert _tombstones(ledger) == []


def test_a_divergent_ledger_is_not_written_over(scratch, tmp_path, keystore):
    ledger = ensure_memory_protection(environ={})
    memory = MemoryManager(ledger)

    try:
        memory.add_memory(FACT, "fact", "drill")
    finally:
        _close(memory)

    folder = back_up(
        scratch, tmp_path / "backups", WHEN, allow_same_drive=True, environ={},
    )
    _insert_tombstone(ledger, "a" * 32)
    changed = _backup_ledger(folder, keystore, tmp_path)
    _insert_tombstone(changed, "b" * 32)
    replace_member(folder, "deletion_ledger.db", changed.read_bytes(), custody=keystore)
    before = (scratch / "freya_memory.db").read_bytes()

    with pytest.raises(BackupRefused, match="does not continue"):
        restore(folder, scratch, environ={})

    assert (scratch / "freya_memory.db").read_bytes() == before
    assert _tombstones(ledger) == [("a" * 32,)]


def test_an_older_live_ledger_gains_tombstones_from_the_backup(scratch, tmp_path, keystore):
    ledger = ensure_memory_protection(environ={})
    memory = MemoryManager(ledger)

    try:
        number = memory.add_memory(FACT, "fact", "drill")
    finally:
        _close(memory)

    older = back_up(
        scratch, tmp_path / "older", WHEN, allow_same_drive=True, environ={},
    )
    empty = read_member(older, "deletion_ledger.db", custody=keystore)
    memory = MemoryManager(ledger)

    try:
        assert memory.delete_memory(number) is True
    finally:
        _close(memory)

    newer = back_up(
        scratch,
        tmp_path / "newer",
        WHEN.replace(minute=5),
        allow_same_drive=True,
        environ={},
    )
    ledger.write_bytes(empty)
    restore(newer, scratch, environ={})

    assert _tombstones(ledger) != []
    assert FACT not in _contents(scratch / "freya_memory.db")


def test_the_scratch_drill_restores_every_store(tmp_path):
    result = run_drill(tmp_path / "drill", environ={})

    assert result.ok, result.problems
    assert result.inside_rto and result.inside_rpo
    assert result.seconds < 120
    assert result.problems == ()
    copied = set(result.copied)
    assert {
        "freya_control.db",
        "freya_usage.db",
        "freya_memory.db",
        "freya_tasks.db",
        "media/freya_media.db",
        "deletion_ledger.db",
    } <= copied
    counts = dict(result.rows)
    assert counts["freya_control.db:audit_event"] == 2
    assert counts["freya_control.db:policy_state"] == 1
    assert counts["freya_usage.db:usage_limit"] == 1
    assert counts["freya_memory.db:memories"] == 1
    assert counts["freya_memory.db:conversation"] == 1
    assert counts["media/freya_media.db:media"] == 1
    assert counts["freya_tasks.db:task"] == 1
    assert "measured on CI scratch data" in result.report()
    assert "not the owner's databases" in result.report()
    assert "encrypted: yes" in result.report()
    stored = b""
    for path in (tmp_path / "drill" / "backups").rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            stored += data
            assert not data.startswith(b"SQLite format 3")
    assert SCRATCH_FACT.encode("utf-8") not in stored

    data = tmp_path / "drill" / "data"
    ledger = ledger_path_for(data, {})
    assert ledger.is_file()
    assert not ledger.is_relative_to(data)
    assert (data / "freya_memory.db").is_file()


def test_the_drill_does_not_touch_the_os_keystore(tmp_path, monkeypatch):
    def refused():
        raise AssertionError("the drill used the OS keystore")

    monkeypatch.setattr(backup, "default_custody", refused)
    result = run_drill(tmp_path / "drill", environ={})

    assert result.ok, result.problems
    assert result.encrypted


def test_the_drill_refuses_the_owners_data_directory(tmp_path, monkeypatch):
    owner = tmp_path / "owner-data"
    owner.mkdir()
    monkeypatch.setenv(DATA_DIR_VARIABLE, str(owner))

    with pytest.raises(BackupRefused, match="owner's data"):
        run_drill(owner, environ={})

    assert list(owner.iterdir()) == []


def test_the_devtools_command_runs_the_drill(tmp_path):
    env = os.environ.copy()
    env.pop("FREYA_DELETION_LEDGER", None)
    env.pop(DATA_DIR_VARIABLE, None)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO / "devtools" / "restore_drill.py"),
            "--root",
            str(tmp_path / "scratch"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "measured on CI scratch data" in completed.stdout
    assert "inside 4 h RTO: yes" in completed.stdout
    assert "inside 24 h RPO: yes" in completed.stdout


def test_main_opens_the_memory_store_in_protected_mode():
    source = (REPO / "src" / "freya" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")

        if name != "FreyaAssistant":
            continue

        found = True
        assert "memory_ledger_path" in {item.arg for item in node.keywords}

    assert found
    assert "ensure_memory_protection" in source


def test_a_backup_that_points_outside_itself_is_refused(tmp_path):
    folder = tmp_path / "backup" / "2026-11-13_120000"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("leave me", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest = {
        "made": folder.name,
        "from": "scratch",
        "copied": [{
            "name": "../outside.txt",
            "kind": "file",
            "bytes": outside.stat().st_size,
            "sha256": digest,
        }],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data = tmp_path / "restored"
    data.mkdir()

    with pytest.raises(BackupRefused, match="not encrypted"):
        restore(folder, data, environ={})

    assert outside.read_text(encoding="utf-8") == "leave me"
    assert list(data.iterdir()) == []

    with pytest.raises(BackupRefused, match="should not"):
        restore(folder, data, environ={}, allow_unencrypted=True)

    assert outside.read_text(encoding="utf-8") == "leave me"
    assert list(data.iterdir()) == []


def test_the_adr_records_the_scratch_measurement():
    text = (REPO / "docs" / "adr" / "015-persistence-recovery.md").read_text(encoding="utf-8")

    assert "measured on CI scratch data" in text
    assert "Not approved" in text
