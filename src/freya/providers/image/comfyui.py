import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import psutil

from freya.child_environment import child_environment
from freya.core.providers import (
    ProviderDefinition,
    ProviderHealth,
)


COMFYUI_PROVIDER = "comfyui-local"

IMAGE_GENERATE = "image.generate"

HEALTH_TIMEOUT_SECONDS = 2
START_TIMEOUT_SECONDS = 180
START_POLL_SECONDS = 1.0
STOP_TIMEOUT_SECONDS = 10


@dataclass
class LaunchPlan:
    """
    How Freya will start ComfyUI.

    headless is True when Freya runs the ComfyUI server itself, with
    no application window. That server belongs to Freya, so she stops
    it when she exits.
    """

    command: List[str]
    cwd: Optional[str] = None
    headless: bool = False


def _local_app_data() -> Path:
    return Path(
        os.environ.get("LOCALAPPDATA")
        or Path.home() / "AppData" / "Local"
    )


def _roaming_app_data() -> Path:
    return Path(
        os.environ.get("APPDATA")
        or Path.home() / "AppData" / "Roaming"
    )


def _desktop_app() -> Path:
    return (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Comfy Desktop"
        / "Comfy Desktop.exe"
    )


def _port(url) -> int:
    return urllib.parse.urlparse(url).port or 8188


def find_server_plan(url) -> Optional[LaunchPlan]:
    """
    Work out how to run the ComfyUI server that Comfy Desktop
    installed, using the same models and input/output folders the
    application uses. Returns None when no such install is found.
    """
    installs = _local_app_data() / "Comfy-Desktop" / "ComfyUI-Installs"

    if not installs.is_dir():
        return None

    for install in sorted(installs.iterdir()):
        python = install / "ComfyUI" / ".venv" / "Scripts" / "python.exe"
        main = install / "ComfyUI" / "main.py"

        if not (python.is_file() and main.is_file()):
            continue

        command = [
            str(python),
            "-s",
            str(Path("ComfyUI") / "main.py"),
            "--port",
            str(_port(url)),
            "--disable-auto-launch",
        ]

        model_configs = sorted(
            (
                _roaming_app_data()
                / "Comfy Desktop"
                / "instance-model-paths"
            ).glob("*.yaml"),
            key=lambda path: path.stat().st_mtime,
        )

        if model_configs:
            command += [
                "--extra-model-paths-config",
                str(model_configs[-1]),
            ]

        shared = _local_app_data() / "Comfy-Desktop" / "ComfyUI-Shared"

        for flag, folder in (
            ("--input-directory", shared / "input"),
            ("--output-directory", shared / "output"),
        ):
            if folder.is_dir():
                command += [flag, str(folder)]

        return LaunchPlan(
            command=command,
            cwd=str(install),
            headless=True,
        )

    return None


def find_launch_plan(url, configured=None) -> Optional[LaunchPlan]:
    """
    Decide how to start ComfyUI, or return None when Freya does not
    know how.

    A configured program wins. Otherwise the server is run directly,
    which needs nobody at the screen. Opening the Comfy Desktop
    application is the last resort, because it waits for a click
    before it starts its server.
    """
    if configured:
        return LaunchPlan(command=[configured])

    plan = find_server_plan(url)

    if plan is not None:
        return plan

    if _desktop_app().is_file():
        return LaunchPlan(command=[str(_desktop_app())])

    return None


def check_comfyui_health(url):
    """
    HEALTHY when the ComfyUI server answers, otherwise OFFLINE.
    """
    try:
        with urllib.request.urlopen(
            f"{url.rstrip('/')}/system_stats",
            timeout=HEALTH_TIMEOUT_SECONDS,
        ) as response:
            json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return ProviderHealth.OFFLINE

    return ProviderHealth.HEALTHY


_job_handle = None


