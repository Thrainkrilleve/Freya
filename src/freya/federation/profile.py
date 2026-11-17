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

Graphics are measured AMD first, then Intel Arc, then NVIDIA. A machine
can have several cards, including a mix of vendors. Every probe is
optional: a missing tool or a tool that does not answer in time is
skipped, and the rest of the profile is still written. Video memory is
whatever a tool or the driver reported. Nothing here fills in a size
from the card's name.
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

# Version 2 adds `runtimes` on each card and measures AMD, Intel Arc and
# NVIDIA. Version 1 offers (NVIDIA only, no `runtimes`) still load: every
# new field is optional. `other_graphics_measured` stays so an older
# reader that expects the key still finds it; version 1 always wrote
# false because only NVIDIA was queried.
PROFILE_VERSION = 2

GIB = 1024 ** 3

# A hung graphics tool must not stall the profile. Missing tools fail
# at once; this bound is only for one that starts and does not finish.
QUERY_TIMEOUT_SECONDS = 5

# A local tool that prints far more than a card list is ignored rather
# than copied into the offer.
MAX_QUERY_CHARS = 1_000_000

# Larger than any card this profiler is meant to describe. A number past
# this is treated as unreadable, not as a huge card.
MAX_VRAM_BYTES = 1024 ** 4

# Win32_VideoController.AdapterRAM is a 32-bit count, so an 8 GB card and
# a 16 GB card both come back just under 4 GB. A figure that close to the
# top is not the card's memory.
WMI_ADAPTER_RAM_CAP = 4 * GIB - 64 * 1024 * 1024

# Integrated graphics with only a small carved-out frame buffer are not
# useful workers. Discrete cards, including Arc cards whose memory could
# not be read, are kept.
TINY_DEDICATED_BYTES = 2 * GIB

MAX_NAME_CHARACTERS = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# The words of the code shown on the machine, so the owner can check on
# the main PC that the machine being approved is this one. Digits are
# used because they read aloud and type without ambiguity.
CODE_DIGITS = 8

# PCI vendor ids. Anything else (a virtual display, a basic adapter) is
# not a card Freya would run a model on.
PCI_VENDORS = {
    0x1002: "amd",
    0x8086: "intel",
    0x10DE: "nvidia",
}

# Reported on a card when the matching runtime is visible, in this order.
RUNTIME_NAMES = ("rocm", "vulkan", "sycl", "cuda")
_RUNTIME_LABELS = {
    "rocm": "ROCm/HIP",
    "vulkan": "Vulkan",
    "sycl": "SYCL/oneAPI",
    "cuda": "CUDA",
}
_VENDOR_RANK = {"amd": 0, "intel": 1, "nvidia": 2}

ROCM_SMI_QUERY = [
    "rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json",
]
AMD_SMI_QUERY = ["amd-smi", "static", "--json"]
HIPINFO_QUERY = ["hipInfo"]
XPU_SMI_QUERY = ["xpu-smi", "discovery"]
SYCL_LS_QUERY = ["sycl-ls"]
NVIDIA_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,memory.total",
    "--format=csv,noheader,nounits",
]
WINDOWS_CONTROLLERS_QUERY = [
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    "Get-CimInstance -ClassName Win32_VideoController | "
    "Select-Object Name,AdapterRAM,PNPDeviceID,AdapterCompatibility | "
    "ConvertTo-Json -Compress",
]

