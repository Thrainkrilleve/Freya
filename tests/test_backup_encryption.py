"""
Encrypted backups.

Round trip, tamper, a wrong key, the header, a passphrase, and the
recovery key. Keys in here are generated for the test and protect
nothing. An old plaintext backup restores only with the migration flag.
"""

import json
import os
import sqlite3
from datetime import datetime

import pytest

from freya.storage import backup
from freya.storage.backup import (
    UNENCRYPTED_MIGRATION,
    BackupRefused,
    acknowledge_recovery_key,
    back_up,
    bundle_path_for,
    check,
    read_member,
    reveal_recovery_key,
    unlock_dek,
)
from freya.storage.crypto import (
    FILE_VERSION,
    KEY_LEN,
    BackupRefused as CryptoRefused,
    open_sealed,
    seal,
)
from freya.storage.custody import (
    BACKEND_DPAPI,
    BACKEND_KERNEL_KEYRING,
    BACKEND_SECRET_SERVICE,
    NoKeyStore,
    MemoryKeyStore,
    parse_recovery_key,
)
from freya.storage.restore import restore


WHEN = datetime(2026, 11, 14, 9, 0, 0)
PASSPHRASE = "made-up-passphrase-for-this-test"


@pytest.fixture
def stored(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    connection = sqlite3.connect(data / "freya_memory.db")
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("INSERT INTO notes (body) VALUES (?)", ("made-up note",))
    connection.commit()
    connection.close()
    (data / "freya_settings.json").write_text('{"voice_volume": 80}', encoding="utf-8")
    return data


def _bytes(folder):
    parts = []

    for path in folder.rglob("*"):
        if path.is_file():
            parts.append(path.read_bytes())

    return b"".join(parts)


def test_a_round_trip_is_not_sqlite_or_json_without_the_key(tmp_path, stored):
    store = MemoryKeyStore(BACKEND_DPAPI)
    lines = []
    folder = back_up(
        stored,
        tmp_path / "backups",
        WHEN,
        allow_same_drive=True,
        environ={},
        custody=store,
        recovery_lines=lines,
    )
    raw = _bytes(folder)

    assert b"SQLite format 3" not in raw
    assert b"made-up note" not in raw
    with pytest.raises((UnicodeDecodeError, json.JSONDecodeError, ValueError)):
        json.loads((folder / "manifest.json").read_bytes().decode("utf-8"))

    dek, _key_id = unlock_dek(folder, custody=store)
    assert dek not in raw
    assert lines and lines[0].encode("ascii") not in raw
    assert parse_recovery_key(lines[0]) not in raw
    assert check(folder, custody=store) == []

    restored = tmp_path / "restored"
    restore(folder, restored, environ={}, custody=store)
    assert (restored / "freya_settings.json").read_text(encoding="utf-8") == (
        '{"voice_volume": 80}'
    )
    copy = sqlite3.connect(restored / "freya_memory.db")

    try:
        assert copy.execute("SELECT body FROM notes").fetchone()[0] == "made-up note"
    finally:
        copy.close()


def test_a_changed_byte_fails_and_writes_no_plaintext(tmp_path, stored):
    store = MemoryKeyStore(BACKEND_SECRET_SERVICE)
    folder = back_up(
        stored,
        tmp_path / "backups",
        WHEN,
        allow_same_drive=True,
        environ={},
        custody=store,
        recovery_lines=[],
    )
    target = folder / "freya_settings.json"
    blob = bytearray(target.read_bytes())
    blob[-1] ^= 0x01
    target.write_bytes(blob)
    destination = tmp_path / "restored"
    destination.mkdir()
    (destination / "marker.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(BackupRefused, match="authentication"):
        restore(folder, destination, environ={}, custody=store)

    assert (destination / "marker.txt").read_text(encoding="utf-8") == "keep"
    assert list(destination.iterdir()) == [destination / "marker.txt"]

    for path in destination.rglob("*"):
        if path.is_file():
            assert b"made-up note" not in path.read_bytes()


def test_a_wrong_keystore_and_a_wrong_recovery_key_fail(tmp_path, stored):
    store = MemoryKeyStore(BACKEND_DPAPI)
    lines = []
    folder = back_up(
        stored,
        tmp_path / "backups",
        WHEN,
        allow_same_drive=True,
        environ={},
        custody=store,
        recovery_lines=lines,
    )
    other = MemoryKeyStore(BACKEND_KERNEL_KEYRING)

    with pytest.raises(BackupRefused, match="recovery key"):
        unlock_dek(folder, custody=other)

    wrong = format_other()

    with pytest.raises(BackupRefused, match="authentication") as refused:
        unlock_dek(folder, recovery_key=wrong)

    assert wrong not in str(refused.value)
    assert lines[0] not in str(refused.value)


def format_other():
    from freya.storage.custody import format_recovery_key

    return format_recovery_key(os.urandom(KEY_LEN))


def test_the_header_rejects_a_bad_version_magic_and_key_id():
    dek = os.urandom(KEY_LEN)
    key_id = os.urandom(16)
    sealed = bytearray(seal(b"plain", dek, key_id=key_id, name="freya_settings.json"))

    changed = bytearray(sealed)
    changed[8] = FILE_VERSION + 1

    with pytest.raises(CryptoRefused, match="format version"):
        open_sealed(bytes(changed), dek, key_id=key_id, name="freya_settings.json")

    changed = bytearray(sealed)
    changed[0] ^= 0x01

    with pytest.raises(CryptoRefused, match="not an encrypted"):
        open_sealed(bytes(changed), dek, key_id=key_id, name="freya_settings.json")

    changed = bytearray(sealed)
    changed[10] ^= 0x01

    with pytest.raises(CryptoRefused, match="does not match"):
        open_sealed(bytes(changed), dek, key_id=key_id, name="freya_settings.json")

    with pytest.raises(CryptoRefused, match="incomplete"):
        open_sealed(bytes(sealed[:20]), dek, key_id=key_id, name="freya_settings.json")

    with pytest.raises(CryptoRefused, match="authentication"):
        open_sealed(bytes(sealed), os.urandom(KEY_LEN), key_id=key_id, name="freya_settings.json")


def test_a_passphrase_opens_the_backup_and_is_not_stored(tmp_path, stored, monkeypatch):
    monkeypatch.setenv("FREYA_BACKUP_PASSPHRASE", "not-read")
    lines = []
    folder = back_up(
        stored,
        tmp_path / "backups",
        WHEN,
        allow_same_drive=True,
        environ={},
        custody=NoKeyStore(),
        passphrase=PASSPHRASE,
        recovery_lines=lines,
    )
    raw = _bytes(folder)
    bundle = bundle_path_for(stored, {}).read_bytes()

    assert PASSPHRASE.encode("utf-8") not in raw
    assert PASSPHRASE.encode("utf-8") not in bundle
    assert b"not-read" not in raw

    with pytest.raises(BackupRefused, match="authentication") as refused:
        unlock_dek(folder, passphrase="wrong-made-up-passphrase")

    assert "wrong-made-up-passphrase" not in str(refused.value)
    assert check(folder, passphrase=PASSPHRASE) == []

    restored = tmp_path / "restored"
    restore(
        folder,
        restored,
        environ={},
        custody=NoKeyStore(),
        passphrase=PASSPHRASE,
    )
    assert b"made-up note" in (restored / "freya_memory.db").read_bytes()


def test_no_keystore_and_no_passphrase_writes_nothing(tmp_path, stored):
    with pytest.raises(BackupRefused, match="Nothing was written"):
        back_up(
            stored,
            tmp_path / "backups",
            WHEN,
            allow_same_drive=True,
            environ={},
            custody=NoKeyStore(),
        )

    assert not (tmp_path / "backups").exists()
    assert not bundle_path_for(stored, {}).exists()


def test_the_recovery_key_opens_a_backup_made_on_another_store(tmp_path, stored):
    windows = MemoryKeyStore(BACKEND_DPAPI)
    lines = []
    folder = back_up(
        stored,
        tmp_path / "backups",
        WHEN,
        allow_same_drive=True,
        environ={},
        custody=windows,
        recovery_lines=lines,
    )
    text = reveal_recovery_key(stored, custody=windows, environ={})

    assert text == lines[0]
    bundle = bundle_path_for(stored, {}).read_bytes()
    assert text.encode("ascii") not in bundle
    assert parse_recovery_key(text) not in bundle
    acknowledge_recovery_key(stored, environ={})

    with pytest.raises(BackupRefused, match="already shown"):
        reveal_recovery_key(stored, custody=windows, environ={})

    restored = tmp_path / "restored"
    restore(
        folder,
        restored,
        environ={},
        custody=NoKeyStore(),
        recovery_key=text,
    )
    assert (restored / "freya_settings.json").read_text(encoding="utf-8").startswith("{")

    later = []
    back_up(
        stored,
        tmp_path / "backups",
        WHEN.replace(minute=5),
        allow_same_drive=True,
        environ={},
        custody=windows,
        recovery_lines=later,
    )
    assert later == []


def test_a_plaintext_backup_is_refused_unless_migration_is_asked_for(tmp_path):
    folder = tmp_path / "backup" / "2026-11-14_090000"
    folder.mkdir(parents=True)
    database = folder / "freya_settings.json"
    database.write_text('{"voice_volume": 1}', encoding="utf-8")
    payload = database.read_bytes()
    manifest = {
        "made": folder.name,
        "from": "scratch",
        "copied": [{
            "name": "freya_settings.json",
            "kind": "file",
            "bytes": len(payload),
            "sha256": __import__("hashlib").sha256(payload).hexdigest(),
        }],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(BackupRefused, match="not encrypted"):
        restore(folder, destination, environ={})

    assert list(destination.iterdir()) == []
    assert UNENCRYPTED_MIGRATION in check(folder)[0]

    restore(folder, destination, environ={}, allow_unencrypted=True)
    assert (destination / "freya_settings.json").read_text(encoding="utf-8") == (
        '{"voice_volume": 1}'
    )


def test_the_backup_command_help_names_the_migration_and_not_a_passphrase_flag(capsys):
    with pytest.raises(SystemExit) as caught:
        backup.main(["--help"])

    assert caught.value.code == 0
    text = capsys.readouterr().out
    assert "--allow-unencrypted" in text
    assert "--show-recovery-key" in text
    assert "--passphrase" not in text
