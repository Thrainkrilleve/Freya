"""
Where Freya keeps her data: the repository's data/ folder, unless
FREYA_DATA_DIR names another.

This module uses only the standard library and imports nothing else
from Freya, so anything may import it.
"""
import os
from pathlib import Path


DATA_DIR_VARIABLE = "FREYA_DATA_DIR"

# data/ next to the source tree, which is why an editable install was
# needed before the override existed.
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def data_dir() -> Path:
    """The folder that holds Freya's data. Nothing is created here."""
    override = os.environ.get(DATA_DIR_VARIABLE, "").strip()

    if override:
        return Path(override).expanduser()

    return DEFAULT_DATA_DIR
