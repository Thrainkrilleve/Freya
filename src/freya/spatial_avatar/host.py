import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import psutil

from freya.child_environment import child_environment
from freya.spatial_avatar import PROTOCOL_VERSION
from freya.spatial_avatar import commands as c
from freya.spatial_avatar.geometry import DisplayLayout, GeometryError
from freya.paths import data_dir


DATA_DIR = data_dir()

START_TIMEOUT_SECONDS = 20.0
STOP_TIMEOUT_SECONDS = 3.0

# An event the host itself raises when the overlay process has gone.
STOPPED = "stopped"


class AvatarError(Exception):
    """
    The avatar could not do something. Safe to show the user.
    """


class AvatarHost:
    """
    Runs the avatar overlay as a separate program and talks to it.

    The overlay is deliberately kept at arm's length. It is another
    process, so if it crashes, hangs, or is closed, nothing here is
    affected beyond `running` becoming False; and nothing it sends is
    trusted beyond being an event to look at. Sending a command never
    raises: if the overlay is not there, the command is simply not
    delivered and send() says so.
    """

    def __init__(
        self,
        renderer: str = "tk-placeholder",
        on_event: Optional[Callable[[dict], None]] = None,
        python: Optional[str] = None,
        extra_arguments=(),
        log_path=None,
        environment=None,
    ):
        self.renderer = renderer
        # Extra environment variables for the overlay program.
        self.environment = dict(environment or {})
        self._on_event = on_event or (lambda message: None)
        self._python = python or sys.executable
        self._extra = list(extra_arguments)
        self._log_path = Path(log_path or DATA_DIR / "avatar_overlay.log")

        self._lock = threading.Lock()
        self._process = None
        self._ready: queue.Queue = queue.Queue()
        self._stopping = False

        self.layout: Optional[DisplayLayout] = None
        self.state: Optional[dict] = None
        self.appearances = []
        self.last_error = ""

    def use(self, renderer: str, environment=None) -> None:
        """
        Choose the renderer the overlay will be started with next.
        """
        self.renderer = renderer
        self.environment = dict(environment or {})

    @property
    def running(self) -> bool:
        process = self._process

        return process is not None and process.poll() is None

    # -- starting and stopping ---------------------------------------------

    def start(self) -> None:
        """
        Start the overlay and wait until it is ready. Raises
        AvatarError when it does not start; Freya is unaffected.
        """
        with self._lock:
            if self.running:
                return

            self._stopping = False
            self._ready = queue.Queue()
            self.state = None
            self.last_error = ""

            try:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                log = open(self._log_path, "w", encoding="utf-8")
            except OSError:
                log = subprocess.DEVNULL

            try:
                process = subprocess.Popen(
                    [
                        self._python,
                        "-m",
                        "freya.spatial_avatar.overlay",
                        "--renderer",
                        self.renderer,
                        *self._extra,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=log,
                    text=True,
                    encoding="utf-8",
                    env=child_environment(
                        {
                            **self.environment,
                            "PYTHONIOENCODING": "utf-8",
                        }
                    ),
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                    ),
                )
            except OSError as exc:
                raise AvatarError(f"The avatar could not be started: {exc}")
            finally:
                if log is not subprocess.DEVNULL:
                    log.close()

            self._process = process

            threading.Thread(
                target=self._read_events,
                args=(process, self._ready),
                name="AvatarEvents",
                daemon=True,
            ).start()

        try:
            ready = self._ready.get(timeout=START_TIMEOUT_SECONDS)
        except queue.Empty:
            self._kill()
            raise AvatarError("The avatar took too long to start.")

        if ready is None:
            self._kill()
            raise AvatarError(
                "The avatar could not start."
                + (f" {self.last_error}" if self.last_error else "")
            )

        if ready.get("protocol") != PROTOCOL_VERSION:
            self._kill()
            raise AvatarError(
                "The avatar renderer speaks a different version and was not used."
            )

    def stop(self) -> None:
        """
        Close the overlay. Safe to call at any time, in any state.
        """
        with self._lock:
            self._stopping = True
            process = self._process

            if process is None:
                return

            if process.poll() is None:
                self._write(c.command(c.SHUTDOWN))

                try:
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass

        self._kill()

    def _kill(self) -> None:
        process, self._process = self._process, None

        if process is None:
            return

        try:
            process.stdin.close()
        except OSError:
            pass

        # The interpreter in a virtual environment is a launcher with
        # the real one as its child, so the whole family is ended.
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

        psutil.wait_procs(family, timeout=5)

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    # -- commands and events -------------------------------------------------

    def _write(self, message: dict) -> bool:
        process = self._process

        if process is None or process.poll() is not None:
            return False

        try:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
        except (OSError, ValueError):
            return False

        return True

    def send(self, message: dict) -> bool:
        """
        Send a command. Returns False, without raising, when the
        overlay is not running or has just gone.
        """
        with self._lock:
            return self._write(message)

    def _read_events(self, process, ready) -> None:
        announced = False

        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue

                if not c.compatible(message) and message.get("event") != c.READY:
                    continue

                kind = message.get("event")

                if kind == c.READY:
                    self._take_layout(message.get("layout"))
                    self.appearances = [
                        str(item) for item in message.get("appearances") or []
                    ]
                    announced = True
                    ready.put(message)
                elif kind == c.STATE and isinstance(message.get("state"), dict):
                    self.state = message["state"]
                elif kind == c.LAYOUT_CHANGED:
                    self._take_layout(message.get("layout"))
                elif kind == c.ERROR:
                    self.last_error = str(message.get("text") or "")[:300]

                self._deliver(message)
        except (OSError, ValueError):
            pass

        if not announced:
            ready.put(None)

        # The overlay has gone. Whether that was asked for or not,
        # nothing else here depends on it.
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = None

        if self._process is process:
            self._process = None

        self.state = None
        self._deliver(
            {
                "event": STOPPED,
                "expected": self._stopping,
                "code": code,
            }
        )

    def _take_layout(self, value) -> None:
        try:
            self.layout = DisplayLayout.from_value(value)
        except GeometryError:
            pass

    def _deliver(self, message: dict) -> None:
        try:
            self._on_event(message)
        except Exception:
            # Whoever is listening must not be able to stop events
            # being read.
            pass
