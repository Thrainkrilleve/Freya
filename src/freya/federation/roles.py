"""
What a machine could do for Freya, worked out from its profile.

These are suggestions for the owner to read, from the machine's own
statement about itself. They are rough on purpose and say so: the
real test of a worker is a measured run on it (the kill test and a
benchmark, `docs/design/worker-nodes.md`).
"""

from dataclasses import dataclass
from typing import List

GIB = 1024 ** 3

# At 4-bit quantization a model's weights take roughly 0.6 GB per
# billion parameters; the cache for a useful context and the runtime
# itself need about 1.5 GB more. Good enough to say which size class
# fits, not to promise speed.
GB_PER_BILLION_AT_4BIT = 0.6
RUNTIME_OVERHEAD_GB = 1.5

MIN_GPU_GB_FOR_MODELS = 6
MIN_GPU_GB_FOR_SPLIT = 4
MIN_RAM_GB_FOR_CPU_MODELS = 16
MIN_RAM_GB_FOR_SANDBOX = 8
MIN_RAM_GB_FOR_HOME_ASSISTANT = 2
MIN_RAM_GB_TO_BE_USEFUL = 4


@dataclass(frozen=True)
class Role:
    name: str
    title: str
    # Why it cannot be enrolled in this version; empty when it can.
    why_not_yet: str = ""


ROLES = {
    "model-worker": Role(
        "model-worker",
        "Runs whole models for Freya on its own graphics card",
    ),
    "cpu-model-worker": Role(
        "cpu-model-worker",
        "Runs small models for Freya on its processor, slowly",
    ),
    "split-inference": Role(
        "split-inference",
        "Holds part of one large model split across machines",
        "Splitting one model across machines waits for the runtime "
        "choice and a measured benchmark (the larger-model work).",
    ),
    # Allowed by the owner's exception to decision 10 (2026-11-10),
    # under the conditions in SANDBOX_HOST_CONDITIONS.
    "sandbox-host": Role(
        "sandbox-host",
        "Runs code a model wrote, away from the main PC",
    ),
    "home-assistant-host": Role(
        "home-assistant-host",
        "Runs Home Assistant for smart-home devices",
        "Freya has no smart-home code yet; hosting Home Assistant is its "
        "own decision.",
    ),
}

# What enrolling a sandbox host commits to. The exception to decision
# 10 holds only while all of these do; they are shown to the owner at
# enrollment, and the code that sends runs to a laptop must check them.
SANDBOX_HOST_CONDITIONS = (
    "Runs are made only inside a sandbox that meets code-sandbox.md: no "
    "network, none of the machine's own files, no credentials, nothing "
    "kept between runs.",
    "The machine holds nothing from Freya's data folder.",
    "It reaches the main PC only through the worker channel; it is on a "
    "guest network or a tailnet rule that blocks everything else.",
    "Each run is approved as it would be on the main PC.",
)

# Inference changes nothing on the lender (decision 10, Q3); a sandbox
# host is allowed by the owner's exception. Everything else waits.
ROLES_ALLOWED_NOW = frozenset(
    name for name, role in ROLES.items() if not role.why_not_yet
)


@dataclass(frozen=True)
class Suggestion:
    role: str
    reason: str
    allowed_now: bool


def model_size_class(gpu_gb):
    """
    The largest model, in billions of parameters at 4-bit, that roughly
    fits in this much graphics memory.
    """
    usable = max(0.0, gpu_gb - RUNTIME_OVERHEAD_GB)
    return int(usable / GB_PER_BILLION_AT_4BIT)


def _memory_bytes(gpu):
    memory = gpu.get("memory_bytes") if isinstance(gpu, dict) else None
    if isinstance(memory, bool) or not isinstance(memory, (int, float)):
        return 0
    if memory <= 0:
        return 0
    return int(memory)


def _vendor_label(gpu):
    vendor = gpu.get("vendor")
    name = (gpu.get("name") or "").lower()
    if vendor == "amd":
        return "AMD"
    if vendor == "intel":
        return "Intel Arc" if "arc" in name else "Intel"
    if vendor == "nvidia":
        return "NVIDIA"
    return "graphics"


