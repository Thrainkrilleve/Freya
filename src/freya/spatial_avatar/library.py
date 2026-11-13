import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from freya.storage.locations import store_path
from typing import Dict, List, Optional, Tuple

from freya.spatial_avatar.vrm import (
    AUTHOR_ONLY,
    EVERYONE,
    LICENSED,
    VrmError,
    read_thumbnail,
    read_vrm,
)


DATA_DIR = Path(__file__).resolve().parents[3] / "data"

# VRM files are put here by the owner. Freya only ever reads them.
MODELS_DIR = store_path("avatar_models")
LIBRARY_PATH = DATA_DIR / "avatar_library.json"

MAX_MODELS = 200
MAX_PROFILES = 100
MAX_NAME_LENGTH = 40
# The largest model sent to the window for previewing.
MAX_PREVIEW_BYTES = 150 * 1024 * 1024

RECORD_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LibraryError(Exception):
    """
    Something about the avatar library could not be done. Safe to show.
    """


def use_check(licence: dict) -> dict:
    """
    Whether the model's own terms let it be used as an avatar here.

    allowed          the terms let anyone use it
    confirmation     what the owner must confirm before it is used,
                     when the terms restrict who may, or say nothing
    """
    who = licence.get("who_may_use")

    if who == EVERYONE:
        return {"allowed": True, "confirmation": ""}

    if who == AUTHOR_ONLY:
        return {
            "allowed": False,
            "confirmation": "Only its author may use this avatar. "
            "I confirm that I am its author.",
        }

    if who == LICENSED:
        return {
            "allowed": False,
            "confirmation": "Only people its author has licensed may use this "
            "avatar. I confirm that I have that licence.",
        }

    return {
        "allowed": False,
        "confirmation": "This file does not say who may use it. I confirm "
        "that I have the right to use it.",
    }


