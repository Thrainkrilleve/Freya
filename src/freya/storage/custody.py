"""
Key custody for backup encryption.

One random data key seals a backup. This module wraps that key and
unwraps it. It does not choose a second store when the one it was
asked for is missing, and it never writes the data key, the recovery
key, or a passphrase as a plain file.

Backends, one per operating system, behind the same wrap and unwrap
methods so a test can stand in for any of them:

- Windows: DPAPI (``CryptProtectData`` / ``CryptUnprotectData``), which
  is how Credential Manager protects a secret for the current user. The
  wrapped bytes travel in the backup. There is no second key file.
- Linux: the Secret Service when a session offers it. Otherwise the
  kernel keyring, which lives in kernel memory and does not survive a
  reboot. Otherwise nothing: the caller must pass a passphrase.
- macOS: the Keychain. The item is marked not to sync off this Mac, and
  a prompt is a refusal rather than a wait.

``keyring``'s own default will pick a plaintext file when no system
store answers. That is the silent fallback this module does not have,
so the system stores are called directly and a missing one refuses.

A passphrase is stretched with argon2id at the OWASP Password Storage
Cheat Sheet's minimum Argon2id configuration: 19 MiB of memory, 2
iterations, parallelism 1 (``m=19456``, ``t=2``, ``p=1``). The same
numbers are used on every OS and are written into the wrap. Any other
set is refused before the stretch runs, so a tampered wrap cannot ask
for a huge allocation. The stretch is for a passphrase a person chose.
The recovery key is already 32 random bytes and is used as an AES-256
key directly.

The recovery key is shown once so it can be written down. It is not
logged. Until that showing, a sealed copy is kept in the local bundle
so it can still be printed; acknowledging it deletes the sealed copy.
The paper key is what remains, and it unwraps the data key on any OS.
"""

import os
import struct
import sys
from typing import Optional, Sequence, Tuple

from argon2.low_level import Type, hash_secret_raw

from freya.storage.crypto import (
    KEY_ID_LEN,
    KEY_LEN,
    METHOD_OS,
    METHOD_PASSPHRASE,
    METHOD_RECOVERY,
    NONCE_LEN,
    TAG_LEN,
    BackupRefused,
    Wrap,
    unwrap_with_kek,
    wrap_with_kek,
)


class CustodyUnavailable(BackupRefused):
    """The OS keystore cannot wrap or unwrap. Nothing is written in its place."""


# OWASP Password Storage Cheat Sheet, Argon2id minimum configuration.
# argon2-cffi takes memory in KiB: 19 MiB is 19 * 1024.
ARGON2_MEMORY_KIB = 19 * 1024
ARGON2_TIME_COST = 2
ARGON2_PARALLELISM = 1
ARGON2_SALT_LEN = 16
PASSPHRASE_PARAMS = (ARGON2_MEMORY_KIB, ARGON2_TIME_COST, ARGON2_PARALLELISM)

BACKEND_DPAPI = 1
BACKEND_SECRET_SERVICE = 2
BACKEND_KERNEL_KEYRING = 3
BACKEND_KEYCHAIN = 4

PURPOSE_DEK = b"dek"
PURPOSE_RECOVERY = b"recovery"
_PURPOSES = (PURPOSE_DEK, PURPOSE_RECOVERY)

# Production key name. Tests pass another name and delete it afterwards,
# so a test run does not replace the key Freya's own backups use.
PRODUCTION_ACCOUNT = "freya-backup-kek"
KEYCHAIN_SERVICE = "freya-backup"

# CryptProtectData flag: never open a Windows dialog.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

# keyctl command numbers, and the special keyring ids, from the kernel.
_KEYCTL_UNLINK = 9
_KEYCTL_READ = 11
_KEY_SPEC_SESSION_KEYRING = -3
_KEY_SPEC_USER_KEYRING = -4
_KEY_SPEC_USER_SESSION_KEYRING = -5
_KEYRINGS = (
    _KEY_SPEC_SESSION_KEYRING,
    _KEY_SPEC_USER_KEYRING,
    _KEY_SPEC_USER_SESSION_KEYRING,
)

