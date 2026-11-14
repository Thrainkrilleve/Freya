"""
Key custody for backup encryption.

Fakes stand in for each operating system's store. The real store on
this runner either round-trips a throwaway key or refuses. A refusal
is the result, not a skipped test. Nothing here is a real passphrase
or a key anyone keeps.
"""

import os
import struct
import sys

import pytest

from freya.storage.crypto import KEY_LEN, BackupRefused
from freya.storage.custody import (
    ARGON2_MEMORY_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LEN,
    ARGON2_TIME_COST,
    BACKEND_DPAPI,
    BACKEND_KERNEL_KEYRING,
    BACKEND_KEYCHAIN,
    BACKEND_SECRET_SERVICE,
    KERNEL_KEYRING_VARIABLE,
    PASSPHRASE_PARAMS,
    PURPOSE_DEK,
    CustodyUnavailable,
    KernelKeyringBackend,
    MacKeychainBackend,
    MemoryKeyStore,
    NoKeyStore,
    SecretServiceBackend,
    WindowsDpapiBackend,
    choose_linux,
    default_custody,
    derive_passphrase_kek,
    format_recovery_key,
    kernel_keyring_opted_in,
    open_passphrase_body,
    params_acceptable,
    parse_recovery_key,
    platform_backend,
    prompt_passphrase,
    select_backend,
)


def test_each_os_fake_round_trips_and_hides_the_data_key():
    dek = os.urandom(KEY_LEN)
    key_id = os.urandom(16)

    for backend_id in (
        BACKEND_DPAPI,
        BACKEND_SECRET_SERVICE,
        BACKEND_KERNEL_KEYRING,
        BACKEND_KEYCHAIN,
    ):
        store = MemoryKeyStore(backend_id)
        blob = store.wrap(dek, key_id=key_id)
        assert store.unwrap(blob, key_id=key_id) == dek
        assert dek not in blob
        assert store.backend_id == backend_id


def test_each_os_fake_refuses_when_the_store_is_missing_and_writes_nothing(tmp_path):
    dek = os.urandom(KEY_LEN)
    key_id = os.urandom(16)

    for backend_id in (
        BACKEND_DPAPI,
        BACKEND_SECRET_SERVICE,
        BACKEND_KERNEL_KEYRING,
        BACKEND_KEYCHAIN,
    ):
        store = MemoryKeyStore(backend_id)
        store.present = False

        with pytest.raises(CustodyUnavailable):
            store.wrap(dek, key_id=key_id)

        assert list(tmp_path.iterdir()) == []


def test_a_different_fake_store_cannot_unwrap():
    dek = os.urandom(KEY_LEN)
    key_id = os.urandom(16)
    first = MemoryKeyStore(BACKEND_DPAPI)
    second = MemoryKeyStore(BACKEND_DPAPI)
    blob = first.wrap(dek, key_id=key_id)
    second.wrap(os.urandom(KEY_LEN), key_id=os.urandom(16))

    with pytest.raises(BackupRefused, match="authentication"):
        second.unwrap(blob, key_id=key_id)


def test_linux_uses_secret_service_before_the_kernel_keyring():
    secret = MemoryKeyStore(BACKEND_SECRET_SERVICE)
    kernel = MemoryKeyStore(BACKEND_KERNEL_KEYRING)

    assert choose_linux(secret, kernel) is secret


def test_linux_does_not_fall_back_to_the_kernel_keyring():
    secret = MemoryKeyStore(BACKEND_SECRET_SERVICE)
    secret.present = False
    kernel = MemoryKeyStore(BACKEND_KERNEL_KEYRING)
    chosen = choose_linux(secret, kernel)

    assert kernel.available()
    with pytest.raises(CustodyUnavailable, match="passphrase"):
        chosen.wrap(os.urandom(KEY_LEN), key_id=os.urandom(16))


