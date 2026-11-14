"""
Authenticated encryption for one backup file, and the key file beside it.

A backup file is not a database and not JSON. It starts with a fixed
header, and that header is the associated data for AES-256-GCM, so a
changed version, key id, or nonce fails the authentication check along
with a changed ciphertext. The data key is 32 bytes. It is not written
in this module's output except as ciphertext inside a wrap.

AES-256-GCM takes a 12-byte nonce. Each seal draws a new one. NIST's
guidance for random nonces stays comfortable well past the number of
files one data key will ever seal here (a backup holds a handful).

The whole plaintext is sealed before any byte is returned, and the
whole ciphertext is checked before any plaintext is returned. A caller
that writes the result only after that returns does not publish a
partial plaintext file.
"""

import hashlib
import os
import struct
from typing import List, Optional, Sequence, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class BackupRefused(RuntimeError):
    """The backup was not made, or could not be opened. The message says why."""


FILE_MAGIC = b"FREYAENC"
FILE_VERSION = 1
KEY_MAGIC = b"FREYAKEY"
KEY_VERSION = 1
BUNDLE_MAGIC = b"FREYABDL"
BUNDLE_VERSION = 1

NONCE_LEN = 12
TAG_LEN = 16
KEY_LEN = 32
KEY_ID_LEN = 16
HEADER_LEN = len(FILE_MAGIC) + 1 + KEY_ID_LEN + NONCE_LEN

# A member larger than this is refused before it is read in. Freya's
# databases are far smaller. The cap is so a damaged length cannot ask
# the process to hold a file it will never publish.
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
MAX_KEY_FILE_BYTES = 1024 * 1024

METHOD_OS = 1
METHOD_PASSPHRASE = 2
METHOD_RECOVERY = 3

# A file name in a backup is one of a fixed list, all short.
MAX_NAME_LEN = 128


class Wrap:
    """One wrapping of the data key. ``body`` is not the data key."""

    def __init__(self, method: int, backend: int, body: bytes):
        self.method = method
        self.backend = backend
        self.body = body


class KeyFile:
    """The wraps and the ciphertext sizes stored in ``keys.bin``."""

    def __init__(self, key_id: bytes, wraps: Sequence[Wrap], files: Sequence[Tuple[str, int]]):
        self.key_id = key_id
        self.wraps = list(wraps)
        self.files = list(files)


class BundleFile:
    """
    The local copy of those wraps, plus, until the owner has been shown
    it, a sealed copy of the recovery key. The sealed copy is not the
    recovery key in the clear, and it is not written into ``keys.bin``.
    """

    def __init__(
        self,
        key_id: bytes,
        acknowledged: bool,
        wraps: Sequence[Wrap],
        recovery_seal: Optional[Wrap],
    ):
        self.key_id = key_id
        self.acknowledged = acknowledged
        self.wraps = list(wraps)
        self.recovery_seal = recovery_seal


def _check_key(dek: bytes, key_id: bytes) -> None:
    if not isinstance(dek, (bytes, bytearray)) or len(dek) != KEY_LEN:
        raise BackupRefused("The backup key is not usable.")

    if not isinstance(key_id, (bytes, bytearray)) or len(key_id) != KEY_ID_LEN:
        raise BackupRefused("The backup key is not usable.")


def seal(plaintext: bytes, dek: bytes, *, key_id: bytes, name: str) -> bytes:
    """Return one complete encrypted file. The name is bound into the tag."""
    _check_key(dek, key_id)

    if not isinstance(plaintext, (bytes, bytearray)):
        raise BackupRefused("The backup key is not usable.")

    if not isinstance(name, str) or name == "" or len(name.encode("utf-8")) > MAX_NAME_LEN:
        raise BackupRefused("The backup names a file it should not.")

    nonce = os.urandom(NONCE_LEN)
    header = FILE_MAGIC + bytes([FILE_VERSION]) + bytes(key_id) + nonce
    associated = header + name.encode("utf-8")
    ciphertext = AESGCM(bytes(dek)).encrypt(nonce, bytes(plaintext), associated)

    return header + ciphertext


def header_info(blob: bytes) -> Tuple[int, bytes]:
    """
    The version and key id in a sealed file. This does not decrypt, and
    it does not say the ciphertext is genuine: only ``open_sealed`` does.
    """
    if len(blob) < HEADER_LEN + TAG_LEN:
        raise BackupRefused("The backup file is incomplete.")

    if blob[: len(FILE_MAGIC)] != FILE_MAGIC:
        raise BackupRefused("This is not an encrypted Freya backup.")

    version = blob[len(FILE_MAGIC)]

    if version != FILE_VERSION:
        raise BackupRefused(
            f"This backup uses format version {version}, "
            "which this Freya does not open."
        )

    key_id = bytes(blob[len(FILE_MAGIC) + 1 : len(FILE_MAGIC) + 1 + KEY_ID_LEN])

    return version, key_id