# add_key, request_key, keyctl. A machine we do not know is not a store.
_SYSCALLS = {
    "x86_64": (248, 249, 250),
    "amd64": (248, 249, 250),
    "aarch64": (217, 218, 219),
    "arm64": (217, 218, 219),
    "i386": (286, 287, 288),
    "i686": (286, 287, 288),
}


def os_associated(backend_id: int, key_id: bytes, purpose: bytes) -> bytes:
    if purpose not in _PURPOSES or len(key_id) != KEY_ID_LEN:
        raise BackupRefused("The backup key is not usable.")

    return b"freya-os-wrap" + bytes([backend_id & 0xFF]) + bytes(key_id) + purpose


def passphrase_associated(key_id: bytes, purpose: bytes, salt: bytes) -> bytes:
    memory, time_cost, parallelism = PASSPHRASE_PARAMS

    if purpose not in _PURPOSES or len(key_id) != KEY_ID_LEN or len(salt) != ARGON2_SALT_LEN:
        raise BackupRefused("The backup key is not usable.")

    return (
        b"freya-pass-wrap"
        + bytes(key_id)
        + purpose
        + salt
        + struct.pack(">IIB", memory, time_cost, parallelism)
    )


def recovery_associated(key_id: bytes) -> bytes:
    if len(key_id) != KEY_ID_LEN:
        raise BackupRefused("The backup key is not usable.")

    return b"freya-recovery-wrap" + bytes(key_id)


def _check_passphrase(passphrase: str) -> None:
    if not isinstance(passphrase, str) or passphrase.strip() == "":
        raise BackupRefused("A passphrase is required.")

    if len(passphrase) > 1024:
        raise BackupRefused("That passphrase is too long.")


def derive_passphrase_kek(passphrase: str, salt: bytes, params: Tuple[int, int, int]) -> bytes:
    """
    Stretch a passphrase. Parameters other than the one documented set
    are refused before argon2 runs.
    """
    _check_passphrase(passphrase)

    if params != PASSPHRASE_PARAMS or len(salt) != ARGON2_SALT_LEN:
        raise BackupRefused("The backup names a passphrase setup this Freya does not use.")

    memory, time_cost, parallelism = params

    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory,
        parallelism=parallelism,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


def _passphrase_body(kek: bytes, payload: bytes, *, key_id: bytes, purpose: bytes, salt: bytes) -> bytes:
    memory, time_cost, parallelism = PASSPHRASE_PARAMS
    wrapped = wrap_with_kek(kek, payload, passphrase_associated(key_id, purpose, salt))

    return salt + struct.pack(">IIB", memory, time_cost, parallelism) + wrapped


def seal_with_passphrase(
    passphrase: str, dek: bytes, recovery_raw: bytes, *, key_id: bytes,
) -> Tuple[bytes, bytes]:
    """
    One stretch, two wraps: the data key, and the recovery key that is
    waiting to be shown. The passphrase is not in either body.
    """
    if len(dek) != KEY_LEN or len(recovery_raw) != KEY_LEN:
        raise BackupRefused("The backup key is not usable.")

    salt = os.urandom(ARGON2_SALT_LEN)
    kek = derive_passphrase_kek(passphrase, salt, PASSPHRASE_PARAMS)
    dek_body = _passphrase_body(kek, dek, key_id=key_id, purpose=PURPOSE_DEK, salt=salt)
    sealed = _passphrase_body(
        kek, recovery_raw, key_id=key_id, purpose=PURPOSE_RECOVERY, salt=salt,
    )

    return dek_body, sealed


def open_passphrase_body(passphrase: str, body: bytes, *, key_id: bytes, purpose: bytes) -> bytes:
    """Open one passphrase wrap. The parameters in the body are allowlisted first."""
    header = ARGON2_SALT_LEN + 4 + 4 + 1
    expect = header + NONCE_LEN + KEY_LEN + TAG_LEN

    if len(body) != expect:
        raise BackupRefused("The backup failed its authentication check.")

    salt = body[:ARGON2_SALT_LEN]
    memory, time_cost, parallelism = struct.unpack(">IIB", body[ARGON2_SALT_LEN:header])
    kek = derive_passphrase_kek(passphrase, salt, (memory, time_cost, parallelism))

    return unwrap_with_kek(kek, body[header:], passphrase_associated(key_id, purpose, salt))


