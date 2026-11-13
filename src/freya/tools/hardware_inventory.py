import json
import platform
import subprocess
import time

from freya.child_environment import child_environment


# Read-only CIM queries. Serial numbers, MAC addresses, and other unique
# identifiers are deliberately not collected.
INVENTORY_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'

function Rows($items, $names) {
    @($items | Select-Object -Property $names)
}

$physicalDisks = Rows (Get-PhysicalDisk) @(
    'FriendlyName', 'MediaType', 'BusType', 'Size', 'HealthStatus'
)

if ($physicalDisks.Count -eq 0) {
    $physicalDisks = @(
        Get-CimInstance Win32_DiskDrive | ForEach-Object {
            [pscustomobject]@{
                FriendlyName = $_.Model
                MediaType    = $_.MediaType
                BusType      = $_.InterfaceType
                Size         = $_.Size
                HealthStatus = $_.Status
            }
        }
    )
}

[pscustomobject]@{
    os = Rows (Get-CimInstance Win32_OperatingSystem) @(
        'Caption', 'Version', 'BuildNumber', 'OSArchitecture'
    )
    system = Rows (Get-CimInstance Win32_ComputerSystem) @(
        'Manufacturer', 'Model'
    )
    motherboard = Rows (Get-CimInstance Win32_BaseBoard) @(
        'Manufacturer', 'Product'
    )
    bios = Rows (Get-CimInstance Win32_BIOS) @(
        'Manufacturer', 'SMBIOSBIOSVersion'
    )
    processors = Rows (Get-CimInstance Win32_Processor) @(
        'Name', 'NumberOfCores', 'NumberOfLogicalProcessors',
        'MaxClockSpeed'
    )
    memory_modules = Rows (Get-CimInstance Win32_PhysicalMemory) @(
        'Manufacturer', 'PartNumber', 'Capacity', 'Speed',
        'ConfiguredClockSpeed', 'DeviceLocator', 'SMBIOSMemoryType'
    )
    disks = $physicalDisks
    graphics = Rows (Get-CimInstance Win32_VideoController) @(
        'Name', 'DriverVersion', 'CurrentHorizontalResolution',
        'CurrentVerticalResolution', 'CurrentRefreshRate'
    )
    network_adapters = Rows (
        Get-CimInstance Win32_NetworkAdapter |
            Where-Object { $_.PhysicalAdapter }
    ) @('Name', 'NetEnabled', 'Speed')
    sound_devices = Rows (Get-CimInstance Win32_SoundDevice) @('Name')
} | ConvertTo-Json -Depth 4 -Compress
"""

# Installed hardware rarely changes while Freya is running, and the query
# takes several seconds, so results are reused for a while.
CACHE_SECONDS = 600

MEMORY_SPEED_NOTE = (
    "current_speed_mhz is the speed the memory is running at now. "
    "Windows does not report a module's rated (XMP/EXPO) speed; the "
    "part number usually contains it."
)

# SMBIOS memory type codes for the generations Freya may meet.
MEMORY_TYPES = {
    24: "DDR3",
    26: "DDR4",
    34: "DDR5",
}

_cache = None
_cache_time = 0.0


def _text(value):
    if value is None:
        return None
    return str(value).strip() or None


def _rows(value):
    """
    PowerShell serializes a one-element list as a bare object.
    """
    if value is None:
        return []

    if isinstance(value, dict):
        return [value]

    return list(value)


def _gb(value):
    if not value:
        return None
    return round(int(value) / 1024**3, 1)


def _join(*parts):
    return " ".join(str(part) for part in parts if part) or None


def _query():
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            INVENTORY_SCRIPT,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=child_environment(),
        # Without this, a Freya running without a console of her own
        # shows a terminal window for as long as the query runs.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    output = completed.stdout.strip()

    if not output:
        raise RuntimeError(
            "The hardware query returned no data."
        )

    return json.loads(output)


def _normalize(raw):
    def first(section):
        rows = _rows(raw.get(section))
        return rows[0] if rows else {}

    os_row = first("os")
    system = first("system")
    board = first("motherboard")
    bios = first("bios")

    modules = [
        {
            "slot": _text(row.get("DeviceLocator")),
            "size_gb": _gb(row.get("Capacity")),
            "type": MEMORY_TYPES.get(
                row.get("SMBIOSMemoryType")
            ),
            "current_speed_mhz": (
                row.get("ConfiguredClockSpeed")
                or row.get("Speed")
            ),
            "manufacturer": _text(row.get("Manufacturer")),
            "part_number": _text(row.get("PartNumber")),
        }
        for row in _rows(raw.get("memory_modules"))
    ]

    adapters = []

    for row in _rows(raw.get("network_adapters")):
        connected = bool(row.get("NetEnabled"))
        speed = row.get("Speed")
        adapters.append(
            {
                "name": _text(row.get("Name")),
                "connected": connected,
                "link_speed_mbps": (
                    round(int(speed) / 1_000_000)
                    if connected and speed
                    else None
                ),
            }
        )

    return {
        "operating_system": _join(
            _text(os_row.get("Caption")),
            _text(os_row.get("OSArchitecture")),
        ),
        "os_build": _text(os_row.get("BuildNumber")),
        "system": _join(
            _text(system.get("Manufacturer")),
            _text(system.get("Model")),
        ),
        "motherboard": _join(
            _text(board.get("Manufacturer")),
            _text(board.get("Product")),
        ),
        "bios_version": _join(
            _text(bios.get("Manufacturer")),
            _text(bios.get("SMBIOSBIOSVersion")),
        ),
        "processors": [
            {
                "name": _text(row.get("Name")),
                "cores": row.get("NumberOfCores"),
                "threads": row.get("NumberOfLogicalProcessors"),
                "max_speed_mhz": row.get("MaxClockSpeed"),
            }
            for row in _rows(raw.get("processors"))
        ],
        "memory_total_gb": (
            round(sum(m["size_gb"] or 0 for m in modules), 1)
            if modules
            else None
        ),
        "memory_modules": modules,
        "memory_speed_note": MEMORY_SPEED_NOTE,
        "physical_disks": [
            {
                "name": _text(row.get("FriendlyName")),
                "size_gb": _gb(row.get("Size")),
                "type": _text(row.get("MediaType")),
                "interface": _text(row.get("BusType")),
                "health": _text(row.get("HealthStatus")),
            }
            for row in _rows(raw.get("disks"))
        ],
        "graphics_cards": [
            {
                "name": _text(row.get("Name")),
                "driver_version": _text(row.get("DriverVersion")),
                "display": (
                    f"{row.get('CurrentHorizontalResolution')}x"
                    f"{row.get('CurrentVerticalResolution')} at "
                    f"{row.get('CurrentRefreshRate')} Hz"
                    if row.get("CurrentHorizontalResolution")
                    else None
                ),
            }
            for row in _rows(raw.get("graphics"))
        ],
        "network_adapters": adapters,
        "audio_devices": [
            _text(row.get("Name"))
            for row in _rows(raw.get("sound_devices"))
        ],
    }


def get_hardware_inventory():
    """
    Return an inventory of the computer's components.

    This is read-only and Windows-only for now.
    """
    global _cache, _cache_time

    if platform.system() != "Windows":
        raise RuntimeError(
            "Hardware inventory is only supported on Windows."
        )

    if (
        _cache is not None
        and time.monotonic() - _cache_time < CACHE_SECONDS
    ):
        return _cache

    _cache = _normalize(_query())
    _cache_time = time.monotonic()

    return _cache


def clear_hardware_inventory_cache():
    global _cache
    _cache = None
