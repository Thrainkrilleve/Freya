"""
Live GPU status: AMD, then Intel, then NVIDIA.

Tool output and sysfs trees here are fixtures. Intel Arc cases have not
been run on Arc hardware.
"""

import json

import pytest

from freya.federation import profile as node_profile
from freya.gpu import model_fit, status
from freya.speech.setup import free_gpu_megabytes
from freya.tools.system_tools import get_gpu_info


GIB = 1024 ** 3
SERIAL = "ZZ-SERIAL-9900"


def _run(outputs):
    def run(command):
        return outputs.get(command[0])

    return run


def _gpus(outputs=None, system="Linux", sysfs=None, registry=None):
    kwargs = {"system": system}
    if sysfs is not None:
        kwargs["sysfs_root"] = sysfs
    if registry is not None:
        kwargs["registry_entries"] = registry
    return status.read_gpus(run=_run(outputs or {}), **kwargs)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_probe_order_is_amd_then_intel_then_nvidia():
    linux = [command[0] for command in status.probe_commands("Linux")]
    darwin = [command[0] for command in status.probe_commands("Darwin")]

    assert linux == darwin
    assert "powershell.exe" not in linux
    assert linux.index("rocm-smi") < linux.index("amd-smi")
    assert linux.index("amd-smi") < linux.index("xpu-smi")
    assert linux.index("xpu-smi") < linux.index("nvidia-smi")

    windows = [command[0] for command in status.probe_commands("Windows")]
    assert windows.index("amd-smi") < windows.index("xpu-smi")
    assert windows.index("xpu-smi") < windows.index("powershell.exe")
    assert windows.index("powershell.exe") < windows.index("nvidia-smi")
    assert windows.count("powershell.exe") == 2


def test_live_probe_matches_the_profile_constants():
    assert status.PCI_VENDORS == node_profile.PCI_VENDORS
    assert status.VIDEO_CLASS_KEY == node_profile.VIDEO_CLASS_KEY
    assert status.WMI_ADAPTER_RAM_CAP == node_profile.WMI_ADAPTER_RAM_CAP
    assert status.VENDOR_RANK == node_profile._VENDOR_RANK


def test_windows_scripts_only_ask_this_machine():
    wmi = status.WMI_QUERY[-1]
    dxgi = status.DXGI_QUERY[-1]

    assert "http" not in wmi.lower()
    assert "http" not in dxgi.lower()
    assert "Win32_VideoController" in wmi
    assert "CreateDXGIFactory1" in dxgi
    assert "DedicatedVideoMemory" in dxgi
    assert "SharedSystemMemory" in dxgi


def test_rocm_smi_reads_an_rx_9070_xt_and_drops_a_serial():
    payload = {
        "card0": {
            "Card series": "AMD Radeon RX 9070 XT",
            "Card model": "0x7550",
            "Card vendor": "Advanced Micro Devices, Inc. [AMD/ATI]",
            "GPU use (%)": "7",
            "Temperature (Sensor edge) (C)": "51.0",
            "Temperature (Sensor junction) (C)": "80",
            "VRAM Total Memory (B)": str(16 * GIB),
            "VRAM Total Used Memory (B)": str(2 * GIB),
            "Serial Number": SERIAL,
        }
    }
    gpus = _gpus({"rocm-smi": json.dumps(payload)})

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "amd"
    assert gpu["name"] == "AMD Radeon RX 9070 XT"
    assert gpu["memory_kind"] == "dedicated"
    assert gpu["memory_total_mib"] == 16 * 1024
    assert gpu["memory_used_mib"] == 2 * 1024
    assert gpu["memory_free_mib"] == 14 * 1024
    assert gpu["utilization_percent"] == 7
    assert gpu["temperature_c"] == 51
    assert SERIAL not in json.dumps(gpus)