def make_recovery_body(recovery_raw: bytes, dek: bytes, key_id: bytes) -> bytes:
    if len(recovery_raw) != KEY_LEN or len(dek) != KEY_LEN:
        raise BackupRefused("The backup key is not usable.")

    return wrap_with_kek(recovery_raw, dek, recovery_associated(key_id))


def open_recovery_body(recovery_raw: bytes, body: bytes, key_id: bytes) -> bytes:
    if len(recovery_raw) != KEY_LEN:
        raise BackupRefused("The recovery key is not valid.")

    return unwrap_with_kek(recovery_raw, body, recovery_associated(key_id))


def format_recovery_key(raw: bytes) -> str:
    """Groups of base32, for printing. Not a secret-storage format."""
    import base64

    if len(raw) != KEY_LEN:
        raise BackupRefused("The recovery key is not valid.")

    text = base64.b32encode(raw).decode("ascii").rstrip("=")

    return "-".join(text[i : i + 4] for i in range(0, len(text), 4))


def parse_recovery_key(text: str) -> bytes:
    """Read a printed recovery key. The message does not repeat it."""
    import base64

    if not isinstance(text, str):
        raise BackupRefused("The recovery key is not valid.")

    compact = "".join(text.split()).replace("-", "").upper()

    if compact == "" or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in compact):
        raise BackupRefused("The recovery key is not valid.")

    pad = "=" * ((8 - len(compact) % 8) % 8)

    try:
        raw = base64.b32decode(compact + pad, casefold=False)
    except Exception:
        raise BackupRefused("The recovery key is not valid.") from None

    if len(raw) != KEY_LEN:
        raise BackupRefused("The recovery key is not valid.")

    return raw


def prompt_passphrase(reader=None) -> str:
    """
    Ask twice on the machine the owner is at. The passphrase is not
    taken from the environment and not from a command argument.
    """
    import getpass

    ask = reader or getpass.getpass
    first = ask("Backup passphrase: ")
    second = ask("Repeat the passphrase: ")

    if not isinstance(first, str) or first != second or first.strip() == "":
        raise BackupRefused("The passphrase was not accepted.")

    _check_passphrase(first)

    return first