def test_linux_uses_the_kernel_keyring_only_when_opted_in():
    secret = MemoryKeyStore(BACKEND_SECRET_SERVICE)
    secret.present = False
    kernel = MemoryKeyStore(BACKEND_KERNEL_KEYRING)

    assert choose_linux(secret, kernel, kernel_opt_in=True) is kernel
    assert choose_linux(secret, None, kernel_opt_in=True).available() is False


def test_linux_refuses_when_neither_keystore_is_present():
    secret = MemoryKeyStore(BACKEND_SECRET_SERVICE)
    kernel = MemoryKeyStore(BACKEND_KERNEL_KEYRING)
    secret.present = False
    kernel.present = False
    chosen = choose_linux(secret, kernel)

    with pytest.raises(CustodyUnavailable):
        chosen.wrap(os.urandom(KEY_LEN), key_id=os.urandom(16))


def test_selecting_an_os_does_not_substitute_another():
    secret = MemoryKeyStore(BACKEND_SECRET_SERVICE)
    kernel = MemoryKeyStore(BACKEND_KERNEL_KEYRING)
    windows = MemoryKeyStore(BACKEND_DPAPI)
    mac = MemoryKeyStore(BACKEND_KEYCHAIN)
    windows.present = False

    chosen = select_backend("win32", secret, kernel, windows, mac)

    assert chosen is windows

    with pytest.raises(CustodyUnavailable):
        chosen.wrap(os.urandom(KEY_LEN), key_id=os.urandom(16))

    assert select_backend("darwin", secret, kernel, windows, mac) is mac
    assert select_backend("linux", secret, kernel, windows, mac) is secret


def test_no_keystore_refuses_and_is_not_a_file(tmp_path):
    store = NoKeyStore()

    with pytest.raises(CustodyUnavailable, match="passphrase"):
        store.wrap(os.urandom(KEY_LEN), key_id=os.urandom(16))

    assert list(tmp_path.iterdir()) == []


def test_a_recovery_key_round_trips_its_printed_form():
    raw = os.urandom(KEY_LEN)
    text = format_recovery_key(raw)

    assert parse_recovery_key(text) == raw
    assert parse_recovery_key(text.lower()) == raw
    assert "-" in text
    assert raw not in text.encode("ascii")


def test_a_damaged_recovery_key_is_refused_without_being_repeated():
    with pytest.raises(BackupRefused, match="not valid") as refused:
        parse_recovery_key("this-is-not-a-key")

    assert "this-is-not-a-key" not in str(refused.value)


def test_passphrase_parameters_are_the_stronger_argon2id_profile():
    # RFC 9106's second recommendation is 64 MiB, t=3, p=4. This is stronger.
    assert PASSPHRASE_PARAMS == (256 * 1024, 3, 4)
    assert ARGON2_MEMORY_KIB == 262144
    assert ARGON2_TIME_COST == 3
    assert ARGON2_PARALLELISM == 4
    assert params_acceptable((256 * 1024, 3, 4))
    assert params_acceptable((512 * 1024, 4, 4))
    assert not params_acceptable((64 * 1024, 3, 4))
    assert not params_acceptable((256 * 1024, 2, 4))
    assert not params_acceptable((256 * 1024, 3, 1))
    salt = os.urandom(16)
    first = derive_passphrase_kek("a made-up passphrase", salt, PASSPHRASE_PARAMS)
    second = derive_passphrase_kek("a made-up passphrase", salt, PASSPHRASE_PARAMS)

    assert first == second and len(first) == KEY_LEN

    with pytest.raises(BackupRefused, match="setup"):
        derive_passphrase_kek("a made-up passphrase", salt, (19 * 1024, 2, 1))


def test_unwrap_rejects_a_new_wrap_below_the_argon2_floor():
    """A wrap that names a weaker stretch is refused before it is hashed."""
    salt = os.urandom(ARGON2_SALT_LEN)
    weak = (
        (19 * 1024, 2, 1),
        (64 * 1024, 3, 4),
        (256 * 1024, 2, 4),
        (256 * 1024, 3, 1),
        (2048 * 1024, 3, 4),
    )

    for memory, time_cost, parallelism in weak:
        body = (
            salt
            + struct.pack(">IIB", memory, time_cost, parallelism)
            + os.urandom(12 + KEY_LEN + 16)
        )

        with pytest.raises(BackupRefused, match="setup"):
            open_passphrase_body(
                "a made-up passphrase",
                body,
                key_id=os.urandom(16),
                purpose=PURPOSE_DEK,
            )


