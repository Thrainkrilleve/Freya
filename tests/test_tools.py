import json
from collections import namedtuple
from types import SimpleNamespace

import pytest

from freya.core.capabilities import CapabilityRegistry
from freya.tools import hardware_inventory, system_tools
from freya.tools.model_tools import MODEL_TOOL
from freya.tools.system_tools import (
    SYSTEM_TOOL_CAPABILITIES,
    SYSTEM_TOOLS_PROVIDER,
    register_system_tools,
)
from freya.tools.tool_router import FreyaToolRouter


RAW_INVENTORY = {
    "os": {"Caption": "Test OS ", "OSArchitecture": "64-bit", "BuildNumber": "1"},
    "system": {"Manufacturer": "Acme", "Model": "Box"},
    "motherboard": {"Manufacturer": "Acme", "Product": "Board 1"},
    "bios": {"Manufacturer": "Acme", "SMBIOSBIOSVersion": "1.2"},
    "processors": {
        "Name": "Test CPU",
        "NumberOfCores": 8,
        "NumberOfLogicalProcessors": 16,
        "MaxClockSpeed": 3000,
    },
    "memory_modules": [
        {
            "Capacity": 17179869184,
            "Speed": 4800,
            "ConfiguredClockSpeed": 4800,
            "Manufacturer": "Acme",
            "PartNumber": "RAM-6400 ",
            "DeviceLocator": "DIMM1",
            "SMBIOSMemoryType": 34,
        },
        {
            "Capacity": 17179869184,
            "Speed": 4800,
            "Manufacturer": "Acme",
            "PartNumber": "RAM-6400",
            "DeviceLocator": "DIMM2",
        },
    ],
    "disks": {
        "FriendlyName": "Test SSD",
        "MediaType": "SSD",
        "BusType": "NVMe",
        "Size": 1073741824000,
        "HealthStatus": "Healthy",
    },
    "graphics": {
        "Name": "Test GPU",
        "DriverVersion": "1.0",
        "CurrentHorizontalResolution": 1920,
        "CurrentVerticalResolution": 1080,
        "CurrentRefreshRate": 60,
    },
    "network_adapters": [
        {"Name": "Wired", "NetEnabled": True, "Speed": 1000000000},
        {"Name": "Wireless", "NetEnabled": False, "Speed": 9223372036854775807},
    ],
    "sound_devices": None,
}


@pytest.fixture
def registry():
    registry = CapabilityRegistry()
    register_system_tools(registry)
    return registry


@pytest.fixture
def fake_inventory(monkeypatch):
    """Replace the PowerShell hardware query with canned output."""
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=json.dumps(RAW_INVENTORY))

    hardware_inventory.clear_hardware_inventory_cache()
    monkeypatch.setattr(hardware_inventory.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hardware_inventory.subprocess, "run", run)
    yield calls
    hardware_inventory.clear_hardware_inventory_cache()


def test_system_tools_register_as_capabilities(registry):
    described = {c["name"]: c for c in registry.describe_capabilities()}

    assert set(described) == set(SYSTEM_TOOL_CAPABILITIES)
    assert set(described) == {
        "system.info",
        "system.disk",
        "system.gpu",
        "system.cpu",
        "system.memory",
        "system.hardware",
        "system.sensors",
    }
    for capability in described.values():
        assert capability["provider"] == SYSTEM_TOOLS_PROVIDER
        assert capability["category"] == "system"
        assert capability["destructive"] is False
        assert capability["metadata"] == {MODEL_TOOL: True}


@pytest.mark.parametrize(
    "alias, name",
    [
        ("system", "system.info"),
        ("computer.info", "system.info"),
        ("disk", "system.disk"),
        ("disk.info", "system.disk"),
        ("drives", "system.disk"),
        ("gpu", "system.gpu"),
        ("gpu.status", "system.gpu"),
        ("cpu", "system.cpu"),
        ("ram", "system.memory"),
        ("hardware", "system.hardware"),
    ],
)
def test_system_tool_aliases_resolve(registry, alias, name):
    assert registry.get(alias).name == name


def test_system_tools_cannot_be_registered_twice(registry):
    with pytest.raises(ValueError):
        register_system_tools(registry)


def test_system_info_executes_through_registry(registry):
    result = registry.execute("system.info")

    assert result.success is True
    assert result.capability_name == "system.info"
    assert result.provider == SYSTEM_TOOLS_PROVIDER
    assert result.result["python_version"]


@pytest.fixture
def fake_machine(monkeypatch):
    """
    A made-up machine behind psutil and shutil, so the test proves the
    tools' shape and arithmetic without reading the one it runs on
    (a runner may have a 0-byte mount, one core, or no frequency).
    """
    Usage = namedtuple("Usage", "total used free")
    Partition = namedtuple("Partition", "mountpoint fstype opts")
    Frequency = namedtuple("Frequency", "current min max")
    Memory = namedtuple("Memory", "total available used percent")
    gb = 1024 ** 3

    monkeypatch.setattr(system_tools.psutil, "cpu_percent", lambda interval=None: 12.5)
    monkeypatch.setattr(system_tools.psutil, "cpu_freq", lambda: Frequency(3400.0, 800.0, 5200.0))
    monkeypatch.setattr(system_tools.psutil, "cpu_count", lambda logical=True: 16 if logical else 8)
    monkeypatch.setattr(system_tools.psutil, "virtual_memory", lambda: Memory(64 * gb, 40 * gb, 24 * gb, 37.5))
    monkeypatch.setattr(system_tools.psutil, "disk_partitions", lambda all=False: [
        Partition("C:\\", "NTFS", "rw,fixed"),
        Partition("E:\\", "", "cdrom"),
        Partition("D:\\", "NTFS", "rw,removable"),
    ])
    monkeypatch.setattr(system_tools.shutil, "disk_usage", lambda mountpoint: {
        "C:\\": Usage(2000 * gb, 1200 * gb, 800 * gb),
        "D:\\": Usage(4000 * gb, 1000 * gb, 3000 * gb),
    }[mountpoint])
    monkeypatch.setattr(system_tools, "_processor_name", lambda: "Fake i9")