class AvatarLibrary:
    """
    The VRM avatar models on this computer, and the avatars the owner
    has made from them.

    Models are the `.vrm` files directly inside the models folder.
    Freya reads them and never changes, moves, or deletes them. Each is
    known by the SHA-256 of its contents, so renaming a file does not
    lose it and a changed file is a different model.

    An avatar profile is the owner's named choice of one model. A model
    whose own terms restrict who may use it can only be made into a
    profile with the owner's statement that they may, which is kept
    with the profile.
    """

    def __init__(self, models_dir=MODELS_DIR, path=LIBRARY_PATH):
        self.models_dir = Path(models_dir)
        self.path = Path(path)
        self._lock = threading.RLock()
        self._models: Dict[str, dict] = {}
        self._errors: List[dict] = []
        self._cache: Dict[str, dict] = {}
        self._profiles: List[dict] = []
        self._active: Optional[str] = None
        self._load()

    # -- storage ---------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if not isinstance(data, dict):
            return

        cache = data.get("cache")

        if isinstance(cache, dict):
            self._cache = {
                key: value
                for key, value in cache.items()
                if isinstance(key, str) and isinstance(value, dict)
            }

        profiles = data.get("profiles")

        if isinstance(profiles, list):
            self._profiles = [
                profile
                for profile in profiles
                if isinstance(profile, dict)
                and RECORD_ID.match(str(profile.get("id", "")))
                and isinstance(profile.get("name"), str)
                and SHA256.match(str(profile.get("model", "")))
            ]

        active = data.get("active")

        if any(profile["id"] == active for profile in self._profiles):
            self._active = active

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "cache": self._cache,
                    "profiles": self._profiles,
                    "active": self._active,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    # -- models ----------------------------------------------------------------

    def _model_files(self) -> List[Path]:
        """
        The `.vrm` files directly inside the models folder. Links and
        anything that resolves outside the folder are ignored.
        """
        try:
            folder = self.models_dir.resolve()
            entries = sorted(self.models_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []

        files = []

        for entry in entries:
            try:
                if (
                    entry.suffix.lower() == ".vrm"
                    and not entry.is_symlink()
                    and entry.is_file()
                    and entry.resolve().parent == folder
                ):
                    files.append(entry)
            except OSError:
                continue

        return files[:MAX_MODELS]

    def scan(self) -> None:
        """
        Look at the models folder again. A file that has not changed
        since it was last read is not read again.
        """
        with self._lock:
            self.models_dir.mkdir(parents=True, exist_ok=True)
            models: Dict[str, dict] = {}
            errors: List[dict] = []
            cache: Dict[str, dict] = {}

            for path in self._model_files():
                try:
                    stat = path.stat()
                except OSError:
                    continue

                remembered = self._cache.get(path.name)

                if (
                    remembered
                    and remembered.get("size") == stat.st_size
                    and remembered.get("modified") == stat.st_mtime_ns
                    and isinstance(remembered.get("info"), dict)
                ):
                    info = remembered["info"]
                else:
                    try:
                        info = read_vrm(path).as_dict()
                    except VrmError as exc:
                        errors.append({"file": path.name[:120], "error": str(exc)})
                        continue

                cache[path.name] = {
                    "size": stat.st_size,
                    "modified": stat.st_mtime_ns,
                    "info": info,
                }
                # Two copies of the same file are one model.
                models.setdefault(info["sha256"], {**info, "file_name": path.name})

            self._models = models
            self._errors = errors
            self._cache = cache
            self._save()

    def models(self) -> List[dict]:
        with self._lock:
            return [
                {
                    **{key: value for key, value in model.items() if key != "thumbnail"},
                    "use": use_check(model["licence"]),
                    "can_preview": model["size"] <= MAX_PREVIEW_BYTES,
                }
                for model in self._models.values()
            ]

    def errors(self) -> List[dict]:
        with self._lock:
            return list(self._errors)

    def _path(self, sha256) -> Optional[Path]:
        """
        The file for a model the library knows, or None. Only a model
        found by scanning the folder has a path; nothing else can be
        reached by asking for it.
        """
        if not isinstance(sha256, str) or not SHA256.match(sha256):
            return None

        with self._lock:
            model = self._models.get(sha256)

            if model is None:
                return None

            path = self.models_dir / model["file_name"]

        return path if path in self._model_files() else None

    def model_bytes(self, sha256) -> Optional[bytes]:
        """
        A model's file, for previewing it in the window.
        """
        path = self._path(sha256)

        if path is None:
            return None

        try:
            if path.stat().st_size > MAX_PREVIEW_BYTES:
                return None

            return path.read_bytes()
        except OSError:
            return None

    def thumbnail(self, sha256) -> Optional[Tuple[str, bytes]]:
        path = self._path(sha256)

        if path is None:
            return None

        with self._lock:
            location = (self._models.get(sha256) or {}).get("thumbnail")

        return read_thumbnail(path, location) if location else None

    # -- profiles --------------------------------------------------------------

    def profiles(self) -> List[dict]:
        with self._lock:
            return [
                {**profile, "model_present": profile["model"] in self._models}
                for profile in self._profiles
            ]

    @property
    def active_id(self) -> Optional[str]:
        with self._lock:
            return self._active

    def add_profile(self, name, model, confirmed=False) -> dict:
        """
        Make a named avatar from a model. When the model's terms
        restrict who may use it, `confirmed` must be True: the owner's
        statement that they may.
        """
        name = " ".join(str(name or "").split())

        if not name:
            raise LibraryError("Give the avatar a name.")

        if len(name) > MAX_NAME_LENGTH:
            raise LibraryError(
                f"An avatar's name can be at most {MAX_NAME_LENGTH} characters."
            )

        with self._lock:
            known = self._models.get(model) if isinstance(model, str) else None

            if known is None:
                raise LibraryError("That model is not in the avatar folder.")

            check = use_check(known["licence"])

            if not check["allowed"] and confirmed is not True:
                raise LibraryError(
                    "This model's terms restrict who may use it. Confirm that "
                    "you may before making it an avatar."
                )

            if len(self._profiles) >= MAX_PROFILES:
                raise LibraryError("There are too many saved avatars.")

            if any(p["name"].lower() == name.lower() for p in self._profiles):
                raise LibraryError(f"There is already an avatar called {name}.")

            profile = {
                "id": uuid.uuid4().hex,
                "name": name,
                "model": model,
                "model_name": known["name"],
                # Recorded only when it was needed and given.
                "licence_confirmed": (
                    {"statement": check["confirmation"], "time": time.time()}
                    if not check["allowed"]
                    else None
                ),
                "created": time.time(),
            }
            self._profiles.append(profile)
            self._save()

            return dict(profile)

    def delete_profile(self, profile_id) -> None:
        with self._lock:
            remaining = [p for p in self._profiles if p["id"] != profile_id]

            if len(remaining) == len(self._profiles):
                raise LibraryError("That avatar no longer exists.")

            self._profiles = remaining

            if self._active == profile_id:
                self._active = None

            self._save()

    def use_profile(self, profile_id) -> None:
        """
        Choose which avatar is Freya's, or None for the placeholder.
        """
        with self._lock:
            if profile_id is not None:
                profile = next(
                    (p for p in self._profiles if p["id"] == profile_id), None
                )

                if profile is None:
                    raise LibraryError("That avatar no longer exists.")

                if profile["model"] not in self._models:
                    raise LibraryError(
                        "That avatar's model file is no longer in the avatar folder."
                    )

            self._active = profile_id
            self._save()

    def active_model_path(self) -> Optional[Path]:
        """
        The model file of the avatar marked as Freya's, or None when
        there is none or its file has gone.
        """
        with self._lock:
            profile = next(
                (p for p in self._profiles if p["id"] == self._active), None
            )

        if profile is None:
            return None

        path = self._path(profile["model"])

        if path is None:
            # The folder may not have been looked at yet this run.
            self.scan()
            path = self._path(profile["model"])

        return path

    def describe(self) -> dict:
        return {
            "folder": str(self.models_dir),
            "models": self.models(),
            "errors": self.errors(),
            "profiles": self.profiles(),
            "active_profile": self.active_id,
        }
