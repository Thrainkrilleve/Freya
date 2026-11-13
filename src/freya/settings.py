import json
import os
import threading
from pathlib import Path
from typing import List

from freya.voice.wake import normalize_name
from freya.paths import data_dir


DATA_DIR = data_dir()
SETTINGS_PATH = DATA_DIR / "freya_settings.json"

DEFAULT_WAKE_NAMES = ["freya"]
# How loudly Freya speaks, as a percentage of the audio as generated.
DEFAULT_VOICE_VOLUME = 100


def clamp_volume(value) -> int:
    """
    A volume as a whole percentage from 0 to 100. Raises ValueError for
    anything that is not a number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("A volume is a number from 0 to 100.")

    if value != value:
        raise ValueError("A volume is a number from 0 to 100.")

    return int(round(min(max(value, 0), 100)))


class Settings:
    """
    Small preferences the owner changes while Freya is running, kept in
    a JSON file next to the databases.

    Unreadable or missing settings fall back to the defaults rather
    than stopping Freya.
    """

    def __init__(self, path=SETTINGS_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._wake_names = list(DEFAULT_WAKE_NAMES)
        self._voice_volume = DEFAULT_VOICE_VOLUME
        # Folders the owner has opened for Freya to read. None to start.
        self._readable_folders = []
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if not isinstance(data, dict):
            return

        try:
            self._voice_volume = clamp_volume(data.get("voice_volume"))
        except ValueError:
            pass

        folders = data.get("readable_folders")

        if isinstance(folders, list):
            self._readable_folders = list(dict.fromkeys(
                folder for folder in folders
                if isinstance(folder, str) and folder.strip()
            ))

        stored = data.get("wake_names")

        if not isinstance(stored, list):
            return

        names = []

        for name in stored:
            if not isinstance(name, str):
                continue

            try:
                name = normalize_name(name)
            except ValueError:
                continue

            if name not in names:
                names.append(name)

        if names:
            self._wake_names = names

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "wake_names": self._wake_names,
                    "voice_volume": self._voice_volume,
                    "readable_folders": self._readable_folders,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @property
    def wake_names(self) -> List[str]:
        with self._lock:
            return list(self._wake_names)

    @property
    def voice_volume(self) -> int:
        with self._lock:
            return self._voice_volume

    @property
    def readable_folders(self) -> List[str]:
        with self._lock:
            return list(self._readable_folders)

    def add_readable_folder(self, folder: str) -> str:
        """
        Open a folder for reading. The caller has already checked that
        it is a folder that may be opened. Raises ValueError if it is
        open already.
        """
        folder = str(folder)

        with self._lock:
            if folder.lower() in (kept.lower() for kept in self._readable_folders):
                raise ValueError("That folder is already open.")

            self._readable_folders.append(folder)
            self._save()

        return folder

    def remove_readable_folder(self, folder: str) -> str:
        """Close a folder. Raises ValueError if it was not open."""
        wanted = str(folder).strip().strip('"').rstrip("\\/").lower()

        with self._lock:
            for kept in self._readable_folders:
                if kept.rstrip("\\/").lower() == wanted:
                    self._readable_folders.remove(kept)
                    self._save()
                    return kept

        raise ValueError("That folder is not open.")

    def set_voice_volume(self, value) -> int:
        """
        Remember how loudly Freya speaks. Returns the stored
        percentage; raises ValueError if it is not a number.
        """
        value = clamp_volume(value)

        with self._lock:
            if value != self._voice_volume:
                self._voice_volume = value
                self._save()

        return value

    def add_wake_name(self, name: str) -> str:
        """
        Add a name Freya answers to. Returns the stored form.
        Raises ValueError for an unusable or duplicate name.
        """
        name = normalize_name(name)

        with self._lock:
            if name in self._wake_names:
                raise ValueError(f"I already answer to {name}.")

            self._wake_names.append(name)
            self._save()

        return name

    def remove_wake_name(self, name: str) -> str:
        """
        Remove a name. The last remaining name cannot be removed,
        because Freya would then never respond to speech.
        """
        name = normalize_name(name)

        with self._lock:
            if name not in self._wake_names:
                raise ValueError(f"I don't answer to {name}.")

            if len(self._wake_names) == 1:
                raise ValueError(
                    "That is the only name I answer to. "
                    "Add another before removing it."
                )

            self._wake_names.remove(name)
            self._save()

        return name