def test_amd_smi_static_and_metric_agree_on_one_card():
    static = [{
        "gpu": 0,
        "asic": {
            "market_name": "AMD Radeon RX 9070 XT",
            "vendor_id": "0x1002",
            "device_id": "0x7550",
            "asic_serial": SERIAL,
        },
        "vram": {"type": "GDDR6", "size": 16368},
    }]
    metric = [{
        "gpu": 0,
        "usage": {"gfx_activity": {"value": 7, "unit": "%"}},
        "mem_usage": {
            "vram_total": {"value": 16368, "unit": "MB"},
            "vram_used": {"value": 2048, "unit": "MB"},
        },
        "temperature": {
            "edge": {"value": 41, "unit": "C"},
            "hotspot": {"value": 90, "unit": "C"},
        },
    }]

    def run(command):
        if command[0] == "amd-smi" and "metric" in command:
            return json.dumps(metric)
        if command[0] == "amd-smi":
            return json.dumps(static)
        return None

    gpus = status.read_gpus(run=run, system="Linux")

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "amd"
    assert gpu["name"] == "AMD Radeon RX 9070 XT"
    assert gpu["memory_total_mib"] == 16368
    assert gpu["memory_used_mib"] == 2048
    assert gpu["memory_free_mib"] == 16368 - 2048
    assert gpu["utilization_percent"] == 7
    assert gpu["temperature_c"] == 41
    assert SERIAL not in json.dumps(gpus)


def test_amd_sysfs_reports_vram_and_busy_percent(tmp_path):
    device = tmp_path / "card0" / "device"
    _write(device / "vendor", "0x1002\n")
    _write(device / "device", "0x7550\n")
    _write(device / "product_name", "AMD Radeon RX 9070 XT\n")
    _write(device / "mem_info_vram_total", f"{16 * GIB}\n")
    _write(device / "mem_info_vram_used", f"{2 * GIB}\n")
    _write(device / "gpu_busy_percent", "12\n")
    _write(device / "serial_number", SERIAL + "\n")
    _write(device / "hwmon" / "hwmon0" / "temp1_input", "51000\n")

    gpus = _gpus(sysfs=tmp_path)

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "amd"
    assert gpu["name"] == "AMD Radeon RX 9070 XT"
    assert gpu["memory_kind"] == "dedicated"
    assert gpu["memory_total_mib"] == 16 * 1024
    assert gpu["memory_used_mib"] == 2 * 1024
    assert gpu["memory_free_mib"] == 14 * 1024
    assert gpu["utilization_percent"] == 12
    assert gpu["temperature_c"] == 51
    assert SERIAL not in json.dumps(gpus)


def test_xpu_smi_discovery_and_stats_describe_an_arc_card():
    discovery = """
+-----------+---------------------------------------------------------------+
| Device ID | Device Information                                            |
+-----------+---------------------------------------------------------------+
| 0         | Device Name: Intel(R) Arc(TM) A770 Graphics                   |
|           | Vendor Name: Intel(R) Corporation                             |
|           | UUID: 00000000-0000-0000-0000-000000000000                     |
+-----------+---------------------------------------------------------------+
"""
    stats = """
| Device ID                   | 0        |
| GPU Utilization (%)         | 4        |
| GPU Memory Used (MiB)       | 512      |
| GPU Memory Total (MiB)      | 16384    |
| GPU Core Temperature (C)    | 42       |
| GPU Memory Temperature (C)  | N/A      |
"""
    def run(command):
        if command[0] != "xpu-smi":
            return None
        if "stats" in command:
            return stats
        return discovery

    gpus = status.read_gpus(run=run, system="Linux")

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "intel"
    assert "Arc" in gpu["name"]
    assert "A770" in gpu["name"]
    assert gpu["memory_kind"] == "dedicated"
    assert gpu["memory_total_mib"] == 16384
    assert gpu["memory_used_mib"] == 512
    assert gpu["memory_free_mib"] == 16384 - 512
    assert gpu["utilization_percent"] == 4
    assert gpu["temperature_c"] == 42


