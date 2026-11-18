"""
Live graphics-card status for this machine.

Cards are read AMD first, then Intel, then NVIDIA — the same order as
the federation profile (`profile.py` in that package). This module does
not import that file. `profile.py` stays a single standard-library
program that can be copied onto another machine, and the rest of Freya
does not import the federation package. The PCI vendor ids, the Windows
display-class key,
and the 4 GB AdapterRAM ceiling are duplicated here on purpose; a test
checks they still match.

A missing tool, a tool that fails, or a field it does not print becomes
an unknown (`None`). Nothing in here raises because a tool is absent.
Integrated adapters (the processor's own graphics, including an Intel
N95's UHD) stay in the list with `memory_kind` `shared` and no dedicated
total. Dedicated VRAM is recorded only when a tool or the driver
reported a size. AdapterRAM values up at the 32-bit 4 GB ceiling are
not that size.

Intel Arc is parsed from xpu-smi text and from i915 and xe sysfs, and
on Windows from WMI plus DXGI adapter rows. Those paths are covered by
fixtures. They have not been run on a real Arc card: this workstation
does not have one.
"""

import json
import os
import platform
import re
import subprocess

from freya.child_environment import child_environment


GIB = 1024 ** 3
MIB = 1024 ** 2

# A hung graphics tool must not stall a turn. Missing tools fail at once.
QUERY_TIMEOUT_SECONDS = 5

MAX_QUERY_CHARS = 1_000_000

# Larger than any card this probe is meant to describe.
MAX_VRAM_BYTES = 1024 ** 4

# Win32_VideoController.AdapterRAM is a 32-bit count, so an 8 GB card and
# a 16 GB card both come back just under 4 GB. A figure that close to the
# top is not the card's memory. Same ceiling as federation.profile.
WMI_ADAPTER_RAM_CAP = 4 * GIB - 64 * MIB

# Integrated graphics with only a small carved-out frame buffer are not
# discrete cards. Arc is not treated as integrated just because its
# memory could not be read.
TINY_DEDICATED_BYTES = 2 * GIB

MAX_NAME_CHARACTERS = 80

PCI_VENDORS = {
    0x1002: "amd",
    0x8086: "intel",
    0x10DE: "nvidia",
}

VENDOR_RANK = {"amd": 0, "intel": 1, "nvidia": 2}

# The display-class key. HardwareInformation.qwMemorySize lives under the
# numbered subkeys. Same path as federation.profile.
VIDEO_CLASS_KEY = (
    "SYSTEM\\ControlSet001\\Control\\Class\\"
    "{4d36e968-e325-11ce-bfc1-08002be10318}"
)

ROCM_SMI_QUERY = [
    "rocm-smi",
    "--showproductname",
    "--showmeminfo",
    "vram",
    "--showuse",
    "--showtemp",
    "--json",
]
AMD_SMI_STATIC_QUERY = ["amd-smi", "static", "--json"]
AMD_SMI_METRIC_QUERY = ["amd-smi", "metric", "--json"]
XPU_DISCOVERY_QUERY = ["xpu-smi", "discovery"]
XPU_STATS_QUERY = ["xpu-smi", "stats"]
NVIDIA_QUERY = [
    "nvidia-smi",
    "--query-gpu=name,temperature.gpu,utilization.gpu,"
    "memory.total,memory.used,memory.free",
    "--format=csv,noheader,nounits",
]

# Marker comments tell the parser which PowerShell script ran. They are
# not shown to anyone.
_WMI_MARKER = "freya-wmi-adapters"
_DXGI_MARKER = "freya-dxgi-adapters"

WMI_QUERY = [
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    (
        f"# {_WMI_MARKER}\n"
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        "Get-CimInstance -ClassName Win32_VideoController | "
        "Select-Object Name,AdapterRAM,PNPDeviceID,AdapterCompatibility | "
        "ConvertTo-Json -Compress\n"
    ),
]

