"""
What a computer could lend Freya, measured on that computer.

This file is deliberately self-contained: it uses only Python's standard
library and imports nothing from Freya, so it can be copied onto a spare
laptop and run there on its own:

    python profile.py              # prints what this machine has
    python profile.py --save FILE  # also writes it for Freya to read

It reads and prints. It opens no network connection, installs nothing,
and changes nothing on the machine. Like `hardware_inventory.py`, it
collects no host name, serial number, MAC address or user name.
"""

import argparse
import ctypes
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time

PROFILE_VERSION = 1

GIB = 1024 ** 3

# nvidia-smi is the only graphics query: it is the one that reports a
# card's memory reliably. Other graphics are listed as not measured.
NVIDIA_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,memory.total",
    "--format=csv,noheader,nounits",
]
QUERY_TIMEOUT_SECONDS = 10

MAX_NAME_CHARACTERS = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# The words of the code shown on the machine, so the owner can check on
# the main PC that the machine being approved is this one. Digits are
# used because they read aloud and type without ambiguity.
CODE_DIGITS = 8


def _clean(text, limit=MAX_NAME_CHARACTERS):
    text = _CONTROL.sub("", str(text or "")).strip()
    return text[:limit]


# The only variables a query is given: enough to find and run the
# program, never a key or sign-in that happens to be set.
QUERY_ENVIRONMENT = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "LANG")


def _query_environment(environ=None):
    environ = os.environ if environ is None else environ
    return {
        name: value
        for name, value in environ.items()
        if name.upper() in QUERY_ENVIRONMENT
    }


def _run(command):
    try:
        completed = subprocess.run(
            command,
            env=_query_environment(),
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None

    return completed.stdout


def parse_nvidia_smi(output):
    """
    Each line is "name, memory in MiB". Lines that do not read cleanly
    are skipped rather than guessed at.
    """
    gpus = []

    for line in (output or "").splitlines():
        parts = [part.strip() for part in line.split(",")]

        if len(parts) != 2:
            continue

        name, memory = parts

        try:
            mebibytes = int(float(memory))
        except ValueError:
            continue

        if not name or mebibytes <= 0:
            continue

        gpus.append({
            "vendor": "nvidia",
            "name": _clean(name),
            "memory_bytes": mebibytes * 1024 * 1024,
        })

    return gpus


def parse_meminfo(text):
    match = re.search(r"^MemTotal:\s+(\d+)\s+kB", text or "", re.MULTILINE)

    if not match:
        return None

    return int(match.group(1)) * 1024


def parse_cpuinfo_model(text):
    match = re.search(r"^model name\s*:\s*(.+)$", text or "", re.MULTILINE)
    return _clean(match.group(1)) if match else ""


def _windows_ram_bytes():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)

    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None

    return int(status.ullTotalPhys)


def ram_bytes():
    system = platform.system()

    try:
        if system == "Windows":
            return _windows_ram_bytes()

        if system == "Linux":
            with open("/proc/meminfo", encoding="ascii") as handle:
                return parse_meminfo(handle.read())

        if system == "Darwin":
            output = _run(["sysctl", "-n", "hw.memsize"])
            return int(output.strip()) if output else None
    except (OSError, ValueError, AttributeError):
        return None

    return None


def cpu_model():
    system = platform.system()

    if system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as h:
                model = parse_cpuinfo_model(h.read())
        except OSError:
            model = ""
        if model:
            return model

    return _clean(platform.processor())


def collect_profile(run=_run, now=None):
    """
    This machine's profile. `run` is replaceable so tests never call
    nvidia-smi.
    """
    try:
        disk = shutil.disk_usage(os.path.expanduser("~"))
        disk_free = int(disk.free)
    except OSError:
        disk_free = None

    gpus = parse_nvidia_smi(run(NVIDIA_QUERY))

    return {
        "profile_version": PROFILE_VERSION,
        "measured_at": int(now if now is not None else time.time()),
        "os": {
            "system": _clean(platform.system()),
            "release": _clean(platform.release()),
            "architecture": _clean(platform.machine()),
        },
        "cpu": {
            "model": cpu_model(),
            "logical_cores": os.cpu_count() or 0,
        },
        "ram_bytes": ram_bytes(),
        "gpus": gpus,
        "other_graphics_measured": False,
        "disk_free_bytes": disk_free,
    }


def new_join_code():
    """
    A code shown on this machine and entered by the owner on the main
    PC, so the owner approves the machine in front of them and not some
    other one. It is random per join request, not derived from anything
    on the machine.
    """
    return "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))


def describe(profile):
    """
    Plain lines for a person to read.
    """
    lines = []
    os_info = profile.get("os") or {}
    lines.append(
        f"System: {os_info.get('system', '?')} {os_info.get('release', '')} "
        f"({os_info.get('architecture', '?')})".strip()
    )

    cpu = profile.get("cpu") or {}
    lines.append(
        f"Processor: {cpu.get('model') or 'unknown'}, "
        f"{cpu.get('logical_cores', 0)} logical cores"
    )

    ram = profile.get("ram_bytes")
    lines.append(
        f"Memory: {ram / GIB:.1f} GB" if ram else "Memory: could not be read"
    )

    gpus = profile.get("gpus") or []
    if gpus:
        for gpu in gpus:
            lines.append(
                f"Graphics: {gpu['name']}, "
                f"{gpu['memory_bytes'] / GIB:.1f} GB"
            )
    else:
        lines.append(
            "Graphics: no NVIDIA card found (other graphics are not measured)"
        )

    disk = profile.get("disk_free_bytes")
    if disk is not None:
        lines.append(f"Free disk space: {disk / GIB:.0f} GB")

    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Show what this computer could lend Freya. "
                    "Reads only; sends nothing anywhere."
    )
    parser.add_argument(
        "--save",
        metavar="FILE",
        help="also write the profile to FILE, to give to Freya",
    )
    parser.add_argument(
        "--name",
        default="",
        help="a name for this machine, e.g. 'old ThinkPad' (optional)",
    )
    arguments = parser.parse_args(argv)

    profile = collect_profile()

    for line in describe(profile):
        print(line)

    if arguments.save:
        code = new_join_code()
        offer = {
            "kind": "freya-node-offer",
            "name": _clean(arguments.name) or "unnamed machine",
            "join_code": code,
            "profile": profile,
        }

        with open(arguments.save, "w", encoding="utf-8") as handle:
            json.dump(offer, handle, indent=2)

        print()
        print(f"Saved to {arguments.save}.")
        print(f"Join code: {code[:4]} {code[4:]}")
        print(
            "Freya will ask for this code before she records this machine. "
            "Nothing has been sent anywhere."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
