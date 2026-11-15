"""
The data key for Freya's live databases.

One 32-byte key encrypts every live store. It is wrapped with the same
KeyCustody, passphrase, and recovery key as a backup (``storage/custody.py``).
There is no second wrapping scheme, and the key is never written as a
plain file beside the data.

The wraps live outside the data directory, in the same kind of bundle a
backup uses. ``FREYA_DB_KEYS`` names that file. A path inside the data
directory is refused. The bundle holds wraps only. The data key stays
in this process after a successful unlock, and nowhere else.

No keystore and no passphrase is a refusal. A locked process does not
open a store, and it does not create a plaintext one. A wrong passphrase
does not try the recovery key, and a wrong recovery key does not try
the passphrase.

Rotation and revocation are ``storage/keys.py``. They replace wraps
in this bundle. A data-key rotation also re-encrypts each store. The
old data key is wiped from the process after that rotation commits.
"""

import os
import threading
from pathlib import Path
from typing import Optional

from freya.storage.crypto import (
    MAX_KEY_FILE_BYTES,
    BackupRefused,
    dump_bundle,
    load_bundle,
)
from freya.storage.custody import (
    default_custody,
    new_data_key,
    prompt_passphrase,
    unwrap_data_key,
)


class StoreLocked(RuntimeError):
    """
    A live database was not opened.

    The message says why. It does not contain a key, a passphrase, or
    a row. Nothing was written in place of the missing unlock.
    """


# The bundle is never inside the data directory. A copy of that
# directory must not be enough to unwrap the databases.
KEYS_DIRECTORY = "freya_db_keys"
KEYS_FILE = "keys.bin"
KEYS_VARIABLE = "FREYA_DB_KEYS"

_lock = threading.Lock()
_unlocked = None


class Unlocked:
    """The data key for this process. It is not a file."""

    def __init__(self, dek: bytes, key_id: bytes):
        self._dek = bytearray(dek)
        self.key_id = bytes(key_id)

    @property
    def dek(self) -> bytes:
        return bytes(self._dek)

    def wipe(self) -> None:
        """Overwrite the in-memory data key. The wraps on disk are left."""
        for index in range(len(self._dek)):
            self._dek[index] = 0

        self._dek = bytearray()

    def __repr__(self) -> str:
        return "Unlocked(key_id=" + self.key_id.hex() + ")"


def bundle_path_for(data_dir, environ=None) -> Path:
    """
    Where the wrapped database key for ``data_dir`` is kept.

    Never inside that folder. ``FREYA_DB_KEYS`` names the file.
    """
    environ = os.environ if environ is None else environ
    data = Path(data_dir).resolve()
    named = str(environ.get(KEYS_VARIABLE, "") or "").strip()

    if named:
        path = Path(named).expanduser().resolve()
    else:
        path = (data.parent / KEYS_DIRECTORY / KEYS_FILE).resolve()

    if path == data or data in path.parents:
        raise StoreLocked("Database keys must be kept outside the data folder.")

    return path


def current() -> Optional[Unlocked]:
    """The unlock held in this process, or None when the stores are locked."""
    with _lock:
        return _unlocked


def require_unlocked() -> Unlocked:
    """The unlock, or a refusal that opens nothing."""
    found = current()

    if found is None:
        raise StoreLocked(
            "Freya's databases are locked. Unlock them with the passphrase "
            "or the recovery key. Nothing was opened."
        )

    return found


def clear_unlock() -> None:
    """Drop the in-memory key. The wraps on disk are left as they are."""
    global _unlocked

    with _lock:
        _unlocked = None


def bind_memory_key(dek: Optional[bytes] = None, key_id: Optional[bytes] = None) -> Unlocked:
    """
    Hold a throwaway key in this process and write nothing.

    Tests and the scratch restore drill use this so they do not touch
    the machine's keystore and do not leave a key file. It is not a
    fallback for a missing keystore: ``ensure_unlocked`` never calls it.
    """
    global _unlocked

    if dek is None:
        dek = os.urandom(32)

    if key_id is None:
        key_id = os.urandom(16)

    if len(dek) != 32 or len(key_id) != 16:
        raise StoreLocked("The database key is not usable.")

    held = Unlocked(dek, key_id)

    with _lock:
        _unlocked = held

    return held


def _read_bundle(path: Path):
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise StoreLocked("The database key could not be read.") from exc

    if len(blob) > MAX_KEY_FILE_BYTES:
        raise StoreLocked("The database key could not be read.")

    try:
        return load_bundle(blob)
    except BackupRefused as exc:
        raise StoreLocked("The database key could not be read.") from exc


