import struct
import threading
from typing import Callable, Optional

import numpy as np


# One binary message of speech: these four bytes, the utterance number
# and the frame's place in it (both unsigned 32-bit, little-endian),
# then mono signed 16-bit little-endian samples.
FRAME_MAGIC = b"FSP1"
FRAME_HEADER = struct.Struct("<4sII")

FRAME_MILLISECONDS = 100
# The longest single utterance sent to a client. Freya speaks a sentence
# at a time; anything longer than this is cut, and says so.
MAX_UTTERANCE_SECONDS = 30
# How long past the audio's own length a client is given to say it has
# finished playing, before Freya stops waiting.
FINISH_MARGIN_SECONDS = 3.0

FINISHED = "finished"         # the client played it to the end
INTERRUPTED = "interrupted"   # Freya was told to stop speaking
LOST = "lost"                 # the client went away part-way through
TIMED_OUT = "timed-out"       # the client never said it had finished
NO_CLIENT = "no-client"       # nobody is playing speech; play it here


def speech_frame(utterance: int, sequence: int, samples: np.ndarray) -> bytes:
    return FRAME_HEADER.pack(FRAME_MAGIC, utterance, sequence) + samples.astype("<i2").tobytes()


class SpeechRelay:
    """
    Sends Freya's speech to the one client that plays it, and knows when
    that client has actually finished playing it.

    At most one client holds playback at a time. While one does, Freya's
    own voice does not play: each sentence is sent to the client at full
    level, with the volume to play it at given separately, so that
    turning her down does not weaken what a face makes of the sound.

    `play` blocks, like playing a sound does, so that whoever called it
    counts as speaking for exactly as long as the client says the sound
    is audible. If the client disappears, the sentence is abandoned and
    never repeated: nobody can know how much of it was heard.
    """

    def __init__(self):
        self._lock = threading.Condition()
        self._send: Optional[Callable[[object], bool]] = None
        self._owner = 0
        self._utterance = 0
        # The utterance being played and how it ended, if it has.
        self._current = 0
        self._outcome: Optional[str] = None

    # -- who plays ------------------------------------------------------------

    @property
    def active(self) -> bool:
        with self._lock:
            return self._send is not None

    def claim(self, send: Callable[[object], bool]) -> Optional[int]:
        """
        Make a client the one that plays speech. `send` delivers one
        message to it (a dict or bytes) and returns False when it could
        not. Returns a number identifying this claim, or None when
        another client already has playback.
        """
        with self._lock:
            if self._send is not None:
                return None

            self._owner += 1
            self._send = send

            return self._owner

    def release(self, owner) -> None:
        """
        The client has gone, or gives playback up. Anything it was
        playing is counted as lost.
        """
        with self._lock:
            if owner != self._owner or self._send is None:
                return

            self._send = None

            if self._current and self._outcome is None:
                self._outcome = LOST

            self._lock.notify_all()

    def acknowledge(self, owner, message: dict) -> None:
        """
        A client's report about playback. Only the client that holds
        playback is listened to, and only about the current utterance.
        """
        kind = message.get("type")
        utterance = message.get("utterance")

        with self._lock:
            if (
                owner != self._owner
                or self._send is None
                or utterance != self._current
                or self._outcome is not None
            ):
                return

            if kind == "speech.finished":
                self._outcome = FINISHED
            elif kind == "speech.interrupted":
                self._outcome = INTERRUPTED
            else:
                return

            self._lock.notify_all()

    # -- playing ----------------------------------------------------------------

    def play(self, audio, sample_rate: int, volume: float,
             interrupted: threading.Event, text: str = "") -> str:
        """
        Have the client play one utterance and wait until it has. Returns
        how it ended. NO_CLIENT means nothing was sent and the caller
        should play it itself.
        """
        samples = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1.0, 1.0)
        samples = samples[: MAX_UTTERANCE_SECONDS * sample_rate]
        pcm = np.round(samples * 32767.0).astype(np.int16)
        seconds = len(pcm) / float(sample_rate)

        with self._lock:
            send = self._send

            if send is None:
                return NO_CLIENT

            owner = self._owner
            self._utterance += 1
            utterance = self._current = self._utterance
            self._outcome = None

        frame = max(int(sample_rate * FRAME_MILLISECONDS / 1000), 1)
        messages = [
            {
                "type": "speech.start",
                "utterance": utterance,
                "sample_rate": sample_rate,
                "channels": 1,
                "format": "pcm_s16le",
                "samples": len(pcm),
                "volume": round(min(max(float(volume), 0.0), 1.0), 3),
                "text": str(text)[:2000],
            }
        ]
        messages += [
            speech_frame(utterance, index, pcm[start:start + frame])
            for index, start in enumerate(range(0, len(pcm), frame))
        ]
        messages.append({"type": "speech.end", "utterance": utterance, "samples": len(pcm)})

        for message in messages:
            if interrupted.is_set():
                break

            if not send(message):
                self.release(owner)
                break

        with self._lock:
            deadline = seconds + FINISH_MARGIN_SECONDS
            waited = 0.0

            while self._outcome is None:
                if interrupted.is_set():
                    self._outcome = INTERRUPTED
                    break

                if waited >= deadline:
                    self._outcome = TIMED_OUT
                    break

                self._lock.wait(0.02)
                waited += 0.02

            outcome = self._outcome
            self._current = 0
            still_there = self._send is send

        if outcome in (INTERRUPTED, TIMED_OUT) and still_there:
            # Tell the client to fall silent; it may still be playing.
            send({"type": "speech.cancel", "utterance": utterance})

        return outcome