def test_i915_sysfs_local_memory_is_an_intel_discrete_card(tmp_path):
    _write(tmp_path / "card0" / "device" / "vendor", "0x8086\n")
    _write(tmp_path / "card0" / "device" / "device", "0x56a0\n")
    _write(tmp_path / "card0" / "lmem_total_bytes", f"{16 * GIB}\n")

    gpus = _gpus(sysfs=tmp_path)

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "intel"
    assert gpu["memory_kind"] == "dedicated"
    assert gpu["memory_total_mib"] == 16 * 1024
    assert gpu["memory_used_mib"] is None
    assert gpu["utilization_percent"] is None
    assert gpu["temperature_c"] is None


def test_xe_sysfs_vram_files_are_dedicated_memory(tmp_path):
    vram = tmp_path / "card0" / "device" / "vram0"
    _write(tmp_path / "card0" / "device" / "vendor", "0x8086\n")
    _write(tmp_path / "card0" / "device" / "device", "0x56a0\n")
    _write(vram / "physical_vram_size_bytes", f"{16 * GIB}\n")
    _write(vram / "physical_vram_used_bytes", f"{1 * GIB}\n")

    gpus = _gpus(sysfs=tmp_path)

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "intel"
    assert gpu["memory_kind"] == "dedicated"
    assert gpu["memory_total_mib"] == 16 * 1024
    assert gpu["memory_used_mib"] == 1024
    assert gpu["memory_free_mib"] == 15 * 1024


def test_arc_name_attaches_to_the_lmem_node_not_the_igpu(tmp_path):
    _write(tmp_path / "card0" / "device" / "vendor", "0x8086\n")
    _write(tmp_path / "card0" / "device" / "device", "0x46d1\n")
    _write(tmp_path / "card1" / "device" / "vendor", "0x8086\n")
    _write(tmp_path / "card1" / "device" / "device", "0x56a0\n")
    _write(tmp_path / "card1" / "lmem_total_bytes", f"{16 * GIB}\n")
    discovery = "Device Name: Intel(R) Arc(TM) A770 Graphics\n"

    gpus = _gpus({"xpu-smi": discovery}, sysfs=tmp_path)
    by_kind = {gpu["memory_kind"]: gpu for gpu in gpus}

    assert set(by_kind) == {"shared", "dedicated"}
    assert "Arc" in by_kind["dedicated"]["name"]
    assert by_kind["dedicated"]["memory_total_mib"] == 16 * 1024
    assert by_kind["shared"]["memory_total_mib"] is None
    assert "Arc" not in by_kind["shared"]["name"]


def test_windows_dxgi_keeps_arc_dedicated_memory_and_ignores_the_cap():
    rows = json.dumps([
        {
            "Name": "Intel(R) Arc(TM) A770 Graphics",
            "AdapterRAM": 4293918720,
            "PNPDeviceID": r"PCI\VEN_8086&DEV_56A0&SUBSYS_00000000",
            "AdapterCompatibility": "Intel Corporation",
        }
    ])
    dxgi = json.dumps([
        {
            "name": "Intel(R) Arc(TM) A770 Graphics",
            "vendor_id": 0x8086,
            "device_id": 0x56A0,
            "dedicated_bytes": 16 * GIB,
            "shared_bytes": 32 * GIB,
        }
    ])

    def run(command):
        script = command[-1]
        if "freya-dxgi-adapters" in script:
            return dxgi
        if "freya-wmi-adapters" in script:
            return rows
        return None

    gpus = status.read_gpus(run=run, system="Windows", registry_entries=[])

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "intel"
    assert "Arc" in gpu["name"]
    assert gpu["memory_kind"] == "dedicated"
    assert gpu["memory_total_mib"] == 16 * 1024
    assert gpu["memory_total_mib"] != 4293918720 // (1024 * 1024)


def test_windows_arc_without_dxgi_does_not_invent_a_four_gigabyte_card():
    rows = json.dumps({
        "Name": "Intel(R) Arc(TM) A770 Graphics",
        "AdapterRAM": 4293918720,
        "PNPDeviceID": r"PCI\VEN_8086&DEV_56A0&SUBSYS_00000000",
    })
    gpus = _gpus({"powershell.exe": rows}, system="Windows", registry=[])

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "intel"
    assert gpu["memory_kind"] == "unknown"
    assert gpu["memory_total_mib"] is None
    assert gpu["memory_used_mib"] is None
    assert gpu["memory_free_mib"] is None