def test_the_kernel_keyring_opt_in_is_only_the_exact_value(monkeypatch):
    assert kernel_keyring_opted_in({}) is False
    assert kernel_keyring_opted_in({KERNEL_KEYRING_VARIABLE: "1"}) is True
    assert kernel_keyring_opted_in({KERNEL_KEYRING_VARIABLE: "true"}) is False
    assert kernel_keyring_opted_in({KERNEL_KEYRING_VARIABLE: "0"}) is False

    monkeypatch.setattr(SecretServiceBackend, "available", lambda self: False)
    monkeypatch.setattr(KernelKeyringBackend, "available", lambda self: True)
    plain = default_custody(environ={})
    opted = default_custody(environ={KERNEL_KEYRING_VARIABLE: "1"})

    if sys.platform.startswith("linux"):
        assert isinstance(plain, NoKeyStore)
        assert isinstance(opted, KernelKeyringBackend)
    else:
        assert not isinstance(plain, KernelKeyringBackend)
        assert not isinstance(opted, KernelKeyringBackend)
        assert type(plain) is type(opted)


def test_the_kernel_keyring_backend_round_trips_or_refuses_when_asked():
    """Session-only store. Missing means refuse. This is not a skip."""
    backend = KernelKeyringBackend(description="freya-test-" + os.urandom(4).hex())
    dek = os.urandom(KEY_LEN)
    key_id = os.urandom(16)

    try:
        if not backend.available():
            with pytest.raises(CustodyUnavailable):
                backend.wrap(dek, key_id=key_id)
            return

        blob = backend.wrap(dek, key_id=key_id)
        assert dek not in blob
        assert backend.unwrap(blob, key_id=key_id) == dek
    finally:
        backend.discard()


def test_a_passphrase_prompt_wants_the_same_answer_twice():
    answers = iter(["one-made-up-phrase", "one-made-up-phrase"])

    assert prompt_passphrase(lambda _prompt: next(answers)) == "one-made-up-phrase"

    mismatched = iter(["one-made-up-phrase", "another-made-up-phrase"])

    with pytest.raises(BackupRefused, match="not accepted"):
        prompt_passphrase(lambda _prompt: next(mismatched))


def test_foreign_backends_refuse_instead_of_being_skipped():
    """A store this operating system does not have is a refusal."""
    samples = []

    if sys.platform != "win32":
        samples.append(WindowsDpapiBackend())

    if sys.platform != "darwin":
        samples.append(MacKeychainBackend(account="freya-test-foreign"))

    if not sys.platform.startswith("linux"):
        samples.append(SecretServiceBackend(account="freya-test-foreign"))
        samples.append(KernelKeyringBackend(description="freya-test-foreign"))

    assert samples

    for backend in samples:
        assert backend.available() is False

        with pytest.raises(CustodyUnavailable):
            backend.wrap(os.urandom(KEY_LEN), key_id=os.urandom(16))


def test_this_runner_keystore_round_trips_or_refuses():
    """
    The real store, under a throwaway name. Missing means refuse.
    The name is deleted afterwards. This is not a skip.
    """
    account = "freya-test-" + os.urandom(4).hex()
    backend = platform_backend(account)
    dek = os.urandom(KEY_LEN)
    key_id = os.urandom(16)

    try:
        if not backend.available():
            with pytest.raises(CustodyUnavailable):
                backend.wrap(dek, key_id=key_id)
            return

        blob = backend.wrap(dek, key_id=key_id)
        assert dek not in blob
        assert backend.unwrap(blob, key_id=key_id) == dek

        with pytest.raises(BackupRefused):
            backend.unwrap(blob, key_id=os.urandom(16))
    finally:
        backend.discard()
