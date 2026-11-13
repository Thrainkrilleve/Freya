"""
Where a store lives. Most of Freya's data sits in data/, next to the
source tree. The stores named in BULK_STORES can be kept somewhere
else instead, such as a hard disk, by the owner's setting.

This module uses only the standard library and imports nothing from
Freya but freya.paths, which is the same, so anything may import it.
"""
import json
import os
from pathlib import Path
from typing import Optional

from freya.paths import data_dir


DATA_DIR = data_dir()
LOCATIONS_NAME = "storage_locations.json"

# Overrides the setting, for tests and one-off runs.
BULK_VARIABLE = "FREYA_BULK_DIR"

# Stores that are large or rarely read and hold only files, so they
# can live on a slower, roomier drive. Databases and settings are
# never here. The media library is not here either: it keeps a
# database beside its files.
BULK_STORES = (
    "images",
    "voice_clips",
    "voice_references",
    "voice_benchmarks",
    "avatar_models",
    "avatar_work",
)


def bulk_root(data_dir=DATA_DIR) -> Optional[Path]:
    """
    The folder bulk stores are kept in, or None when they are kept in
    data/ like everything else.

    An unreadable setting means None. A setting that names a folder
    which is not there is still returned: if the drive is missing,
    Freya should fail to find her images, not quietly start a second,
    empty set of them somewhere else.
    """
    named = os.environ.get(BULK_VARIABLE)

    if not named:
        try:
            stored = json.loads(
                (Path(data_dir) / LOCATIONS_NAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

        named = stored.get("bulk") if isinstance(stored, dict) else None

    if not isinstance(named, str) or not named.strip():
        return None

    root = Path(named)

    return root if root.is_absolute() else None


def store_path(name: str, data_dir=DATA_DIR) -> Path:
    """Where the named store is kept."""
    if name in BULK_STORES:
        root = bulk_root(data_dir)
        if root is not None:
            return root / name

    return Path(data_dir) / name


def set_bulk_root(root, data_dir=DATA_DIR) -> None:
    """Record where bulk stores are kept. None puts them back in data/."""
    path = Path(data_dir) / LOCATIONS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"bulk": str(root) if root is not None else None}, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