def _backend_note(gpu):
    """
    Which local backends can run this card. AMD and Intel Arc are
    first-class: lacking CUDA is not the same as lacking a GPU.
    """
    vendor = gpu.get("vendor")
    runtimes = set(gpu.get("runtimes") or [])
    if vendor == "amd":
        if "rocm" in runtimes and "vulkan" in runtimes:
            return " Ollama can run it through its ROCm or Vulkan backend."
        if "rocm" in runtimes:
            return " Ollama can run it through its ROCm backend."
        if "vulkan" in runtimes:
            return " Ollama can run it through its Vulkan backend."
        return " Ollama runs AMD cards through its ROCm or Vulkan backend."
    if vendor == "intel":
        if "vulkan" in runtimes and "sycl" in runtimes:
            return (
                " Ollama can run it through its Vulkan backend, and "
                "oneAPI/SYCL is installed."
            )
        if "vulkan" in runtimes:
            return " Ollama can run Intel Arc through its Vulkan backend."
        if "sycl" in runtimes:
            return (
                " oneAPI/SYCL is installed; Ollama's Vulkan backend "
                "is the one that runs Intel Arc."
            )
        return " Ollama's Vulkan backend is the one that runs Intel Arc."
    if vendor == "nvidia" and "cuda" in runtimes:
        return " Ollama can run it through its CUDA backend."
    return ""


def suggest_roles(profile) -> List[Suggestion]:
    ram_gb = (profile.get("ram_bytes") or 0) / GIB
    gpus = [gpu for gpu in profile.get("gpus") or [] if isinstance(gpu, dict)]
    sized = [gpu for gpu in gpus if _memory_bytes(gpu)]
    # The same amount of memory prefers AMD, then Intel, then NVIDIA.
    best = max(
        sized,
        key=lambda gpu: (
            _memory_bytes(gpu),
            -{"amd": 0, "intel": 1, "nvidia": 2}.get(gpu.get("vendor"), 9),
        ),
        default=None,
    )
    best_gpu_gb = (_memory_bytes(best) / GIB) if best else 0.0
    unmeasured = [gpu for gpu in gpus if not _memory_bytes(gpu) and gpu.get("name")]
    architecture = ((profile.get("os") or {}).get("architecture") or "").lower()
    x86 = architecture in ("amd64", "x86_64", "x64")

    suggestions = []

    def add(role, reason):
        suggestions.append(
            Suggestion(role, reason, role in ROLES_ALLOWED_NOW)
        )

    if ram_gb and ram_gb < MIN_RAM_GB_TO_BE_USEFUL and best_gpu_gb == 0:
        if ram_gb >= MIN_RAM_GB_FOR_HOME_ASSISTANT:
            add(
                "home-assistant-host",
                f"{ram_gb:.0f} GB of memory is too little for models, but "
                "enough for Home Assistant.",
            )
        return suggestions

    if best is not None and best_gpu_gb >= MIN_GPU_GB_FOR_MODELS:
        size = model_size_class(best_gpu_gb)
        several = ""
        if len(sized) > 1:
            total_gb = sum(_memory_bytes(gpu) for gpu in sized) / GIB
            several = (
                f" {len(sized)} cards are fitted, {total_gb:.0f} GB altogether."
            )
        add(
            "model-worker",
            f"A {best_gpu_gb:.0f} GB {_vendor_label(best)} card fits models "
            f"up to about {size}B parameters at 4-bit, such as a small "
            f"worker model or embeddings, leaving the main PC's card for "
            f"Freya's replies.{several}{_backend_note(best)}",
        )

    if best is not None and best_gpu_gb >= MIN_GPU_GB_FOR_SPLIT:
        add(
            "split-inference",
            f"Its {best_gpu_gb:.0f} GB {_vendor_label(best)} card could hold "
            "part of a model too big for the main PC alone. Over home "
            "Ethernet that is slower per word; it is for size, not speed.",
        )

    if best_gpu_gb < MIN_GPU_GB_FOR_MODELS and ram_gb >= MIN_RAM_GB_FOR_CPU_MODELS:
        if best is None and unmeasured:
            add(
                "cpu-model-worker",
                f"{unmeasured[0]['name']} is fitted but its memory was not "
                f"measured, so no model size was guessed. {ram_gb:.0f} GB of "
                "memory can run models of about 3 to 4B parameters on the "
                "processor, a few words a second.",
            )
        else:
            add(
                "cpu-model-worker",
                f"No usable graphics card, but {ram_gb:.0f} GB of memory runs "
                "models of about 3 to 4B parameters on the processor, a few "
                "words a second.",
            )

    if ram_gb >= MIN_RAM_GB_FOR_SANDBOX and x86:
        add(
            "sandbox-host",
            "A separate machine is a stronger wall around generated code "
            "than a container on the main PC.",
        )

    if ram_gb >= MIN_RAM_GB_FOR_HOME_ASSISTANT:
        add(
            "home-assistant-host",
            "Home Assistant needs little; an always-on old laptop suits it.",
        )

    return suggestions