def open_sealed(blob: bytes, dek: bytes, *, key_id: bytes, name: str) -> bytes:
    """
    Return the full plaintext, or refuse. Nothing is written. A wrong
    key and a changed byte fail the same way: the check does not say
    which, and the message carries none of the file.
    """
    _check_key(dek, key_id)

    if not isinstance(name, str) or name == "":
        raise BackupRefused("The backup names a file it should not.")

    _version, found = header_info(blob)

    if found != bytes(key_id):
        raise BackupRefused("The backup header does not match its key.")

    header = blob[:HEADER_LEN]
    nonce = header[-NONCE_LEN:]
    associated = header + name.encode("utf-8")

    try:
        return AESGCM(bytes(dek)).decrypt(nonce, blob[HEADER_LEN:], associated)
    except Exception:
        raise BackupRefused("The backup failed its authentication check.") from None


def wrap_with_kek(kek: bytes, payload: bytes, associated: bytes) -> bytes:
    """Encrypt ``payload`` under a key-encryption key. The result is nonce and tag."""
    if not isinstance(kek, (bytes, bytearray)) or len(kek) != KEY_LEN:
        raise BackupRefused("The backup key is not usable.")

    nonce = os.urandom(NONCE_LEN)

    return nonce + AESGCM(bytes(kek)).encrypt(nonce, bytes(payload), associated)


def unwrap_with_kek(kek: bytes, blob: bytes, associated: bytes) -> bytes:
    """Open a wrap from ``wrap_with_kek``, or refuse."""
    if (
        not isinstance(kek, (bytes, bytearray))
        or len(kek) != KEY_LEN
        or len(blob) < NONCE_LEN + TAG_LEN
    ):
        raise BackupRefused("The backup failed its authentication check.")

    try:
        return AESGCM(bytes(kek)).decrypt(blob[:NONCE_LEN], blob[NONCE_LEN:], associated)
    except Exception:
        raise BackupRefused("The backup failed its authentication check.") from None


def _u16(value: int) -> bytes:
    if not isinstance(value, int) or value < 0 or value > 0xFFFF:
        raise BackupRefused("The backup key file is not usable.")

    return struct.pack(">H", value)


def _u32(value: int) -> bytes:
    if not isinstance(value, int) or value < 0 or value > 0xFFFFFFFF:
        raise BackupRefused("The backup key file is not usable.")

    return struct.pack(">I", value)


def _u64(value: int) -> bytes:
    if not isinstance(value, int) or value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise BackupRefused("The backup key file is not usable.")

    return struct.pack(">Q", value)


def _need(blob: bytes, offset: int, size: int) -> int:
    if offset < 0 or size < 0 or offset + size > len(blob):
        raise BackupRefused("The backup key file is incomplete.")

    return offset + size


def _wraps_out(wraps: Sequence[Wrap]) -> bytes:
    parts = [_u16(len(wraps))]

    for wrap in wraps:
        if wrap.method not in (METHOD_OS, METHOD_PASSPHRASE, METHOD_RECOVERY):
            raise BackupRefused("The backup key file is not usable.")

        if not isinstance(wrap.body, (bytes, bytearray)) or len(wrap.body) > MAX_KEY_FILE_BYTES:
            raise BackupRefused("The backup key file is not usable.")

        parts.append(bytes([wrap.method, wrap.backend & 0xFF]))
        parts.append(_u32(len(wrap.body)))
        parts.append(bytes(wrap.body))

    return b"".join(parts)


def _wraps_in(blob: bytes, offset: int) -> Tuple[List[Wrap], int]:
    offset = _need(blob, offset, 2)
    count = struct.unpack(">H", blob[offset - 2 : offset])[0]

    if count > 16:
        raise BackupRefused("The backup key file is not usable.")

    found = []

    for _ in range(count):
        offset = _need(blob, offset, 1 + 1 + 4)
        method = blob[offset - 6]
        backend = blob[offset - 5]
        length = struct.unpack(">I", blob[offset - 4 : offset])[0]

        if length > MAX_KEY_FILE_BYTES or method not in (
            METHOD_OS,
            METHOD_PASSPHRASE,
            METHOD_RECOVERY,
        ):
            raise BackupRefused("The backup key file is not usable.")

        offset = _need(blob, offset, length)
        found.append(Wrap(method, backend, bytes(blob[offset - length : offset])))

    return found, offset


def dump_keys(keys: KeyFile) -> bytes:
    """Serialize ``keys.bin``. The data key is not an argument and not in the result."""
    if len(keys.key_id) != KEY_ID_LEN:
        raise BackupRefused("The backup key is not usable.")

    if len(keys.files) > 64:
        raise BackupRefused("The backup key file is not usable.")

    parts = [KEY_MAGIC, bytes([KEY_VERSION]), bytes(keys.key_id), _wraps_out(keys.wraps)]
    parts.append(_u16(len(keys.files)))

    for name, size in keys.files:
        encoded = name.encode("utf-8")

        if name == "" or len(encoded) > MAX_NAME_LEN or size < 0:
            raise BackupRefused("The backup key file is not usable.")

        parts.append(_u16(len(encoded)))
        parts.append(encoded)
        parts.append(_u64(size))

    return b"".join(parts)


