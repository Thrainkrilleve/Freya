"""Trusted audio decoder helper; no Freya imports or network inputs."""
import base64
import json
import os
from pathlib import Path
import sys

RATE = 16000
CHUNK = RATE * 30
LIMIT = RATE * 4 * 3600


def emit(value):
    print(json.dumps(value), flush=True)


def checked(handle, path):
    if os.name == "nt":
        import ctypes
        import msvcrt
        get_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
        get_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint]
        get_path.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_path(msvcrt.get_osfhandle(handle.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ValueError("Cannot verify input.")
        name = buffer.value
        name = "\\\\" + name[8:] if name.startswith("\\\\?\\UNC\\") else name.removeprefix("\\\\?\\")
    else:
        name = os.path.realpath(f"/proc/self/fd/{handle.fileno()}")
    if Path(name) != path or path.resolve(strict=True) != path:
        raise ValueError("Input path changed.")


def decode(path, size, modified):
    import av
    import numpy as np
    path = Path(path)
    with path.open("rb") as handle:
        checked(handle, path)
        info = os.fstat(handle.fileno())
        if info.st_size != size or info.st_mtime_ns != modified:
            raise ValueError("Input changed.")
        with av.open(handle, mode="r", options={
            "protocol_whitelist": "pipe",
            "format_whitelist": "mov,matroska,webm,mp3,aac,ogg,wav,flac",
            "enable_drefs": "0",
        }) as container:
            if not container.streams.audio:
                raise ValueError("No audio stream.")
            emit({"type": "info", "duration": container.duration / av.time_base if container.duration else None})
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=RATE)
            pending = np.empty(0, dtype=np.float32)
            total = 0
            origin = None
            container_start = container.start_time / av.time_base if container.start_time else 0
            for frame in container.decode(container.streams.audio[0]):
                if frame.samples > RATE * 300:
                    raise ValueError("Audio frame too large.")
                for normalized in resampler.resample(frame):
                    stamp = float(normalized.pts * normalized.time_base) - container_start if normalized.pts is not None else None
                    if origin is None:
                        origin = max(0.0, stamp or 0.0)
                    if stamp is not None and abs(stamp - origin - (total + pending.size) / RATE) > 0.1:
                        raise ValueError("Audio has a discontinuous timeline; supply timed subtitles instead.")
                    pending = np.concatenate((pending, normalized.to_ndarray().reshape(-1).astype(np.float32)))
                    while pending.size >= CHUNK:
                        if total >= LIMIT:
                            emit({"type": "done", "complete": False})
                            return
                        audio, pending = pending[:CHUNK], pending[CHUNK:]
                        emit({"type": "audio", "offset": origin + total / RATE, "pcm": base64.b64encode(audio.astype("<f4").tobytes()).decode("ascii")})
                        total += CHUNK
            for normalized in resampler.resample(None):
                pending = np.concatenate((pending, normalized.to_ndarray().reshape(-1).astype(np.float32)))
            if pending.size and total < LIMIT:
                pending = pending[:min(CHUNK, LIMIT - total)]
                emit({"type": "audio", "offset": (origin or 0) + total / RATE, "pcm": base64.b64encode(pending.astype("<f4").tobytes()).decode("ascii")})
            emit({"type": "done", "complete": total + pending.size < LIMIT})


if __name__ == "__main__":
    try:
        decode(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
    except Exception:
        emit({"type": "error", "message": "The audio decoder could not read this file."})
        sys.exit(1)
