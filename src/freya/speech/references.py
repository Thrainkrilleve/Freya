import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from freya.storage.locations import store_path
from typing import Iterable, List, Optional

import numpy as np
import soundfile as sf

from freya.speech.engines import SpeechError


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
REFERENCES_DIR = store_path("voice_references")

RECORD_ID = re.compile(r"^[0-9a-f]{32}$")

ORIGIN_UPLOAD = "upload"
ORIGIN_DESIGNED = "designed"

MIN_SECONDS = 6
MAX_SECONDS = 30
MIN_SAMPLE_RATE = 16000
MAX_SAMPLE_RATE = 48000
MAX_TEXT_LENGTH = 600

# Quieter than this and the recording is effectively silence.
MIN_PEAK = 0.01


class ReferenceStore:
    """
    The recordings voices are made from: ones the owner supplied for
    cloning, and samples a design engine generated.

    A supplied recording is only accepted together with the owner's
    statement that they have the right to use that voice, and the
    statement is kept with it. Recordings stay on this computer under
    `data/`, which is never committed.
    """

    def __init__(self, directory=REFERENCES_DIR):
        self.directory = Path(directory)
        self._lock = threading.Lock()
        self._records: dict = {}
        self._load()

    @property
    def _index_path(self) -> Path:
        return self.directory / "index.json"

    def _load(self) -> None:
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if not isinstance(data, dict):
            return

        self._records = {
            key: record
            for key, record in data.items()
            if RECORD_ID.match(str(key))
            and isinstance(record, dict)
            and record.get("origin") in (ORIGIN_UPLOAD, ORIGIN_DESIGNED)
            and (self.directory / f"{key}.wav").is_file()
        }

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self._index_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._records, indent=1), encoding="utf-8"
        )
        os.replace(temporary, self._index_path)

    def _add(self, audio, sample_rate: int, record: dict) -> dict:
        reference_id = uuid.uuid4().hex
        record = {
            **record,
            "seconds": round(len(audio) / sample_rate, 2),
            "sample_rate": sample_rate,
            "created": time.time(),
        }

        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            sf.write(
                self.directory / f"{reference_id}.wav",
                audio,
                sample_rate,
                subtype="PCM_16",
            )
            self._records[reference_id] = record
            self._save()

        return {"id": reference_id, **record}

    def add_upload(self, pcm: bytes, sample_rate, consent, transcript="") -> dict:
        """
        Keep a recording the owner supplied, given as mono 16-bit
        samples. Refused unless the owner has stated they have the
        right to use the voice.
        """
        if consent is not True:
            raise SpeechError(
                "Confirm that you have the right to use this voice "
                "before cloning it."
            )

        if (
            not isinstance(sample_rate, int)
            or isinstance(sample_rate, bool)
            or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
        ):
            raise SpeechError("The recording could not be read.")

        if not isinstance(pcm, (bytes, bytearray)) or len(pcm) % 2:
            raise SpeechError("The recording could not be read.")

        audio = np.frombuffer(bytes(pcm), dtype="<i2").astype(np.float32)
        audio /= 32768.0
        seconds = len(audio) / sample_rate

        if seconds < MIN_SECONDS:
            raise SpeechError(
                f"The recording must be at least {MIN_SECONDS} seconds long."
            )

        if seconds > MAX_SECONDS + 0.5:
            raise SpeechError(
                f"The recording can be at most {MAX_SECONDS} seconds long."
            )

        if float(np.abs(audio).max()) < MIN_PEAK:
            raise SpeechError("The recording is silent.")

        transcript = " ".join(str(transcript or "").split())

        if len(transcript) > MAX_TEXT_LENGTH:
            raise SpeechError(
                f"The transcript can be at most {MAX_TEXT_LENGTH} characters."
            )

        return self._add(
            audio,
            sample_rate,
            {
                "origin": ORIGIN_UPLOAD,
                "engine": None,
                "text": transcript,
                "consent": True,
                "consent_time": time.time(),
            },
        )

    def add_designed(self, audio, sample_rate: int, engine: str, text: str) -> dict:
        """
        Keep a sample that a design engine generated. It belongs to
        that engine.
        """
        return self._add(
            np.asarray(audio, dtype=np.float32),
            int(sample_rate),
            {
                "origin": ORIGIN_DESIGNED,
                "engine": engine,
                "text": text,
                "consent": None,
            },
        )

    def get(self, reference_id) -> Optional[dict]:
        if not isinstance(reference_id, str):
            return None

        with self._lock:
            record = self._records.get(reference_id)

            return {"id": reference_id, **record} if record else None

    def path(self, reference_id) -> Optional[Path]:
        if self.get(reference_id) is None:
            return None

        return self.directory / f"{reference_id}.wav"

    def list(self) -> List[dict]:
        with self._lock:
            return [
                {"id": key, **record} for key, record in self._records.items()
            ]

    def delete(self, reference_id) -> bool:
        with self._lock:
            if self._records.pop(reference_id, None) is None:
                return False

            (self.directory / f"{reference_id}.wav").unlink(missing_ok=True)
            self._save()

            return True

    def keep_only(self, used: Iterable[str]) -> None:
        """
        Delete every recording that no saved voice uses.
        """
        used = set(used)

        for record in self.list():
            if record["id"] not in used:
                self.delete(record["id"])
