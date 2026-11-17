import ast
import json
import subprocess
from pathlib import Path

import pytest

from freya.federation import network
from freya.federation import profile as node_profile
from freya.federation import roles as node_roles
from freya.federation.__main__ import main as federation_main
from freya.federation.registry import (
    DAY_SECONDS,
    MAX_OPEN_OFFERS,
    OFFER_LIFETIME_SECONDS,
    OWNER_PRINCIPAL,
    NodeRegistry,
    OwnerApproval,
    RegistryError,
)

GIB = 1024 ** 3
OWNER = OwnerApproval(OWNER_PRINCIPAL, "typed")
FINGERPRINT = "ab" * 32


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def make_profile(
    ram_gb=16, gpu_gb=None, architecture="AMD64", vendor="nvidia",
    name=None, runtimes=None, gpus=None,
):
    if gpus is None:
        gpus = []
        if gpu_gb:
            gpu = {
                "vendor": vendor,
                "name": name or "NVIDIA GeForce GTX 1070",
                "memory_bytes": int(gpu_gb * GIB),
            }
            if runtimes is not None:
                gpu["runtimes"] = list(runtimes)
            gpus.append(gpu)
    return {
        "profile_version": 1,
        "os": {"system": "Windows", "release": "10", "architecture": architecture},
        "cpu": {"model": "Intel Core i7", "logical_cores": 8},
        "ram_bytes": int(ram_gb * GIB),
        "gpus": gpus,
        "disk_free_bytes": 100 * GIB,
    }


def make_offer(code="12345678", **profile_args):
    return {
        "kind": "freya-node-offer",
        "name": "old ThinkPad",
        "join_code": code,
        "profile": make_profile(**profile_args),
    }


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def registry(tmp_path, clock):
    return NodeRegistry(tmp_path / "federation_nodes.json", clock=clock)


# The profile program


def test_nvidia_smi_lines_are_read_and_bad_ones_skipped():
    output = (
        "NVIDIA GeForce RTX 4090, 24564\n"
        "garbage line\n"
        "NVIDIA GeForce GTX 1060, not-a-number\n"
        ", 4096\n"
        "Quadro\x1b[31m P2000, 5120\n"
    )
    gpus = node_profile.parse_nvidia_smi(output)
    assert [gpu["name"] for gpu in gpus] == [
        "NVIDIA GeForce RTX 4090", "Quadro[31m P2000",
    ]
    assert gpus[0]["memory_bytes"] == 24564 * 1024 * 1024
    assert node_profile.parse_nvidia_smi(None) == []


def test_meminfo_and_cpuinfo_are_read():
    assert node_profile.parse_meminfo("MemTotal:  16314376 kB\n") == 16314376 * 1024
    assert node_profile.parse_meminfo("nothing") is None
    assert node_profile.parse_cpuinfo_model(
        "processor : 0\nmodel name\t: Intel(R) Core(TM) i5-8250U\n"
    ) == "Intel(R) Core(TM) i5-8250U"


def _quiet_sources(root):
    """
    Readers that cannot see this machine. Tests pass these so a real
    card, ICD or `/dev/kfd` cannot leak into the result.
    """
    return {
        "sysfs_root": str(root),
        "vulkan_dirs": [],
        "rocm_path": "",
        "registry_cards": [],
    }


def test_collect_profile_never_runs_real_commands_in_tests_and_names_nothing_unique(tmp_path):
    asked = []

    def run(command):
        asked.append(command)
        return "NVIDIA GeForce GTX 1070, 8192\n"

    profile = node_profile.collect_profile(
        run=run, now=5, system="Linux", **_quiet_sources(tmp_path)
    )

    assert asked == node_profile.gpu_queries("Linux")
    assert asked[-1] == node_profile.NVIDIA_QUERY
    assert [command[0] for command in asked[:2]] == ["rocm-smi", "amd-smi"]
    assert profile["profile_version"] == 2
    assert profile["gpus"][0]["memory_bytes"] == 8192 * 1024 * 1024
    assert profile["gpus"][0]["vendor"] == "nvidia"
    text = json.dumps(profile).lower()
    import platform
    host = platform.node().lower()
    if host:
        assert host not in text


def test_queries_are_given_only_what_they_need_to_run():
    environment = node_profile._query_environment({
        "PATH": "/usr/bin",
        "SystemRoot": r"C:\Windows",
        "HF_TOKEN": "fake-hub-token",
        "FREYA_JELLYFIN_TOKEN": "fake-jellyfin-token",
        "HOME": "/home/someone",
    })
    assert environment == {"PATH": "/usr/bin", "SystemRoot": r"C:\Windows"}


