"""
The sandbox profile for Windows 11 Home: a fresh Linux container, started
inside a WSL2 distribution used for nothing else, for every run.

What it holds and what it does not is in `docs/design/code-sandbox.md`,
section 3. In short: it holds mistakes; it is not proven to hold a
deliberate attack on the Linux kernel. The probes in `probes.py` are what
show, on this machine, that the statements in `PROFILE` are true. Nothing
registers this sandbox with Freya: the owner does that, after the probes
pass (NEW-AR).

Freya never builds a command line from text a model wrote. The program is
started directly, without a command interpreter, from fixed arguments; the
files, command name and target travel as one JSON document on standard
input, and the runner inside the container checks them again.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

from freya.sandbox.contract import (
    MAX_OUTPUT_CHARACTERS,
    RunRequest,
    RunResult,
    SandboxError,
    SandboxProfile,
)


DISTRIBUTION = "freya-sandbox"
DISTRIBUTION_ENV = "FREYA_SANDBOX_DISTRIBUTION"

# The distribution's ordinary user, who runs the containers (rootless
# Podman). A fixed number, so its runtime folder is known in advance.
SANDBOX_USER = "sandbox"
SANDBOX_UID = 1500

IMAGE = "localhost/freya-sandbox:1"

# Limits on every container. The runner sets its own limits on the code
# as well, so neither is the only one.
CONTAINER_MEMORY = "768m"
CONTAINER_PROCESSES = 128
CONTAINER_CPUS = "2"
WORK_FOLDER_SIZE = "64m"
TEMP_FOLDER_SIZE = "16m"

# Starting WSL and the container takes a few seconds the first time.
START_ALLOWANCE_SECONDS = 30
READY_CHECK_SECONDS = 30
READY_CACHE_SECONDS = 60

# The runner's answer is JSON, in which some characters take six bytes.
MAX_ANSWER_BYTES = MAX_OUTPUT_CHARACTERS * 6 + 65_536

PROFILE = SandboxProfile(
    name="wsl2-container",
    network=False,
    host_files=False,
    credentials=False,
    fresh_each_run=True,
    boundary="container",
)

# The Windows variables wsl.exe itself needs. Nothing else of Freya's
# environment is passed, and WSLENV is empty so WSL shares nothing.
_WINDOWS_VARIABLES = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "PATHEXT")


def distribution_name(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return environ.get(DISTRIBUTION_ENV, "").strip() or DISTRIBUTION


def wsl_environment(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    kept = {
        name: environ[name]
        for name in environ
        if name.upper() in _WINDOWS_VARIABLES
    }
    kept["WSLENV"] = ""
    return kept


def in_distribution(distribution, user, argv) -> list:
    """
    The arguments that run `argv` directly (no shell) in the distribution
    as `user`, with the variables rootless Podman needs to find its
    runtime folder.
    """
    prefix = ["wsl.exe", "-d", distribution, "-u", user, "--exec"]

    if user == SANDBOX_USER:
        runtime = f"/run/user/{SANDBOX_UID}"
        prefix += [
            "/usr/bin/env",
            f"XDG_RUNTIME_DIR={runtime}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        ]

    return prefix + list(argv)


def container_arguments(name, timeout) -> list:
    """The Podman command for one run. Nothing in it comes from a request."""
    return [
        "podman", "run",
        "--rm",
        "--interactive",
        "--name", name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--tmpfs", f"/work:rw,size={WORK_FOLDER_SIZE},mode=1777",
        "--tmpfs", f"/tmp:rw,size={TEMP_FOLDER_SIZE},mode=1777",
        "--user", "65534:65534",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--memory={CONTAINER_MEMORY}",
        f"--memory-swap={CONTAINER_MEMORY}",
        f"--pids-limit={CONTAINER_PROCESSES}",
        f"--cpus={CONTAINER_CPUS}",
        "--timeout", str(int(timeout) + 10),
        IMAGE,
        "python3", "/opt/freya/runner.py",
    ]


def parse_answer(raw: bytes) -> RunResult:
    """
    Read the runner's answer: the last line that is a JSON object.
    Raises SandboxError when there is none, which means the container
    did not run the code (Podman, WSL or the image failed).
    """
    text = raw.decode("utf-8", errors="replace")

    for line in reversed(text.splitlines()):
        line = line.strip()

        if not line.startswith("{"):
            continue

        try:
            answer = json.loads(line)
        except ValueError:
            continue

        if not isinstance(answer, dict) or "exit_code" not in answer:
            continue

        if answer.get("error"):
            raise SandboxError("The sandbox could not run the code.", detail=str(answer["error"])[:2000])

        exit_code = answer.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, (int, type(None))):
            break

        output = answer.get("output")
        seconds = answer.get("seconds")

        return RunResult(
            exit_code=exit_code,
            output=output if isinstance(output, str) else "",
            timed_out=answer.get("timed_out") is True,
            truncated=answer.get("truncated") is True,
            seconds=float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else 0.0,
        )

    raise SandboxError("The sandbox gave an answer Freya could not read.", detail=text[-2000:])


def _read_bounded(stream, limit, into, overflow):
    """Read a stream to its end, keeping at most `limit` bytes."""
    kept = 0
    while True:
        data = stream.read(65536)
        if not data:
            return
        if kept < limit:
            into.append(data[: limit - kept])
            kept += len(data)
        if kept >= limit:
            overflow.set()


class WslContainerSandbox:
    """
    Runs each request in a new rootless Podman container inside the
    sandbox distribution: no network, a read-only system, a small work
    folder, no rights, limits on memory and processes, removed at the end.
    """

    def __init__(self, distribution=None, popen=subprocess.Popen, run=subprocess.run, platform=None, which=shutil.which, clock=time.monotonic):
        self.distribution = distribution or distribution_name()
        self._popen = popen
        self._run = run
        self._platform = platform or sys.platform
        self._which = which
        self._clock = clock
        self._ready_until = 0.0
        self._lock = threading.Lock()

    def profile(self) -> SandboxProfile:
        return PROFILE

    def _command(self, user, argv):
        return in_distribution(self.distribution, user, argv)

    def ready(self) -> bool:
        """
        True when WSL, the distribution and the image are all there.
        A positive answer is kept for a minute, because each check
        starts WSL.
        """
        if not self._platform.startswith("win") or not self._which("wsl.exe"):
            return False

        with self._lock:
            if self._clock() < self._ready_until:
                return True

        try:
            checked = self._run(
                self._command(SANDBOX_USER, ["podman", "image", "exists", IMAGE]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=wsl_environment(),
                timeout=READY_CHECK_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False

        if checked.returncode != 0:
            return False

        with self._lock:
            self._ready_until = self._clock() + READY_CACHE_SECONDS

        return True

    def _remove(self, name):
        """Remove a container that outlived its run. Best effort."""
        try:
            self._run(
                self._command(SANDBOX_USER, ["podman", "rm", "--force", "--ignore", name]),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=wsl_environment(),
                timeout=READY_CHECK_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def run(self, request: RunRequest) -> RunResult:
        name = "freya-run-" + uuid.uuid4().hex[:16]
        payload = json.dumps(
            {
                "files": dict(request.files),
                "command": request.command,
                "target": request.target,
                "timeout": request.timeout,
            }
        ).encode("utf-8")

        started = self._clock()

        try:
            process = self._popen(
                self._command(SANDBOX_USER, container_arguments(name, request.timeout)),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=wsl_environment(),
            )
        except OSError as exc:
            raise SandboxError("The sandbox could not be started.", detail=str(exc)[:2000]) from exc

        chunks, overflow = [], threading.Event()
        reader = threading.Thread(
            target=_read_bounded,
            args=(process.stdout, MAX_ANSWER_BYTES, chunks, overflow),
            daemon=True,
        )
        reader.start()

        try:
            process.stdin.write(payload)
            process.stdin.close()
        except OSError:
            pass

        try:
            process.wait(timeout=request.timeout + START_ALLOWANCE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            self._remove(name)
            return RunResult(
                exit_code=None,
                output="",
                timed_out=True,
                seconds=self._clock() - started,
            )

        reader.join(timeout=5)

        if overflow.is_set():
            self._remove(name)
            raise SandboxError("The sandbox gave back more than Freya reads.")

        return parse_answer(b"".join(chunks))