_BASIC_ADAPTER = re.compile(
    r"basic display|basic render|remote display|standard vga|"
    r"microsoft hyper-v|virtual display|indirect display|"
    r"vmware|virtualbox|\bqxl\b|virtio|parsec|spacedesk|"
    r"iddcx|citrix|generic non-pnp",
    re.IGNORECASE,
)
_IGPU_NAME = re.compile(
    r"radeon\(tm\) graphics|radeon\(r\) graphics|"
    r"\bamd radeon graphics\b|\bradeon graphics\b|"
    r"intel\(r\) uhd|\buhd graphics\b|"
    r"intel\(r\) hd graphics|\bhd graphics\b|"
    r"iris\(r\) xe graphics|iris xe graphics|intel\(r\) iris|"
    r"radeon vega \d+ graphics",
    re.IGNORECASE,
)
_DISCRETE_NAME = re.compile(
    r"\barc\b|radeon rx|radeon pro|\binstinct\b|geforce|quadro|"
    r"\btesla\b|\brtx\b|\bgtx\b|data center gpu|\bxe max\b",
    re.IGNORECASE,
)
_GENERIC_NAME = re.compile(
    r"^(?:amd|intel|nvidia) gpu(?: \(device [0-9a-f]+\))?$",
    re.IGNORECASE,
)
_CARD_DIR = re.compile(r"card\d+\Z")

_DEFAULT = object()

# The only variables a query is given: enough to find and run the
# program, never a key or sign-in that happens to be set.
QUERY_ENVIRONMENT = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "LANG")


def _clean(text, limit=MAX_NAME_CHARACTERS):
    text = _CONTROL.sub("", str(text or "")).strip()
    return text[:limit]


def _query_environment(environ=None):
    environ = os.environ if environ is None else environ
    return {
        name: value
        for name, value in environ.items()
        if name.upper() in QUERY_ENVIRONMENT
    }