def test_alder_lake_n95_is_intel_with_shared_memory_not_a_dedicated_total():
    """
    AdapterRAM on an N95-class iGPU is a carved-out or capped figure.
    It must not be reported as dedicated VRAM. DXGI's shared system
    memory must not be reported as VRAM either.
    """
    rows = json.dumps({
        "Name": "Intel(R) UHD Graphics",
        "AdapterRAM": 2147479552,
        "PNPDeviceID": r"PCI\VEN_8086&DEV_46D1&SUBSYS_00000000",
        "AdapterCompatibility": "Intel Corporation",
    })
    dxgi = json.dumps({
        "name": "Intel(R) UHD Graphics",
        "vendor_id": 0x8086,
        "device_id": 0x46D1,
        "dedicated_bytes": 128 * 1024 * 1024,
        "shared_bytes": 8 * GIB,
    })

    def run(command):
        script = command[-1]
        if "freya-dxgi-adapters" in script:
            return dxgi
        if "freya-wmi-adapters" in script:
            return rows
        return None

    gpus = status.read_gpus(
        run=run,
        system="Windows",
        registry_entries=[{
            "DriverDesc": "Intel(R) UHD Graphics",
            "ProviderName": "Intel Corporation",
            "MatchingDeviceId": r"PCI\VEN_8086&DEV_46D1",
            "MemoryBytes": 128 * 1024 * 1024,
        }],
    )

    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu["vendor"] == "intel"
    assert "UHD" in gpu["name"]
    assert gpu["memory_kind"] == "shared"
    assert gpu["memory_total_mib"] is None
    assert gpu["memory_used_mib"] is None
    assert gpu["memory_free_mib"] is None
    assert 2147479552 // (1024 * 1024) != gpu["memory_total_mib"]
    dumped = json.dumps(gpus)
    assert "8589934592" not in dumped
    assert str(8 * GIB) not in dumped


def test_intel_sysfs_without_local_memory_is_shared(tmp_path):
    _write(tmp_path / "card0" / "device" / "vendor", "0x8086\n")
    _write(tmp_path / "card0" / "device" / "device", "0x46d1\n")
    _write(tmp_path / "card0" / "device" / "lmem_total_bytes", "0\n")

    gpus = _gpus(sysfs=tmp_path)

    assert len(gpus) == 1
    assert gpus[0]["vendor"] == "intel"
    assert gpus[0]["memory_kind"] == "shared"
    assert gpus[0]["memory_total_mib"] is None


def test_nvidia_smi_keeps_a_card_and_leaves_na_fields_unknown():
    output = "\n".join([
        "not,a,valid,line",
        "NVIDIA GeForce RTX 4090, [N/A], [Not Supported], 24564, [N/A], [N/A]",
        "NVIDIA GeForce RTX 4060, 40, 5, 8182, 100, 8082",
    ])
    gpus = _gpus({"nvidia-smi": output})

    assert [gpu["name"] for gpu in gpus] == [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA GeForce RTX 4060",
    ]
    first, second = gpus
    assert first["vendor"] == "nvidia"
    assert first["memory_kind"] == "dedicated"
    assert first["temperature_c"] is None
    assert first["utilization_percent"] is None
    assert first["memory_total_mib"] == 24564
    assert first["memory_used_mib"] is None
    assert first["memory_free_mib"] is None
    assert second["temperature_c"] == 40
    assert second["utilization_percent"] == 5
    assert second["memory_used_mib"] == 100
    assert second["memory_free_mib"] == 8082


def test_no_tools_and_no_sysfs_returns_an_empty_list(tmp_path):
    assert _gpus(sysfs=tmp_path) == []
    assert _gpus(system="Darwin") == []
    assert _gpus(system="Windows", registry=[]) == []