# DXGI runs in its own process. A bad interface layout can take that
# process down; Freya still has the WMI names and the registry size.
DXGI_QUERY = [
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
    r"""
# freya-dxgi-adapters
$ErrorActionPreference = 'Stop'
try {
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class FreyaDxgi {
    [StructLayout(LayoutKind.Sequential)]
    public struct Luid {
        public uint LowPart;
        public int HighPart;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct Desc1 {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
        public string Description;
        public uint VendorId;
        public uint DeviceId;
        public uint SubSysId;
        public uint Revision;
        public ulong DedicatedVideoMemory;
        public ulong DedicatedSystemMemory;
        public ulong SharedSystemMemory;
        public Luid AdapterLuid;
        public uint Flags;
    }
    public class Row {
        public string Name;
        public uint VendorId;
        public uint DeviceId;
        public ulong DedicatedVideoMemory;
        public ulong SharedSystemMemory;
    }
    [ComImport, Guid("770aae78-f26f-4dba-a829-253c83d1b387"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface Factory1 {
        void SetPrivateData(ref Guid n, uint s, IntPtr d);
        void SetPrivateDataInterface(ref Guid n, IntPtr o);
        void GetPrivateData(ref Guid n, ref uint s, IntPtr d);
        void GetParent(ref Guid n, out IntPtr p);
        void EnumAdapters(uint i, out IntPtr a);
        void MakeWindowAssociation(IntPtr w, uint f);
        void GetWindowAssociation(out IntPtr w);
        void CreateSwapChain(IntPtr d, IntPtr desc, out IntPtr sc);
        void CreateSoftwareAdapter(IntPtr m, out IntPtr a);
        void EnumAdapters1(uint i, out Adapter1 a);
        int IsCurrent();
    }
    [ComImport, Guid("29038f61-3839-4626-91fd-086879011a05"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface Adapter1 {
        void SetPrivateData(ref Guid n, uint s, IntPtr d);
        void SetPrivateDataInterface(ref Guid n, IntPtr o);
        void GetPrivateData(ref Guid n, ref uint s, IntPtr d);
        void GetParent(ref Guid n, out IntPtr p);
        void EnumOutputs(uint i, out IntPtr o);
        void GetDesc(IntPtr d);
        void CheckInterfaceSupport(ref Guid n, out long v);
        void GetDesc1(IntPtr desc);
    }
    [DllImport("dxgi.dll")]
    static extern int CreateDXGIFactory1(ref Guid id, out Factory1 factory);
    public static List<Row> Query() {
        var found = new List<Row>();
        if (IntPtr.Size != 8) return found;
        var id = new Guid("770aae78-f26f-4dba-a829-253c83d1b387");
        Factory1 factory;
        if (CreateDXGIFactory1(ref id, out factory) < 0 || factory == null) return found;
        for (uint i = 0; i < 16; i++) {
            Adapter1 adapter;
            try { factory.EnumAdapters1(i, out adapter); }
            catch { break; }
            if (adapter == null) break;
            int bytes = Marshal.SizeOf(typeof(Desc1));
            IntPtr ptr = Marshal.AllocHGlobal(bytes);
            try {
                adapter.GetDesc1(ptr);
                var desc = (Desc1)Marshal.PtrToStructure(ptr, typeof(Desc1));
                if ((desc.Flags & 2) == 0 && desc.VendorId != 0x1414) {
                    found.Add(new Row {
                        Name = desc.Description,
                        VendorId = desc.VendorId,
                        DeviceId = desc.DeviceId,
                        DedicatedVideoMemory = desc.DedicatedVideoMemory,
                        SharedSystemMemory = desc.SharedSystemMemory
                    });
                }
            } finally {
                Marshal.FreeHGlobal(ptr);
            }
            Marshal.ReleaseComObject(adapter);
        }
        Marshal.ReleaseComObject(factory);
        return found;
    }
}
'@
  $rows = [FreyaDxgi]::Query()
  if ($rows -eq $null -or $rows.Count -eq 0) { '[]' } else { $rows | ConvertTo-Json -Compress }
} catch {
  '[]'
}
""",
]

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
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
_ARC_NAME = re.compile(r"\barc\b|\bxe max\b", re.IGNORECASE)
_GENERIC_NAME = re.compile(
    r"^(?:amd|intel|nvidia) gpu(?: \(device [0-9a-f]+\))?$",
    re.IGNORECASE,
)
_CARD_DIR = re.compile(r"card\d+\Z")
_NOT_A_NUMBER = re.compile(
    r"^(?:n/?a|none|null|\[n/a\]|\[not supported\])$",
    re.IGNORECASE,
)

