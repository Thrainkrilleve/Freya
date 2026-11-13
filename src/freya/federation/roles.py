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


def suggest_roles(profile) -> List[Suggestion]:
    ram_gb = (profile.get("ram_bytes") or 0) / GIB
    gpus = [
        gpu for gpu in profile.get("gpus") or []
        if gpu.get("vendor") == "nvidia"
    ]
    best_gpu_gb = max(
        (gpu["memory_bytes"] / GIB for gpu in gpus), default=0.0
    )
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

    if best_gpu_gb >= MIN_GPU_GB_FOR_MODELS:
        size = model_size_class(best_gpu_gb)
        add(
            "model-worker",
            f"A {best_gpu_gb:.0f} GB NVIDIA card fits models up to about "
            f"{size}B parameters at 4-bit, such as a small worker model "
            "or embeddings, leaving the main PC's card for Freya's replies.",
        )

    if best_gpu_gb >= MIN_GPU_GB_FOR_SPLIT:
        add(
            "split-inference",
            f"Its {best_gpu_gb:.0f} GB card could hold part of a model too "
            "big for the main PC alone. Over home Ethernet that is slower "
            "per word; it is for size, not speed.",
        )

    if best_gpu_gb < MIN_GPU_GB_FOR_MODELS and ram_gb >= MIN_RAM_GB_FOR_CPU_MODELS:
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
