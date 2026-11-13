import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import psutil
import soundfile as sf

from freya.child_environment import child_environment
from freya.speech.engines import (
    STATUS_NOT_INSTALLED,
    STATUS_READY,
    SpeechEngine,
    SpeechError,
)
from freya.paths import data_dir


DATA_DIR = data_dir()
WORKERS_DIR = Path(__file__).resolve().parent / "workers"

# Each helper engine lives in its own Python environment here, because
# the engines need library versions that conflict with Freya's and with
# each other's. Their model files are kept together in MODELS_DIR.
ENVS_DIR = Path(os.environ.get("FREYA_SPEECH_ENVS") or DATA_DIR / "speech_envs")
MODELS_DIR = Path(
    os.environ.get("FREYA_SPEECH_MODELS") or DATA_DIR / "speech_models"
)

START_TIMEOUT_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 600
IDLE_UNLOAD_SECONDS = 300


def environment_python(engine_id: str, envs_dir=None) -> Path:
    root = Path(envs_dir or ENVS_DIR) / engine_id

    if os.name == "nt":
        return root / "Scripts" / "python.exe"

    return root / "bin" / "python"


class HelperProcess:
    """
    One speech engine running as a separate program in its own Python
    environment.

    It is started the first time it is needed and stopped to give its
    memory back: on request, or after it has sat idle. Requests and
    answers are single lines of JSON over the helper's standard input
    and output; whatever else it prints goes to a log file.
    """

    def __init__(
        self,
        python,
        script,
        log_path=None,
        idle_seconds=IDLE_UNLOAD_SECONDS,
        environment=None,
    ):
        self._python = str(python)
        self._script = str(script)
        self._log_path = Path(log_path) if log_path else None
        self._idle_seconds = idle_seconds
        self._environment = dict(environment or {})

        self._lock = threading.Lock()
        self._process = None
        self._answers: queue.Queue = queue.Queue()
        self._idle_timer = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start(self) -> None:
        log = subprocess.DEVNULL

        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(self._log_path, "w", encoding="utf-8")

        environment = child_environment(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                **self._environment,
            }
        )
        # The helper must use its own environment's packages only.
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment.pop("VIRTUAL_ENV", None)

        try:
            self._process = subprocess.Popen(
                [self._python, self._script],
                cwd=str(Path(self._script).parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                env=environment,
                text=True,
                encoding="utf-8",
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
        except OSError as exc:
            self._process = None
            raise SpeechError(f"The speech helper could not be started: {exc}")
        finally:
            if log is not subprocess.DEVNULL:
                log.close()

        # If Freya dies without a normal exit, Windows ends the helper.
        from freya.providers.image.comfyui import _tie_to_freya

        _tie_to_freya(self._process)

        self._answers = queue.Queue()
        threading.Thread(
            target=self._read_answers,
            args=(self._process, self._answers),
            name="FreyaSpeechHelper",
            daemon=True,
        ).start()

        ready = self._next_answer(START_TIMEOUT_SECONDS)

        if not ready.get("ready"):
            self._kill()
            raise SpeechError("The speech helper did not start properly.")

    @staticmethod
    def _read_answers(process, answers) -> None:
        for line in process.stdout:
            try:
                answers.put(json.loads(line))
            except ValueError:
                continue

        # The helper has exited.
        answers.put(None)

    def _next_answer(self, timeout) -> dict:
        try:
            answer = self._answers.get(timeout=timeout)
        except queue.Empty:
            self._kill()
            raise SpeechError("The speech helper took too long and was stopped.")

        if answer is None:
            self._kill()
            raise SpeechError(
                "The speech helper stopped unexpectedly." + self._log_hint()
            )

        return answer

    def _log_hint(self) -> str:
        if self._log_path is None:
            return ""

        return f" Its output is in {self._log_path}."

    def request(self, message: dict, timeout=REQUEST_TIMEOUT_SECONDS) -> dict:
        """
        Send one request and wait for its answer. Raises SpeechError
        when the helper reports a failure, dies, or takes too long.
        """
        with self._lock:
            self._cancel_idle_timer()

            if not self.running:
                self._start()

            try:
                self._process.stdin.write(json.dumps(message) + "\n")
                self._process.stdin.flush()
            except OSError:
                self._kill()
                raise SpeechError(
                    "The speech helper stopped unexpectedly."
                    + self._log_hint()
                )

            try:
                answer = self._next_answer(timeout)
            finally:
                self._start_idle_timer()

        if not answer.get("ok"):
            raise SpeechError(
                str(answer.get("error") or "The speech helper failed.")
            )

        return answer

    def memory_megabytes(self) -> Optional[int]:
        """
        The most main memory the helper has used so far, or None when
        it is not running.
        """
        if not self.running:
            return None

        try:
            parent = psutil.Process(self._process.pid)
            family = parent.children(recursive=True) + [parent]
            total = 0

            for member in family:
                memory = member.memory_info()
                total += getattr(memory, "peak_wset", memory.rss)

            return round(total / 1024 / 1024)
        except psutil.Error:
            return None

    def _start_idle_timer(self) -> None:
        if self._idle_seconds and self.running:
            self._idle_timer = threading.Timer(self._idle_seconds, self.stop)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def _kill(self) -> None:
        process, self._process = self._process, None

        if process is None:
            return

        try:
            process.stdin.close()
        except OSError:
            pass

        # A virtual environment's python.exe on Windows is a small
        # launcher that runs the real interpreter as a child, so the
        # whole family is ended, not just the process Freya started.
        try:
            parent = psutil.Process(process.pid)
            family = parent.children(recursive=True) + [parent]
        except psutil.Error:
            family = []

        for member in family:
            try:
                member.kill()
            except psutil.Error:
                pass

        psutil.wait_procs(family, timeout=10)

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def stop(self) -> None:
        """
        End the helper, which gives back all the memory it was using.
        """
        with self._lock:
            self._cancel_idle_timer()
            self._kill()


class HelperEngine(SpeechEngine):
    """
    Base for an engine that runs through a HelperProcess.

    Only one such engine is kept loaded at a time: the studio stops the
    others before using one, because together they would not fit in
    the graphics card's memory alongside the language model.
    """

    # Stopped to make room when another engine is about to load.
    exclusive = True

    worker_script = ""
    # Roughly what the engine needs free on the graphics card.
    gpu_megabytes_needed = 0

    def __init__(
        self,
        references,
        envs_dir=None,
        python=None,
        script=None,
        make_room: Optional[Callable[[int], None]] = None,
        idle_seconds=IDLE_UNLOAD_SECONDS,
    ):
        self._references = references
        self._python = Path(python or environment_python(self.id, envs_dir))
        self._make_room = make_room
        self._helper = HelperProcess(
            self._python,
            script or WORKERS_DIR / self.worker_script,
            log_path=DATA_DIR / f"speech_{self.id}.log",
            idle_seconds=idle_seconds,
            environment={
                "HF_HOME": str(MODELS_DIR),
                # Model files are fetched at install time. In use, the
                # helper never contacts the network.
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
            },
        )
        # What the last request cost, for the benchmark.
        self.last_metrics: dict = {}

    @property
    def installed(self) -> bool:
        return self._python.is_file()

    @property
    def loaded(self) -> bool:
        return self._helper.running

    def _status(self) -> str:
        return STATUS_READY if self.installed else STATUS_NOT_INSTALLED

    def _run(self, operation: str, **fields):
        """
        Have the helper generate speech into a temporary WAV file, and
        return (audio, sample_rate).
        """
        if not self.installed:
            raise SpeechError(f"{self.info().name} is not installed.")

        if not self.loaded and self._make_room is not None:
            self._make_room(self.gpu_megabytes_needed)

        handle, output = tempfile.mkstemp(suffix=".wav", prefix="freya_speech_")
        os.close(handle)

        try:
            started = time.perf_counter()
            answer = self._helper.request(
                {"op": operation, "output": output, **fields}
            )
            audio, sample_rate = sf.read(output, dtype="float32")
        finally:
            try:
                os.unlink(output)
            except OSError:
                pass

        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        if not len(audio):
            raise SpeechError(f"{self.info().name} produced no audio.")

        self.last_metrics = {
            key: answer.get(key)
            for key in (
                "load_seconds",
                "generate_seconds",
                "first_audio_seconds",
                "gpu_peak_mb",
                "device",
            )
        }
        self.last_metrics["total_seconds"] = round(
            time.perf_counter() - started, 3
        )

        return np.asarray(audio, dtype=np.float32), int(sample_rate)

    def unload(self) -> None:
        self._helper.stop()


def number(value, low, high, default, name) -> float:
    """
    A control's value, checked against the range the engine supports.
    """
    if value is None:
        return default

    try:
        value = float(value)
    except (TypeError, ValueError):
        raise SpeechError(f"{name} must be a number.")

    if not low <= value <= high:
        raise SpeechError(f"{name} must be between {low} and {high}.")

    return round(value, 2)