# Filenames worth reading under a DRM card. A serial file sitting beside
# them is never opened.
_SYSFS_TOTAL = {
    "mem_info_vram_total",
    "lmem_total_bytes",
    "physical_vram_size_bytes",
}
_SYSFS_USED = {
    "mem_info_vram_used",
    "physical_vram_used_bytes",
}

_DEFAULT = object()


def probe_commands(system):
    """
    Tools asked on this operating system, NVIDIA last.

    macOS uses the Linux list. The tools are simply absent there, and
    the probe still returns an empty list.
    """
    amd = [ROCM_SMI_QUERY, AMD_SMI_STATIC_QUERY, AMD_SMI_METRIC_QUERY]
    intel = [XPU_DISCOVERY_QUERY, XPU_STATS_QUERY]
    if system == "Windows":
        return amd + intel + [WMI_QUERY, DXGI_QUERY, NVIDIA_QUERY]
    return amd + intel + [NVIDIA_QUERY]


def _clean(text, limit=MAX_NAME_CHARACTERS):
    text = _CONTROL.sub("", str(text or "")).strip().strip("|").strip()
    return text[:limit]


def _device_token(value):
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not re.fullmatch(r"[0-9a-f]{1,8}", text):
        return ""
    return text.lstrip("0") or "0"


def _is_generic(name):
    return bool(_GENERIC_NAME.match((name or "").strip()))


def _generic_name(vendor, device=""):
    label = {"amd": "AMD", "intel": "Intel", "nvidia": "NVIDIA"}.get(vendor, "GPU")
    if device:
        return f"{label} GPU (device {device})"
    return f"{label} GPU"


def _norm_name(name):
    text = (name or "").lower().replace("(r)", " ").replace("(tm)", " ")
    text = re.sub(r"\b(amd|intel|nvidia|advanced micro devices)\b", " ", text)
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


def _pci_vendor(text):
    text = (text or "").strip()
    if re.fullmatch(r"0x[0-9A-Fa-f]+", text):
        return PCI_VENDORS.get(int(text, 16), "")
    return ""


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or _NOT_A_NUMBER.match(text):
        return None
    match = re.match(r"^-?[0-9]*\.?[0-9]+", text)
    if not match:
        return None
    return float(match.group(0))


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


def _percent(value):
    number = _number(value)
    if number is None or number < 0 or number > 100:
        return None
    return int(number)


def _temperature(value):
    number = _number(value)
    if number is None or number < 0 or number > 150:
        return None
    return int(number)


def _memory_kind(vendor, name, dedicated):
    """
    `dedicated` when a discrete card reported memory, `shared` for an
    integrated adapter, `unknown` when a discrete card's memory could
    not be read, `drop` for a virtual display.

    Arc is decided before the integrated-name check, so a name that
    contains both "Arc" and "Graphics" stays a discrete card. A generic
    AMD row with no memory yet is unknown, not an integrated adapter:
    a later tool may still fill it in.
    """
    if _BASIC_ADAPTER.search(name or ""):
        return "drop"
    if _ARC_NAME.search(name or ""):
        return "dedicated" if dedicated else "unknown"
    if _IGPU_NAME.search(name or ""):
        return "shared"
    if vendor == "intel" and not _DISCRETE_NAME.search(name or ""):
        if not dedicated or dedicated < TINY_DEDICATED_BYTES:
            return "shared"
    if (
        vendor == "amd"
        and _is_generic(name)
        and dedicated
        and dedicated < TINY_DEDICATED_BYTES
    ):
        return "shared"
    if dedicated:
        return "dedicated"
    return "unknown"


def _blank(vendor, name, device="", **fields):
    card = {
        "vendor": vendor,
        "name": _clean(name) or _generic_name(vendor, device),
        "device": device or "",
        "dedicated_bytes": None,
        "used_bytes": None,
        "free_bytes": None,
        "utilization_percent": None,
        "temperature_c": None,
    }
    card.update(fields)
    return card


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


