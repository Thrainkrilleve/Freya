import queue
import re
import threading

import numpy as np
import sounddevice as sd
from kokoro import KPipeline


VOICE_ENABLED = True
FREYA_VOICE = "af_heart"
VOICE_SPEED = 1.0
SAMPLE_RATE = 24000


class FreyaVoice:
    def __init__(self, enabled=None):
        self.enabled = VOICE_ENABLED if enabled is None else enabled
        self.startup_error = None
        self.voice = FREYA_VOICE
        self.speed = VOICE_SPEED
        # How loud speech is played, from 0 (silent) to 1 (as generated).
        self.volume = 1.0
        self.pipeline = None

        self._speech_queue = queue.Queue()
        self._shutdown_event = threading.Event()
        self._interrupt_event = threading.Event()
        self._speaking_event = threading.Event()
        self._worker = None

        # Told when speech starts, completes, or is interrupted (see
        # freya.spatial_avatar.speech.SpeechListener). Anything may
        # listen; the voice does not know or care what does.
        self.speech_listener = None

        # When a GUI client plays Freya's voice (see
        # freya.gateway.speech_relay), speech goes to it instead of to
        # this computer's speakers. Unset, she plays it herself.
        self.remote_playback = None

        if self.enabled:
            print("Loading Freya voice engine...")
            try:
                self.pipeline = KPipeline(
                    lang_code="a",
                    repo_id="hexgrad/Kokoro-82M",
                )
            except Exception as exc:
                # Speech is optional: a missing model must not prevent chat.
                self.enabled = False
                self.startup_error = str(exc)
                print("Voice unavailable; text chat remains available.")
                return

            self._worker = threading.Thread(
                target=self._speech_worker,
                name="FreyaVoiceWorker",
                daemon=True,
            )
            self._worker.start()

    def _tell_listener(self, what, *arguments):
        listener = self.speech_listener

        if listener is None:
            return

        try:
            getattr(listener, what)(*arguments)
        except Exception:
            # A listener must never be able to break speech.
            pass

    def set_voice(self, voice, speed=VOICE_SPEED, lang_code="a"):
        """
        Change the voice Freya speaks with from now on.

        voice is a supplied voice's name or the data of a blended
        voice. A different language gets its own pipeline, sharing the
        model that is already loaded.
        """

        if not self.enabled:
            return

        if lang_code != self.pipeline.lang_code:
            self.pipeline = KPipeline(
                lang_code=lang_code,
                repo_id="hexgrad/Kokoro-82M",
                model=self.pipeline.model,
            )

        self.voice = voice
        self.speed = speed

    def set_volume(self, percent):
        """
        Set how loudly Freya speaks, as a percentage from 0 to 100. It
        applies from the next sentence she starts.
        """
        self.volume = min(max(float(percent), 0.0), 100.0) / 100.0

    def reset_voice(self):
        self.set_voice(FREYA_VOICE, VOICE_SPEED, "a")

    def _split_into_chunks(self, text):
        text = text.strip()

        if not text:
            return []

        chunks = re.split(
            r"(?<=[.!?])\s+",
            text,
        )

        return [
            chunk.strip()
            for chunk in chunks
            if chunk.strip()
        ]

    def _generate_audio(self, text):
        if not self.enabled or not text.strip():
            return None

        generator = self.pipeline(
            text,
            voice=self.voice,
            speed=self.speed,
        )

        audio_parts = []

        for _, _, audio in generator:
            if self._interrupt_event.is_set():
                return None

            audio_parts.append(audio)

        if not audio_parts:
            return None

        return np.concatenate(audio_parts)

    def _play_audio(self, audio):
        if audio is None:
            return

        self._speaking_event.set()
        self._tell_listener("speech_started", "")
        interrupted = False

        try:
            relay = getattr(self, "remote_playback", None)

            if relay is not None and relay.active:
                # The client gets the sound at full level and is told the
                # volume, so a quiet voice still moves a face fully. This
                # waits until the client says it has finished, which is
                # what keeps the microphone ignoring her own voice.
                outcome = relay.play(
                    audio,
                    SAMPLE_RATE,
                    self.volume,
                    self._interrupt_event,
                )

                if outcome != "no-client":
                    # Played, stopped, or lost with the client. A lost
                    # sentence is not repeated: part of it may have been
                    # heard.
                    interrupted = outcome == "interrupted"
                    return

            volume = self.volume

            if volume != 1.0:
                audio = np.asarray(audio, dtype=np.float32) * volume

            sd.play(
                audio,
                SAMPLE_RATE,
                blocking=False,
            )

            while True:
                if self._interrupt_event.is_set():
                    sd.stop()
                    interrupted = True
                    return

                stream = sd.get_stream()

                if stream is None or not stream.active:
                    return

                self._interrupt_event.wait(0.02)

        except Exception as exc:
            print(
                f"\n[Voice playback error: {exc}]"
            )

        finally:
            self._speaking_event.clear()
            self._tell_listener(
                "speech_interrupted" if interrupted else "speech_completed"
            )

    def _discard_queued_speech(self):
        while True:
            try:
                item = self._speech_queue.get_nowait()
            except queue.Empty:
                break

            if item is None:
                # Preserve the shutdown sentinel if one was queued.
                self._speech_queue.put(None)

            self._speech_queue.task_done()

            if item is None:
                break

    def _speech_worker(self):
        while True:
            item = self._speech_queue.get()

            try:
                if item is None:
                    return

                # An interrupt applies to the speech that existed at the
                # moment interrupt() was called. New speech may proceed.
                if self._interrupt_event.is_set():
                    continue

                audio = self._generate_audio(
                    item
                )

                if self._interrupt_event.is_set():
                    continue

                self._play_audio(
                    audio
                )

            except Exception as exc:
                print(
                    f"\n[Voice worker error: {exc}]"
                )

            finally:
                self._speech_queue.task_done()

                # Once the interrupted item and its old queue are gone,
                # allow future speak() calls to start a fresh utterance.
                if (
                    self._interrupt_event.is_set()
                    and self._speech_queue.empty()
                ):
                    self._interrupt_event.clear()

    def speak(self, text):
        if (
            not self.enabled
            or self._shutdown_event.is_set()
        ):
            return

        # If a previous interrupt has fully drained, make sure new speech
        # is allowed immediately.
        if (
            self._interrupt_event.is_set()
            and not self._speaking_event.is_set()
            and self._speech_queue.empty()
        ):
            self._interrupt_event.clear()

        chunks = self._split_into_chunks(
            text
        )

        for chunk in chunks:
            self._speech_queue.put(
                chunk
            )

    def interrupt(self):
        if not self.enabled:
            return

        self._interrupt_event.set()
        sd.stop()
        self._discard_queued_speech()

    def is_speaking(self):
        return self._speaking_event.is_set()

    def is_busy(self):
        """
        True while speech is playing or still waiting to be spoken.
        """

        if not self.enabled:
            return False

        return (
            self._speaking_event.is_set()
            or self._speech_queue.unfinished_tasks > 0
        )

    def wait_until_done(self):
        if not self.enabled:
            return

        self._speech_queue.join()

    def shutdown(self):
        if (
            not self.enabled
            or self._shutdown_event.is_set()
        ):
            return

        self._shutdown_event.set()
        self.interrupt()

        self._speech_queue.put(
            None
        )

        if self._worker is not None:
            self._worker.join()
