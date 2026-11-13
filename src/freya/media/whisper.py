import base64
from contextlib import nullcontext
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from freya.child_environment import child_environment
from freya.media.files import UnsafePath

MAX_LINE = 3 * 1024 * 1024


class WhisperTranscriber:
    """Decode bounded audio chunks in a helper; transcribe locally."""

    def __init__(self, model, name="small", admission=None):
        self._model = model
        self.name = name
        self._admission = admission or nullcontext

    def available(self):
        return hasattr(self._model(), "transcribe_media_chunk")

    def transcribe(self, path, cancelled=None, *, size, modified_ns):
        speech = self._model()
        if not hasattr(speech, "transcribe_media_chunk"):
            raise RuntimeError("The local speech recognizer is not ready.")
        path = Path(path)
        info = path.stat()
        if info.st_size != size or info.st_mtime_ns != modified_ns:
            raise UnsafePath("That file changed since it was imported. Delete its old catalog record and import it again.")
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("decode_worker.py")), str(path), str(size), str(modified_ns)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=child_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        messages = queue.Queue(maxsize=2)
        closed = threading.Event()

        def reader():
            try:
                while not closed.is_set():
                    line = process.stdout.readline(MAX_LINE + 1)
                    if not line or len(line) > MAX_LINE:
                        item = {"type": "error", "message": "Audio decoder stopped or exceeded its output limit."}
                    else:
                        item = json.loads(line)
                    while not closed.is_set():
                        try:
                            messages.put(item, timeout=0.2)
                            break
                        except queue.Full:
                            continue
                    if item.get("type") in ("done", "error"):
                        return
            except Exception:
                if not closed.is_set():
                    try:
                        messages.put({"type": "error", "message": "Audio decoder failed."}, timeout=1)
                    except queue.Full:
                        pass

        thread = threading.Thread(target=reader, daemon=True, name="FreyaMediaDecoderRead")
        thread.start()
        metadata = {"duration": None, "complete": False}

        def timed():
            import numpy as np
            try:
                while True:
                    deadline = time.monotonic() + 45
                    while True:
                        if cancelled is not None and cancelled.is_set():
                            return
                        try:
                            message = messages.get(timeout=0.2)
                            break
                        except queue.Empty:
                            if time.monotonic() >= deadline:
                                raise RuntimeError("Audio decoder timed out.")
                    kind = message.get("type")
                    if kind == "error":
                        raise RuntimeError(message.get("message", "Audio decoding failed."))
                    if kind == "info":
                        duration = message.get("duration")
                        metadata["duration"] = duration if isinstance(duration, (int, float)) and math.isfinite(duration) else None
                    elif kind == "done":
                        metadata["complete"] = message.get("complete") is True
                        return
                    elif kind == "audio":
                        raw = base64.b64decode(message["pcm"], validate=True)
                        if len(raw) > 16000 * 30 * 4 or len(raw) % 4:
                            raise RuntimeError("Invalid decoder audio size.")
                        audio = np.frombuffer(raw, dtype="<f4")
                        offset = float(message["offset"])
                        if not math.isfinite(offset) or not 0 <= offset <= 4 * 3600 or not np.isfinite(audio).all():
                            raise RuntimeError("Invalid decoded audio.")
                        with self._admission():
                            if cancelled is not None and cancelled.is_set():
                                return
                            segments = speech.transcribe_media_chunk(audio)
                        for start, end, text in segments:
                            yield offset + start, offset + end, text
            finally:
                closed.set()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()
                thread.join(timeout=2)

        return timed(), metadata