def _rows(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _card_records(payload):
    """
    Tool JSON is either a list of cards, a map of card0/card1, or one
    card. The shape has changed between releases, so any of those is
    accepted.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("gpu_data", "cards", "gpus"):
        if isinstance(payload.get(key), list):
            return [item for item in payload[key] if isinstance(item, dict)]
    nested = []
    for key, value in payload.items():
        if isinstance(value, dict) and str(key).lower().startswith("card"):
            nested.append(value)
    if nested:
        return nested
    return [payload]


def _valued_fields(obj, path=""):
    found = []
    if isinstance(obj, dict):
        if "value" in obj and not isinstance(obj.get("value"), (dict, list)):
            found.append((path, obj.get("value"), obj.get("unit")))
        for key, value in obj.items():
            if key == "value":
                continue
            found.extend(_valued_fields(value, f"{path}.{str(key).lower()}"))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_valued_fields(item, path))
    return found


def _first_field(fields, predicate):
    for path, value, unit in fields:
        if predicate(path):
            return value, unit
    return None, None


def _apply_live_fields(card, record):
    fields = _valued_fields(record)
    flat_checks = []
    if isinstance(record, dict):
        for key, value in record.items():
            if isinstance(value, (dict, list)):
                continue
            flat_checks.append((str(key).lower(), value, None))
    fields = fields + flat_checks

    util, _unit = _first_field(
        fields,
        lambda path: (
            "gfx_activity" in path
            or "gpu use" in path
            or "gpu_busy" in path
            or path.endswith("utilization")
        ),
    )
    if card["utilization_percent"] is None:
        card["utilization_percent"] = _percent(util)

    temp, _unit = _first_field(
        fields,
        lambda path: (
            "hotspot" not in path
            and "junction" not in path
            and "mem" not in path
            and ("edge" in path or path.endswith("temperature") or "(c)" in path)
        ),
    )
    if card["temperature_c"] is None:
        card["temperature_c"] = _temperature(temp)

    total, total_unit = _first_field(
        fields,
        lambda path: (
            "vram" in path
            and "used" not in path
            and "free" not in path
            and ("total" in path or path.endswith("vram") or path.endswith("size"))
        ),
    )
    used, used_unit = _first_field(
        fields,
        lambda path: "vram" in path and "used" in path and "free" not in path,
    )
    if card["dedicated_bytes"] is None:
        card["dedicated_bytes"] = _int_bytes(total, total_unit)
    if card["used_bytes"] is None:
        card["used_bytes"] = _int_bytes(used, used_unit)
    return card


def _name_from_rocm(record):
    for key in ("Card SKU", "Card series", "Card model", "Card Name"):
        text = _clean(record.get(key))
        if text and not _device_token(text):
            return text
    return ""


def parse_rocm_smi(output):
    """`rocm-smi` JSON. Names and VRAM come from whichever keys exist."""
    payload = _load_json(output)
    cards = []
    for record in _card_records(payload):
        name = _name_from_rocm(record)
        vendor = _vendor_from_text(record.get("Card vendor")) or _vendor_from_text(name) or "amd"
        if vendor not in VENDOR_RANK:
            continue
        card = _blank(vendor, name)
        _apply_live_fields(card, record)
        if card["name"] or card["dedicated_bytes"]:
            cards.append(card)
    return cards


def _vram_size_bytes(record):
    vram = None
    if isinstance(record, dict):
        for key, value in record.items():
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


def parse_amd_smi_static(output):
    """`amd-smi static --json`. Marketing name and VRAM size."""
    payload = _load_json(output)
    cards = []
    for record in _card_records(payload):
        asic = record.get("asic") if isinstance(record.get("asic"), dict) else {}
        vendor_id = _device_token(asic.get("vendor_id"))
        vendor = PCI_VENDORS.get(int(vendor_id, 16), "") if vendor_id else ""
        name = _clean(asic.get("market_name") or asic.get("market name"))
        if not vendor:
            vendor = _vendor_from_text(name) or "amd"
        if vendor not in VENDOR_RANK:
            continue
        device = _device_token(asic.get("device_id"))
        card = _blank(vendor, name, device)
        card["dedicated_bytes"] = _vram_size_bytes(record)
        if not card["name"] and not card["dedicated_bytes"]:
            continue
        cards.append(card)
    return cards


def parse_amd_smi_metric(output):
    """`amd-smi metric --json`. Use, temperature, and VRAM used."""
    payload = _load_json(output)
    cards = []
    for record in _card_records(payload):
        card = _blank("amd", "")
        _apply_live_fields(card, record)
        if (
            card["dedicated_bytes"]
            or card["used_bytes"]
            or card["utilization_percent"] is not None
            or card["temperature_c"] is not None
        ):
            cards.append(card)
    return cards


def _xpu_cards_from_text(output):
    """
    xpu-smi prints an ASCII table. Device names and the few live
    figures are taken from the labels; everything else is ignored.
    """
    cards = []
    current = None

    def ensure():
        nonlocal current
        if current is None:
            current = _blank("intel", "")
            cards.append(current)
        return current

    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        name_match = re.search(r"Device Name:\s*(.+)", line)
        if name_match:
            name = _clean(name_match.group(1).strip(" |"))
            if current is not None and current.get("name") and not _is_generic(current["name"]):
                current = None
            card = ensure()
            card["name"] = _better_name(card["name"], name)
            vendor = _vendor_from_text(name)
            if vendor in VENDOR_RANK:
                card["vendor"] = vendor
            continue
        util = re.search(r"GPU Utilization\s*\(%\)\s*[:|]\s*([^|]+)", line, re.I)
        if util:
            ensure()["utilization_percent"] = _percent(util.group(1))
            continue
        temp = re.search(
            r"GPU Core Temperature\s*\(C\)\s*[:|]\s*([^|]+)",
            line,
            re.I,
        )
        if temp:
            ensure()["temperature_c"] = _temperature(temp.group(1))
            continue
        used = re.search(r"GPU Memory Used\s*\(MiB\)\s*[:|]\s*([^|]+)", line, re.I)
        if used:
            mib = _number(used.group(1))
            if mib is not None and mib >= 0:
                ensure()["used_bytes"] = int(mib) * MIB
            continue
        total = re.search(
            r"GPU Memory(?:\s+Total)?\s*\(MiB\)\s*[:|]\s*([^|]+)",
            line,
            re.I,
        )
        if total and "used" not in line.lower():
            mib = _number(total.group(1))
            if mib is not None and mib > 0:
                ensure()["dedicated_bytes"] = int(mib) * MIB
    return [
        card
        for card in cards
        if card["vendor"] in VENDOR_RANK
        and (not _is_generic(card["name"]) or card["dedicated_bytes"] or card["used_bytes"])
    ]


def parse_xpu_discovery(output):
    return _xpu_cards_from_text(output)


def parse_xpu_stats(output):
    return _xpu_cards_from_text(output)


def _adapter_ram_bytes(value):
    memory = _int_bytes(value, "B")
    if memory is None or memory >= WMI_ADAPTER_RAM_CAP:
        return None
    return memory


def parse_windows_adapters(output):
    """
    `Win32_VideoController` JSON. AdapterRAM is kept only when it is
    clearly below the 4 GB ceiling.
    """
    payload = _load_json(output)
    cards = []
    for row in _rows(payload):
        name = _clean(row.get("Name") or row.get("name"))
        if not name:
            continue
        pnp = str(row.get("PNPDeviceID") or row.get("pnp") or "")
        vendor = (
            _vendor_from_pnp(pnp)
            or _vendor_from_text(name)
            or _vendor_from_text(row.get("AdapterCompatibility") or row.get("compatibility"))
        )
        if vendor not in VENDOR_RANK:
            continue
        card = _blank(vendor, name, _device_from_pnp(pnp))
        card["dedicated_bytes"] = _adapter_ram_bytes(
            row.get("AdapterRAM") if "AdapterRAM" in row else row.get("adapter_ram")
        )
        cards.append(card)
    return cards


def parse_dxgi_adapters(output):
    """
    DXGI adapter rows: description, vendor id, dedicated video memory,
    and shared system memory. Shared memory is not reported as VRAM.
    """
    payload = _load_json(output)
    cards = []
    for row in _rows(payload):
        name = _clean(row.get("Name") or row.get("name"))
        vendor_id = row.get("VendorId")
        if vendor_id is None:
            vendor_id = row.get("vendor_id")
        try:
            vendor = PCI_VENDORS.get(int(vendor_id), "")
        except (TypeError, ValueError):
            vendor = ""
        if not vendor:
            vendor = _vendor_from_text(name)
        if vendor not in VENDOR_RANK:
            continue
        device_raw = row.get("DeviceId") if "DeviceId" in row else row.get("device_id")
        # DXGI's device id is an integer. The PCI id compared with WMI is hex.
        if isinstance(device_raw, int) and not isinstance(device_raw, bool):
            device_raw = f"{device_raw:04x}"
        device = _device_token(device_raw)
        dedicated = _int_bytes(
            row.get("DedicatedVideoMemory")
            if "DedicatedVideoMemory" in row
            else row.get("dedicated_bytes"),
            "B",
        )
        card = _blank(vendor, name, device, dedicated_bytes=dedicated)
        if card["name"] or card["dedicated_bytes"]:
            cards.append(card)
    return cards


def parse_nvidia_smi(output):
    """
    One CSV line per card: name, temperature, utilization, total, used,
    free. `[N/A]` and a short line are skipped, not raised.
    """
    cards = []
    for line in (output or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 6:
            continue
        name, temp, util, total, used, free = parts
        name = _clean(name)
        if not name or _BASIC_ADAPTER.search(name):
            continue
        total_mib = _number(total)
        used_mib = _number(used)
        free_mib = _number(free)

        def as_bytes(mib, allow_zero):
            if mib is None or mib < 0 or (mib == 0 and not allow_zero):
                return None
            return int(mib) * MIB

        cards.append(
            _blank(
                "nvidia",
                name,
                dedicated_bytes=as_bytes(total_mib, False),
                used_bytes=as_bytes(used_mib, True),
                free_bytes=as_bytes(free_mib, True),
                utilization_percent=_percent(util),
                temperature_c=_temperature(temp),
            )
        )
    return cards


def observations_from_registry(entries):
    """
    One dict per display-class subkey: DriverDesc, MatchingDeviceId,
    and MemoryBytes from HardwareInformation.qwMemorySize.
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
        if vendor not in VENDOR_RANK:
            continue
        memory = entry.get("MemoryBytes")
        parsed = _int_bytes(memory, "B") if memory else None
        cards.append(_blank(vendor, name, _device_from_pnp(matching), dedicated_bytes=parsed))
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
    if value <= 0:
        return None
    return int(value)


def read_windows_registry_entries():
    """`HardwareInformation.qwMemorySize` under the display class key."""
    if platform.system() != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, VIDEO_CLASS_KEY)
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
                    "MatchingDeviceId": _registry_value(winreg, sub, "MatchingDeviceId"),
                    "MemoryBytes": _qword_bytes(
                        _registry_value(winreg, sub, "HardwareInformation.qwMemorySize")
                    ),
                })
            finally:
                winreg.CloseKey(sub)
    finally:
        winreg.CloseKey(root)
    return entries


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(256)
    except OSError:
        return ""