def load_keys(blob: bytes) -> KeyFile:
    """Read ``keys.bin``, or refuse. Extra trailing bytes are refused too."""
    if len(blob) > MAX_KEY_FILE_BYTES:
        raise BackupRefused("The backup key file is not usable.")

    if len(blob) < len(KEY_MAGIC) + 1 + KEY_ID_LEN + 2:
        raise BackupRefused("The backup key file is incomplete.")

    if blob[: len(KEY_MAGIC)] != KEY_MAGIC:
        raise BackupRefused("This is not an encrypted Freya backup.")

    version = blob[len(KEY_MAGIC)]

    if version != KEY_VERSION:
        raise BackupRefused(
            f"This backup uses format version {version}, "
            "which this Freya does not open."
        )

    offset = len(KEY_MAGIC) + 1
    key_id = bytes(blob[offset : offset + KEY_ID_LEN])
    offset += KEY_ID_LEN
    wraps, offset = _wraps_in(blob, offset)
    offset = _need(blob, offset, 2)
    count = struct.unpack(">H", blob[offset - 2 : offset])[0]

    if count > 64:
        raise BackupRefused("The backup key file is not usable.")

    files = []

    for _ in range(count):
        offset = _need(blob, offset, 2)
        length = struct.unpack(">H", blob[offset - 2 : offset])[0]

        if length == 0 or length > MAX_NAME_LEN:
            raise BackupRefused("The backup key file is not usable.")

        offset = _need(blob, offset, length)

        try:
            name = blob[offset - length : offset].decode("utf-8")
        except UnicodeDecodeError:
            raise BackupRefused("The backup key file is not usable.") from None

        offset = _need(blob, offset, 8)
        size = struct.unpack(">Q", blob[offset - 8 : offset])[0]
        files.append((name, size))

    if offset != len(blob):
        raise BackupRefused("The backup key file is not usable.")

    if not wraps:
        raise BackupRefused("The backup key file is not usable.")

    return KeyFile(key_id, wraps, files)


# Bit 0: a sealed recovery key is still in the bundle, waiting to be shown.
FLAG_RECOVERY_SEALED = 0x01


def dump_bundle(bundle: BundleFile) -> bytes:
    """Serialize the local bundle. The recovery key itself is not written."""
    if len(bundle.key_id) != KEY_ID_LEN:
        raise BackupRefused("The backup key is not usable.")

    flags = FLAG_RECOVERY_SEALED if bundle.recovery_seal is not None else 0

    if bundle.acknowledged and bundle.recovery_seal is not None:
        raise BackupRefused("The backup key file is not usable.")

    parts = [
        BUNDLE_MAGIC,
        bytes([BUNDLE_VERSION, flags]),
        bytes(bundle.key_id),
        _wraps_out(bundle.wraps),
    ]

    if bundle.recovery_seal is not None:
        parts.append(_wraps_out([bundle.recovery_seal]))

    blob = b"".join(parts)

    if len(blob) > MAX_KEY_FILE_BYTES:
        raise BackupRefused("The backup key file is not usable.")

    return blob


def load_bundle(blob: bytes) -> BundleFile:
    """Read a bundle, or refuse. A damaged bundle is not treated as empty."""
    if len(blob) > MAX_KEY_FILE_BYTES or len(blob) < len(BUNDLE_MAGIC) + 2 + KEY_ID_LEN:
        raise BackupRefused("The backup key file is incomplete.")

    if blob[: len(BUNDLE_MAGIC)] != BUNDLE_MAGIC:
        raise BackupRefused("This is not an encrypted Freya backup.")

    version = blob[len(BUNDLE_MAGIC)]

    if version != BUNDLE_VERSION:
        raise BackupRefused(
            f"This backup uses format version {version}, "
            "which this Freya does not open."
        )

    flags = blob[len(BUNDLE_MAGIC) + 1]
    offset = len(BUNDLE_MAGIC) + 2
    key_id = bytes(blob[offset : offset + KEY_ID_LEN])
    offset += KEY_ID_LEN
    wraps, offset = _wraps_in(blob, offset)
    seal = None

    if flags & FLAG_RECOVERY_SEALED:
        seals, offset = _wraps_in(blob, offset)

        if len(seals) != 1:
            raise BackupRefused("The backup key file is not usable.")

        seal = seals[0]

    if flags & ~FLAG_RECOVERY_SEALED:
        raise BackupRefused("The backup key file is not usable.")

    if offset != len(blob) or not wraps:
        raise BackupRefused("The backup key file is not usable.")

    return BundleFile(key_id, seal is None, wraps, seal)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