class NoKeyStore:
    """
    A machine with no OS keystore. Wrap and unwrap refuse. The caller
    passes a passphrase or the recovery key. This is not a store.
    """

    backend_id = 0

    def available(self) -> bool:
        return False

    def wrap(self, payload: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        raise CustodyUnavailable(
            "No keystore is available. A passphrase or the recovery key is required."
        )

    def unwrap(self, blob: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        raise CustodyUnavailable(
            "No keystore is available. A passphrase or the recovery key is required."
        )

    def discard(self) -> None:
        return None


class MemoryKeyStore:
    """
    An OS keystore stand-in. The key-encryption key stays in this object,
    which is the process, and the blob it returns does not contain the
    data key. ``present`` False is the refusal a missing store gives.
    """

    def __init__(self, backend_id: int):
        self.backend_id = backend_id
        self.present = True
        self._kek = None

    def available(self) -> bool:
        return self.present

    def wrap(self, payload: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        if not self.present:
            raise CustodyUnavailable("No keystore is available.")

        if self._kek is None:
            self._kek = os.urandom(KEY_LEN)

        return wrap_with_kek(
            self._kek, payload, os_associated(self.backend_id, key_id, purpose),
        )

    def unwrap(self, blob: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        if not self.present or self._kek is None:
            raise CustodyUnavailable("No keystore is available.")

        return unwrap_with_kek(self._kek, blob, os_associated(self.backend_id, key_id, purpose))

    def discard(self) -> None:
        self._kek = None


def choose_linux(secret, kernel):
    """
    Secret Service when it answers, otherwise the kernel keyring, otherwise
    a refusal. The order is the one ADR-015 names. It is not a fallback to
    a file, and a wrap records which of the two was used.
    """
    if secret.available():
        return secret

    if kernel.available():
        return kernel

    return NoKeyStore()


def select_backend(system: str, secret, kernel, windows, mac):
    """The one backend for ``system``. A different OS is not tried."""
    if system == "win32":
        return windows

    if system == "darwin":
        return mac

    if system == "linux":
        return choose_linux(secret, kernel)

    return NoKeyStore()


class WindowsDpapiBackend:
    """
    DPAPI for the current Windows user. The blob is the wrap and travels
    with the backup. Optional entropy binds it to the key id and the
    purpose, so a data-key blob does not open as the recovery key.
    """

    backend_id = BACKEND_DPAPI

    def __init__(self):
        self._works = None

    def available(self) -> bool:
        if sys.platform != "win32":
            return False

        if self._works is None:
            self._works = self._round_trip_probe()

        return self._works

    def _round_trip_probe(self) -> bool:
        try:
            sample = os.urandom(KEY_LEN)
            entropy = b"freya-dpapi-probe"
            blob = self._protect(sample, entropy)
            return self._unprotect(blob, entropy) == sample and sample not in blob
        except Exception:
            return False

    def wrap(self, payload: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        if not self.available():
            raise CustodyUnavailable("No keystore is available.")

        try:
            return self._protect(payload, os_associated(self.backend_id, key_id, purpose))
        except CustodyUnavailable:
            raise
        except Exception:
            raise CustodyUnavailable("The Windows keystore did not accept the key.") from None

    def unwrap(self, blob: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        if not self.available():
            raise CustodyUnavailable("No keystore is available.")

        try:
            return self._unprotect(blob, os_associated(self.backend_id, key_id, purpose))
        except CustodyUnavailable:
            raise
        except Exception:
            raise BackupRefused("The backup failed its authentication check.") from None

    def discard(self) -> None:
        return None

    def _protect(self, payload: bytes, entropy: bytes) -> bytes:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def as_blob(data: bytes):
            buffer = ctypes.create_string_buffer(data, len(data))
            return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

        incoming, in_buf = as_blob(bytes(payload))
        extra, extra_buf = as_blob(bytes(entropy))
        outgoing = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_wchar_p,
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        ok = crypt32.CryptProtectData(
            ctypes.byref(incoming),
            None,
            ctypes.byref(extra),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(outgoing),
        )
        del in_buf, extra_buf

        if not ok:
            raise CustodyUnavailable("The Windows keystore did not accept the key.")

        try:
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            kernel32.LocalFree(outgoing.pbData)

    def _unprotect(self, blob: bytes, entropy: bytes) -> bytes:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def as_blob(data: bytes):
            buffer = ctypes.create_string_buffer(data, len(data))
            return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

        incoming, in_buf = as_blob(bytes(blob))
        extra, extra_buf = as_blob(bytes(entropy))
        outgoing = DATA_BLOB()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(DATA_BLOB),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(DATA_BLOB),
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(incoming),
            None,
            ctypes.byref(extra),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(outgoing),
        )
        del in_buf, extra_buf

        if not ok:
            raise BackupRefused("The backup failed its authentication check.")

        try:
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            kernel32.LocalFree(outgoing.pbData)


class SecretServiceBackend:
    """
    The Secret Service holds the key-encryption key. A locked collection
    is a refusal: unlocking would prompt, and a prompt is not a store
    that quietly answered. The data key is wrapped locally under that
    key and is not what the service stores.
    """

    backend_id = BACKEND_SECRET_SERVICE

    def __init__(self, account: str = PRODUCTION_ACCOUNT):
        self.account = account

    def available(self) -> bool:
        if sys.platform != "linux":
            return False

        connection = self._connect()

        if connection is None:
            return False

        try:
            import secretstorage

            collection = secretstorage.get_default_collection(connection)

            return not collection.is_locked()
        except Exception:
            return False
        finally:
            self._close(connection)

    def wrap(self, payload: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        kek = self._load_or_create()

        return wrap_with_kek(kek, payload, os_associated(self.backend_id, key_id, purpose))

    def unwrap(self, blob: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        kek = self._load()

        return unwrap_with_kek(kek, blob, os_associated(self.backend_id, key_id, purpose))

    def discard(self) -> None:
        connection = self._connect()

        if connection is None:
            return

        try:
            import secretstorage

            collection = secretstorage.get_default_collection(connection)

            for item in collection.search_items(self._attributes()):
                item.delete()
        except Exception:
            return
        finally:
            self._close(connection)

    def _attributes(self):
        return {"application": "freya-backup", "account": self.account}

    def _connect(self):
        if sys.platform != "linux":
            return None

        try:
            import secretstorage

            return secretstorage.dbus_init()
        except Exception:
            return None

    def _close(self, connection) -> None:
        try:
            connection.close()
        except Exception:
            return

    def _items(self, connection):
        import secretstorage

        collection = secretstorage.get_default_collection(connection)

        if collection.is_locked():
            raise CustodyUnavailable("No keystore is available.")

        return collection, list(collection.search_items(self._attributes()))

    def _load(self) -> bytes:
        connection = self._connect()

        if connection is None:
            raise CustodyUnavailable("No keystore is available.")

        try:
            _collection, items = self._items(connection)

            if not items:
                raise CustodyUnavailable("No keystore is available.")

            secret = items[0].get_secret()
        except CustodyUnavailable:
            raise
        except Exception:
            raise CustodyUnavailable("The Linux secret service did not accept the key.") from None
        finally:
            self._close(connection)

        if not isinstance(secret, (bytes, bytearray)) or len(secret) != KEY_LEN:
            raise CustodyUnavailable("The Linux secret service did not accept the key.")

        return bytes(secret)

    def _load_or_create(self) -> bytes:
        connection = self._connect()

        if connection is None:
            raise CustodyUnavailable("No keystore is available.")

        try:
            collection, items = self._items(connection)

            if items:
                secret = items[0].get_secret()
            else:
                secret = os.urandom(KEY_LEN)
                collection.create_item(
                    "Freya backup key", self._attributes(), secret, replace=False,
                )
        except CustodyUnavailable:
            raise
        except Exception:
            raise CustodyUnavailable("The Linux secret service did not accept the key.") from None
        finally:
            self._close(connection)

        if not isinstance(secret, (bytes, bytearray)) or len(secret) != KEY_LEN:
            raise CustodyUnavailable("The Linux secret service did not accept the key.")

        return bytes(secret)


class KernelKeyringBackend:
    """
    The kernel keyring, for a Linux machine with no Secret Service
    session. The key-encryption key is in kernel memory. It is gone
    after a reboot, and the recovery key or a passphrase is what opens
    a backup then. Nothing is written to a file here.
    """

    backend_id = BACKEND_KERNEL_KEYRING

    def __init__(self, description: str = PRODUCTION_ACCOUNT):
        self.description = description
        self._ring = None
        self._numbers = None

    def available(self) -> bool:
        if not sys.platform.startswith("linux"):
            return False

        numbers = _SYSCALLS.get(os.uname().machine.lower())

        if numbers is None:
            return False

        self._numbers = numbers
        probe = self.description + ".probe." + os.urandom(4).hex()

        for ring in _KEYRINGS:
            serial = self._add(probe, os.urandom(KEY_LEN), ring)

            if serial >= 0:
                self._unlink(serial, ring)
                self._ring = ring
                return True

        return False

    def wrap(self, payload: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        kek = self._load_or_create()

        return wrap_with_kek(kek, payload, os_associated(self.backend_id, key_id, purpose))

    def unwrap(self, blob: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        kek = self._load()

        return unwrap_with_kek(kek, blob, os_associated(self.backend_id, key_id, purpose))

    def discard(self) -> None:
        if self._numbers is None or self._ring is None:
            if not self.available():
                return

        serial = self._request(self.description, self._ring)

        if serial >= 0:
            self._unlink(serial, self._ring)

    def _syscall(self, argtypes):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        call = libc.syscall
        call.restype = ctypes.c_long
        call.argtypes = argtypes

        return ctypes, call

    def _add(self, description: str, payload: bytes, ring: int) -> int:
        try:
            import ctypes

            _module, call = self._syscall([
                ctypes.c_long,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_long,
            ])
            buffer = ctypes.create_string_buffer(len(payload))
            ctypes.memmove(buffer, bytes(payload), len(payload))
            serial = call(
                self._numbers[0],
                b"user",
                description.encode("ascii"),
                ctypes.cast(buffer, ctypes.c_void_p),
                len(payload),
                ring,
            )
        except Exception:
            return -1

        return int(serial)

    def _request(self, description: str, ring: int) -> int:
        try:
            import ctypes

            _module, call = self._syscall([
                ctypes.c_long,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_char_p,
                ctypes.c_long,
            ])
            serial = call(
                self._numbers[1],
                b"user",
                description.encode("ascii"),
                None,
                ring,
            )
        except Exception:
            return -1

        return int(serial)

    def _read(self, serial: int) -> Optional[bytes]:
        try:
            import ctypes

            _module, call = self._syscall([
                ctypes.c_long,
                ctypes.c_long,
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_size_t,
            ])
            buffer = ctypes.create_string_buffer(256)
            size = call(
                self._numbers[2],
                _KEYCTL_READ,
                serial,
                ctypes.cast(buffer, ctypes.c_void_p),
                len(buffer),
            )
        except Exception:
            return None

        if int(size) != KEY_LEN:
            return None

        return buffer.raw[:KEY_LEN]

    def _unlink(self, serial: int, ring: int) -> None:
        try:
            import ctypes

            _module, call = self._syscall([
                ctypes.c_long,
                ctypes.c_long,
                ctypes.c_long,
                ctypes.c_long,
            ])
            call(self._numbers[2], _KEYCTL_UNLINK, serial, ring)
        except Exception:
            return

    def _load(self) -> bytes:
        if not self.available():
            raise CustodyUnavailable("No keystore is available.")

        serial = self._request(self.description, self._ring)

        if serial < 0:
            raise CustodyUnavailable("No keystore is available.")

        secret = self._read(serial)

        if secret is None:
            raise CustodyUnavailable("No keystore is available.")

        return secret

    def _load_or_create(self) -> bytes:
        if not self.available():
            raise CustodyUnavailable("No keystore is available.")

        serial = self._request(self.description, self._ring)

        if serial >= 0:
            secret = self._read(serial)

            if secret is None:
                raise CustodyUnavailable("No keystore is available.")

            return secret

        secret = os.urandom(KEY_LEN)
        created = self._add(self.description, secret, self._ring)

        if created < 0:
            raise CustodyUnavailable("The kernel keyring did not accept the key.")

        return secret


class MacKeychainBackend:
    """
    The login keychain. Items are kept on this Mac only, and a call that
    would put a prompt on screen is a refusal. The key-encryption key is
    what the keychain stores; the data key is wrapped locally under it.
    """

    backend_id = BACKEND_KEYCHAIN

    def __init__(self, account: str = PRODUCTION_ACCOUNT):
        self.account = account
        self._works = None

    def available(self) -> bool:
        if sys.platform != "darwin":
            return False

        if self._works is None:
            self._works = self._probe()

        return self._works

    def _probe(self) -> bool:
        probe = self.account + "-probe-" + os.urandom(4).hex()
        worked = False

        try:
            self._write(probe, os.urandom(KEY_LEN))
            worked = True
        except Exception:
            worked = False

        try:
            self._delete(probe)
        except Exception:
            return False

        return worked

    def wrap(self, payload: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        kek = self._load_or_create()

        return wrap_with_kek(kek, payload, os_associated(self.backend_id, key_id, purpose))

    def unwrap(self, blob: bytes, *, key_id: bytes, purpose: bytes = PURPOSE_DEK) -> bytes:
        kek = self._load()

        return unwrap_with_kek(kek, blob, os_associated(self.backend_id, key_id, purpose))

    def discard(self) -> None:
        try:
            self._delete(self.account)
        except Exception:
            return

    def _load(self) -> bytes:
        if not self.available():
            raise CustodyUnavailable("No keystore is available.")

        try:
            secret = self._read(self.account)
        except CustodyUnavailable:
            raise
        except Exception:
            raise CustodyUnavailable("The macOS keychain did not accept the key.") from None

        if secret is None or len(secret) != KEY_LEN:
            raise CustodyUnavailable("No keystore is available.")

        return secret

    def _load_or_create(self) -> bytes:
        if not self.available():
            raise CustodyUnavailable("No keystore is available.")

        try:
            found = self._read(self.account)

            if found is not None and len(found) == KEY_LEN:
                return found

            secret = os.urandom(KEY_LEN)
            self._write(self.account, secret)
        except CustodyUnavailable:
            raise
        except Exception:
            raise CustodyUnavailable("The macOS keychain did not accept the key.") from None

        return secret

    def _libs(self):
        import ctypes
        import ctypes.util

        security_path = ctypes.util.find_library("Security")
        foundation_path = ctypes.util.find_library("CoreFoundation")

        if not security_path or not foundation_path:
            raise CustodyUnavailable("No keystore is available.")

        security = ctypes.cdll.LoadLibrary(security_path)
        foundation = ctypes.cdll.LoadLibrary(foundation_path)

        return ctypes, security, foundation

    def _symbol(self, library, name: str):
        import ctypes

        return ctypes.c_void_p.in_dll(library, name)

    def _string(self, foundation, text: str):
        import ctypes

        foundation.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
        made = foundation.CFStringCreateWithCString(None, text.encode("utf-8"), 0x08000100)

        if not made:
            raise CustodyUnavailable("No keystore is available.")

        return made

    def _data(self, foundation, payload: bytes):
        import ctypes

        foundation.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        foundation.CFDataCreate.restype = ctypes.c_void_p
        buffer = ctypes.create_string_buffer(payload, len(payload))
        made = foundation.CFDataCreate(
            None, ctypes.cast(buffer, ctypes.c_void_p), len(payload),
        )

        if not made:
            raise CustodyUnavailable("No keystore is available.")

        return made, buffer

    def _dictionary(self, foundation, pairs):
        import ctypes

        class KeyCallbacks(ctypes.Structure):
            _fields_ = [
                ("version", ctypes.c_long),
                ("retain", ctypes.c_void_p),
                ("release", ctypes.c_void_p),
                ("copyDescription", ctypes.c_void_p),
                ("equal", ctypes.c_void_p),
                ("hash", ctypes.c_void_p),
            ]

        class ValueCallbacks(ctypes.Structure):
            _fields_ = [
                ("version", ctypes.c_long),
                ("retain", ctypes.c_void_p),
                ("release", ctypes.c_void_p),
                ("copyDescription", ctypes.c_void_p),
                ("equal", ctypes.c_void_p),
            ]

        count = len(pairs)
        keys = (ctypes.c_void_p * count)(*(key for key, _value in pairs))
        values = (ctypes.c_void_p * count)(*(value for _key, value in pairs))
        foundation.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_long,
            ctypes.POINTER(KeyCallbacks),
            ctypes.POINTER(ValueCallbacks),
        ]
        foundation.CFDictionaryCreate.restype = ctypes.c_void_p
        made = foundation.CFDictionaryCreate(
            None,
            keys,
            values,
            count,
            ctypes.byref(KeyCallbacks.in_dll(foundation, "kCFTypeDictionaryKeyCallBacks")),
            ctypes.byref(ValueCallbacks.in_dll(foundation, "kCFTypeDictionaryValueCallBacks")),
        )

        if not made:
            raise CustodyUnavailable("No keystore is available.")

        return made

    def _release(self, foundation, *refs) -> None:
        for ref in refs:
            if ref:
                foundation.CFRelease(ref)

    def _query(self, security, foundation, account: str, extra):
        service = self._string(foundation, KEYCHAIN_SERVICE)
        name = self._string(foundation, account)
        pairs = [
            (self._symbol(security, "kSecClass"), self._symbol(security, "kSecClassGenericPassword")),
            (self._symbol(security, "kSecAttrService"), service),
            (self._symbol(security, "kSecAttrAccount"), name),
            (
                self._symbol(security, "kSecUseAuthenticationUI"),
                self._symbol(security, "kSecUseAuthenticationUIFail"),
            ),
        ]
        owned = [service, name]

        for key_name, value in extra:
            pairs.append((self._symbol(security, key_name), value))

        query = self._dictionary(foundation, pairs)

        return query, owned

    def _status(self, code: int, writing: bool) -> None:
        if code == 0:
            return

        # errSecItemNotFound, errSecInteractionNotAllowed, errSecAuthFailed.
        if code in (-25300, -25308, -25293):
            if writing:
                raise CustodyUnavailable("No keystore is available.")

            return

        if writing:
            raise CustodyUnavailable("The macOS keychain did not accept the key.")

        raise BackupRefused("The backup failed its authentication check.")

    def _write(self, account: str, secret: bytes) -> None:
        ctypes, security, foundation = self._libs()
        payload, buffer = self._data(foundation, secret)
        query, owned = self._query(
            security,
            foundation,
            account,
            (
                ("kSecValueData", payload),
                ("kSecAttrAccessible", self._symbol(
                    security, "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly",
                )),
                ("kSecAttrSynchronizable", self._symbol(foundation, "kCFBooleanFalse")),
            ),
        )
        security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        security.SecItemAdd.restype = ctypes.c_int

        try:
            code = int(security.SecItemAdd(query, None))

            if code == -25299:
                self._delete(account)
                code = int(security.SecItemAdd(query, None))

            self._status(code, writing=True)
        finally:
            del buffer
            self._release(foundation, query, payload, *owned)

    def _read(self, account: str) -> Optional[bytes]:
        ctypes, security, foundation = self._libs()
        query, owned = self._query(
            security,
            foundation,
            account,
            (
                ("kSecReturnData", self._symbol(foundation, "kCFBooleanTrue")),
                ("kSecMatchLimit", self._symbol(security, "kSecMatchLimitOne")),
            ),
        )
        security.SecItemCopyMatching.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        security.SecItemCopyMatching.restype = ctypes.c_int
        result = ctypes.c_void_p()

        try:
            code = int(security.SecItemCopyMatching(query, ctypes.byref(result)))

            if code == -25300:
                return None

            self._status(code, writing=False)

            if not result:
                return None

            foundation.CFDataGetLength.argtypes = [ctypes.c_void_p]
            foundation.CFDataGetLength.restype = ctypes.c_long
            foundation.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
            foundation.CFDataGetBytePtr.restype = ctypes.c_void_p
            length = int(foundation.CFDataGetLength(result))
            pointer = foundation.CFDataGetBytePtr(result)

            if length < 0 or length > 256 or not pointer:
                raise CustodyUnavailable("The macOS keychain did not accept the key.")

            return ctypes.string_at(pointer, length)
        finally:
            self._release(foundation, query, result, *owned)

    def _delete(self, account: str) -> None:
        ctypes, security, foundation = self._libs()
        query, owned = self._query(security, foundation, account, ())
        security.SecItemDelete.argtypes = [ctypes.c_void_p]
        security.SecItemDelete.restype = ctypes.c_int

        try:
            code = int(security.SecItemDelete(query))

            if code not in (0, -25300):
                self._status(code, writing=True)
        finally:
            self._release(foundation, query, *owned)


def default_custody():
    """The backend for this operating system, or a refusal when it has none."""
    return select_backend(
        sys.platform,
        SecretServiceBackend(),
        KernelKeyringBackend(),
        WindowsDpapiBackend(),
        MacKeychainBackend(),
    )


def platform_backend(account: str):
    """
    The real backend, using ``account`` as its key name. Tests pass a
    throwaway name and call ``discard`` so nothing is left behind.
    """
    if sys.platform == "win32":
        return WindowsDpapiBackend()

    if sys.platform == "darwin":
        return MacKeychainBackend(account=account)

    if sys.platform.startswith("linux"):
        return choose_linux(
            SecretServiceBackend(account=account),
            KernelKeyringBackend(description=account),
        )

    return NoKeyStore()


def wrap_for(keys_wraps: Sequence[Wrap], method: int) -> Wrap:
    found = [item for item in keys_wraps if item.method == method]

    if len(found) != 1:
        raise BackupRefused("The backup key file is not usable.")

    return found[0]


def unwrap_os(wraps: Sequence[Wrap], custody, key_id: bytes, *, purpose: bytes) -> bytes:
    """Open the OS wrap for this custody's backend only. Another OS is a refusal."""
    if custody is None or not custody.available():
        raise CustodyUnavailable(
            "No keystore is available. A passphrase or the recovery key is required."
        )

    matches = [
        item
        for item in wraps
        if item.method == METHOD_OS and item.backend == custody.backend_id
    ]

    if len(matches) != 1:
        raise BackupRefused(
            "This machine's keystore does not hold the key for this backup. "
            "Use the recovery key."
        )

    opened = custody.unwrap(matches[0].body, key_id=key_id, purpose=purpose)

    if not isinstance(opened, (bytes, bytearray)) or len(opened) != KEY_LEN:
        raise BackupRefused("The backup failed its authentication check.")

    return bytes(opened)
