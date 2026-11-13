import platform
import shutil
import subprocess

import psutil

from freya.child_environment import child_environment
from freya.core.capabilities import (
    CapabilityDefinition,
)
from freya.core.providers import (
    ProviderDefinition,
)
from freya.tools.hardware_inventory import (
    get_hardware_inventory,
)
from freya.tools.model_tools import (
    MODEL_TOOL,
)
from freya.tools.sensors import (
    get_sensor_readings,
)


SYSTEM_TOOLS_PROVIDER = "system-tools-local"

SYSTEM_TOOL_CAPABILITIES = [
    "system.info",
    "system.disk",
    "system.gpu",
    "system.cpu",
    "system.memory",
    "system.hardware",
    "system.sensors",
]

CPU_SAMPLE_SECONDS = 0.5


def _bytes_to_gb(value):
    return round(
        value / 1024 / 1024 / 1024,
        2,
    )


def get_system_info():
    """
    Return basic information about Freya's host system.
    """

    return {
        "computer_name": (
            platform.node()
        ),
        "operating_system": (
            platform.system()
        ),
        "os_release": (
            platform.release()
        ),
        "os_version": (
            platform.version()
        ),
        "architecture": (
            platform.machine()
        ),
        "processor": (
            platform.processor()
        ),
        "python_version": (
            platform.python_version()
        ),
    }


def get_disk_info():
    """
    Return usage for every mounted drive.

    Drives that cannot be read, such as an empty card reader or
    optical drive, are skipped. This is read-only.
    """

    drives = []

    for partition in psutil.disk_partitions(
        all=False
    ):
        if not partition.fstype:
            continue

        try:
            usage = shutil.disk_usage(
                partition.mountpoint
            )
        except OSError:
            continue

        drives.append(
            {
                "drive": partition.mountpoint,
                "filesystem": partition.fstype,
                "removable": (
                    "removable" in partition.opts
                ),
                "total_gb": _bytes_to_gb(
                    usage.total
                ),
                "used_gb": _bytes_to_gb(
                    usage.used
                ),
                "free_gb": _bytes_to_gb(
                    usage.free
                ),
                "used_percent": (
                    round(
                        usage.used / usage.total * 100,
                        1,
                    )
                    if usage.total
                    else 0.0
                ),
            }
        )

    if not drives:
        raise RuntimeError(
            "No readable drives were found."
        )

    return drives


def _processor_name():
    """
    Return the processor's marketing name where the platform exposes
    it, falling back to the generic identifier.
    """

    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                name, _ = winreg.QueryValueEx(
                    key,
                    "ProcessorNameString",
                )

            if name.strip():
                return name.strip()
        except OSError:
            pass

    return platform.processor()


def get_cpu_info():
    """
    Return live CPU load, measured over a short sample.

    This is read-only.
    """

    utilization = psutil.cpu_percent(
        interval=CPU_SAMPLE_SECONDS
    )

    frequency = psutil.cpu_freq()

    return {
        "processor": _processor_name(),
        "physical_cores": psutil.cpu_count(
            logical=False
        ),
        "logical_cores": psutil.cpu_count(
            logical=True
        ),
        "utilization_percent": utilization,
        "frequency_mhz": (
            round(frequency.current)
            if frequency is not None
            else None
        ),
    }


def get_memory_info():
    """
    Return live system memory (RAM) usage.

    This is read-only.
    """

    memory = psutil.virtual_memory()

    return {
        "total_gb": _bytes_to_gb(
            memory.total
        ),
        "used_gb": _bytes_to_gb(
            memory.total - memory.available
        ),
        "available_gb": _bytes_to_gb(
            memory.available
        ),
        "used_percent": memory.percent,
    }


def get_gpu_info():
    """
    Query NVIDIA GPU status using nvidia-smi.

    This is read-only.
    """

    command = [
        "nvidia-smi",
        "--query-gpu="
        "name,"
        "temperature.gpu,"
        "utilization.gpu,"
        "memory.total,"
        "memory.used,"
        "memory.free",
        "--format=csv,noheader,nounits",
    ]

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env=child_environment(),
        # Without this, a Freya running without a console of her own
        # flashes a terminal window each time the GPU is queried.
        creationflags=getattr(
            subprocess,
            "CREATE_NO_WINDOW",
            0,
        ),
    )

    output = completed.stdout.strip()

    if not output:
        raise RuntimeError(
            "nvidia-smi returned no GPU data."
        )

    gpus = []

    for index, line in enumerate(
        output.splitlines()
    ):
        parts = [
            part.strip()
            for part in line.split(",")
        ]

        if len(parts) != 6:
            raise RuntimeError(
                "Unexpected nvidia-smi output: "
                f"{line}"
            )

        (
            name,
            temperature,
            utilization,
            memory_total,
            memory_used,
            memory_free,
        ) = parts

        gpus.append(
            {
                "index": index,
                "name": name,
                "temperature_c": int(
                    temperature
                ),
                "utilization_percent": int(
                    utilization
                ),
                "memory_total_mib": int(
                    memory_total
                ),
                "memory_used_mib": int(
                    memory_used
                ),
                "memory_free_mib": int(
                    memory_free
                ),
            }
        )

    return gpus


