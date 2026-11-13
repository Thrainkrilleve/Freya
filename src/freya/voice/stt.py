from pathlib import Path
from functools import wraps
import math
import threading
import tempfile
import time

import numpy as np
import sounddevice as sd
import soundfile as sf
from faster_whisper import WhisperModel


WHISPER_MODEL = "small"
WHISPER_DEVICE = "cuda"
WHISPER_COMPUTE_TYPE = "float16"

MICROPHONE_DEVICE = 1
MICROPHONE_SAMPLE_RATE = 44100
MICROPHONE_CHANNELS = 1

# Automatic end-of-speech detection.
VAD_BLOCK_DURATION = 0.05
VAD_START_THRESHOLD = 0.012
VAD_STOP_THRESHOLD = 0.009
VAD_SILENCE_SECONDS = 1.25
VAD_MAX_WAIT_SECONDS = 10.0
VAD_MAX_RECORD_SECONDS = 60.0

_RECOGNITION_LOCK = threading.RLock()


def serialized_recognition(method):
    @wraps(method)
    def run(*args, **kwargs):
        with _RECOGNITION_LOCK:
            return method(*args, **kwargs)
    return run


class FreyaSTT:
    def __init__(self):
        print("Loading Freya speech recognition engine...")

        self.model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )

        print("Freya speech recognition ready.")

    @serialized_recognition
    def transcribe(self, audio_file):
        audio_path = Path(audio_file)

        if not audio_path.exists():
            raise FileNotFoundError(
                f"Audio file not found: {audio_path}"
            )

        print(f"Transcribing: {audio_path.name}")

        segments, info = self.model.transcribe(
            str(audio_path),
            beam_size=5,
            vad_filter=True,
        )

        text_parts = []

        for segment in segments:
            text = segment.text.strip()

            if text:
                text_parts.append(text)

        transcript = " ".join(text_parts)

        return transcript, info

    @serialized_recognition
    def transcribe_audio(
        self,
        audio,
        sample_rate=MICROPHONE_SAMPLE_RATE,
        hotwords=None,
    ):
        """
        Transcribe audio already held in memory, without printing.

        hotwords nudges recognition towards particular words, such
        as the names Freya answers to.
        """

        temp_path = (
            Path(tempfile.gettempdir())
            / "freya_background_listen.wav"
        )

        sf.write(
            temp_path,
            audio,
            sample_rate,
        )

        try:
            segments, _ = self.model.transcribe(
                str(temp_path),
                beam_size=5,
                vad_filter=True,
                hotwords=hotwords,
            )

            return " ".join(
                segment.text.strip()
                for segment in segments
                if segment.text.strip()
            )
        finally:
            # Background audio is not kept once it has been read.
            temp_path.unlink(missing_ok=True)

    @serialized_recognition
    def transcribe_media_chunk(self, audio):
        """Analyze at most thirty seconds of already decoded 16 kHz sound."""
        if len(audio) > 16000 * 30:
            raise ValueError("Media audio chunk is too long.")
        segments, _ = self.model.transcribe(audio, beam_size=5, vad_filter=True)
        result = []
        for segment in segments:
            start, end = float(segment.start), float(segment.end)
            if math.isfinite(start) and math.isfinite(end) and 0 <= start <= end <= 30.1:
                result.append((start, end, segment.text))
        return result

    def record_push_to_talk(self):
        print()
        input("Press ENTER to start talking...")

        print("Listening... Press ENTER again to stop.")

        frames = []

        def callback(indata, frames_count, time_info, status):
            if status:
                print(f"Microphone status: {status}")
            frames.append(indata.copy())

        with sd.InputStream(
            device=MICROPHONE_DEVICE,
            samplerate=MICROPHONE_SAMPLE_RATE,
            channels=MICROPHONE_CHANNELS,
            dtype="float32",
            callback=callback,
        ):
            input()

        if not frames:
            raise RuntimeError("No microphone audio was captured.")

        audio = np.concatenate(frames, axis=0)
        temp_path = Path(tempfile.gettempdir()) / "freya_microphone.wav"

        sf.write(
            temp_path,
            audio,
            MICROPHONE_SAMPLE_RATE,
        )

        print("Recording stopped.")
        return temp_path

    def record_until_silence(self, wait_for_enter=True):
        print()
        if wait_for_enter:
            input("Press ENTER when you're ready to talk...")

        print("Listening... Start speaking.")

        blocksize = int(
            MICROPHONE_SAMPLE_RATE
            * VAD_BLOCK_DURATION
        )

        frames = []
        speech_started = False
        silence_duration = 0.0
        wait_started = time.monotonic()
        speech_started_at = None

        with sd.InputStream(
            device=MICROPHONE_DEVICE,
            samplerate=MICROPHONE_SAMPLE_RATE,
            channels=MICROPHONE_CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                audio_block, overflowed = stream.read(
                    blocksize
                )

                if overflowed:
                    print(
                        "Microphone warning: "
                        "input overflow."
                    )

                level = float(
                    np.sqrt(
                        np.mean(
                            np.square(audio_block)
                        )
                    )
                )

                if not speech_started:
                    if level >= VAD_START_THRESHOLD:
                        speech_started = True
                        speech_started_at = (
                            time.monotonic()
                        )
                        frames.append(
                            audio_block.copy()
                        )
                        print(
                            "Speech detected..."
                        )

                    elif (
                        time.monotonic()
                        - wait_started
                        >= VAD_MAX_WAIT_SECONDS
                    ):
                        raise RuntimeError(
                            "No speech was detected."
                        )

                    continue

                frames.append(
                    audio_block.copy()
                )

                if level < VAD_STOP_THRESHOLD:
                    silence_duration += (
                        VAD_BLOCK_DURATION
                    )
                else:
                    silence_duration = 0.0

                if (
                    silence_duration
                    >= VAD_SILENCE_SECONDS
                ):
                    print(
                        "Silence detected. "
                        "Recording stopped."
                    )
                    break

                if (
                    speech_started_at is not None
                    and time.monotonic()
                    - speech_started_at
                    >= VAD_MAX_RECORD_SECONDS
                ):
                    print(
                        "Maximum recording time "
                        "reached."
                    )
                    break

        if not frames:
            raise RuntimeError(
                "No microphone audio was captured."
            )

        audio = np.concatenate(
            frames,
            axis=0,
        )

        temp_path = (
            Path(tempfile.gettempdir())
            / "freya_microphone_vad.wav"
        )

        sf.write(
            temp_path,
            audio,
            MICROPHONE_SAMPLE_RATE,
        )

        return temp_path