def test_a_missing_tool_and_garbage_output_do_not_raise(tmp_path):
    def run(command):
        if command[0] == "rocm-smi":
            raise OSError("not installed")
        if command[0] == "nvidia-smi":
            return "this is not csv {{{"
        raise status.subprocess.SubprocessError("timed out")

    assert status.read_gpus(run=run, system="Linux", sysfs_root=tmp_path) == []


def test_vendor_priority_when_amd_arc_and_nvidia_are_all_present():
    rocm = json.dumps({
        "card0": {
            "Card series": "AMD Radeon RX 9070 XT",
            "VRAM Total Memory (B)": str(16 * GIB),
            "VRAM Total Used Memory (B)": str(GIB),
            "GPU use (%)": "3",
        }
    })
    discovery = "Device Name: Intel(R) Arc(TM) A770 Graphics\n"
    stats = "GPU Memory Total (MiB) | 16384\nGPU Memory Used (MiB) | 256\n"
    nvidia = "NVIDIA GeForce RTX 4090, 45, 1, 24564, 100, 24464\n"

    def run(command):
        if command[0] == "rocm-smi":
            return rocm
        if command[0] == "xpu-smi" and "stats" in command:
            return stats
        if command[0] == "xpu-smi":
            return discovery
        if command[0] == "nvidia-smi":
            return nvidia
        return None

    gpus = status.read_gpus(run=run, system="Linux")

    assert [gpu["vendor"] for gpu in gpus] == ["amd", "intel", "nvidia"]
    assert [gpu["index"] for gpu in gpus] == [0, 1, 2]
    assert gpus[0]["memory_total_mib"] == 16 * 1024
    assert "Arc" in gpus[1]["name"]
    assert gpus[2]["name"].startswith("NVIDIA")


def test_discrete_amd_stays_ahead_of_the_processors_radeon_graphics():
    rocm = json.dumps({
        "card0": {
            "Card series": "AMD Radeon RX 9070 XT",
            "VRAM Total Memory (B)": str(16 * GIB),
        }
    })
    wmi = json.dumps([
        {
            "Name": "AMD Radeon(TM) Graphics",
            "AdapterRAM": 512 * 1024 * 1024,
            "PNPDeviceID": r"PCI\VEN_1002&DEV_13C0",
        },
        {
            "Name": "AMD Radeon RX 9070 XT",
            "AdapterRAM": 4293918720,
            "PNPDeviceID": r"PCI\VEN_1002&DEV_7550",
        },
        {
            "Name": "Microsoft Basic Display Adapter",
            "AdapterRAM": 0,
            "PNPDeviceID": r"PCI\VEN_1414&DEV_008C",
        },
    ])
    gpus = status.read_gpus(
        run=_run({"rocm-smi": rocm, "powershell.exe": wmi}),
        system="Windows",
        registry_entries=[{
            "DriverDesc": "AMD Radeon RX 9070 XT",
            "ProviderName": "Advanced Micro Devices, Inc.",
            "MatchingDeviceId": r"PCI\VEN_1002&DEV_7550",
            "MemoryBytes": 16 * GIB,
        }],
    )

    assert [gpu["name"] for gpu in gpus] == [
        "AMD Radeon RX 9070 XT",
        "AMD Radeon(TM) Graphics",
    ]
    assert gpus[0]["memory_kind"] == "dedicated"
    assert gpus[0]["memory_total_mib"] == 16 * 1024
    assert gpus[1]["vendor"] == "amd"
    assert gpus[1]["memory_kind"] == "shared"
    assert gpus[1]["memory_total_mib"] is None