def test_cpu_memory_and_disk_tools_return_their_shape(registry, fake_machine):
    cpu = registry.execute("system.cpu").result
    memory = registry.execute("system.memory").result
    drives = registry.execute("system.disk").result

    assert cpu["processor"]
    assert 0 <= cpu["utilization_percent"] <= 100
    assert cpu["logical_cores"] >= cpu["physical_cores"] >= 1

    assert memory["total_gb"] > 0
    assert 0 < memory["used_percent"] <= 100
    assert memory["used_gb"] + memory["available_gb"] == pytest.approx(
        memory["total_gb"], abs=0.02
    )

    # The drive with no file system (an empty optical drive) is skipped.
    assert [d["drive"] for d in drives] == ["C:\\", "D:\\"]
    assert [d["removable"] for d in drives] == [False, True]
    for drive in drives:
        assert drive["filesystem"] == "NTFS"
        assert drive["total_gb"] > 0
        assert 0 <= drive["used_percent"] <= 100
    assert drives[0]["used_percent"] == 60.0


def test_unreadable_drives_are_skipped(monkeypatch):
    Partition = namedtuple("Partition", "device mountpoint fstype opts")
    Usage = namedtuple("Usage", "total used free")

    monkeypatch.setattr(
        system_tools.psutil,
        "disk_partitions",
        lambda all=False: [
            Partition("C:\\", "C:\\", "NTFS", "rw,fixed"),
            Partition("E:\\", "E:\\", "", "cdrom"),
            Partition("F:\\", "F:\\", "exFAT", "rw,removable"),
            Partition("G:\\", "G:\\", "NTFS", "rw,fixed"),
        ],
    )

    def usage(path):
        if path == "G:\\":
            raise PermissionError("The device is not ready.")
        return Usage(100 * 1024**3, 25 * 1024**3, 75 * 1024**3)

    monkeypatch.setattr(system_tools.shutil, "disk_usage", usage)

    drives = system_tools.get_disk_info()

    assert [d["drive"] for d in drives] == ["C:\\", "F:\\"]
    assert [d["removable"] for d in drives] == [False, True]
    assert drives[0]["used_percent"] == 25.0
    assert drives[0]["free_gb"] == 75.0


def test_hardware_inventory_is_normalized(fake_inventory):
    inventory = hardware_inventory.get_hardware_inventory()

    assert inventory["operating_system"] == "Test OS 64-bit"
    assert inventory["motherboard"] == "Acme Board 1"
    assert inventory["bios_version"] == "Acme 1.2"
    assert inventory["processors"] == [
        {"name": "Test CPU", "cores": 8, "threads": 16, "max_speed_mhz": 3000}
    ]
    assert inventory["memory_total_gb"] == 32.0
    assert inventory["memory_modules"][0] == {
        "slot": "DIMM1",
        "size_gb": 16.0,
        "type": "DDR5",
        "current_speed_mhz": 4800,
        "manufacturer": "Acme",
        "part_number": "RAM-6400",
    }
    assert inventory["memory_modules"][1]["current_speed_mhz"] == 4800
    assert inventory["memory_modules"][1]["type"] is None
    # The model must not mistake the current speed for the rated speed.
    assert "rated" in inventory["memory_speed_note"]
    # A single object from PowerShell becomes a one-row list.
    assert inventory["physical_disks"] == [
        {
            "name": "Test SSD",
            "size_gb": 1000.0,
            "type": "SSD",
            "interface": "NVMe",
            "health": "Healthy",
        }
    ]
    assert inventory["graphics_cards"][0]["display"] == "1920x1080 at 60 Hz"
    assert inventory["network_adapters"] == [
        {"name": "Wired", "connected": True, "link_speed_mbps": 1000},
        {"name": "Wireless", "connected": False, "link_speed_mbps": None},
    ]
    assert inventory["audio_devices"] == []
    # It is handed to the model as JSON.
    json.dumps(inventory)


def test_hardware_inventory_is_cached(fake_inventory):
    first = hardware_inventory.get_hardware_inventory()
    second = hardware_inventory.get_hardware_inventory()

    assert first is second
    assert len(fake_inventory) == 1

    hardware_inventory.clear_hardware_inventory_cache()
    hardware_inventory.get_hardware_inventory()

    assert len(fake_inventory) == 2


def test_hardware_inventory_is_windows_only(fake_inventory, monkeypatch):
    monkeypatch.setattr(hardware_inventory.platform, "system", lambda: "Linux")

    with pytest.raises(RuntimeError):
        hardware_inventory.get_hardware_inventory()


@pytest.mark.parametrize(
    "text, prompt",
    [
        ("generate an image of a red fox", "a red fox"),
        ("Please draw me a picture of a castle.", "a castle"),
        ("could you create an illustration of two owls?", "two owls"),
    ],
)
def test_router_recognises_image_requests(text, prompt):
    request = FreyaToolRouter().route(text)

    assert request.tool == "image"
    assert request.prompt == prompt


@pytest.mark.parametrize(
    "text",
    [
        "",
        "tell me a joke",
        "what's my cpu usage",
        "how much disk space is free",
        "what do you remember about me",
        "I saw an image of a fox yesterday",
    ],
)
def test_router_leaves_everything_else_to_the_model(text):
    assert FreyaToolRouter().route(text) is None