def _read_int_file(path, allow_zero=False):
    text = _read_text(path).strip()
    if not text:
        return None
    if allow_zero and re.fullmatch(r"0+", text):
        return 0
    return _int_bytes(text, "B")


def read_sysfs_cards(root):
    """
    Linux DRM nodes. AMD VRAM from mem_info_vram_*, Intel Arc from
    i915 `lmem_total_bytes` or xe `physical_vram_size_bytes`. An Intel
    node with neither is an integrated adapter.
    """
    if not root:
        return []
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
        vendor = _pci_vendor(_read_text(os.path.join(device_dir, "vendor")))
        if not vendor:
            continue
        device = _device_token(_read_text(os.path.join(device_dir, "device")).strip())
        product = _clean(_read_text(os.path.join(device_dir, "product_name")))
        if not product or _device_token(product):
            product = ""
        total = None
        used = None
        busy = None
        temp = None
        for dirpath, dirnames, filenames in os.walk(card_dir, onerror=lambda _err: None):
            depth = dirpath[len(card_dir):].count(os.sep)
            if depth > 4:
                dirnames.clear()
                continue
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                if filename in _SYSFS_TOTAL:
                    value = _read_int_file(path)
                    if value and (total is None or value > total):
                        total = value
                elif filename in _SYSFS_USED:
                    value = _read_int_file(path, allow_zero=True)
                    if value is not None and (used is None or value > used):
                        used = value
                elif filename == "gpu_busy_percent" and busy is None:
                    busy = _percent(_read_text(path).strip())
                elif filename == "temp1_input" and temp is None:
                    raw = _number(_read_text(path).strip())
                    if raw is not None and raw >= 1000:
                        temp = _temperature(raw / 1000)
        cards.append(
            _blank(
                vendor,
                product,
                device,
                dedicated_bytes=total,
                used_bytes=used,
                utilization_percent=busy,
                temperature_c=temp,
            )
        )
    return cards


