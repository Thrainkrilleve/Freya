"""
Runs one request inside the sandbox container. This file is copied
into the container image when the owner builds it. It runs there, under
the image's own Python, and must not import anything from Freya.

It reads one JSON request from standard input (the files, which command,
the target and the time limit), writes the files into the work folder,
runs the command with its own limits, and prints one JSON line with how
the run ended and what it printed.

The code being run can write to this program's output too, so the
answer is untrusted, like everything else from the sandbox: it can lie
about its own result, which it could do anyway by choosing its exit
code. Freya reads only the last line.
"""

import json
import os
import resource
import subprocess
import sys
import time


WORK = "/work"
MAX_REQUEST_BYTES = 16_000_000
MAX_OUTPUT_CHARACTERS = 100_000

# Limits on the code itself, set inside the container as well as on the
# container, so that neither is the only one.
MEMORY_BYTES = 512 * 1024 * 1024
PROCESSES = 64
FILE_BYTES = 16 * 1024 * 1024


def _answer(exit_code, output, timed_out=False, truncated=False, seconds=0.0, error=""):
    line = json.dumps(
        {
            "exit_code": exit_code,
            "output": output,
            "timed_out": timed_out,
            "truncated": truncated,
            "seconds": round(seconds, 3),
            "error": error,
        }
    )
    sys.stdout.write("\n" + line + "\n")
    sys.stdout.flush()


def _limits(seconds):
    def lower(kind, value):
        # A limit can only be lowered here, never raised.
        _, hard = resource.getrlimit(kind)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(kind, (value, value))

    def apply():
        lower(resource.RLIMIT_AS, MEMORY_BYTES)
        lower(resource.RLIMIT_NPROC, PROCESSES)
        lower(resource.RLIMIT_FSIZE, FILE_BYTES)
        lower(resource.RLIMIT_CORE, 0)
        lower(resource.RLIMIT_CPU, int(seconds) + 5)
        os.setsid()

    return apply


def _write_files(files):
    root = os.path.realpath(WORK)

    for name, text in files.items():
        path = os.path.realpath(os.path.join(root, name))

        # Freya checked the names already; this is a second check.
        if not path.startswith(root + os.sep):
            raise ValueError("bad file name")

        os.makedirs(os.path.dirname(path), exist_ok=True)

        with open(path, "x", encoding="utf-8") as handle:
            handle.write(text)


def _command(command, target):
    if command == "python":
        return [sys.executable, "-I", target]
    if command == "pytest":
        argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        return argv + ([target] if target else [])
    raise ValueError("unknown command")


def main():
    try:
        request = json.loads(sys.stdin.read(MAX_REQUEST_BYTES))
        files = request["files"]
        argv = _command(request["command"], request.get("target", ""))
        seconds = float(request["timeout"])
        _write_files(files)
    except Exception:
        _answer(None, "", error="The request could not be read.")
        return 2

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=WORK,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": WORK, "PYTHONDONTWRITEBYTECODE": "1"},
            preexec_fn=_limits(seconds),
        )
    except (OSError, subprocess.SubprocessError):
        _answer(None, "", error="The code could not be started.")
        return 2

    chunks = []
    kept = 0
    truncated = False
    timed_out = False
    deadline = started + seconds

    os.set_blocking(process.stdout.fileno(), False)

    while True:
        try:
            data = process.stdout.read(65536)
        except BlockingIOError:
            data = None

        if data:
            if kept < MAX_OUTPUT_CHARACTERS * 4:
                chunks.append(data)
                kept += len(data)
            else:
                truncated = True
            continue

        if process.poll() is not None:
            # The code has ended. Anything it started is ended too, so
            # a leftover process cannot hold the run open.
            try:
                os.killpg(process.pid, 9)
            except OSError:
                pass
            if data == b"" or not data:
                break

        if time.monotonic() > deadline:
            timed_out = True
            try:
                os.killpg(process.pid, 9)
            except OSError:
                pass
            process.wait()
            break

        time.sleep(0.02)

    exit_code = process.wait()
    output = b"".join(chunks).decode("utf-8", errors="replace")

    if len(output) > MAX_OUTPUT_CHARACTERS:
        output = output[:MAX_OUTPUT_CHARACTERS]
        truncated = True

    _answer(
        None if timed_out else exit_code,
        output,
        timed_out=timed_out,
        truncated=truncated,
        seconds=time.monotonic() - started,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
