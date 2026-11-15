"""
Key rotation and revocation.

Every key in here is generated for the test and protects nothing. The
passphrases are made up. A recovery key is kept in a local variable so
the assertion can use it, and it is not written to a log or a file.
"""

import io
import os
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime

import pytest

from freya.memory.deletion_ledger import ledger_path_for
from freya.storage.crypto import METHOD_RECOVERY, BundleFile, Wrap, dump_bundle, load_bundle
from freya.storage.custody import (
    BACKEND_DPAPI,
    PASSPHRASE_PARAMS,
    MemoryKeyStore,
    NoKeyStore,
    format_recovery_key,
    make_recovery_body,
)
from freya.storage.dbkey import (
    StoreLocked,
    bind_memory_key,
    bundle_path_for,
    clear_unlock,
    current,
    ensure_unlocked,
    replace_bundle,
    require_unlocked,
    reveal_recovery_key,
)
from freya.storage.keys import (
    main,
    revoke,
    rollback_dek,
    rotate_dek,
    rotate_passphrase,
    rotate_recovery,
    set_rotation_hook,
    status,
)
from freya.storage.sqlite import LIVE_DATABASES, StoreError, connect, open_with


PASSPHRASE = "made-up-passphrase-for-this-test"
OTHER = "another-made-up-passphrase-for-this-test"
WRONG = "wrong-made-up-phrase"
MARKER = "rotation-marker-made-up-row"
WHEN = datetime(2026, 11, 15, 12, 0, 0)
ENVIRON = {}


@pytest.fixture(autouse=True)
def _clear_rotation_hook():
    set_rotation_hook(None)
    yield
    set_rotation_hook(None)