def _run(command):
    kwargs = {}
    if platform.system() == "Windows":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
    try:
        completed = subprocess.run(
            command,
            env=_query_environment(),
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None

    output = completed.stdout or ""
    if len(output) > MAX_QUERY_CHARS:
        return None
    return output


def _quietly(run, command):
    try:
        return run(command)
    except (OSError, subprocess.SubprocessError):
        return None


def gpu_queries(system):
    """
    The tools asked on this operating system, NVIDIA last. Each one is
    optional. macOS uses the Linux list; the tools are simply absent.
    """
    if system == "Windows":
        return [
            WINDOWS_CONTROLLERS_QUERY,
            HIPINFO_QUERY,
            AMD_SMI_QUERY,
            XPU_SMI_QUERY,
            SYCL_LS_QUERY,
            NVIDIA_QUERY,
        ]
    return [
        ROCM_SMI_QUERY,
        AMD_SMI_QUERY,
        XPU_SMI_QUERY,
        SYCL_LS_QUERY,
        NVIDIA_QUERY,
    ]


def _load_json(text):
    if not text:
        return None
    start = None
    for index, char in enumerate(text):
        if char in "{[":
            start = index
            break
    if start is None:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(text[start:])
    except (ValueError, TypeError):
        return None
    return value


def _device_token(value):
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not re.fullmatch(r"[0-9a-f]{1,8}", text):
        return ""
    return text.lstrip("0") or "0"


def _int_bytes(value, unit=None):
    """
    A memory size as bytes. A unit in the text wins. A bare number of a
    million or more is bytes; a smaller bare number of at least 256 is
    mebibytes, which is how the vendor tools usually print it.
    """
    if isinstance(value, bool) or value is None:
        return None
    unit_text = unit
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        match = re.fullmatch(
            r"([0-9]*\.?[0-9]+)\s*([kmgt]?i?b|bytes)?",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        number = float(match.group(1))
        if match.group(2):
            unit_text = match.group(2)
    if number <= 0:
        return None
    unit_text = (unit_text or "").lower()
    if unit_text in ("b", "bytes"):
        scale = 1
    elif unit_text in ("kb", "kib"):
        scale = 1024
    elif unit_text in ("mb", "mib"):
        scale = 1024 ** 2
    elif unit_text in ("gb", "gib"):
        scale = 1024 ** 3
    elif unit_text in ("tb", "tib"):
        scale = 1024 ** 4
    elif number >= 1_000_000:
        scale = 1
    elif number >= 256:
        scale = 1024 ** 2
    else:
        return None
    result = int(number * scale)
    if result <= 0 or result > MAX_VRAM_BYTES:
        return None
    return result


def _add_runtime(card, runtime):
    if runtime not in RUNTIME_NAMES:
        return
    runtimes = list(card.get("runtimes") or [])
    if runtime not in runtimes:
        runtimes.append(runtime)
    card["runtimes"] = runtimes


def _vendor_from_text(text):
    lowered = (text or "").lower()
    if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered:
        return "nvidia"
    if "intel" in lowered or re.search(r"\barc\b", lowered):
        return "intel"
    if (
        "amd" in lowered
        or "advanced micro" in lowered
        or "radeon" in lowered
        or "instinct" in lowered
    ):
        return "amd"
    return ""


def _vendor_from_pnp(text):
    match = re.search(r"VEN_([0-9A-Fa-f]{4})", text or "")
    if not match:
        return ""
    return PCI_VENDORS.get(int(match.group(1), 16), "")


def _device_from_pnp(text):
    match = re.search(r"DEV_([0-9A-Fa-f]{4})", text or "")
    if not match:
        return ""
    return _device_token(match.group(1))


def _is_generic(name):
    return bool(_GENERIC_NAME.match((name or "").strip()))


def _norm_name(name):
    text = (name or "").lower().replace("(r)", " ").replace("(tm)", " ")
    text = re.sub(
        r"\b(amd|intel|nvidia|advanced micro devices)\b", " ", text
    )
    return re.sub(r"[^a-z0-9]+", "", text)


def _names_compatible(left, right):
    if not left or not right or _is_generic(left) or _is_generic(right):
        return True
    one, two = _norm_name(left), _norm_name(right)
    if not one or not two:
        return True
    return one == two or one in two or two in one


def _better_name(current, new):
    current = _clean(current)
    new = _clean(new)
    if not new:
        return current
    if not current or _is_generic(current):
        return new
    if _is_generic(new):
        return current
    if len(new) > len(current) and _names_compatible(current, new):
        return new
    return current


def _generic_name(vendor, device):
    label = {"amd": "AMD", "intel": "Intel", "nvidia": "NVIDIA"}.get(
        vendor, "GPU"
    )
    if device:
        return f"{label} GPU (device {device})"
    return f"{label} GPU"


def _is_ignored(card):
    """
    Virtual and basic adapters are never workers. An integrated adapter
    with a tiny carved-out frame buffer is skipped too. Arc is kept even
    when its memory could not be read, and so is any other discrete card.
    """
    name = card.get("name") or ""
    lowered = name.lower()
    memory = card.get("memory_bytes") or 0
    if _BASIC_ADAPTER.search(lowered):
        return True
    if "arc" in lowered or "xe max" in lowered:
        return False
    if _IGPU_NAME.search(lowered) and memory < TINY_DEDICATED_BYTES:
        return True
    if (
        card.get("vendor") == "intel"
        and memory < TINY_DEDICATED_BYTES
        and not _DISCRETE_NAME.search(lowered)
    ):
        return True
    if (
        card.get("vendor") == "amd"
        and _is_generic(name)
        and 0 < memory < TINY_DEDICATED_BYTES
    ):
        return True
    return False


def _blank_card(vendor, name, memory=None, device="", runtimes=()):
    card = {"vendor": vendor, "name": _clean(name) or _generic_name(vendor, device)}
    if device:
        card["device"] = device
    if memory:
        card["memory_bytes"] = memory
    for runtime in runtimes:
        _add_runtime(card, runtime)
    return card


def _card_dicts(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("gpu_data", "cards", "GPUs"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = []
    for key, value in payload.items():
        if isinstance(value, dict) and (
            str(key).lower().startswith("card")
            or str(key).lower().startswith("gpu")
        ):
            nested.append(value)
    if nested:
        return nested
    return [payload]


def _flatten(obj, depth=0):
    flat = {}
    if not isinstance(obj, dict) or depth > 4:
        return flat
    for key, value in obj.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, depth + 1))
        elif not isinstance(value, (list, tuple)):
            flat[str(key)] = value
    return flat


def _name_from_flat(flat):
    wanted = (
        "product name",
        "card series",
        "card sku",
        "market_name",
        "market name",
        "device name",
        "name",
    )
    lowered = {str(key).lower(): value for key, value in flat.items()}
    for key in wanted:
        if key not in lowered:
            continue
        text = _clean(lowered[key])
        if text and not _device_token(text):
            return text
    return ""


def _device_from_flat(flat):
    for key, value in flat.items():
        if str(key).lower() in (
            "device id", "device_id", "card model", "card model id",
        ):
            token = _device_token(value)
            if token:
                return token
    return ""


def _memory_from_flat(flat):
    for key, value in flat.items():
        lowered = str(key).lower()
        if "used" in lowered or "free" in lowered or "visible" in lowered:
            continue
        if "vram" not in lowered and "memory" not in lowered:
            continue
        if "total" not in lowered and lowered not in ("vram", "size"):
            continue
        unit = None
        if "(b)" in lowered or lowered.endswith("bytes"):
            unit = "B"
        elif "mib" in lowered or "(mb)" in lowered or lowered.endswith(" mb"):
            unit = "MiB"
        memory = _int_bytes(value, unit)
        if memory:
            return memory
    return None


def _vram_field(card):
    vram = None
    for key, value in card.items():
        if str(key).lower() == "vram" and isinstance(value, dict):
            vram = value
            break
    if not isinstance(vram, dict) or "size" not in vram:
        return None
    size = vram.get("size")
    if isinstance(size, dict):
        return _int_bytes(size.get("value"), size.get("unit"))
    # amd-smi's bare vram.size is megabytes. A value already in bytes
    # (a full card is far past a million) is left as bytes.
    if isinstance(size, (int, float)) and not isinstance(size, bool):
        if size < 1_000_000:
            return _int_bytes(size, "MiB")
        return _int_bytes(size, "B")
    return _int_bytes(size)


def _cards_from_tool_json(payload, default_vendor, runtimes):
    cards = []
    for raw in _card_dicts(payload):
        flat = _flatten(raw)
        asic = raw.get("asic") if isinstance(raw.get("asic"), dict) else {}
        vendor_id = _device_token(
            asic.get("vendor_id") or flat.get("vendor_id") or flat.get("Card vendor")
        )
        vendor = PCI_VENDORS.get(int(vendor_id, 16), "") if vendor_id else ""
        name = _clean(
            asic.get("market_name")
            or asic.get("market name")
            or _name_from_flat(flat)
        )
        if not vendor:
            vendor = _vendor_from_text(name) or default_vendor
        if vendor not in _VENDOR_RANK:
            continue
        device = _device_token(asic.get("device_id")) or _device_from_flat(flat)
        if not name:
            gfx = _clean(
                asic.get("target_graphics_version")
                or asic.get("gfx_target_version"),
                40,
            )
            # The architecture name is what the tool printed. It is not
            # turned into a product name, and no memory is invented for it.
            name = f"{_generic_name(vendor, '')} ({gfx})" if gfx else ""
        memory = _vram_field(raw) or _memory_from_flat(flat)
        if not name and not device and not memory:
            continue
        cards.append(
            _blank_card(vendor, name or _generic_name(vendor, device), memory, device, runtimes)
        )
    return cards


def parse_rocm_smi(output):
    """
    `rocm-smi --showproductname --showmeminfo vram --json`. The shape of
    that JSON has changed between ROCm releases, so names and the VRAM
    total are taken from whichever keys are present.
    """
    payload = _load_json(output)
    if payload is None:
        return []
    return _cards_from_tool_json(payload, "amd", ("rocm",))


def parse_amd_smi(output):
    """
    `amd-smi static --json`. Marketing name, PCI ids and VRAM size are
    read. The ASIC serial, when the tool prints one, is not.
    """
    payload = _load_json(output)
    if payload is None:
        return []
    return _cards_from_tool_json(payload, "amd", ("rocm",))


def parse_hipinfo(output):
    """
    `hipInfo`. One block per device, starting at `device#`.
    """
    cards = []
    for block in re.split(r"(?m)^(?:HIP device |device#)", output or ""):
        name_match = re.search(r"(?m)^Name:\s*(.+)$", block)
        if not name_match:
            continue
        name = _clean(name_match.group(1))
        if not name:
            continue
        vendor = _vendor_from_text(name) or "amd"
        if vendor not in _VENDOR_RANK:
            continue
        memory_match = re.search(r"(?m)^totalGlobalMem:\s*(.+)$", block)
        memory = _int_bytes(memory_match.group(1)) if memory_match else None
        cards.append(_blank_card(vendor, name, memory, "", ("rocm",)))
    return cards


def parse_xpu_smi(output):
    """
    `xpu-smi discovery`. Integrated Intel adapters are left for the
    shared filter; Arc and other named discrete GPUs are kept.
    """
    cards = []
    for match in re.finditer(r"Device Name:\s*(.+)", output or ""):
        name = _clean(match.group(1).strip(" |"))
        if not name:
            continue
        vendor = _vendor_from_text(name) or "intel"
        if vendor not in _VENDOR_RANK:
            continue
        cards.append(_blank_card(vendor, name, None, "", ("sycl",)))
    return cards


def parse_sycl_ls(output):
    """
    `sycl-ls`. Level Zero lines name the discrete GPU. OpenCL lines for
    the integrated adapter are not read.
    """
    cards = []
    for line in (output or "").splitlines():
        if "level_zero" not in line.lower():
            continue
        # `[level_zero:gpu][level_zero:0] Intel(R) Level-Zero, <device> 1.3 [1.3]`
        text = re.sub(r"^(?:\[[^\]]*\]\s*)+", "", line).strip()
        text = re.sub(r"^Intel\(R\)\s+Level-Zero,\s*", "", text)
        text = re.sub(r"\s+\d+\.\d+.*$", "", text).strip()
        name = _clean(text)
        if not name:
            continue
        cards.append(_blank_card("intel", name, None, "", ("sycl",)))
    return cards


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


def _wmi_memory(value):
    memory = _int_bytes(value, "B")
    if memory is None or memory >= WMI_ADAPTER_RAM_CAP:
        return None
    return memory


def parse_windows_controllers(output):
    """
    `Win32_VideoController` as JSON. AdapterRAM is kept only when it is
    clearly below the 4 GB cap; the registry qword is the size that is
    trusted for a larger card. Fields other than the name, the PCI id
    and that size are ignored.
    """
    payload = _load_json(output)
    if payload is None:
        return []
    rows = payload if isinstance(payload, list) else [payload]
    cards = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("Name"))
        if not name:
            continue
        pnp = str(row.get("PNPDeviceID") or "")
        vendor = (
            _vendor_from_pnp(pnp)
            or _vendor_from_text(name)
            or _vendor_from_text(row.get("AdapterCompatibility"))
        )
        if vendor not in _VENDOR_RANK:
            continue
        cards.append(
            _blank_card(
                vendor,
                name,
                _wmi_memory(row.get("AdapterRAM")),
                _device_from_pnp(pnp),
            )
        )
    return cards


def cards_from_registry_properties(entries):
    """
    One dict per video-class registry key: DriverDesc, ProviderName,
    MatchingDeviceId and MemoryBytes (the qword, already in bytes).
    """
    cards = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("DriverDesc"))
        if not name:
            continue
        matching = str(entry.get("MatchingDeviceId") or "")
        vendor = (
            _vendor_from_pnp(matching)
            or _vendor_from_text(name)
            or _vendor_from_text(entry.get("ProviderName"))
        )
        if vendor not in _VENDOR_RANK:
            continue
        memory = entry.get("MemoryBytes")
        parsed = _int_bytes(memory, "B") if memory else None
        cards.append(
            _blank_card(vendor, name, parsed, _device_from_pnp(matching))
        )
    return cards


