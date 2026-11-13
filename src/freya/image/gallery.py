import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from freya.storage.locations import store_path
from typing import List, Optional

from freya.paths import data_dir


DATA_DIR = data_dir()
GALLERY_DIR = store_path("images")

INDEX_NAME = "index.json"

# Image types Freya will keep, by the bytes a file starts with.
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"RIFF", "webp", "image/webp"),
)

IMAGE_ID = re.compile(r"^[0-9a-f]{32}$")

MAX_PROMPT_LENGTH = 2000


class ImageGallery:
    """
    Freya's own copy of every image she generates, with the prompt
    that produced it.

    ComfyUI keeps its output in its own folder and only serves it
    while it is running. Keeping copies here lets the window show past
    images at any time.
    """

    def __init__(self, directory=GALLERY_DIR):
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._records = self._load()

    def _index_path(self) -> Path:
        return self.directory / INDEX_NAME

    def _load(self) -> List[dict]:
        try:
            data = json.loads(
                self._index_path().read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return []

        if not isinstance(data, list):
            return []

        return [
            record
            for record in data
            if isinstance(record, dict)
            and IMAGE_ID.match(str(record.get("id", "")))
            and (self.directory / str(record.get("file", ""))).is_file()
        ]

    def _save(self) -> None:
        temporary = self._index_path().with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._records, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, self._index_path())

    def add(self, prompt: str, image: bytes, seed=None) -> dict:
        """
        Store an image and return its record. Raises ValueError when
        the bytes are not a supported image.
        """
        for signature, extension, media_type in SIGNATURES:
            if image.startswith(signature):
                break
        else:
            raise ValueError("That is not a supported image.")

        image_id = uuid.uuid4().hex

        record = {
            "id": image_id,
            "file": f"{image_id}.{extension}",
            "media_type": media_type,
            "prompt": str(prompt)[:MAX_PROMPT_LENGTH],
            "seed": seed,
            "created": time.time(),
        }

        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / record["file"]).write_bytes(image)
            self._records.append(record)
            self._save()

        return dict(record)

    def list(self) -> List[dict]:
        """
        Every stored image, newest first.
        """
        with self._lock:
            return [dict(record) for record in reversed(self._records)]

    def find(self, image_id: str) -> Optional[dict]:
        if not isinstance(image_id, str) or not IMAGE_ID.match(image_id):
            return None

        with self._lock:
            for record in self._records:
                if record["id"] == image_id:
                    return dict(record)

        return None

    def read(self, image_id: str) -> Optional[tuple]:
        """
        Return (media_type, bytes) for a stored image, or None. Only
        files listed in the index can be read, whatever is asked for.
        """
        record = self.find(image_id)

        if record is None:
            return None

        try:
            return (
                record["media_type"],
                (self.directory / record["file"]).read_bytes(),
            )
        except OSError:
            return None
