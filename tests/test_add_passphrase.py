"""
Adding a passphrase wrap beside an existing OS wrap.

Every key in here is generated for the test. The passphrases are made up.
A recovery key stays in a local variable for the assertion and is not
written to a file.
"""

import sys

import pytest

from freya.storage.custody import (
    BACKEND_DPAPI,
    MIN_PASSPHRASE_LENGTH,
    PASSPHRASE_PARAMS,
    MemoryKeyStore,
    NoKeyStore,
)
from freya.storage.dbkey import (
    StoreLocked,
    bundle_path_for,
    clear_unlock,
    current,
    ensure_unlocked,
)
from freya.storage.encrypt import main as encrypt_main
from freya.storage.keys import add_passphrase, main, set_rotation_hook, status


PASSPHRASE = "made-up-passphrase-for-this-test"
OTHER = "another-made-up-passphrase-for-this-test"
WRONG = "wrong-made-up-phrase-value"
ENVIRON = {}


@pytest.fixture(autouse=True)
def _clear_rotation_hook():
    set_rotation_hook(None)
    yield
    set_rotation_hook(None)
    clear_unlock()


def _open(data, custody):
    clear_unlock()
    text = ensure_unlocked(data, custody=custody, environ=ENVIRON)
    return text


def test_add_passphrase_keeps_the_os_wrap_and_the_recovery_key(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = _open(data, custody)

    add_passphrase(data, PASSPHRASE, custody=custody, environ=ENVIRON)

    report = status(data, ENVIRON)
    assert report["wraps"] == ["os", "recovery", "passphrase"]
    assert report["stretch"] == {
        "memory_kib": PASSPHRASE_PARAMS[0],
        "time": PASSPHRASE_PARAMS[1],
        "parallelism": PASSPHRASE_PARAMS[2],
    }
    assert PASSPHRASE not in str(report)
    assert text not in str(report)
    bundle = bundle_path_for(data, ENVIRON).read_bytes()
    assert PASSPHRASE.encode() not in bundle
    assert text.encode() not in bundle

    clear_unlock()
    ensure_unlocked(data, custody=NoKeyStore(), recovery_key=text, environ=ENVIRON)
    assert current() is not None

    clear_unlock()
    ensure_unlocked(data, custody=custody, environ=ENVIRON)
    assert current() is not None


def test_a_second_add_is_refused(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    _open(data, custody)
    add_passphrase(data, PASSPHRASE, custody=custody, environ=ENVIRON)
    before = bundle_path_for(data, ENVIRON).read_bytes()

    with pytest.raises(StoreLocked, match="rotate-passphrase"):
        add_passphrase(data, OTHER, custody=custody, environ=ENVIRON)

    assert bundle_path_for(data, ENVIRON).read_bytes() == before


def test_a_wrong_passphrase_does_not_fall_back_to_the_keystore(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    _open(data, custody)
    add_passphrase(data, PASSPHRASE, custody=custody, environ=ENVIRON)
    clear_unlock()

    with pytest.raises(StoreLocked):
        ensure_unlocked(data, custody=custody, passphrase=WRONG, environ=ENVIRON)

    assert current() is None
    ensure_unlocked(
        data,
        custody=custody,
        environ={**ENVIRON, "FREYA_REQUIRE_PASSPHRASE": "1"},
        passphrase=PASSPHRASE,
    )
    assert current() is not None
    clear_unlock()

    with pytest.raises(StoreLocked, match="A passphrase is required"):
        ensure_unlocked(
            data,
            custody=custody,
            environ={**ENVIRON, "FREYA_REQUIRE_PASSPHRASE": "1"},
            interactive=False,
        )

    assert current() is None


def test_a_crash_while_adding_resumes_without_recording_the_passphrase(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    text = _open(data, custody)

    def stop(kind, _path):
        if kind == "add-prepared":
            raise RuntimeError("stop")

    set_rotation_hook(stop)

    with pytest.raises(RuntimeError, match="^stop$"):
        add_passphrase(data, PASSPHRASE, custody=custody, environ=ENVIRON)

    set_rotation_hook(None)
    journal = bundle_path_for(data, ENVIRON).with_name("keys.bin.rotation")
    recorded = journal.read_bytes()
    assert b"add-passphrase" in recorded
    assert PASSPHRASE.encode() not in recorded
    assert text.encode() not in recorded
    assert "passphrase" not in status(data, ENVIRON)["wraps"]

    add_passphrase(data, PASSPHRASE, custody=custody, environ=ENVIRON)

    assert not journal.exists()
    assert "passphrase" in status(data, ENVIRON)["wraps"]
    clear_unlock()
    ensure_unlocked(data, custody=NoKeyStore(), recovery_key=text, environ=ENVIRON)
    assert current() is not None


def test_a_short_passphrase_is_refused_before_a_wrap_is_written(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    _open(data, custody)
    before = bundle_path_for(data, ENVIRON).read_bytes()

    with pytest.raises(StoreLocked, match="too short"):
        add_passphrase(data, "x" * (MIN_PASSPHRASE_LENGTH - 1), custody=custody, environ=ENVIRON)

    assert bundle_path_for(data, ENVIRON).read_bytes() == before
    assert not bundle_path_for(data, ENVIRON).with_name("keys.bin.rotation").exists()


def test_the_command_asks_on_a_terminal_and_not_from_an_argument(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    custody = MemoryKeyStore(BACKEND_DPAPI)
    _open(data, custody)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert main(["--data", str(data), "add-passphrase"]) == 1

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("freya.storage.dbkey.default_custody", lambda environ=None: custody)
    monkeypatch.setattr("freya.storage.keys.default_custody", lambda environ=None: custody)
    asked = []

    def ask(label="Database passphrase"):
        asked.append(label)
        return PASSPHRASE

    monkeypatch.setattr("freya.storage.keys.prompt_passphrase", ask)
    monkeypatch.setenv("FREYA_DB_PASSPHRASE", WRONG)

    assert main(["--data", str(data), "add-passphrase"]) == 0
    assert asked == ["Database passphrase"]
    assert "passphrase" in status(data, ENVIRON)["wraps"]

    with pytest.raises(SystemExit):
        main(["--data", str(data), "add-passphrase", PASSPHRASE])


def test_a_new_key_can_include_a_passphrase_and_the_default_does_not(tmp_path, monkeypatch):
    clear_unlock()
    data = tmp_path / "plain" / "data"
    other = tmp_path / "with" / "data"
    data.mkdir(parents=True)
    other.mkdir(parents=True)
    custody = MemoryKeyStore(BACKEND_DPAPI)
    monkeypatch.setattr("freya.storage.dbkey.default_custody", lambda environ=None: custody)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompted = []

    def ask(label="Database passphrase"):
        prompted.append(label)
        return PASSPHRASE

    monkeypatch.setattr("freya.storage.encrypt.prompt_passphrase", ask)

    assert encrypt_main(["--data", str(data)]) == 0
    assert prompted == []
    assert "passphrase" not in status(data, ENVIRON)["wraps"]

    clear_unlock()
    assert encrypt_main(["--data", str(other), "--with-passphrase"]) == 0
    assert prompted == ["Database passphrase"]
    assert status(other, ENVIRON)["wraps"] == ["os", "passphrase", "recovery"]