def _fsync_write(path: Path, blob: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC

    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    descriptor = os.open(path, flags, 0o600)
    view = memoryview(blob)

    try:
        while view:
            written = os.write(descriptor, view)
            view = view[written:]

        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _fsync_dir(folder: Path) -> None:
    """Flush the directory entry. Windows may refuse a directory, which is ignored."""
    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return

    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_bundle(path: Path, bundle) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    _fsync_write(temporary, dump_bundle(bundle))
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def read_bundle(data_dir, environ=None):
    """The on-disk wraps. The data key is not returned."""
    return _read_bundle(bundle_path_for(data_dir, environ))


def replace_bundle(path: Path, bundle, *, check) -> None:
    """
    Replace a key bundle atomically.

    The previous bytes are copied to ``keys.bin.bak`` and fsynced first.
    The new bytes are written to a temporary file, fsynced, and renamed
    over the bundle. ``check`` must open the new bundle. If it fails,
    the backup is renamed back. The backup is removed only after
    ``check`` succeeds.
    """
    from freya.storage.sqlite import wipe_file

    path = Path(path)
    blob = dump_bundle(bundle)
    backup = path.with_name(path.name + ".bak")
    backup_tmp = path.with_name(path.name + ".bak.tmp")
    temporary = path.with_name(path.name + ".tmp")
    previous = path.read_bytes() if path.is_file() else None

    if previous is not None:
        _fsync_write(backup_tmp, previous)
        os.replace(backup_tmp, backup)

    try:
        _fsync_write(temporary, blob)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
        check()
    except BaseException:
        if previous is not None and backup.is_file() and path.read_bytes() != previous:
            os.replace(backup, path)
            _fsync_dir(path.parent)

        wipe_file(temporary)
        raise

    wipe_file(backup)
    wipe_file(temporary)


def _bind(dek: bytes, key_id: bytes) -> Unlocked:
    global _unlocked
    held = Unlocked(dek, key_id)

    with _lock:
        _unlocked = held

    return held


def recovery_waiting(data_dir, environ=None) -> bool:
    """True when a recovery key has been sealed and not yet shown."""
    path = bundle_path_for(data_dir, environ)

    if not path.is_file():
        return False

    try:
        bundle = _read_bundle(path)
    except StoreLocked:
        return False

    return not bundle.acknowledged and bundle.recovery_seal is not None


def reveal_recovery_key(
    data_dir, *, custody=None, passphrase=None, environ=None,
) -> str:
    """
    Return the recovery key text while it is still sealed.

    The caller prints it and then acknowledges it. A second call refuses.
    """
    from freya.storage.custody import (
        METHOD_OS,
        METHOD_PASSPHRASE,
        PURPOSE_RECOVERY,
        format_recovery_key,
        open_stretch_wrap,
        unwrap_os,
    )

    path = bundle_path_for(data_dir, environ)
    bundle = _read_bundle(path)

    if bundle.acknowledged or bundle.recovery_seal is None:
        raise StoreLocked(
            "The recovery key was already shown. The printed copy is the "
            "one that still opens these databases."
        )

    seal_wrap = bundle.recovery_seal

    try:
        if seal_wrap.method == METHOD_OS:
            if custody is None:
                custody = default_custody(environ)

            raw = unwrap_os(
                [seal_wrap], custody, bundle.key_id, purpose=PURPOSE_RECOVERY,
            )
        elif seal_wrap.method == METHOD_PASSPHRASE:
            if passphrase is None:
                raise StoreLocked("A passphrase is required.")

            raw = open_stretch_wrap(
                passphrase,
                seal_wrap.body,
                key_id=bundle.key_id,
                purpose=PURPOSE_RECOVERY,
            )
        else:
            raise StoreLocked("The database key file is not usable.")
    except BackupRefused as exc:
        raise StoreLocked(str(exc)) from exc

    return format_recovery_key(raw)


def acknowledge_recovery_key(data_dir, *, environ=None) -> None:
    """Drop the sealed recovery key. The printed copy is the one that remains."""
    path = bundle_path_for(data_dir, environ)
    bundle = _read_bundle(path)

    if bundle.acknowledged or bundle.recovery_seal is None:
        return

    bundle.acknowledged = True
    bundle.recovery_seal = None
    _write_bundle(path, bundle)


def startup_warning(data_dir, environ=None) -> list:
    """One reminder when the database recovery key has not been written down."""
    if not recovery_waiting(data_dir, environ):
        return []

    return [
        "A recovery key for the databases has not been written down yet. "
        "Run python -m freya.storage.encrypt --show-recovery-key and keep "
        "what it prints off this machine."
    ]


def ensure_unlocked(
    data_dir,
    *,
    custody=None,
    passphrase=None,
    recovery_key=None,
    environ=None,
    interactive=False,
) -> Optional[str]:
    """
    Unlock the live databases, or create the key on a new install.

    Returns the recovery key text when a new key was created, so the
    caller can show it once. Returns None when an existing key was
    unwrapped. A missing keystore without a passphrase raises
    ``StoreLocked`` and writes nothing. An unlock that is already held
    in this process is left as it is.
    """
    if current() is not None:
        return None

    if custody is None:
        custody = default_custody(environ)

    path = bundle_path_for(data_dir, environ)

    if path.is_file():
        bundle = _read_bundle(path)

        try:
            dek = unwrap_data_key(
                bundle,
                custody,
                passphrase,
                recovery_key,
                missing_recovery="This database key has no recovery wrap.",
                missing_passphrase=(
                    "This database key has no passphrase wrap. Use the recovery key."
                ),
            )
        except BackupRefused as exc:
            raise StoreLocked(str(exc)) from exc

        _bind(dek, bundle.key_id)
        return None

    if passphrase is None and recovery_key is not None:
        raise StoreLocked(
            "There is no database key on this machine for that recovery key. "
            "Nothing was opened."
        )

    if passphrase is None and not custody.available():
        if not interactive:
            raise StoreLocked(
                "No keystore is available. A passphrase is required. "
                "Nothing was opened."
            )

        try:
            passphrase = prompt_passphrase(label="Database passphrase")
        except BackupRefused as exc:
            raise StoreLocked(str(exc)) from exc

    try:
        dek, bundle, recovery_text = new_data_key(custody, passphrase)
    except BackupRefused as exc:
        raise StoreLocked(str(exc)) from exc

    _write_bundle(path, bundle)
    _bind(dek, bundle.key_id)
    return recovery_text
