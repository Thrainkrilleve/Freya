"""
Move Freya's bulk stores to another drive.

It copies each store, checks the copy file by file, and only then
switches Freya over to the new place. The originals are left where
they were, marked as stale, for the owner to remove. Nothing is ever
deleted here.
"""
import argparse
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Dict, List

from freya import single_instance
from freya.storage.locations import (
    BULK_STORES,
    DATA_DIR,
    bulk_root,
    set_bulk_root,
)
from freya.storage.placement import drive_of


STALE_NOTE = "MOVED-this-folder-is-a-stale-copy.txt"


class MoveRefused(RuntimeError):
    """Nothing was switched over. The message says why."""


def _files(folder: Path) -> Dict[str, Path]:
    return {
        path.relative_to(folder).as_posix(): path
        for path in folder.rglob("*")
        if path.is_file() and path.name != STALE_NOTE
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _same(source: Path, copy: Path) -> bool:
    original, copied = _files(source), _files(copy)

    if sorted(original) != sorted(copied):
        return False

    return all(
        original[name].stat().st_size == copied[name].stat().st_size
        and _sha256(original[name]) == _sha256(copied[name])
        for name in original
    )


def move_bulk(
    target_root,
    data_dir=DATA_DIR,
    allow_same_drive: bool = False,
    running_check=single_instance.claim,
) -> List[str]:
    """
    Copy every bulk store to `target_root`, check the copies, and
    switch Freya over. Returns the names of the stores that moved.

    Freya must not be running: a file saved during the copy would be
    left behind. The check also keeps her from starting until this
    process ends.
    """
    data_dir, target_root = Path(data_dir), Path(target_root)

    if not target_root.is_absolute():
        raise MoveRefused("The new place must be a full path.")
    if bulk_root(data_dir) is not None:
        raise MoveRefused(
            f"Bulk data is already kept in {bulk_root(data_dir)}."
        )
    if not allow_same_drive and drive_of(target_root) == drive_of(data_dir):
        raise MoveRefused("That is the same drive the data is on now.")
    if not running_check():
        raise MoveRefused("Freya is running. Close her first.")

    present = [name for name in BULK_STORES if (data_dir / name).is_dir()]

    for name in present:
        destination = target_root / name
        if destination.exists() and any(destination.iterdir()):
            raise MoveRefused(
                f"{destination} already holds something; it will not be "
                "written over."
            )

    try:
        for name in present:
            shutil.copytree(
                data_dir / name, target_root / name, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(STALE_NOTE),
            )
    except OSError as exc:
        raise MoveRefused(
            f"A store could not be copied ({exc}). Nothing was switched "
            "over; the originals are untouched."
        ) from exc

    for name in present:
        if not _same(data_dir / name, target_root / name):
            raise MoveRefused(
                f"The copy of {name} does not match the original. Nothing "
                "was switched over; the originals are untouched."
            )

    # A store that does not exist yet still needs its new folder, or
    # the first file saved to it would have nowhere to go.
    for name in BULK_STORES:
        (target_root / name).mkdir(parents=True, exist_ok=True)

    set_bulk_root(target_root, data_dir)

    for name in present:
        (data_dir / name / STALE_NOTE).write_text(
            "Freya no longer uses this folder. Its contents were copied to\n"
            f"{target_root / name}\n"
            "and checked file by file. It is safe to remove this folder once\n"
            "you have seen that Freya works from the new place.\n",
            encoding="utf-8",
        )

    return present


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Move Freya's bulk stores to another drive.",
    )
    parser.add_argument("--to", type=Path, required=True, help="The folder to keep bulk data in.")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    args = parser.parse_args(argv)

    try:
        moved = move_bulk(args.to, args.data)
    except MoveRefused as refused:
        print(f"Nothing was moved: {refused}", file=sys.stderr)
        return 1

    print(f"Bulk data is now kept in {args.to}")
    for name in moved:
        print(f"  {name}: copied and checked; the original is left in place, marked stale")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
