import hashlib
import os
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

from freya.child_environment import child_environment


# Where a search looks. The owner's Downloads folder by default;
# FREYA_AVATAR_SEARCH replaces it with other folders, separated by ";".
def search_folders() -> List[Path]:
    configured = os.environ.get("FREYA_AVATAR_SEARCH", "").strip()

    if configured:
        return [Path(part) for part in configured.split(";") if part.strip()]

    return [Path.home() / "Downloads"]


READY = "ready"                # a VRM avatar: only needs adding
CONVERTIBLE = "convertible"    # a 3D character Freya can try to convert
ARCHIVE = "archive"            # a compressed folder containing one of the above
UNSUITABLE = "unsuitable"      # a 3D file that cannot become an avatar

AVATAR_FILES = (".vrm",)
CHARACTER_FILES = (".blend", ".fbx", ".glb", ".gltf")
ARCHIVE_FILES = (".zip", ".7z", ".rar")
# Formats that hold only a surface: no skeleton, no face shapes, and for
# most of them no colour. They are for printing or static scenes.
SURFACE_FILES = (".stl", ".obj", ".3mf", ".ply")

MAX_DEPTH = 2
MAX_RESULTS = 60
MAX_ARCHIVE_ENTRIES = 5000
LIST_TIMEOUT_SECONDS = 30


def _megabytes(size: int) -> float:
    return round(size / (1024 * 1024), 1)


def archive_names(path: Path) -> Optional[List[str]]:
    """
    The names of the files inside an archive, or None when it cannot be
    listed. Nothing is extracted.
    """
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                return [info.filename for info in archive.infolist()[:MAX_ARCHIVE_ENTRIES]]
        except (OSError, zipfile.BadZipFile):
            return None

    # Windows ships a tar that reads 7z and rar listings.
    tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"

    if not tar.is_file():
        return None

    try:
        listing = subprocess.run(
            [str(tar), "-tf", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=LIST_TIMEOUT_SECONDS,
            env=child_environment(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if listing.returncode != 0:
        return None

    return listing.stdout.splitlines()[:MAX_ARCHIVE_ENTRIES]


def best_inside(names: List[str]) -> List[str]:
    """
    The files in an archive worth trying, most promising first: a ready
    avatar, then a character file. Among character files, one that says
    it is rigged comes before one that says it is not, and posed copies
    come last.
    """
    def kind(name):
        return Path(name).suffix.lower()

    def rank(name):
        lowered = name.lower()
        score = 0

        if kind(name) in AVATAR_FILES:
            score -= 100

        if "no_rig" in lowered or "norig" in lowered or "no rig" in lowered:
            score += 20
        elif "rig" in lowered:
            score -= 10

        if "pose" in lowered:
            score += 10

        return (score, len(name))

    return sorted(
        (name for name in names if kind(name) in AVATAR_FILES + CHARACTER_FILES),
        key=rank,
    )


def classify(path: Path) -> Optional[dict]:
    """
    What a file is, as far as becoming an avatar goes, or None when it
    is not a 3D file at all. Only its name, size, and (for an archive)
    the names inside it are looked at.
    """
    kind = path.suffix.lower()

    try:
        stat = path.stat()
    except OSError:
        return None

    entry = {
        "id": hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24],
        "name": path.name[:160],
        "folder": str(path.parent),
        "megabytes": _megabytes(stat.st_size),
        "modified": stat.st_mtime,
    }

    if kind in AVATAR_FILES:
        return {**entry, "status": READY, "note": "A VRM avatar. It only needs adding."}

    if kind in CHARACTER_FILES:
        return {
            **entry,
            "status": CONVERTIBLE,
            "note": "A 3D character file. Freya can check whether it has a "
            "skeleton and convert it.",
        }

    if kind in SURFACE_FILES:
        return {
            **entry,
            "status": UNSUITABLE,
            "note": "A solid shape for printing or still scenes. It has no "
            "skeleton or face controls, so it cannot be an avatar.",
        }

    if kind in ARCHIVE_FILES:
        names = archive_names(path)

        if names is None:
            return None

        inside = best_inside(names)

        if not inside:
            if any(Path(name).suffix.lower() in SURFACE_FILES for name in names):
                return {
                    **entry,
                    "status": UNSUITABLE,
                    "note": "Contains only solid shapes with no skeleton.",
                }

            return None

        return {
            **entry,
            "status": ARCHIVE,
            "inside": [name[:200] for name in inside[:8]],
            "note": f"A compressed folder with {len(inside)} character "
            f"file{'s' if len(inside) != 1 else ''} in it. Freya can unpack "
            "it, pick the rigged one, and convert it.",
        }

    return None


class SourceSearch:
    """
    Finds files that are, or could be turned into, avatars.

    It looks in the owner's search folders and a little way below them.
    It reads names and sizes, and the list of names inside archives; it
    opens no model. What it finds is remembered so that a later request
    to prepare a file can only name one of these, never an arbitrary
    path.
    """

    def __init__(self, folders=None, skip=None):
        self._folders = folders
        # Folders never searched: the avatar folder itself.
        self._skip = [Path(path).resolve() for path in (skip or [])]
        self._found: Dict[str, Path] = {}

    def folders(self) -> List[Path]:
        return list(self._folders) if self._folders is not None else search_folders()

    def _walk(self, folder: Path, depth: int):
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return

        for entry in entries:
            try:
                if entry.is_symlink():
                    continue

                if entry.is_dir():
                    if depth < MAX_DEPTH and entry.resolve() not in self._skip:
                        yield from self._walk(entry, depth + 1)
                elif entry.is_file():
                    yield entry
            except OSError:
                continue

    def search(self) -> List[dict]:
        found: Dict[str, Path] = {}
        results = []

        for folder in self.folders():
            for path in self._walk(folder, 0):
                entry = classify(path)

                if entry is None:
                    continue

                found[entry["id"]] = path
                results.append(entry)

                if len(results) >= MAX_RESULTS:
                    break

        self._found = found
        order = {READY: 0, ARCHIVE: 1, CONVERTIBLE: 1, UNSUITABLE: 2}
        results.sort(key=lambda e: (order[e["status"]], -e["modified"]))

        return results

    def path(self, source_id) -> Optional[Path]:
        """
        The file a search result refers to, if it is still there.
        """
        path = self._found.get(source_id) if isinstance(source_id, str) else None

        return path if path is not None and path.is_file() else None