def _row(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    connection.execute("CREATE TABLE notes (body TEXT)")
    connection.execute("INSERT INTO notes (body) VALUES (?)", (body,))
    connection.commit()
    connection.close()


def _read(path):
    connection = connect(path)

    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        return connection.execute("SELECT body FROM notes").fetchone()[0]
    finally:
        connection.close()


def _absent(blob, *secrets):
    for secret in secrets:
        if not secret:
            continue

        encoded = secret if isinstance(secret, (bytes, bytearray)) else str(secret).encode("utf-8")
        assert encoded not in blob


def test_passphrase_rotation_keeps_the_database_and_rejects_a_wrong_one(tmp_path, caplog):
    data = tmp_path / "data"
    data.mkdir()
    clear_unlock()
    text = ensure_unlocked(data, custody=NoKeyStore(), passphrase=PASSPHRASE, environ=ENVIRON)
    path = data / "freya_memory.db"
    _row(path, MARKER)
    before = path.read_bytes()
    bundle = bundle_path_for(data, ENVIRON)
    wrapped = bundle.read_bytes()
    backup = tmp_path / "backups"
    caplog.set_level("DEBUG")

    with pytest.raises(StoreLocked, match="not accepted") as caught:
        rotate_dek(
            data,
            backup,
            custody=NoKeyStore(),
            passphrase=WRONG,
            recovery_key=text,
            allow_same_drive=True,
            environ=ENVIRON,
            now=WHEN,
        )

    assert WRONG not in str(caught.value)
    assert PASSPHRASE not in str(caught.value)
    assert text not in str(caught.value)
    assert path.read_bytes() == before
    assert bundle.read_bytes() == wrapped
    assert not backup.exists()
    assert not bundle.with_name(bundle.name + ".rotation").exists()

    with pytest.raises(StoreLocked, match="not accepted") as caught:
        rotate_passphrase(data, WRONG, OTHER, custody=NoKeyStore(), environ=ENVIRON)

    assert WRONG not in str(caught.value)
    assert path.read_bytes() == before
    assert bundle.read_bytes() == wrapped
    rotate_passphrase(data, PASSPHRASE, OTHER, custody=NoKeyStore(), environ=ENVIRON)
    assert path.read_bytes() == before
    assert not bundle.with_name(bundle.name + ".bak").exists()
    assert not bundle.with_name(bundle.name + ".tmp").exists()
    assert reveal_recovery_key(data, passphrase=OTHER, environ=ENVIRON) == text
    _absent(bundle.read_bytes(), PASSPHRASE, OTHER, WRONG, text, current().dek)
    report = status(data, ENVIRON)
    assert report["stretch"] == {
        "memory_kib": PASSPHRASE_PARAMS[0],
        "time": PASSPHRASE_PARAMS[1],
        "parallelism": PASSPHRASE_PARAMS[2],
    }
    assert report["stretch"]["memory_kib"] == 256 * 1024
    assert text not in str(report)
    assert current().dek.hex() not in str(report)
    clear_unlock()

    with pytest.raises(StoreLocked):
        ensure_unlocked(data, custody=NoKeyStore(), passphrase=PASSPHRASE, environ=ENVIRON)

    ensure_unlocked(data, custody=NoKeyStore(), passphrase=OTHER, environ=ENVIRON)
    assert _read(path) == MARKER
    revoke(data, "passphrase", custody=NoKeyStore(), recovery_key=text, environ=ENVIRON)
    assert path.read_bytes() == before
    assert "passphrase" not in status(data, ENVIRON)["wraps"]
    assert "recovery" in status(data, ENVIRON)["wraps"]

    with pytest.raises(StoreLocked, match="only unlock"):
        revoke(data, "recovery", custody=NoKeyStore(), recovery_key=text, environ=ENVIRON)

    assert "recovery" in status(data, ENVIRON)["wraps"]
    assert path.read_bytes() == before
    _absent(caplog.text.encode("utf-8"), PASSPHRASE, OTHER, WRONG, text, current().dek)


def test_recovery_rotation_invalidates_the_old_key_and_is_not_logged(tmp_path, caplog):
    data = tmp_path / "data"
    data.mkdir()
    clear_unlock()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = ensure_unlocked(data, custody=custody, environ=ENVIRON)
    path = data / "freya_control.db"
    _row(path, MARKER)
    before = path.read_bytes()
    caplog.set_level("DEBUG")
    fresh = rotate_recovery(
        data, custody=custody, recovery_key=text, environ=ENVIRON,
    )
    bundle = bundle_path_for(data, ENVIRON)
    assert fresh != text
    assert path.read_bytes() == before
    assert not bundle.with_name(bundle.name + ".bak").exists()
    _absent(bundle.read_bytes(), text, fresh, current().dek)
    _absent(caplog.text.encode("utf-8"), text, fresh, current().dek)
    clear_unlock()

    with pytest.raises(StoreLocked):
        ensure_unlocked(data, custody=custody, recovery_key=text, environ=ENVIRON)

    ensure_unlocked(data, custody=custody, recovery_key=fresh, environ=ENVIRON)
    assert _read(path) == MARKER
    report = status(data, ENVIRON)
    assert report["recovery_shown"] is False
    assert "recovery" in report["wraps"]
    assert fresh not in str(report)


def test_dek_rotation_reencrypts_every_store_and_wipes_the_old_key(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    clear_unlock()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = ensure_unlocked(data, custody=custody, environ=ENVIRON)
    held = require_unlocked()
    old = bytes(held.dek)
    paths = []

    for name in LIVE_DATABASES:
        path = data / name
        _row(path, MARKER + name)
        paths.append(path)

    ledger = ledger_path_for(data, ENVIRON)
    _row(ledger, MARKER + "ledger")
    paths.append(ledger)
    backup = tmp_path / "backups"
    rotate_dek(
        data,
        backup,
        custody=custody,
        recovery_key=text,
        allow_same_drive=True,
        environ=ENVIRON,
        now=WHEN,
    )
    assert held.dek == b""
    assert require_unlocked().dek != old
    assert not bundle_path_for(data, ENVIRON).with_name("keys.bin.rotation").exists()
    folder = backup / "2026-11-15_120000"
    assert folder.is_dir()

    for path in paths:
        assert MARKER.encode("utf-8") not in path.read_bytes()
        assert old not in path.read_bytes()
        assert require_unlocked().dek not in path.read_bytes()

        with pytest.raises(StoreLocked):
            open_with(path, old)

        assert _read(path).startswith(MARKER)

    clear_unlock()
    ensure_unlocked(data, custody=custody, recovery_key=text, environ=ENVIRON)
    assert _read(paths[0]).startswith(MARKER)
    report = status(data, ENVIRON)
    assert text not in str(report)
    assert require_unlocked().dek.hex() not in str(report)
    assert report["rotation"] is None


def test_a_crash_while_copying_resumes_and_a_crash_while_swapping_rolls_back(tmp_path):
    root = tmp_path / "copy"
    data = root / "data"
    data.mkdir(parents=True)
    clear_unlock()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = ensure_unlocked(data, custody=custody, environ=ENVIRON)
    old = bytes(require_unlocked().dek)
    first = data / "freya_memory.db"
    second = data / "freya_control.db"
    _row(first, MARKER)
    _row(second, MARKER)
    originals = {first: first.read_bytes(), second: second.read_bytes()}
    backup = root / "backups"

    def stop_copy(kind, _path):
        if kind == "copied":
            raise RuntimeError("stop")

    set_rotation_hook(stop_copy)

    with pytest.raises(RuntimeError, match="^stop$"):
        rotate_dek(
            data,
            backup,
            custody=custody,
            recovery_key=text,
            allow_same_drive=True,
            environ=ENVIRON,
            now=WHEN,
        )

    set_rotation_hook(None)
    journal = bundle_path_for(data, ENVIRON).with_name("keys.bin.rotation")
    assert journal.is_file()
    recorded = journal.read_bytes()
    _absent(recorded, text, old, MARKER)
    assert b"stop" not in recorded
    assert first.read_bytes() == originals[first]
    assert first.with_name(first.name + ".rekey-new").is_file()
    assert _read(first) == MARKER
    assert (backup / "2026-11-15_120000").is_dir()
    rotate_dek(
        data,
        backup,
        custody=custody,
        recovery_key=text,
        allow_same_drive=True,
        environ=ENVIRON,
        now=WHEN,
    )
    assert not journal.exists()
    assert not first.with_name(first.name + ".rekey-new").exists()

    with pytest.raises(StoreLocked):
        open_with(first, old)

    assert _read(first) == MARKER
    assert _read(second) == MARKER

    # A second data directory, stopped after the first store is swapped.
    # Its key bundle lives beside its own parent, not beside the first.
    other_root = tmp_path / "swap"
    other = other_root / "data"
    other.mkdir(parents=True)
    clear_unlock()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = ensure_unlocked(other, custody=custody, environ=ENVIRON)
    old = bytes(require_unlocked().dek)
    first = other / "freya_memory.db"
    second = other / "freya_control.db"
    _row(first, MARKER)
    _row(second, MARKER)

    def stop_swap(kind, _path):
        if kind == "swapped":
            raise RuntimeError("stop")

    set_rotation_hook(stop_swap)

    with pytest.raises(RuntimeError, match="^stop$"):
        rotate_dek(
            other,
            other_root / "backups",
            custody=custody,
            recovery_key=text,
            allow_same_drive=True,
            environ=ENVIRON,
            now=WHEN.replace(minute=5),
        )

    set_rotation_hook(None)
    journal = bundle_path_for(other, ENVIRON).with_name("keys.bin.rotation")
    assert journal.is_file()
    _absent(journal.read_bytes(), text, old)
    rollback_dek(other, ENVIRON)
    assert not journal.exists()

    for path in (first, second):
        assert not path.with_name(path.name + ".rekey-new").exists()
        assert not path.with_name(path.name + ".rekey-old").exists()
        assert not path.with_name(path.name + ".rollback").exists()
        assert _read(path) == MARKER

    with pytest.raises(StoreError, match="no rotation"):
        rollback_dek(other, ENVIRON)


def test_revoking_the_last_unlock_writes_nothing(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    clear_unlock()
    dek = os.urandom(32)
    key_id = os.urandom(16)
    raw = os.urandom(32)
    bundle = BundleFile(
        key_id,
        True,
        [Wrap(METHOD_RECOVERY, 0, make_recovery_body(raw, dek, key_id))],
        None,
    )
    path = bundle_path_for(data, ENVIRON)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_bundle(bundle))
    before = path.read_bytes()
    bind_memory_key(dek, key_id)
    text = format_recovery_key(raw)

    with pytest.raises(StoreLocked, match="only unlock") as caught:
        revoke(data, "recovery", recovery_key=text, environ=ENVIRON)

    assert text not in str(caught.value)
    assert dek not in str(caught.value).encode("utf-8")
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".tmp").exists()


def test_revoking_one_wrap_leaves_the_other(tmp_path):
    data = tmp_path / "nested" / "data"
    data.mkdir(parents=True)
    clear_unlock()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = ensure_unlocked(data, custody=custody, environ=ENVIRON)
    path = data / "freya_memory.db"
    _row(path, MARKER)
    before = path.read_bytes()
    bundle = bundle_path_for(data, ENVIRON)
    wrapped = bundle.read_bytes()

    with pytest.raises(StoreLocked, match="not on the database key"):
        revoke(data, "passphrase", custody=custody, environ=ENVIRON)

    assert bundle.read_bytes() == wrapped
    other = MemoryKeyStore(BACKEND_DPAPI)

    with pytest.raises(StoreLocked, match="not accepted"):
        revoke(data, "recovery", custody=other, recovery_key=text, environ=ENVIRON)

    assert bundle.read_bytes() == wrapped
    revoke(data, "recovery", custody=custody, environ=ENVIRON)
    assert path.read_bytes() == before
    assert status(data, ENVIRON)["wraps"] == ["os"]
    _absent(bundle.read_bytes(), text, current().dek)
    clear_unlock()

    with pytest.raises(StoreLocked):
        ensure_unlocked(data, custody=custody, recovery_key=text, environ=ENVIRON)

    ensure_unlocked(data, custody=custody, environ=ENVIRON)
    assert _read(path) == MARKER


def test_a_failed_bundle_replace_restores_the_previous_bytes(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    keys = tmp_path / "outside" / "keys.bin"
    environ = {"FREYA_DB_KEYS": str(keys)}
    monkeypatch.setenv("FREYA_DB_KEYS", str(keys))
    clear_unlock()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = ensure_unlocked(data, custody=custody, environ=environ)
    original = keys.read_bytes()
    _absent(original, text, current().dek)

    def refuse():
        raise StoreLocked("The new wraps did not open. The previous wraps were kept.")

    with pytest.raises(StoreLocked, match="did not open"):
        replace_bundle(keys, load_bundle(original), check=refuse)

    assert keys.read_bytes() == original
    assert not keys.with_name("keys.bin.tmp").exists()
    assert not keys.with_name("keys.bin.bak.tmp").exists()
    output = io.StringIO()
    errors = io.StringIO()

    with redirect_stdout(output), redirect_stderr(errors):
        code = main(["--data", str(data), "status"])

    report = status(data, environ)
    rendered = output.getvalue() + errors.getvalue() + str(report)
    assert code == 0
    assert text not in rendered
    assert current().dek.hex() not in rendered
    assert report["key_id"]
    assert "os" in report["wraps"]
    assert "key id: " in output.getvalue()
    assert "rotation: none" in output.getvalue()