def _tie_to_freya(process) -> bool:
    """
    Make Windows end the server when Freya's own process ends, however
    that happens: closing her console window or a crash skips the
    normal shutdown that would stop it.

    The server is placed in a job object set to kill its processes
    when the last handle to the job closes, and Freya holds the only
    handle. Returns False when this could not be arranged.
    """
    global _job_handle

    if os.name != "nt":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [
            wintypes.LPVOID,
            wintypes.LPCWSTR,
        ]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kill_on_job_close = 0x2000
        extended_limit_information = 9

        if _job_handle is None:
            job = kernel32.CreateJobObjectW(None, None)

            if not job:
                return False

            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = kill_on_job_close

            if not kernel32.SetInformationJobObject(
                job,
                extended_limit_information,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                return False

            _job_handle = job

        return bool(
            kernel32.AssignProcessToJobObject(
                _job_handle,
                wintypes.HANDLE(int(process._handle)),
            )
        )
    except Exception:
        return False


def _launch(plan: LaunchPlan, log_path=None):
    """
    Start the planned program and return its process.

    A headless server gets no console window and writes its output to
    the log file. An application is started detached, so it keeps
    running after Freya exits.
    """
    flags = 0
    output = subprocess.DEVNULL

    if plan.headless:
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW

        if log_path is not None:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            output = open(log_path, "w", encoding="utf-8")
    elif os.name == "nt":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        process = subprocess.Popen(
            plan.command,
            cwd=plan.cwd,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=child_environment(),
            creationflags=flags,
            close_fds=True,
        )
    finally:
        if output is not subprocess.DEVNULL:
            output.close()

    if plan.headless:
        _tie_to_freya(process)

    return process


def _stop_process_tree(process) -> None:
    """
    Stop a process Freya started, along with anything it started.
    """
    try:
        parent = psutil.Process(process.pid)
    except psutil.NoSuchProcess:
        return

    family = parent.children(recursive=True) + [parent]

    for member in family:
        try:
            member.terminate()
        except psutil.NoSuchProcess:
            pass

    _, alive = psutil.wait_procs(family, timeout=STOP_TIMEOUT_SECONDS)

    for member in alive:
        try:
            member.kill()
        except psutil.NoSuchProcess:
            pass


class ComfyUIServer:
    """
    Starts ComfyUI when it is needed and stops it again if, and only
    if, Freya was the one who started its server.
    """

    def __init__(
        self,
        url,
        configured_command=None,
        log_path=None,
        timeout=START_TIMEOUT_SECONDS,
        launch=None,
        stop_process=None,
        sleep=None,
        clock=None,
    ):
        self.url = url
        self.plan = find_launch_plan(url, configured_command)
        self._log_path = log_path
        self._timeout = timeout
        self._launch = launch or _launch
        self._stop_process = stop_process or _stop_process_tree
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._process = None

    @property
    def owns_server(self) -> bool:
        return self._process is not None

    def health(self):
        return check_comfyui_health(self.url)

    def start(self) -> str:
        """
        Make sure the ComfyUI server is answering, starting it when it
        is not. Raises RuntimeError when it cannot be started or does
        not come up in time.
        """
        if self.health() == ProviderHealth.HEALTHY:
            return "already running"

        if self.plan is None:
            raise RuntimeError(
                "ComfyUI is not running and Freya does not know how "
                "to start it. Start it yourself, or set "
                "FREYA_COMFYUI_COMMAND to the program that starts it."
            )

        # A server Freya started earlier that has stopped answering.
        self.stop()

        try:
            process = self._launch(self.plan, self._log_path)
        except OSError as exc:
            raise RuntimeError(
                f"ComfyUI could not be started: {exc}"
            ) from exc

        if self.plan.headless:
            self._process = process

        deadline = self._clock() + self._timeout

        while self._clock() < deadline:
            self._sleep(START_POLL_SECONDS)

            if self.health() == ProviderHealth.HEALTHY:
                return "started"

            if (
                self.plan.headless
                and process is not None
                and process.poll() is not None
            ):
                self._process = None
                raise RuntimeError(
                    "The ComfyUI server stopped while starting up"
                    + self._log_hint()
                )

        self.stop()

        if self.plan.headless:
            raise RuntimeError(
                "The ComfyUI server did not become ready within "
                f"{self._timeout:.0f} seconds" + self._log_hint()
            )

        raise RuntimeError(
            "ComfyUI was opened but did not become ready within "
            f"{self._timeout:.0f} seconds. It may be waiting for you "
            "in its own window."
        )

    def stop(self) -> str:
        """
        Stop the server if Freya started it. A ComfyUI the owner
        started is left alone.
        """
        if self._process is None:
            return "not ours"

        process, self._process = self._process, None
        self._stop_process(process)

        return "stopped"

    def _log_hint(self) -> str:
        if self._log_path is None:
            return "."

        reason = failure_reason(self._log_path)

        if reason:
            return f": {reason} Its output is in {self._log_path}."

        return f". Its output is in {self._log_path}."


LOG_TAIL_BYTES = 20_000
MAX_REASON_CHARACTERS = 300


def failure_reason(log_path) -> str:
    """
    Why the server stopped, in a sentence, from the end of its own
    log. Empty when the log does not say.

    The log is ComfyUI's output, which is not trusted text: the
    reason is cut to a line and a length before anyone is shown it.
    """
    try:
        with open(log_path, "rb") as log:
            log.seek(0, 2)
            log.seek(max(0, log.tell() - LOG_TAIL_BYTES))
            tail = log.read().decode("utf-8", errors="replace")
    except OSError:
        return ""

    lines = [line.strip() for line in tail.splitlines() if line.strip()]

    for line in reversed(lines):
        if "Application Control policy has blocked" in line:
            # Windows Smart App Control, or a similar policy, refused
            # to load one of ComfyUI's own files.
            return (
                "Windows blocked one of ComfyUI's files (an Application "
                "Control policy, such as Smart App Control)."
            )

    for line in reversed(lines[-5:]):
        if "Error" in line or "error" in line:
            return line[:MAX_REASON_CHARACTERS].rstrip(".") + "."

    return ""


def create_comfyui_provider(server: ComfyUIServer):
    """
    Describe the local ComfyUI server as an image provider.
    """
    return ProviderDefinition(
        provider_id=COMFYUI_PROVIDER,
        name="Local ComfyUI image server",
        capabilities=[IMAGE_GENERATE],
        start_handler=server.start,
        stop_handler=server.stop,
        health_handler=server.health,
        metadata={
            "url": server.url,
            "can_start": server.plan is not None,
            "headless": bool(server.plan and server.plan.headless),
        },
    )