def _registry_value(winreg, key, name):
    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value


def _qword_bytes(value):
    if isinstance(value, bytes):
        if len(value) < 8:
            return None
        return int.from_bytes(value[:8], "little")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


# The display-class key. The brace is part of the key name; it is not
# escaped. HardwareInformation.qwMemorySize lives under the numbered
# subkeys. AdapterRAM's 4 GB ceiling is why this is preferred on Windows.
VIDEO_CLASS_KEY = (
    "SYSTEM\\ControlSet001\\Control\\Class\\"
    "{4d36e968-e325-11ce-bfc1-08002be10318}"
)


def read_windows_registry_cards():
    """
    `HardwareInformation.qwMemorySize` under the display class key.
    AdapterRAM's 4 GB ceiling is why this is preferred on Windows.
    """
    if platform.system() != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []
    path = VIDEO_CLASS_KEY
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
    except OSError:
        return []
    entries = []
    try:
        for index in range(32):
            try:
                subname = winreg.EnumKey(root, index)
            except OSError:
                break
            if not re.fullmatch(r"\d{4}", subname):
                continue
            try:
                sub = winreg.OpenKey(root, subname)
            except OSError:
                continue
            try:
                entries.append({
                    "DriverDesc": _registry_value(winreg, sub, "DriverDesc"),
                    "ProviderName": _registry_value(winreg, sub, "ProviderName"),
                    "MatchingDeviceId": _registry_value(
                        winreg, sub, "MatchingDeviceId"
                    ),
                    "MemoryBytes": _qword_bytes(_registry_value(
                        winreg, sub, "HardwareInformation.qwMemorySize"
                    )),
                })
            finally:
                winreg.CloseKey(sub)
    finally:
        winreg.CloseKey(root)
    return cards_from_registry_properties(entries)


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(256)
    except OSError:
        return ""