def _find_slot(slots, incoming):
    incoming_kind = _memory_kind(
        incoming["vendor"], incoming["name"], incoming.get("dedicated_bytes")
    )
    if incoming_kind == "drop":
        return None
    candidates = []
    for slot in slots:
        if slot.get("_taken") or slot["vendor"] != incoming["vendor"]:
            continue
        if slot.get("device") and incoming.get("device") and slot["device"] != incoming["device"]:
            continue
        slot_kind = _memory_kind(slot["vendor"], slot["name"], slot.get("dedicated_bytes"))
        if slot_kind == "drop":
            continue
        if slot_kind == "shared" and incoming_kind == "dedicated":
            continue
        if slot_kind == "dedicated" and incoming_kind == "shared":
            continue
        if not _names_compatible(slot["name"], incoming["name"]):
            continue
        candidates.append(slot)
    if len(candidates) == 1:
        return candidates[0]
    # A named Arc card belongs with the node that actually has local
    # memory, not with the integrated adapter beside it.
    if _ARC_NAME.search(incoming.get("name") or ""):
        discrete = [
            slot for slot in candidates
            if (slot.get("dedicated_bytes") or 0) >= TINY_DEDICATED_BYTES
        ]
        if len(discrete) == 1:
            return discrete[0]
    generics = [slot for slot in candidates if _is_generic(slot["name"])]
    if len(generics) == 1 and not _is_generic(incoming["name"]):
        return generics[0]
    return None