def test_the_profile_program_imports_nothing_from_freya_and_opens_no_network():
    source = Path(node_profile.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "freya" not in imported
    assert not imported & {"socket", "urllib", "http", "requests", "ssl", "websockets"}


def test_saving_an_offer_writes_a_code_and_the_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(node_profile, "_run", lambda command: None)
    monkeypatch.setattr(node_profile, "read_sysfs_cards", lambda root=None, system=None: [])
    monkeypatch.setattr(node_profile, "read_windows_registry_cards", lambda: [])
    monkeypatch.setattr(node_profile, "vulkan_vendors_present", lambda dirs=None: set())
    monkeypatch.setattr(node_profile, "rocm_device_present", lambda path=None: False)
    target = tmp_path / "offer.json"

    assert node_profile.main(["--save", str(target), "--name", "spare\x07 laptop"]) == 0

    offer = json.loads(target.read_text(encoding="utf-8"))
    assert offer["kind"] == "freya-node-offer"
    assert offer["name"] == "spare laptop"
    assert len(offer["join_code"]) == 8 and offer["join_code"].isdigit()
    printed = capsys.readouterr().out
    assert offer["join_code"][:4] in printed
    assert "Nothing has been sent anywhere" in printed


def _write_card(root, name, vendor, device, memory=None, product=None, serial=None):
    device_dir = root / name / "device"
    device_dir.mkdir(parents=True)
    (device_dir / "vendor").write_text(vendor + "\n", encoding="utf-8")
    (device_dir / "device").write_text(device + "\n", encoding="utf-8")
    if memory is not None:
        (device_dir / "mem_info_vram_total").write_text(
            str(memory) + "\n", encoding="utf-8"
        )
    if product is not None:
        (device_dir / "product_name").write_text(product + "\n", encoding="utf-8")
    if serial is not None:
        # Present on real cards. The profiler must not open it.
        (device_dir / "serial_number").write_text(serial + "\n", encoding="utf-8")


def _collect(run, tmp_path, system="Linux", **overrides):
    sources = _quiet_sources(tmp_path)
    sources.update(overrides)
    return node_profile.collect_profile(
        run=run, now=5, system=system, **sources
    )


def test_an_amd_card_is_read_from_rocm_smi_and_the_windows_registry(tmp_path):
    rocm = json.dumps({
        "card0": {
            "Card series": "AMD Radeon RX 9070 XT",
            "Card model": "0x7550",
            "Card vendor": "Advanced Micro Devices, Inc. [AMD/ATI]",
            "VRAM Total Memory (B)": str(16 * GIB),
            "Serial Number": "ZZ-SERIAL-9900",
        }
    })

    def run(command):
        if command[0] == "rocm-smi":
            return rocm
        return None

    profile = _collect(run, tmp_path)
    assert [gpu["name"] for gpu in profile["gpus"]] == ["AMD Radeon RX 9070 XT"]
    gpu = profile["gpus"][0]
    assert gpu["vendor"] == "amd"
    assert gpu["memory_bytes"] == 16 * GIB
    assert gpu["runtimes"] == ["rocm"]
    assert "ZZ-SERIAL-9900" not in json.dumps(profile)
    assert "no NVIDIA" not in "\n".join(node_profile.describe(profile))

    windows = _collect(
        lambda command: None,
        tmp_path,
        system="Windows",
        registry_cards=node_profile.cards_from_registry_properties([
            {
                "DriverDesc": "AMD Radeon RX 9070 XT",
                "ProviderName": "Advanced Micro Devices, Inc.",
                "MatchingDeviceId": r"PCI\VEN_1002&DEV_7550&SUBSYS_01234567",
                "MemoryBytes": 16 * GIB,
            },
            {
                "DriverDesc": "AMD Radeon(TM) Graphics",
                "MatchingDeviceId": r"PCI\VEN_1002&DEV_13C0",
                "MemoryBytes": 512 * 1024 * 1024,
            },
            {
                "DriverDesc": "Microsoft Basic Display Adapter",
                "ProviderName": "Microsoft",
                "MemoryBytes": 0,
            },
        ]),
    )
    assert [gpu["name"] for gpu in windows["gpus"]] == ["AMD Radeon RX 9070 XT"]
    assert windows["gpus"][0]["memory_bytes"] == 16 * GIB


def test_the_windows_display_class_key_matches_the_real_path():
    assert node_profile.VIDEO_CLASS_KEY == (
        "SYSTEM\\ControlSet001\\Control\\Class\\"
        "{4d36e968-e325-11ce-bfc1-08002be10318}"
    )
    # One separator before the brace. A doubled backslash would miss the key.
    assert node_profile.VIDEO_CLASS_KEY.endswith(
        "\\{4d36e968-e325-11ce-bfc1-08002be10318}"
    )
    assert "\\\\{" not in node_profile.VIDEO_CLASS_KEY


def test_wmi_does_not_trust_adapter_ram_at_the_four_gigabyte_cap():
    capped = json.dumps({
        "Name": "AMD Radeon RX 9070 XT",
        "AdapterRAM": 4293918720,
        "PNPDeviceID": r"PCI\VEN_1002&DEV_7550&SUBSYS_00000000",
        "AdapterCompatibility": "Advanced Micro Devices, Inc.",
        "MACAddress": "02:00:00:00:00:01",
        "SerialNumber": "ZZ-SERIAL-9900",
    })
    small = json.dumps([
        {
            "Name": "AMD Radeon RX 6400",
            "AdapterRAM": 4 * GIB // 2,
            "PNPDeviceID": r"PCI\VEN_1002&DEV_743F",
        },
        {
            "Name": "Microsoft Basic Display Adapter",
            "AdapterRAM": 0,
            "PNPDeviceID": r"PCI\VEN_1414&DEV_008C",
        },
    ])
    capped_cards = node_profile.parse_windows_controllers(capped)
    assert capped_cards[0]["name"] == "AMD Radeon RX 9070 XT"
    assert "memory_bytes" not in capped_cards[0]
    assert "02:00:00:00:00:01" not in json.dumps(capped_cards)
    assert "ZZ-SERIAL-9900" not in json.dumps(capped_cards)
    small_cards = node_profile.parse_windows_controllers(small)
    assert [card["name"] for card in small_cards] == ["AMD Radeon RX 6400"]
    assert small_cards[0]["memory_bytes"] == 2 * GIB


def test_a_mixed_amd_machine_reports_both_cards_and_not_an_assumed_mi50_size(tmp_path):
    # The MI50's size is whatever the tool printed, not 32 GiB filled in
    # from the name. gfx906 is kept as the tool reported it when that is
    # all the tool gave.
    mi50_bytes = 31 * GIB + 512 * 1024 * 1024
    amd_smi = json.dumps([
        {
            "gpu": 0,
            "asic": {
                "market_name": "AMD Radeon RX 7800 XT",
                "vendor_id": "0x1002",
                "device_id": "0x747e",
                "asic_serial": "ZZ-SERIAL-9900",
                "target_graphics_version": "gfx1101",
            },
            "vram": {"type": "GDDR6", "size": 16368},
        },
        {
            "gpu": 1,
            "asic": {
                "market_name": "AMD Instinct MI50",
                "vendor_id": "0x1002",
                "device_id": "0x66a1",
                "asic_serial": "ZZ-SERIAL-9901",
                "target_graphics_version": "gfx906",
            },
            "vram": {"size": {"value": mi50_bytes, "unit": "B"}},
        },
    ])
    _write_card(
        tmp_path, "card0", "0x1002", "0x747e", 8 * GIB,
        serial="ZZ-SERIAL-FROM-SYSFS",
    )
    _write_card(
        tmp_path, "card1", "0x1002", "0x66a1", mi50_bytes // 2,
        serial="ZZ-SERIAL-FROM-SYSFS",
    )
    _write_card(tmp_path, "card2", "0x1002", "0x13c0", 512 * 1024 * 1024)
    (tmp_path / "card0" / "device" / "serial_number").write_text(
        "02:00:00:00:00:01\n", encoding="utf-8"
    )

    def run(command):
        if command[0] == "amd-smi":
            return amd_smi
        return None

    profile = _collect(run, tmp_path, sysfs_root=str(tmp_path))
    assert [gpu["vendor"] for gpu in profile["gpus"]] == ["amd", "amd"]
    assert [gpu["name"] for gpu in profile["gpus"]] == [
        "AMD Radeon RX 7800 XT", "AMD Instinct MI50",
    ]
    assert profile["gpus"][0]["memory_bytes"] == 16368 * 1024 * 1024
    assert profile["gpus"][1]["memory_bytes"] == mi50_bytes
    assert profile["gpus"][1]["memory_bytes"] != 32 * GIB
    text = json.dumps(profile)
    assert "ZZ-SERIAL" not in text
    assert "02:00:00:00:00:01" not in text
    assert "asic_serial" not in text
    assert profile["gpus"][0]["runtimes"] == ["rocm"]


def test_intel_arc_is_kept_and_an_integrated_adapter_is_not(tmp_path):
    _write_card(tmp_path, "card0", "0x8086", "0x4680", 128 * 1024 * 1024)
    _write_card(tmp_path, "card1", "0x8086", "0x56a0", 16 * GIB)
    discovery = (
        "Device Name: Intel(R) UHD Graphics 770\n"
        "Device Name: Intel(R) Arc(TM) A770 Graphics\n"
    )
    sycl = (
        "[opencl:gpu][opencl:0] Intel(R) OpenCL HD Graphics, "
        "Intel(R) UHD Graphics 770 12.2.0 [1.2]\n"
        "[level_zero:gpu][level_zero:0] Intel(R) Level-Zero, "
        "Intel(R) Arc(TM) A770 Graphics 1.3 [1.3]\n"
    )

    def run(command):
        if command[0] == "xpu-smi":
            return discovery
        if command[0] == "sycl-ls":
            return sycl
        return None

    profile = _collect(run, tmp_path, sysfs_root=str(tmp_path))
    assert [gpu["name"] for gpu in profile["gpus"]] == [
        "Intel(R) Arc(TM) A770 Graphics",
    ]
    assert profile["gpus"][0]["vendor"] == "intel"
    assert profile["gpus"][0]["memory_bytes"] == 16 * GIB
    assert profile["gpus"][0]["runtimes"] == ["sycl"]


def test_nvidia_only_still_reads_nvidia_smi(tmp_path):
    def run(command):
        if command[0] == "nvidia-smi":
            return "NVIDIA GeForce RTX 4070, 12282\n"
        return None

    profile = _collect(run, tmp_path)
    assert len(profile["gpus"]) == 1
    assert profile["gpus"][0]["vendor"] == "nvidia"
    assert profile["gpus"][0]["runtimes"] == ["cuda"]
    assert "NVIDIA" in profile["gpus"][0]["name"]


def test_mixed_vendors_are_ordered_amd_then_arc_then_nvidia(tmp_path):
    _write_card(tmp_path, "card0", "0x10de", "0x2782", 12 * GIB)
    _write_card(tmp_path, "card1", "0x8086", "0x56a0", 16 * GIB)
    _write_card(tmp_path, "card2", "0x1002", "0x7550", 16 * GIB)
    icd = tmp_path / "icd"
    icd.mkdir()
    (icd / "radeon_icd.json").write_text(
        '{"ICD": {"library_path": "libvulkan_radeon.so"}}', encoding="utf-8"
    )
    (icd / "intel_icd.json").write_text(
        '{"ICD": {"library_path": "libvulkan_intel.so"}}', encoding="utf-8"
    )

    def run(command):
        if command[0] == "rocm-smi":
            return json.dumps({
                "card0": {
                    "Card series": "AMD Radeon RX 9070 XT",
                    "Card model": "0x7550",
                    "VRAM Total Memory (B)": str(16 * GIB),
                }
            })
        if command[0] == "xpu-smi":
            return "Device Name: Intel(R) Arc(TM) B580 Graphics\n"
        if command[0] == "nvidia-smi":
            return "NVIDIA GeForce RTX 4070, 12282\n"
        return None

    profile = _collect(
        run, tmp_path, sysfs_root=str(tmp_path), vulkan_dirs=[str(icd)],
        rocm_path=str(tmp_path / "no-kfd"),
    )
    assert [gpu["vendor"] for gpu in profile["gpus"]] == ["amd", "intel", "nvidia"]
    assert profile["gpus"][0]["name"] == "AMD Radeon RX 9070 XT"
    assert "Arc" in profile["gpus"][1]["name"]
    assert "rocm" in profile["gpus"][0]["runtimes"]
    assert "vulkan" in profile["gpus"][0]["runtimes"]
    assert "vulkan" in profile["gpus"][1]["runtimes"]
    assert "sycl" in profile["gpus"][1]["runtimes"]
    assert profile["gpus"][2]["runtimes"] == ["cuda"]
    assert "vulkan" not in profile["gpus"][2]["runtimes"]


def test_missing_tools_leave_the_profile_usable(tmp_path):
    profile = _collect(lambda command: None, tmp_path)
    assert profile["gpus"] == []
    graphics = [
        line for line in node_profile.describe(profile)
        if line.startswith("Graphics:")
    ]
    assert graphics == ["Graphics: no graphics card found"]


def test_a_hung_tool_is_skipped_and_the_query_has_a_short_timeout(tmp_path, monkeypatch):
    def run(command):
        if command[0] in ("rocm-smi", "amd-smi", "xpu-smi"):
            raise subprocess.TimeoutExpired(command, 5)
        if command[0] == "nvidia-smi":
            return "NVIDIA GeForce RTX 4070, 12282\n"
        return None

    profile = _collect(run, tmp_path)
    assert [gpu["vendor"] for gpu in profile["gpus"]] == ["nvidia"]

    seen = {}

    def fake_run(*args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout") or 0)

    monkeypatch.setattr(node_profile.subprocess, "run", fake_run)
    assert node_profile._run(["rocm-smi"]) is None
    assert seen["timeout"] == node_profile.QUERY_TIMEOUT_SECONDS
    assert seen["timeout"] <= 5

    empty = _collect(
        lambda command: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(command, 5)
        ),
        tmp_path,
    )
    assert empty["gpus"] == []


def test_a_version_one_offer_still_loads_and_nvidia_still_suggests_a_model(registry):
    offer = make_offer(gpu_gb=8)
    assert offer["profile"]["profile_version"] == 1
    assert "runtimes" not in offer["profile"]["gpus"][0]
    node_id = registry.record_offer(offer)
    node = registry.waiting()[0]
    assert node["node_id"] == node_id
    assert node["profile"]["profile_version"] == 1
    assert node["profile"]["gpus"][0]["vendor"] == "nvidia"
    assert "runtimes" not in node["profile"]["gpus"][0]
    assert "model-worker" in node["suggested_roles"]


def test_the_profile_does_not_keep_a_host_name_serial_or_mac(tmp_path):
    source = Path(node_profile.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            calls.append(node.attr)
    assert "node" not in calls
    assert "getnode" not in calls
    rocm = json.dumps({
        "card0": {
            "Card series": "AMD Radeon RX 9070 XT",
            "VRAM Total Memory (B)": str(16 * GIB),
            "Serial Number": "ZZ-SERIAL-9900",
            "MAC": "02:00:00:00:00:01",
        }
    })
    profile = _collect(
        lambda command: rocm if command[0] == "rocm-smi" else None,
        tmp_path,
    )
    text = json.dumps(profile)
    import platform
    host = platform.node()
    if host:
        assert host not in text
    assert "ZZ-SERIAL-9900" not in text
    assert "02:00:00:00:00:01" not in text


def test_an_amd_card_is_a_model_worker_without_nvidia():
    suggestions = node_roles.suggest_roles(make_profile(
        ram_gb=32, gpu_gb=16, vendor="amd",
        name="AMD Radeon RX 9070 XT", runtimes=["rocm", "vulkan"],
    ))
    by_role = {item.role: item for item in suggestions}
    assert by_role["model-worker"].allowed_now
    assert "24B" in by_role["model-worker"].reason
    assert "AMD" in by_role["model-worker"].reason
    assert "ROCm" in by_role["model-worker"].reason
    assert "Vulkan" in by_role["model-worker"].reason
    assert "NVIDIA" not in by_role["model-worker"].reason
    assert "No usable" not in by_role["model-worker"].reason
    assert "cpu-model-worker" not in by_role


def test_intel_arc_is_suggested_the_same_way():
    suggestions = node_roles.suggest_roles(make_profile(
        ram_gb=32, gpu_gb=12, vendor="intel",
        name="Intel(R) Arc(TM) A770 Graphics", runtimes=["sycl"],
    ))
    by_role = {item.role: item for item in suggestions}
    assert "17B" in by_role["model-worker"].reason
    assert "Intel Arc" in by_role["model-worker"].reason
    assert "Vulkan" in by_role["model-worker"].reason
    assert "oneAPI" in by_role["model-worker"].reason
    assert "cpu-model-worker" not in by_role


def test_two_amd_cards_use_the_larger_one_and_mention_both():
    profile = make_profile(ram_gb=64, gpus=[
        {
            "vendor": "amd",
            "name": "AMD Radeon RX 7800 XT",
            "memory_bytes": 16 * GIB,
            "runtimes": ["rocm"],
        },
        {
            "vendor": "amd",
            "name": "AMD Instinct MI50",
            "memory_bytes": 31 * GIB,
            "runtimes": ["rocm"],
        },
    ])
    reason = {
        item.role: item.reason for item in node_roles.suggest_roles(profile)
    }["model-worker"]
    assert "31 GB" in reason
    assert "2 cards" in reason
    assert "47 GB" in reason
    assert "ROCm" in reason


def test_a_card_with_no_measured_memory_is_not_called_absent():
    profile = make_profile(ram_gb=32, gpus=[{
        "vendor": "intel",
        "name": "Intel(R) Arc(TM) B580 Graphics",
        "runtimes": ["vulkan"],
    }])
    roles = {item.role: item.reason for item in node_roles.suggest_roles(profile)}
    assert "model-worker" not in roles
    assert "Arc" in roles["cpu-model-worker"]
    assert "not measured" in roles["cpu-model-worker"]
    assert "No usable graphics card" not in roles["cpu-model-worker"]


def test_runtimes_on_an_offer_are_kept_and_anything_else_is_not(registry):
    offer = make_offer()
    offer["profile"]["gpus"] = [{
        "vendor": "amd",
        "name": "AMD Radeon RX 9070 XT",
        "memory_bytes": 16 * GIB,
        "runtimes": ["vulkan", "rocm", "cuda", "not-a-runtime", "x" * 80],
        "serial": "ZZ-SERIAL-9900",
    }]
    registry.record_offer(offer)
    gpu = registry.waiting()[0]["profile"]["gpus"][0]
    assert gpu["runtimes"] == ["rocm", "vulkan", "cuda"]
    assert "serial" not in gpu
    assert "ZZ-SERIAL" not in json.dumps(gpu)


# Suggestions


def test_a_gaming_laptop_is_suggested_as_a_model_worker():
    suggestions = node_roles.suggest_roles(make_profile(ram_gb=16, gpu_gb=8))
    by_role = {s.role: s for s in suggestions}
    assert by_role["model-worker"].allowed_now
    assert "10B" in by_role["model-worker"].reason
    assert not by_role["split-inference"].allowed_now
    assert by_role["sandbox-host"].allowed_now


def test_a_laptop_without_a_card_gets_cpu_models_and_hosting_ideas():
    roles = [s.role for s in node_roles.suggest_roles(make_profile(ram_gb=16))]
    assert "model-worker" not in roles
    assert {"cpu-model-worker", "sandbox-host", "home-assistant-host"} <= set(roles)


def test_a_small_old_laptop_is_only_good_for_home_assistant():
    roles = [s.role for s in node_roles.suggest_roles(make_profile(ram_gb=3))]
    assert roles == ["home-assistant-host"]


def test_an_arm_machine_is_not_offered_as_a_sandbox():
    roles = [
        s.role for s in node_roles.suggest_roles(
            make_profile(ram_gb=16, architecture="aarch64")
        )
    ]
    assert "sandbox-host" not in roles


def test_inference_and_sandbox_roles_may_be_enrolled_in_this_version():
    assert node_roles.ROLES_ALLOWED_NOW == {
        "model-worker", "cpu-model-worker", "sandbox-host",
    }
    for name, role in node_roles.ROLES.items():
        assert bool(role.why_not_yet) == (name not in node_roles.ROLES_ALLOWED_NOW)


# Offers and enrollment


def test_an_offer_is_only_a_suggestion(registry):
    node_id = registry.record_offer(make_offer(gpu_gb=8))

    waiting = registry.waiting()
    assert [node["node_id"] for node in waiting] == [node_id]
    assert "model-worker" in waiting[0]["suggested_roles"]
    assert "join_code" not in waiting[0]
    assert registry.may_assign(node_id, "model-worker").allowed is False


def test_bad_offers_are_refused_without_repeating_them(registry):
    with pytest.raises(RegistryError, match="not a Freya machine offer"):
        registry.record_offer({"kind": "something"})
    with pytest.raises(RegistryError, match="join code"):
        registry.record_offer(make_offer(code="12ab"))
    with pytest.raises(RegistryError, match="too large"):
        offer = make_offer()
        offer["name"] = "x" * 20_000
        registry.record_offer(offer)
    with pytest.raises(RegistryError, match="key"):
        registry.record_offer(make_offer(), fingerprint="not-hex")


def test_offer_contents_are_cleaned(registry):
    offer = make_offer()
    offer["name"] = "evil\x1b[2J name" + "y" * 200
    offer["profile"]["gpus"] = [
        {"vendor": "nvidia", "name": "card", "memory_bytes": "lots"},
        {"vendor": "nvidia", "name": "real", "memory_bytes": 4 * GIB},
    ]
    offer["profile"]["extra"] = "ignored"
    node_id = registry.record_offer(offer)
    node = registry.waiting()[0]
    assert node["node_id"] == node_id
    assert "\x1b" not in node["name"] and len(node["name"]) <= 64
    assert [gpu["name"] for gpu in node["profile"]["gpus"]] == ["real"]
    assert "extra" not in node["profile"]


def test_the_list_of_open_offers_is_bounded(registry):
    for _ in range(MAX_OPEN_OFFERS):
        registry.record_offer(make_offer())
    with pytest.raises(RegistryError, match="too many"):
        registry.record_offer(make_offer())


def test_the_owner_enrolls_with_the_code_for_an_allowed_role(registry, clock):
    node_id = registry.record_offer(make_offer(gpu_gb=8), fingerprint=FINGERPRINT)

    registry.approve(node_id, "1234 5678", OWNER, ["model-worker"], valid_days=10)

    assert registry.may_assign(node_id, "model-worker").allowed
    assert not registry.may_assign(node_id, "cpu-model-worker").allowed
    clock.now += 10 * DAY_SECONDS
    decision = registry.may_assign(node_id, "model-worker")
    assert not decision.allowed and "expired" in decision.reason
    assert registry.nodes()[0]["expired"] is True


def test_voice_and_other_principals_cannot_enroll(registry):
    node_id = registry.record_offer(make_offer())
    with pytest.raises(RegistryError, match="not by voice"):
        registry.approve(
            node_id, "12345678", OwnerApproval(OWNER_PRINCIPAL, "voice"),
            ["model-worker"],
        )
    with pytest.raises(RegistryError, match="Only the owner"):
        registry.approve(
            node_id, "12345678", OwnerApproval("freya-b", "typed"),
            ["model-worker"],
        )
    with pytest.raises(RegistryError, match="Only the owner"):
        registry.approve(node_id, "12345678", None, ["model-worker"])
    assert registry.waiting()[0]["state"] == "offered"


def test_roles_needing_a_decision_cannot_be_enrolled(registry):
    node_id = registry.record_offer(make_offer())
    with pytest.raises(RegistryError, match="larger-model"):
        registry.approve(node_id, "12345678", OWNER, ["split-inference"])
    with pytest.raises(RegistryError, match="smart-home"):
        registry.approve(node_id, "12345678", OWNER, ["home-assistant-host"])
    with pytest.raises(RegistryError, match="does not exist"):
        registry.approve(node_id, "12345678", OWNER, ["root"])
    with pytest.raises(RegistryError, match="at least one"):
        registry.approve(node_id, "12345678", OWNER, [])


def test_enrollment_length_is_bounded(registry):
    node_id = registry.record_offer(make_offer())
    for days in (0, 91, "30", True):
        with pytest.raises(RegistryError, match="1 to 90"):
            registry.approve(node_id, "12345678", OWNER, ["model-worker"], days)


def test_three_wrong_codes_withdraw_the_offer(registry):
    node_id = registry.record_offer(make_offer())
    for _ in range(2):
        with pytest.raises(RegistryError, match="does not match"):
            registry.approve(node_id, "00000000", OWNER, ["model-worker"])
    with pytest.raises(RegistryError, match="withdrawn"):
        registry.approve(node_id, "00000000", OWNER, ["model-worker"])
    with pytest.raises(RegistryError, match="No machine is waiting"):
        registry.approve(node_id, "12345678", OWNER, ["model-worker"])


def test_an_old_offer_lapses(registry, clock):
    node_id = registry.record_offer(make_offer())
    clock.now += OFFER_LIFETIME_SECONDS + 1
    with pytest.raises(RegistryError, match="No machine is waiting"):
        registry.approve(node_id, "12345678", OWNER, ["model-worker"])


def test_a_machine_from_a_file_is_never_given_work_until_bound_to_a_key(registry):
    node_id = registry.record_offer(make_offer(gpu_gb=8))
    registry.approve(node_id, "12345678", OWNER, ["model-worker"])
    decision = registry.may_assign(node_id, "model-worker")
    assert not decision.allowed and "key" in decision.reason


def test_revoking_is_immediate_and_needs_no_approval(registry):
    node_id = registry.record_offer(make_offer(), fingerprint=FINGERPRINT)
    registry.approve(node_id, "12345678", OWNER, ["model-worker"])

    registry.revoke(node_id)

    assert not registry.may_assign(node_id, "model-worker").allowed
    with pytest.raises(RegistryError):
        registry.renew(node_id, OWNER)
    with pytest.raises(RegistryError):
        registry.revoke(node_id)


def test_renewing_needs_the_owner_typing(registry, clock):
    node_id = registry.record_offer(make_offer(), fingerprint=FINGERPRINT)
    registry.approve(node_id, "12345678", OWNER, ["model-worker"], valid_days=1)
    clock.now += 2 * DAY_SECONDS
    with pytest.raises(RegistryError, match="not by voice"):
        registry.renew(node_id, OwnerApproval(OWNER_PRINCIPAL, "voice"))
    registry.renew(node_id, OWNER, valid_days=5)
    assert registry.may_assign(node_id, "model-worker").allowed


def test_unknown_nodes_get_nothing(registry):
    assert not registry.may_assign("node-nope", "model-worker").allowed


def test_decisions_survive_a_restart_and_codes_are_not_kept_after_use(tmp_path, clock):
    path = tmp_path / "federation_nodes.json"
    first = NodeRegistry(path, clock=clock)
    node_id = first.record_offer(make_offer(), fingerprint=FINGERPRINT)
    first.approve(node_id, "12345678", OWNER, ["model-worker"])

    second = NodeRegistry(path, clock=clock)
    assert second.may_assign(node_id, "model-worker").allowed
    assert "12345678" not in path.read_text(encoding="utf-8")
    events = [entry["event"] for entry in second.history()]
    assert events == ["offered", "enrolled"]


def test_an_unreadable_file_refuses_everything_and_is_not_overwritten(tmp_path, clock):
    path = tmp_path / "federation_nodes.json"
    path.write_text("{not json", encoding="utf-8")

    registry = NodeRegistry(path, clock=clock)

    assert registry.unreadable
    assert not registry.may_assign("node-x", "model-worker").allowed
    with pytest.raises(RegistryError, match="could not be read"):
        registry.record_offer(make_offer())
    with pytest.raises(RegistryError, match="could not be read"):
        registry.revoke("node-x")
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_file_with_an_unknown_state_is_unreadable(tmp_path, clock):
    path = tmp_path / "federation_nodes.json"
    path.write_text(
        json.dumps({"nodes": {"node-a": {"state": "trusted"}}}), encoding="utf-8"
    )
    assert NodeRegistry(path, clock=clock).unreadable


# The owner's commands


def test_the_commands_take_an_offer_file_to_enrollment(tmp_path, clock):
    registry = NodeRegistry(tmp_path / "nodes.json", clock=clock)
    offer_file = tmp_path / "offer.json"
    offer_file.write_text(json.dumps(make_offer(gpu_gb=8)), encoding="utf-8")
    lines = []

    assert federation_main(["add", str(offer_file)], registry=registry, out=lines.append) == 0
    node_id = registry.waiting()[0]["node_id"]
    assert any("Nothing was enrolled" in line for line in lines)
    assert any("model-worker:" in line for line in lines)

    lines.clear()
    assert federation_main(
        ["approve", node_id], registry=registry,
        read_code=lambda prompt: "1234-5678", out=lines.append,
    ) == 0
    assert any("No work can reach it" in line for line in lines)
    assert registry.nodes()[0]["approved_via"] == "typed"

    lines.clear()
    assert federation_main(["nodes"], registry=registry, out=lines.append) == 0
    assert any("Not yet bound to a key" in line for line in lines)

    assert federation_main(["revoke", node_id], registry=registry, out=lines.append) == 0
    assert registry.nodes()[0]["state"] == "revoked"


def test_the_commands_report_refusals_without_a_traceback(tmp_path, clock):
    registry = NodeRegistry(tmp_path / "nodes.json", clock=clock)
    bad = tmp_path / "bad.json"
    bad.write_text("nope", encoding="utf-8")
    lines = []
    assert federation_main(["add", str(bad)], registry=registry, out=lines.append) == 1
    assert federation_main(["revoke", "node-x"], registry=registry, out=lines.append) == 1
    assert federation_main(
        ["add", str(tmp_path / "missing.json")], registry=registry, out=lines.append
    ) == 1


def test_core_does_not_import_federation_yet():
    source = Path(node_profile.__file__).resolve().parents[1]
    for path in source.rglob("*.py"):
        if "federation" in path.parts:
            continue
        assert "freya.federation" not in path.read_text(encoding="utf-8"), path


# Addresses and the tailnet

TAILSCALE_STATUS = json.dumps({
    "Self": {"HostName": "astra", "TailscaleIPs": ["100.64.0.1"]},
    "Peer": {
        "key1": {
            "HostName": "old-thinkpad",
            "DNSName": "old-thinkpad.tail1234.ts.net.",
            "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"],
            "OS": "windows",
            "Online": True,
        },
        "key2": {
            "HostName": "phone\x1b[2J",
            "DNSName": "evil.example.com.",
            "TailscaleIPs": ["8.8.8.8", "100.90.1.2"],
            "OS": "android",
            "Online": False,
        },
        "key3": {"HostName": "no-address", "TailscaleIPs": ["8.8.4.4"]},
    },
})


@pytest.mark.parametrize("host, kind", [
    ("127.0.0.1", network.LOOPBACK),
    ("::1", network.LOOPBACK),
    ("100.101.102.103", network.TAILNET),
    ("[fd7a:115c:a1e0::1]", network.TAILNET),
    ("old-thinkpad.tail1234.ts.net", network.TAILNET),
    ("192.168.1.20", network.HOME),
    ("10.0.0.5", network.HOME),
    ("fe80::1", network.HOME),
    ("8.8.8.8", network.PUBLIC),
    ("laptop.example.com", network.INVALID),
    ("laptop", network.INVALID),
    ("ts.net", network.INVALID),
    ("", network.INVALID),
    ("192.168.1.2\n", network.INVALID),
])
def test_addresses_are_classified(host, kind):
    assert network.classify_address(host) == kind


def test_workers_live_on_the_home_network_or_the_tailnet_only():
    assert network.worker_address_allowed("192.168.1.20")
    assert network.worker_address_allowed("old-thinkpad.tail1234.ts.net")
    assert not network.worker_address_allowed("8.8.8.8")
    assert not network.worker_address_allowed("127.0.0.1")
    assert not network.worker_address_allowed("laptop.example.com")


def test_tailnet_devices_are_read_and_cleaned():
    devices, problem = network.read_tailnet(run=lambda: TAILSCALE_STATUS)

    assert problem == ""
    assert [device["name"] for device in devices] == ["old-thinkpad", "phone[2J"]
    thinkpad, phone = devices
    assert thinkpad["dns_name"] == "old-thinkpad.tail1234.ts.net"
    assert thinkpad["online"] is True
    assert phone["addresses"] == ["100.90.1.2"]
    assert phone["dns_name"] == ""


def test_no_tailscale_or_bad_status_lists_nothing():
    devices, problem = network.read_tailnet(run=lambda: None)
    assert devices == [] and "not installed" in problem
    devices, problem = network.read_tailnet(run=lambda: "not json")
    assert devices == [] and "could not be read" in problem


def test_an_offer_records_where_the_machine_is(registry):
    node_id = registry.record_offer(
        make_offer(), address="old-thinkpad.tail1234.ts.net"
    )
    node = registry.waiting()[0]
    assert node["node_id"] == node_id
    assert node["address"] == "old-thinkpad.tail1234.ts.net"
    assert node["network"] == "tailnet"
    with pytest.raises(RegistryError, match="home network or the tailnet"):
        registry.record_offer(make_offer(), address="8.8.8.8")


def test_being_on_the_tailnet_enrolls_nothing(tmp_path, clock):
    registry = NodeRegistry(tmp_path / "nodes.json", clock=clock)
    lines = []

    assert federation_main(
        ["tailnet"], registry=registry, out=lines.append,
        tailnet=lambda: network.read_tailnet(run=lambda: TAILSCALE_STATUS),
    ) == 0

    assert registry.nodes() == []
    assert any("old-thinkpad" in line and "not offered yet" in line for line in lines)
    assert any("does not make a machine a worker" in line for line in lines)


def test_enrolling_a_sandbox_host_shows_its_conditions(tmp_path, clock):
    registry = NodeRegistry(tmp_path / "nodes.json", clock=clock)
    node_id = registry.record_offer(make_offer(), address="192.168.1.20")
    lines = []

    assert federation_main(
        ["approve", node_id, "--role", "sandbox-host"], registry=registry,
        read_code=lambda prompt: "12345678", out=lines.append,
    ) == 0

    assert registry.nodes()[0]["roles"] == ["sandbox-host"]
    assert any("code-sandbox.md" in line for line in lines)
    assert any("worker channel" in line for line in lines)