def _read_int_file(path):
    text = _read_text(path).strip()
    if not text:
        return None
    return _int_bytes(text, "B")


def read_sysfs_cards(root=None, system=None):
    """
    Linux DRM nodes. Vendor, device and VRAM total only: a serial file
    sitting beside them is never opened.
    """
    system = platform.system() if system is None else system
    if root is None:
        if system != "Linux":
            return []
        root = "/sys/class/drm"
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    cards = []
    for name in names:
        if not _CARD_DIR.fullmatch(name):
            continue
        card_dir = os.path.join(root, name)
        device_dir = os.path.join(card_dir, "device")
        vendor_text = _read_text(os.path.join(device_dir, "vendor")).strip()
        vendor = ""
        if re.fullmatch(r"0x[0-9A-Fa-f]+", vendor_text):
            vendor = PCI_VENDORS.get(int(vendor_text, 16), "")
        if not vendor:
            continue
        device = _device_token(
            _read_text(os.path.join(device_dir, "device")).strip()
        )
        memory = None
        for relative in (
            os.path.join(device_dir, "mem_info_vram_total"),
            os.path.join(device_dir, "lmem_total_bytes"),
            os.path.join(card_dir, "lmem_total_bytes"),
        ):
            memory = _read_int_file(relative)
            if memory:
                break
        # A product file that is only a PCI id is not a name. The
        # marketing name then comes from a tool, if one ran.
        product = _clean(_read_text(os.path.join(device_dir, "product_name")))
        if not product or _device_token(product):
            product = ""
        cards.append(
            _blank_card(
                vendor,
                product or _generic_name(vendor, device),
                memory,
                device,
            )
        )
    return cards


