"""
The tests a real sandbox must pass on this machine before the owner allows
`code.run` (docs/design/code-sandbox.md, section 6). Each one runs made-up
code inside the sandbox that tries to do something the profile says it
cannot, and passes only when the attempt fails.

The last check is different: it asks whether an administrator inside the
WSL distribution, outside any container, can attach the Windows drives.
The answer is recorded either way; it says how much the container, rather
than WSL, is holding.
"""

import subprocess
from dataclasses import dataclass

from freya.sandbox.contract import MAX_OUTPUT_CHARACTERS, build_request, checked_result
from freya.sandbox.wsl import distribution_name, in_distribution, wsl_environment


@dataclass(frozen=True)
class ProbeResult:
    number: int
    name: str
    passed: bool
    note: str


_NETWORK = """\
import socket
reached = []
try:
    socket.getaddrinfo("example.com", 443)
    reached.append("name lookup")
except OSError:
    pass
for host in ("1.1.1.1", "8.8.8.8"):
    try:
        socket.create_connection((host, 53), timeout=3).close()
        reached.append(host)
    except OSError:
        pass
print("REACHED", reached)
raise SystemExit(1 if reached else 0)
"""

_LOOPBACK = """\
import socket
reached = []
for port in (11434, 8770, 8188, 4173):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        reached.append(port)
    except OSError:
        pass
try:
    with open("/proc/net/route") as handle:
        routes = handle.read().splitlines()[1:]
except OSError:
    routes = []
print("REACHED", reached, "ROUTES", len(routes))
raise SystemExit(1 if reached or routes else 0)
"""

_DRIVES = """\
import os
found = []
for path in ("/mnt/c", "/mnt/d", "/mnt/wsl", "/mnt/wslg"):
    if os.path.exists(path):
        found.append(path)
with open("/proc/mounts") as handle:
    for line in handle:
        kind = line.split()[2]
        if kind in ("drvfs", "9p", "virtiofs"):
            found.append(line.split()[1])
print("FOUND", found)
raise SystemExit(1 if found else 0)
"""

_WRITE = """\
for path in ("/work/left-behind.txt", "/tmp/left-behind.txt"):
    with open(path, "w") as handle:
        handle.write("from the first run")
print("WROTE")
"""

_READ = """\
import os
left = [p for p in ("/work/left-behind.txt", "/tmp/left-behind.txt") if os.path.exists(p)]
print("LEFT", left)
raise SystemExit(1 if left else 0)
"""

_LOOP = "while True:\n    pass\n"

_MEMORY = """\
try:
    block = bytearray(2 * 1024 * 1024 * 1024)
    print("ALLOCATED")
except MemoryError:
    print("REFUSED")
    raise SystemExit(0)
raise SystemExit(1)
"""

_PROCESSES = """\
import os, time
started = 0
children = []
for _ in range(1000):
    try:
        pid = os.fork()
    except OSError:
        break
    if pid == 0:
        time.sleep(5)
        os._exit(0)
    children.append(pid)
    started += 1
print("STARTED", started)
raise SystemExit(0 if started < 500 else 1)
"""

_OUTPUT = "import sys\nsys.stdout.write('x' * 5_000_000)\n"

_ENVIRONMENT = """\
import os, re
wrong = [name for name in os.environ
         if name.startswith(("FREYA", "OLLAMA", "WSL"))
         or re.search("TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL|USERPROFILE|APPDATA", name)]
wrong += [name for name, value in os.environ.items() if "Windows" in value or ":\\\\" in value]
status = open("/proc/self/status").read()
capabilities = [line for line in status.splitlines() if line.startswith("CapEff")]
print("WRONG", wrong, "UID", os.getuid(), capabilities)
raise SystemExit(1 if wrong or os.getuid() == 0 or "0000000000000000" not in capabilities[0] else 0)
"""


def _run(sandbox, files, command="python", target="main.py", timeout=30):
    return checked_result(sandbox.run(build_request(files, command, target, timeout)))


def _code_probe(number, name, source, timeout=30):
    def probe(sandbox):
        result = _run(sandbox, {"main.py": source}, timeout=timeout)
        return ProbeResult(number, name, result.passed, result.output.strip()[-300:])

    return probe


def _fresh(sandbox):
    first = _run(sandbox, {"main.py": _WRITE})
    second = _run(sandbox, {"main.py": _READ})
    return ProbeResult(4, "Nothing is kept between runs", first.passed and second.passed, second.output.strip()[-300:])


def _limits(sandbox):
    loop = _run(sandbox, {"main.py": _LOOP}, timeout=5)
    memory = _run(sandbox, {"main.py": _MEMORY})
    processes = _run(sandbox, {"main.py": _PROCESSES})
    passed = loop.timed_out and memory.exit_code is not None and memory.exit_code != 1 and processes.passed
    note = f"loop stopped: {loop.timed_out}; memory: {memory.output.strip()[-80:] or memory.exit_code}; {processes.output.strip()[-80:]}"
    return ProbeResult(5, "Endless loop, memory hog and process storm are stopped", passed, note)


def _output(sandbox):
    result = _run(sandbox, {"main.py": _OUTPUT})
    passed = result.truncated and len(result.output) <= MAX_OUTPUT_CHARACTERS
    return ProbeResult(6, "Enormous output arrives cut", passed, f"{len(result.output)} characters kept")


PROBES = [
    _code_probe(1, "No outside network", _NETWORK),
    _code_probe(2, "No loopback or route to this machine", _LOOPBACK),
    _code_probe(3, "No Windows drives", _DRIVES),
    _fresh,
    _limits,
    _output,
    _code_probe(7, "None of Freya's variables, no rights", _ENVIRONMENT),
]


def guest_administrator_can_attach_drives(distribution=None, run=subprocess.run) -> ProbeResult:
    """
    Check 8: as root in the distribution (outside any container), try to
    attach drive C: read-only, then detach it. Recorded either way.
    """
    distribution = distribution or distribution_name()
    script = (
        "mkdir -p /mnt/freya-probe && "
        "if mount -t drvfs C: /mnt/freya-probe -o ro 2>/dev/null; then "
        "ls /mnt/freya-probe >/dev/null 2>&1 && echo ATTACHED; umount /mnt/freya-probe; "
        "else echo REFUSED; fi; rmdir /mnt/freya-probe"
    )
    try:
        completed = run(
            in_distribution(distribution, "root", ["/bin/sh", "-c", script]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=wsl_environment(),
            timeout=60,
            check=False,
        )
        said = completed.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        said = f"could not check: {exc}"

    attached = "ATTACHED" in said
    note = (
        "Yes: an administrator in the WSL guest can attach the Windows drives, so the container is the barrier."
        if attached
        else "No: the drives could not be attached from the WSL guest." if "REFUSED" in said
        else said.strip()[-300:]
    )
    # Informational: it passes either way once an answer is known.
    return ProbeResult(8, "Administrator in the WSL guest attaching Windows drives", attached or "REFUSED" in said, note)


def run_probes(sandbox, distribution=None, run=subprocess.run) -> list:
    results = []
    for probe in PROBES:
        try:
            results.append(probe(sandbox))
        except Exception as exc:
            results.append(ProbeResult(len(results) + 1, getattr(probe, "__name__", "probe"), False, f"error: {exc}"[:300]))
    results.append(guest_administrator_can_attach_drives(distribution, run))
    return results
