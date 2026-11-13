import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from freya.storage.locations import store_path
from typing import List, Optional

import numpy as np
import soundfile as sf

from freya.speech.engines import ROLES, SpeechError


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
PROFILES_PATH = DATA_DIR / "voice_profiles.json"
CLIPS_DIR = store_path("voice_clips")

RECORD_ID = re.compile(r"^[0-9a-f]{32}$")

MAX_NAME_LENGTH = 40
MAX_PROFILES = 200
MAX_CLIPS = 40


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(temporary, path)


class VoiceProfileStore:
    """
    The voices the owner has saved.

    Every profile belongs to the engine that made it and records what
    kind of voice it is. Its settings mean something only to that
    engine, so a profile is never offered to, or used with, another.
    """

    def __init__(self, path=PROFILES_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._profiles: List[dict] = []
        self._active: Optional[str] = None
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if not isinstance(data, dict):
            return

        profiles = data.get("profiles")

        if isinstance(profiles, list):
            self._profiles = [
                profile
                for profile in profiles
                if isinstance(profile, dict)
                and RECORD_ID.match(str(profile.get("id", "")))
                and isinstance(profile.get("name"), str)
                and isinstance(profile.get("engine"), str)
                and profile.get("kind") in ROLES
                and isinstance(profile.get("settings"), dict)
            ]

        active = data.get("active")

        if any(profile["id"] == active for profile in self._profiles):
            self._active = active

    def _save(self) -> None:
        _write_json(
            self.path,
            {"profiles": self._profiles, "active": self._active},
        )

    def list(self) -> List[dict]:
        with self._lock:
            return [dict(profile) for profile in self._profiles]

    def get(self, profile_id) -> Optional[dict]:
        with self._lock:
            for profile in self._profiles:
                if profile["id"] == profile_id:
                    return dict(profile)

        return None

    @property
    def active_id(self) -> Optional[str]:
        with self._lock:
            return self._active

    def add(self, name, engine: str, kind: str, settings: dict) -> dict:
        name = " ".join(str(name or "").split())

        if not name:
            raise SpeechError("Give the voice a name.")

        if len(name) > MAX_NAME_LENGTH:
            raise SpeechError(
                f"A voice name can be at most {MAX_NAME_LENGTH} "
                "characters."
            )

        with self._lock:
            if len(self._profiles) >= MAX_PROFILES:
                raise SpeechError("There are too many saved voices.")

            if any(
                profile["name"].lower() == name.lower()
                for profile in self._profiles
            ):
                raise SpeechError(
                    f"There is already a saved voice called {name}."
                )

            profile = {
                "id": uuid.uuid4().hex,
                "name": name,
                "engine": engine,
                "kind": kind,
                "settings": settings,
                "created": time.time(),
            }
            self._profiles.append(profile)
            self._save()

            return dict(profile)

    def delete(self, profile_id) -> bool:
        with self._lock:
            remaining = [
                profile
                for profile in self._profiles
                if profile["id"] != profile_id
            ]

            if len(remaining) == len(self._profiles):
                return False

            self._profiles = remaining

            if self._active == profile_id:
                self._active = None

            self._save()

            return True

    def set_active(self, profile_id: Optional[str]) -> None:
        with self._lock:
            if profile_id is not None and not any(
                profile["id"] == profile_id for profile in self._profiles
            ):
                raise SpeechError("That saved voice no longer exists.")

            self._active = profile_id
            self._save()


class ClipStore:
    """
    Recently generated speech, kept as WAV files so the window can play
    and export it. Only the newest few are kept.
    """

    def __init__(self, directory=CLIPS_DIR, limit=MAX_CLIPS):
        self.directory = Path(directory)
        self._limit = limit
        self._lock = threading.Lock()
        self._clips: List[dict] = []

    def add(self, audio, sample_rate: int, engine: str, label: str) -> dict:
        audio = np.asarray(audio, dtype=np.float32)

        clip = {
            "id": uuid.uuid4().hex,
            "engine": engine,
            "label": label[:80],
            "seconds": round(len(audio) / sample_rate, 2),
            "created": time.time(),
        }

        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            sf.write(
                self.directory / f"{clip['id']}.wav",
                audio,
                sample_rate,
            )
            self._clips.append(clip)

            while len(self._clips) > self._limit:
                old = self._clips.pop(0)
                (self.directory / f"{old['id']}.wav").unlink(
                    missing_ok=True
                )

        return dict(clip)

    def read(self, clip_id) -> Optional[bytes]:
        """
        The WAV bytes of a known clip, or None. Only clips this store
        created can be read, whatever is asked for.
        """
        if not isinstance(clip_id, str) or not RECORD_ID.match(clip_id):
            return None

        with self._lock:
            if not any(clip["id"] == clip_id for clip in self._clips):
                return None

        try:
            return (self.directory / f"{clip_id}.wav").read_bytes()
        except OSError:
            return None

    def clear(self) -> None:
        with self._lock:
            for clip in self._clips:
                (self.directory / f"{clip['id']}.wav").unlink(
                    missing_ok=True
                )

            self._clips = []