def _vulkan_vendor(text):
    lowered = (text or "").lower()
    if "nvidia" in lowered:
        return "nvidia"
    if "intel" in lowered or "hasvk" in lowered:
        return "intel"
    if "radeon" in lowered or re.search(r"\bamd\b", lowered):
        return "amd"
    return ""


def _default_vulkan_dirs():
    dirs = [
        "/usr/share/vulkan/icd.d",
        "/usr/local/share/vulkan/icd.d",
        "/etc/vulkan/icd.d",
    ]
    home = os.path.expanduser("~")
    if home and home != "~":
        dirs.append(os.path.join(home, ".local", "share", "vulkan", "icd.d"))
    return dirs


def vulkan_vendors_present(dirs=None):
    """
    Which vendors have a Vulkan ICD installed. The file names the
    driver library; it is not executed.
    """
    if dirs is None:
        dirs = _default_vulkan_dirs()
    found = set()
    for directory in dirs:
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not str(name).endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    text = handle.read(65536)
            except OSError:
                text = ""
            vendor = _vulkan_vendor(name + "\n" + text)
            if vendor:
                found.add(vendor)
    return found


def rocm_device_present(path=None):
    """
    Linux exposes the ROCm kernel driver as `/dev/kfd`. Presence is a
    hint that HIP can see the AMD cards; it adds no card by itself.
    """
    if path is None:
        path = "/dev/kfd"
    if not path:
        return False
    try:
        return os.path.exists(path)
    except OSError:
        return False