def test_preferred_card_is_the_first_dedicated_total_in_vendor_order():
    gpus = [
        {
            "vendor": "amd",
            "name": "AMD Radeon(TM) Graphics",
            "memory_kind": "shared",
            "memory_total_mib": None,
            "memory_free_mib": None,
        },
        {
            "vendor": "amd",
            "name": "AMD Radeon RX 9070 XT",
            "memory_kind": "dedicated",
            "memory_total_mib": 16384,
            "memory_free_mib": 14000,
        },
        {
            "vendor": "nvidia",
            "name": "NVIDIA GeForce RTX 4090",
            "memory_kind": "dedicated",
            "memory_total_mib": 24564,
            "memory_free_mib": 1,
        },
    ]

    assert status.preferred_gpu(gpus)["name"] == "AMD Radeon RX 9070 XT"
    assert status.preferred_free_mib(gpus) == 14000

    unknown_free = [dict(gpus[1], memory_free_mib=None), gpus[2]]
    assert status.preferred_free_mib(unknown_free) is None


def test_only_an_integrated_adapter_is_still_named():
    gpus = [{
        "vendor": "intel",
        "name": "Intel(R) UHD Graphics",
        "memory_kind": "shared",
        "memory_total_mib": None,
        "memory_free_mib": None,
    }]

    assert status.preferred_gpu(gpus)["vendor"] == "intel"
    assert status.preferred_free_mib(gpus) is None


def test_this_machine_uses_the_amd_total_when_nvidia_is_also_present(monkeypatch):
    monkeypatch.setattr(
        "freya.tools.system_tools.get_gpu_info",
        lambda: [
            {
                "vendor": "amd",
                "name": "AMD Radeon RX 9070 XT",
                "memory_kind": "dedicated",
                "memory_total_mib": 16384,
            },
            {
                "vendor": "nvidia",
                "name": "NVIDIA GeForce RTX 4090",
                "memory_kind": "dedicated",
                "memory_total_mib": 24564,
            },
        ],
    )
    monkeypatch.setattr(
        "freya.tools.system_tools.get_memory_info",
        lambda: {"total_gb": 32.0},
    )

    machine = model_fit.this_machine()

    assert machine.gpu_name == "AMD Radeon RX 9070 XT"
    assert machine.vram_total_gb == 16
    assert machine.ram_total_gb == 32


def test_this_machine_treats_shared_memory_as_no_dedicated_total(monkeypatch):
    monkeypatch.setattr(
        "freya.tools.system_tools.get_gpu_info",
        lambda: [{
            "vendor": "intel",
            "name": "Intel(R) UHD Graphics",
            "memory_kind": "shared",
            "memory_total_mib": None,
        }],
    )
    monkeypatch.setattr(
        "freya.tools.system_tools.get_memory_info",
        lambda: {"total_gb": 16.0},
    )

    machine = model_fit.this_machine()

    assert machine.gpu_name == "Intel(R) UHD Graphics"
    assert machine.vram_total_gb == 0


def test_speech_free_memory_uses_the_same_preferred_card(monkeypatch):
    monkeypatch.setattr(
        status,
        "read_gpus",
        lambda *args, **kwargs: [
            {
                "vendor": "amd",
                "name": "AMD Radeon RX 9070 XT",
                "memory_kind": "dedicated",
                "memory_total_mib": 16384,
                "memory_free_mib": 12000,
            },
            {
                "vendor": "nvidia",
                "name": "NVIDIA GeForce RTX 4090",
                "memory_kind": "dedicated",
                "memory_total_mib": 24564,
                "memory_free_mib": 1,
            },
        ],
    )

    assert free_gpu_megabytes() == 12000


def test_get_gpu_info_returns_the_live_list(monkeypatch):
    sample = [{"vendor": "amd", "name": "AMD Radeon RX 9070 XT", "index": 0}]
    monkeypatch.setattr(status, "read_gpus", lambda: sample)

    assert get_gpu_info() == sample


@pytest.mark.parametrize(
    "output",
    ["", "   ", "{", "[]", "null", "NVIDIA only half a line"],
)
def test_empty_and_broken_tool_output_adds_no_card(output):
    assert _gpus({
        "rocm-smi": output,
        "amd-smi": output,
        "xpu-smi": output,
        "nvidia-smi": output,
        "powershell.exe": output,
    }) == []