def register_system_tools(
    registry,
):
    """
    Register Freya's read-only local system tools.
    """

    registry.register(
        CapabilityDefinition(
            name="system.info",
            description=(
                "Returns the computer's name, operating "
                "system and version, architecture, and Python "
                "version."
            ),
            provider=SYSTEM_TOOLS_PROVIDER,
            handler=get_system_info,
            category="system",
            metadata={
                MODEL_TOOL: True,
            },
            aliases=[
                "system",
                "computer.info",
            ],
        )
    )

    registry.register(
        CapabilityDefinition(
            name="system.disk",
            description=(
                "Returns how full each drive letter is right "
                "now: filesystem and total, used, and free "
                "space. For the physical disks' model, type, "
                "or health, use system_hardware."
            ),
            provider=SYSTEM_TOOLS_PROVIDER,
            handler=get_disk_info,
            category="system",
            metadata={
                MODEL_TOOL: True,
            },
            aliases=[
                "disk",
                "disk.info",
                "drives",
            ],
        )
    )

    registry.register(
        CapabilityDefinition(
            name="system.gpu",
            description=(
                "Returns the graphics card's live status: "
                "temperature, utilization, and VRAM used and "
                "free. For its hot spot temperature, fans, or "
                "power draw, use system_sensors."
            ),
            provider=SYSTEM_TOOLS_PROVIDER,
            handler=get_gpu_info,
            category="system",
            metadata={
                MODEL_TOOL: True,
            },
            aliases=[
                "gpu",
                "gpu.status",
            ],
        )
    )

    registry.register(
        CapabilityDefinition(
            name="system.cpu",
            description=(
                "Returns the processor's live load: current "
                "utilization, core and thread counts, and "
                "clock speed. For its temperature or power "
                "draw, use system_sensors."
            ),
            provider=SYSTEM_TOOLS_PROVIDER,
            handler=get_cpu_info,
            category="system",
            metadata={
                MODEL_TOOL: True,
            },
            aliases=[
                "cpu",
                "cpu.status",
            ],
        )
    )

    registry.register(
        CapabilityDefinition(
            name="system.memory",
            description=(
                "Returns how much system memory (RAM) is in "
                "use right now: total, used, and available. "
                "For the installed modules' type, speed, or "
                "slots, use system_hardware."
            ),
            provider=SYSTEM_TOOLS_PROVIDER,
            handler=get_memory_info,
            category="system",
            metadata={
                MODEL_TOOL: True,
            },
            aliases=[
                "ram",
                "ram.status",
            ],
        )
    )

    registry.register(
        CapabilityDefinition(
            name="system.hardware",
            description=(
                "Returns what the computer is built from: "
                "motherboard, BIOS version, processor model, "
                "each memory module's size, type, and current "
                "speed, physical disks with model and health, "
                "graphics card and driver, network adapters, "
                "and audio devices. Use this for questions "
                "about components, specifications, or how "
                "hardware is configured, rather than live "
                "usage."
            ),
            provider=SYSTEM_TOOLS_PROVIDER,
            handler=get_hardware_inventory,
            category="system",
            metadata={
                MODEL_TOOL: True,
            },
            aliases=[
                "hardware",
                "components",
            ],
        )
    )

    registry.register(
        CapabilityDefinition(
            name="system.sensors",
            description=(
                "Returns live sensor readings for each "
                "component: temperatures of the processor, "
                "motherboard, memory modules, graphics card, "
                "and drives; fan speeds; processor and "
                "graphics power draw; and SSD wear. Use this "
                "for questions about heat, cooling, fans, or "
                "power."
            ),
            provider=SYSTEM_TOOLS_PROVIDER,
            handler=get_sensor_readings,
            category="system",
            metadata={
                MODEL_TOOL: True,
            },
            optional_dependencies=[
                "LibreHardwareMonitor web server",
            ],
            aliases=[
                "sensors",
                "temperatures",
            ],
        )
    )


def create_system_tools_provider():
    """
    Describe the in-process provider behind Freya's
    read-only local system tools.

    It has nothing to start and no external dependency,
    so it reports healthy whenever it is asked.
    """

    return ProviderDefinition(
        provider_id=SYSTEM_TOOLS_PROVIDER,
        name="Local system tools",
        capabilities=list(
            SYSTEM_TOOL_CAPABILITIES
        ),
        start_handler=lambda: None,
        stop_handler=lambda: None,
        health_handler=lambda: True,
    )