def _find_slot(slots, incoming):
    vendor = incoming.get("vendor")
    device = incoming.get("device") or ""
    free = [
        slot for slot in slots
        if not slot.get("_taken") and slot.get("vendor") == vendor
    ]
    if device:
        for slot in free:
            if slot.get("device") == device:
                return slot
    specific = _norm_name(incoming.get("name"))
    if specific and not _is_generic(incoming.get("name")):
        named = [
            slot for slot in free
            if _norm_name(slot.get("name")) == specific
        ]
        if len(named) == 1:
            return named[0]
    generics = [
        slot for slot in free
        if _is_generic(slot.get("name")) or not slot.get("name")
    ]
    if _DISCRETE_NAME.search(incoming.get("name") or ""):
        big = [
            slot for slot in generics
            if (slot.get("memory_bytes") or 0) >= TINY_DEDICATED_BYTES
        ]
        if big:
            return big[0]
    if len(generics) == 1:
        return generics[0]
    if len(free) == 1 and _names_compatible(
        free[0].get("name"), incoming.get("name")
    ):
        return free[0]
    return None


def _fill(target, incoming):
    target["name"] = _better_name(target.get("name"), incoming.get("name"))
    if incoming.get("device") and not target.get("device"):
        target["device"] = incoming["device"]
    new = incoming.get("memory_bytes")
    old = target.get("memory_bytes")
    if new and (not old or new > old):
        target["memory_bytes"] = new
    for runtime in incoming.get("runtimes") or []:
        _add_runtime(target, runtime)


def _add_source(slots, cards):
    for slot in slots:
        slot["_taken"] = False
    for incoming in cards or []:
        if not isinstance(incoming, dict):
            continue
        if incoming.get("vendor") not in _VENDOR_RANK:
            continue
        slot = _find_slot(slots, incoming)
        if slot is None:
            slots.append(dict(incoming))
            slots[-1]["_taken"] = True
        else:
            _fill(slot, incoming)
            slot["_taken"] = True


def _ordered(cards):
    ranked = sorted(
        enumerate(cards),
        key=lambda item: (
            _VENDOR_RANK.get(item[1].get("vendor"), 9),
            item[0],
        ),
    )
    return [card for _, card in ranked]


def _public_card(card):
    published = {
        "vendor": card["vendor"],
        "name": _clean(card.get("name")) or _generic_name(card["vendor"], ""),
    }
    memory = card.get("memory_bytes")
    if isinstance(memory, int) and not isinstance(memory, bool) and memory > 0:
        published["memory_bytes"] = memory
    runtimes = [
        name for name in RUNTIME_NAMES
        if name in set(card.get("runtimes") or [])
    ]
    if runtimes:
        published["runtimes"] = runtimes
    return published