def _fill(slot, incoming):
    slot["name"] = _better_name(slot.get("name"), incoming.get("name"))
    if incoming.get("device") and not slot.get("device"):
        slot["device"] = incoming["device"]
    new = incoming.get("dedicated_bytes")
    old = slot.get("dedicated_bytes")
    if new and (not old or new > old):
        slot["dedicated_bytes"] = new
    for key in ("used_bytes", "free_bytes", "utilization_percent", "temperature_c"):
        if slot.get(key) is None and incoming.get(key) is not None:
            slot[key] = incoming[key]


def _add(slots, cards):
    for slot in slots:
        slot["_taken"] = False
    for incoming in cards or []:
        if not isinstance(incoming, dict) or incoming.get("vendor") not in VENDOR_RANK:
            continue
        slot = _find_slot(slots, incoming)
        if slot is None:
            if _memory_kind(
                incoming["vendor"], incoming["name"], incoming.get("dedicated_bytes")
            ) == "drop":
                continue
            slots.append(dict(incoming))
            slots[-1]["_taken"] = True
        else:
            _fill(slot, incoming)
            slot["_taken"] = True


def _mib(value, allow_zero):
    if value is None or isinstance(value, bool):
        return None
    if value < 0 or (value == 0 and not allow_zero):
        return None
    return int(value // MIB)


def _public(card, index):
    dedicated = card.get("dedicated_bytes")
    kind = _memory_kind(card["vendor"], card.get("name"), dedicated)
    if kind == "drop":
        return None
    total = used = free = None
    if kind == "dedicated":
        total = _mib(dedicated, False)
        used = _mib(card.get("used_bytes"), True)
        free = _mib(card.get("free_bytes"), True)
        if total is not None and used is not None and free is None:
            free = max(0, total - used)
        elif total is not None and free is not None and used is None:
            used = max(0, total - free)
        if total is None:
            kind = "unknown"
    return {
        "index": index,
        "vendor": card["vendor"],
        "name": _clean(card.get("name")) or _generic_name(card["vendor"]),
        "temperature_c": card.get("temperature_c"),
        "utilization_percent": card.get("utilization_percent"),
        "memory_total_mib": total,
        "memory_used_mib": used,
        "memory_free_mib": free,
        "memory_kind": kind,
    }


def _ordered(cards):
    ranked = sorted(
        enumerate(cards),
        key=lambda item: (
            VENDOR_RANK.get(item[1].get("vendor"), 9),
            0 if _memory_kind(
                item[1].get("vendor"),
                item[1].get("name"),
                item[1].get("dedicated_bytes"),
            ) == "dedicated" else 1,
            item[0],
        ),
    )
    published = []
    for _index, card in ranked:
        public = _public(card, len(published))
        if public is not None:
            published.append(public)
    return published


def _parse_command(command, output):
    name = command[0]
    script = command[-1] if command else ""
    if name == "rocm-smi":
        return parse_rocm_smi(output)
    if name == "amd-smi":
        if "metric" in command:
            return parse_amd_smi_metric(output)
        return parse_amd_smi_static(output)
    if name == "xpu-smi":
        if "stats" in command:
            return parse_xpu_stats(output)
        return parse_xpu_discovery(output)
    if name == "powershell.exe":
        if _DXGI_MARKER in script:
            return parse_dxgi_adapters(output)
        return parse_windows_adapters(output)
    if name == "nvidia-smi":
        return parse_nvidia_smi(output)
    return []


def _default_run(command):
    try:
        # env is a keyword on purpose: a program started without it
        # inherits Freya's own environment, credentials included.
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
            env=child_environment(),
            # Without this, a Freya running without a console of her own
            # flashes a terminal window each time a graphics tool is asked.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(completed, "returncode", 0) not in (0, None):
        return None
    output = completed.stdout or ""
    if len(output) > MAX_QUERY_CHARS:
        return None
    return output


def read_gpus(run=_DEFAULT, system=None, sysfs_root=_DEFAULT, registry_entries=_DEFAULT):
    """
    Every graphics card this machine reported, AMD first, then Intel,
    then NVIDIA. Discrete cards of a vendor come before that vendor's
    integrated adapter, so an RX 9070 XT stays ahead of the processor's
    Radeon graphics.

    Pass `run` to feed tool output in tests. The real sysfs tree and
    the Windows registry are read only when this function asks the
    tools itself.
    """
    system = platform.system() if system is None else system
    real = run is _DEFAULT
    if real:
        run = _default_run
    if sysfs_root is _DEFAULT:
        sysfs_root = "/sys/class/drm" if system == "Linux" and real else None
    if registry_entries is _DEFAULT:
        if system == "Windows" and real and platform.system() == "Windows":
            registry_entries = read_windows_registry_entries()
        else:
            registry_entries = []

    slots = []
    if sysfs_root:
        _add(slots, read_sysfs_cards(sysfs_root))
    for command in probe_commands(system):
        try:
            output = run(command)
        except (OSError, subprocess.SubprocessError):
            output = None
        if not output:
            continue
        try:
            parsed = _parse_command(command, output)
        except (TypeError, ValueError):
            parsed = []
        _add(slots, parsed)
    if system == "Windows":
        _add(slots, observations_from_registry(registry_entries))
    return _ordered(slots)


def preferred_gpu(gpus):
    """
    The card model fit and speech unloading should use.

    The first card with a dedicated total wins, which is the AMD card
    when one reported memory. An integrated adapter with no dedicated
    total does not hide a later discrete card. With no dedicated total
    at all, the first card is returned so the caller can still name it.
    """
    for gpu in gpus or []:
        total = gpu.get("memory_total_mib")
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            return gpu
    if gpus:
        return gpus[0]
    return None


def preferred_free_mib(gpus=None):
    """
    Free dedicated memory, in MiB, on the preferred card.

    None when that card's free memory was not reported. Another card's
    free memory is not substituted.
    """
    if gpus is None:
        gpus = read_gpus()
    gpu = preferred_gpu(gpus)
    if not gpu:
        return None
    free = gpu.get("memory_free_mib")
    if isinstance(free, bool) or not isinstance(free, int):
        return None
    return free
