"""
Scratch restore drill.

    python devtools/restore_drill.py
    python devtools/restore_drill.py --root <empty-folder>

Creates made-up rows in a temporary data directory, backs them up,
deletes them, restores them, and prints how long it took. It does
not read the owner's data directory.
"""

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from freya.storage.restore_drill import main


if __name__ == "__main__":
    raise SystemExit(main())