def collect_gpus(
    run,
    *,
    system,
    sysfs_root=_DEFAULT,
    registry_cards=_DEFAULT,
    vulkan_dirs=_DEFAULT,
    rocm_path=_DEFAULT,
):
    slots = []
    if system == "Windows":
        if registry_cards is _DEFAULT:
            registry_cards = read_windows_registry_cards()
        _add_source(slots, registry_cards or [])
    else:
        root = None if sysfs_root is _DEFAULT else sysfs_root
        _add_source(slots, read_sysfs_cards(root, system=system))

    for command in gpu_queries(system):
        output = _quietly(run, command)
        if command[0] == "powershell.exe":
            _add_source(slots, parse_windows_controllers(output))
        elif command[0] == "hipInfo":
            _add_source(slots, parse_hipinfo(output))
        elif command[0] == "rocm-smi":
            _add_source(slots, parse_rocm_smi(output))
        elif command[0] == "amd-smi":
            _add_source(slots, parse_amd_smi(output))
        elif command[0] == "xpu-smi":
            _add_source(slots, parse_xpu_smi(output))
        elif command[0] == "sycl-ls":
            _add_source(slots, parse_sycl_ls(output))
        elif command[0] == "nvidia-smi":
            nvidia = parse_nvidia_smi(output)
            for card in nvidia:
                _add_runtime(card, "cuda")
            _add_source(slots, nvidia)

    if rocm_path is _DEFAULT:
        kfd = rocm_device_present(None) if system != "Windows" else False
    else:
        kfd = rocm_device_present(rocm_path)
    if kfd:
        for slot in slots:
            if slot.get("vendor") == "amd":
                _add_runtime(slot, "rocm")

    if vulkan_dirs is _DEFAULT:
        vulkan = vulkan_vendors_present(None)
    else:
        vulkan = vulkan_vendors_present(vulkan_dirs)
    for vendor in vulkan:
        for slot in slots:
            if slot.get("vendor") == vendor:
                _add_runtime(slot, "vulkan")

    kept = [slot for slot in slots if not _is_ignored(slot)]
    return [_public_card(slot) for slot in _ordered(kept)]


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
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
                model = parse_cpuinfo_model(handle.read())
        except OSError:
            model = ""
        if model:
            return model

    return _clean(platform.processor())


def collect_profile(
    run=_run,
    now=None,
    *,
    system=None,
    sysfs_root=_DEFAULT,
    registry_cards=_DEFAULT,
    vulkan_dirs=_DEFAULT,
    rocm_path=_DEFAULT,
):
    """
    This machine's profile. `run` and the filesystem readers are
    replaceable so tests never call a real graphics tool.
    """
    system = platform.system() if system is None else system
    try:
        disk = shutil.disk_usage(os.path.expanduser("~"))
        disk_free = int(disk.free)
    except OSError:
        disk_free = None

    gpus = collect_gpus(
        run,
        system=system,
        sysfs_root=sysfs_root,
        registry_cards=registry_cards,
        vulkan_dirs=vulkan_dirs,
        rocm_path=rocm_path,
    )

    return {
        "profile_version": PROFILE_VERSION,
        "measured_at": int(now if now is not None else time.time()),
        "os": {
            "system": _clean(platform.system() if system is None else system),
            "release": _clean(platform.release()),
            "architecture": _clean(platform.machine()),
        },
        "cpu": {
            "model": cpu_model(),
            "logical_cores": os.cpu_count() or 0,
        },
        "ram_bytes": ram_bytes(),
        "gpus": gpus,
        "other_graphics_measured": True,
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
            memory = gpu.get("memory_bytes")
            if isinstance(memory, (int, float)) and not isinstance(memory, bool) and memory > 0:
                size = f", {memory / GIB:.1f} GB"
            else:
                size = ""
            labels = [
                _RUNTIME_LABELS[name]
                for name in RUNTIME_NAMES
                if name in set(gpu.get("runtimes") or [])
            ]
            hint = f" ({', '.join(labels)})" if labels else ""
            lines.append(
                f"Graphics: {gpu.get('name') or 'graphics card'}{size}{hint}"
            )
    else:
        lines.append("Graphics: no graphics card found")

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
