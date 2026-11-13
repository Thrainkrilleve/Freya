"""
Whether a model fits on this workstation, and how.

A model can sit entirely on the graphics card, be split between the
card and system memory, or not fit at all. A split model still works,
but every token has to read the part in system memory over a bus about
ten to twenty times slower than the card's own memory, so where the
split falls decides whether it is usable for conversation.

Mixture-of-experts models change that arithmetic. Each token uses only
a few of the model's experts, so when the experts are the part kept in
system memory, only a small share of them is read per token. That is
how a model several times larger than the card can still answer at a
usable speed.

Everything here is an estimate from the model's own description and
this machine's totals. `measure` loads the model and reports what
Ollama actually did, which is what a decision should rest on.
"""

from dataclasses import dataclass, field, asdict
import time


GIB = 1024 ** 3

# Bytes per cached key or value element for Ollama's cache types.
KV_CACHE_BYTES = {
    "f16": 2.0,
    "q8_0": 34 / 32,
    "q4_0": 18 / 32,
}

# What the desktop, browser and other programs keep on the card. Measured
# at about 2.1 GB on this workstation on 2026-10-09; a little is added.
DEFAULT_VRAM_RESERVE_GB = 2.5

# System memory left for Windows, Freya herself, speech recognition,
# Kokoro, the window, and ComfyUI's own process when it is idle.
DEFAULT_RAM_RESERVE_GB = 14.0

# Working space the runtime needs on the card beyond weights and cache.
COMPUTE_BUFFER_GB = 1.0

# Rough sustained read speeds, used only for the speed ceiling. A 4090
# reads about 1 TB/s; dual-channel DDR5 about 60-80 GB/s in practice,
# DDR4 about 40-50. The owner can give the real figure.
DEFAULT_GPU_BANDWIDTH_GBS = 800.0
DEFAULT_RAM_BANDWIDTH_GBS = 55.0

# Reading a model file from an NVMe drive on first load.
DEFAULT_DISK_READ_GBS = 2.0

# When the description does not say how much of a mixture-of-experts
# model is experts, this share of its weights is assumed to be.
ASSUMED_EXPERT_SHARE = 0.9

ON_CARD = "on the card"
SPLIT = "split between card and system memory"
TOO_LARGE = "too large for this machine"


@dataclass
class ModelFacts:
    """What is known about one model, from Ollama or from the owner."""

    name: str
    file_bytes: int
    layers: int = 0
    kv_heads: int = 0
    key_length: int = 0
    value_length: int = 0
    total_params: int = 0
    expert_count: int = 0
    experts_used: int = 0
    expert_params: int = 0
    active_params: int = 0
    sliding_window: int = 0
    # Layers that keep a full-length cache, when not all of them do
    # (hybrid models with linear attention, or alternating windows).
    cached_layers: int = 0
    notes: list = field(default_factory=list)

    @property
    def mixture_of_experts(self) -> bool:
        if self.expert_count > 1 and self.experts_used > 0:
            return True
        return 0 < self.active_params < self.total_params


@dataclass
class Machine:
    """The totals that decide fit, and what is held back from them."""

    vram_total_gb: float
    ram_total_gb: float
    vram_reserve_gb: float = DEFAULT_VRAM_RESERVE_GB
    ram_reserve_gb: float = DEFAULT_RAM_RESERVE_GB
    gpu_bandwidth_gbs: float = DEFAULT_GPU_BANDWIDTH_GBS
    ram_bandwidth_gbs: float = DEFAULT_RAM_BANDWIDTH_GBS
    disk_read_gbs: float = DEFAULT_DISK_READ_GBS
    gpu_name: str = ""

    @property
    def vram_budget_gb(self) -> float:
        return max(0.0, self.vram_total_gb - self.vram_reserve_gb)

    @property
    def ram_budget_gb(self) -> float:
        return max(0.0, self.ram_total_gb - self.ram_reserve_gb)


@dataclass
class Plan:
    model: str
    context_tokens: int
    kv_cache: str
    verdict: str
    weights_gb: float
    cache_gb: float
    on_card_gb: float
    in_ram_gb: float
    card_share_percent: int
    tokens_per_second_ceiling: float
    first_load_seconds: float
    notes: list

    def as_dict(self) -> dict:
        return asdict(self)


def kv_cache_bytes(facts: ModelFacts, context_tokens: int, cache_type="f16") -> int:
    """
    The key/value cache for `context_tokens`. Only layers that keep a
    full-length cache are counted; the small fixed state of linear or
    windowed layers is left out, so treat it as approximate.
    """
    if not facts.layers or not facts.kv_heads:
        return 0

    layers = facts.cached_layers or facts.layers
    per_element = KV_CACHE_BYTES.get(cache_type, KV_CACHE_BYTES["f16"])
    width = facts.kv_heads * (facts.key_length + facts.value_length)
    return int(layers * width * context_tokens * per_element)


def expert_share(facts: ModelFacts) -> float:
    """The share of the file that is routed experts (0 for a dense model)."""
    if not facts.mixture_of_experts:
        return 0.0

    if facts.expert_params and facts.total_params:
        return min(0.98, facts.expert_params / facts.total_params)

    return ASSUMED_EXPERT_SHARE


def expert_bytes(facts: ModelFacts) -> int:
    return int(facts.file_bytes * expert_share(facts))


def expert_read_ratio(facts: ModelFacts) -> float:
    """
    The share of the experts each token reads. From the expert counts
    when the file gives them; otherwise from active and total parameter
    counts, with the non-expert part read in full.
    """
    if facts.expert_count > 1 and facts.experts_used > 0:
        return facts.experts_used / facts.expert_count

    share = expert_share(facts)

    if not share or not facts.total_params:
        return 1.0

    active = facts.active_params / facts.total_params
    return min(1.0, max(0.01, (active - (1 - share)) / share))


def plan(
    facts: ModelFacts,
    machine: Machine,
    context_tokens: int,
    kv_cache: str = "f16",
) -> Plan:
    """
    Where the model would go and what that costs.

    The split assumed is the one that suits each kind of model: for a
    mixture-of-experts model the experts leave the card first and
    everything else stays on it; for a dense model whole layers leave
    the card. llama.cpp's automatic fitting, which Ollama uses, places
    weights the same way when nothing forces a layer count.
    """
    notes = list(facts.notes)
    weights = facts.file_bytes / GIB
    cache = kv_cache_bytes(facts, context_tokens, kv_cache) / GIB
    needed_on_card = weights + cache + COMPUTE_BUFFER_GB
    card = machine.vram_budget_gb
    ram = machine.ram_budget_gb

    if not facts.layers:
        notes.append(
            "The model does not describe its layers, so the cache could "
            "not be estimated; the figures count weights only."
        )

    if needed_on_card <= card:
        verdict = ON_CARD
        in_ram = 0.0
    elif needed_on_card <= card + ram:
        verdict = SPLIT
        in_ram = needed_on_card - card
    else:
        verdict = TOO_LARGE
        in_ram = max(0.0, needed_on_card - card)

    on_card = needed_on_card - in_ram
    ceiling = _speed_ceiling(facts, machine, weights, in_ram)

    if verdict == SPLIT and facts.mixture_of_experts:
        experts = expert_bytes(facts) / GIB

        if in_ram <= experts:
            notes.append(
                "Only experts need to leave the card, which is the "
                "split that keeps a mixture-of-experts model quick."
            )
        else:
            notes.append(
                "Even with every expert in system memory the rest does "
                "not fit on the card, so some shared layers leave it too "
                "and speed drops sharply."
            )
    elif verdict == SPLIT:
        notes.append(
            "A dense model reads all of its weights for every token, so "
            "the part in system memory sets the pace."
        )

    if verdict == TOO_LARGE:
        short = needed_on_card - card - ram
        notes.append(
            f"About {short:.1f} GB more than the card and the system "
            "memory left for models can hold."
        )

    first_load = weights / machine.disk_read_gbs if machine.disk_read_gbs else 0.0

    if weights + cache > ram + card * 0.5:
        notes.append(
            "Too large for Windows to keep the file cached, so every "
            "reload reads it from disk again. Freya unloads her model for "
            "each picture and each large voice engine, so each of those "
            "costs a full reload."
        )

    return Plan(
        model=facts.name,
        context_tokens=context_tokens,
        kv_cache=kv_cache,
        verdict=verdict,
        weights_gb=round(weights, 1),
        cache_gb=round(cache, 1),
        on_card_gb=round(on_card, 1),
        in_ram_gb=round(in_ram, 1),
        card_share_percent=int(round(100 * on_card / needed_on_card)),
        tokens_per_second_ceiling=round(ceiling, 1),
        first_load_seconds=round(first_load),
        notes=notes,
    )


def _speed_ceiling(facts, machine, weights_gb, in_ram_gb) -> float:
    """
    The fastest a reply could be written, from how many bytes each
    token has to read from each kind of memory. Real speed is lower;
    this is for comparing options, not for promising a figure.
    """
    if weights_gb <= 0:
        return 0.0

    in_ram_gb = min(in_ram_gb, weights_gb)
    on_card_gb = weights_gb - in_ram_gb

    if facts.mixture_of_experts:
        used = expert_read_ratio(facts)
        experts = expert_bytes(facts) / GIB
        experts_in_ram = min(in_ram_gb, experts)
        shared_in_ram = in_ram_gb - experts_in_ram
        experts_on_card = experts - experts_in_ram
        shared_on_card = on_card_gb - experts_on_card
        card_read = shared_on_card + experts_on_card * used
        ram_read = shared_in_ram + experts_in_ram * used
    else:
        card_read = on_card_gb
        ram_read = in_ram_gb

    seconds = (
        card_read / machine.gpu_bandwidth_gbs
        + ram_read / machine.ram_bandwidth_gbs
    )
    return 1 / seconds if seconds > 0 else 0.0


# Reading the model's description from Ollama.


def _get(obj, *names):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _model_info(show_response) -> dict:
    info = _get(show_response, "modelinfo", "model_info")
    return dict(info) if info else {}


def facts_from_ollama(client, model: str) -> ModelFacts:
    """
    The model's description as Ollama reports it. Reading it loads
    nothing. Raises LookupError when the model is not installed.
    """
    size = None

    for entry in _get(client.list(), "models") or []:
        name = _get(entry, "model", "name")
        if name in (model, f"{model}:latest"):
            size = _get(entry, "size")
            break

    if size is None:
        raise LookupError(f"{model} is not installed in Ollama.")

    info = _model_info(client.show(model))
    arch = info.get("general.architecture") or ""

    def number(key, default=0):
        value = info.get(f"{arch}.{key}", default)
        if isinstance(value, list):
            value = max(value) if value else default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    heads = number("attention.head_count")
    width = number("embedding_length")
    kv_heads = number("attention.head_count_kv", heads)
    default_head = width // heads if heads else 0
    layers = number("block_count")
    experts = number("expert_count")
    ff = number("expert_feed_forward_length")
    # Three projection matrices per expert (gate, up, down).
    expert_params = layers * experts * 3 * width * ff if ff else 0
    used = number("expert_used_count")
    total = int(info.get("general.parameter_count") or 0)
    active = 0

    window = number("attention.sliding_window")
    interval = number("full_attention_interval")
    cached_layers = 0

    if interval > 1:
        cached_layers = max(1, layers // interval)
    elif window:
        # Models with a window alternate windowed and full layers.
        cached_layers = max(1, layers // 2)

    if total and expert_params and experts and used:
        active = int(total - expert_params * (1 - used / experts))

    return ModelFacts(
        name=model,
        file_bytes=int(size),
        layers=layers,
        kv_heads=kv_heads,
        key_length=number("attention.key_length", default_head),
        value_length=number("attention.value_length", default_head),
        total_params=total,
        expert_count=experts,
        experts_used=used,
        expert_params=expert_params,
        active_params=max(0, active),
        sliding_window=window,
        cached_layers=cached_layers,
    )


def facts_from_figures(
    name, size_gb, total_b=0.0, active_b=0.0, layers=0, kv_heads=0, head_dim=0
) -> ModelFacts:
    """
    A model that is not installed, described by the owner from its
    download page, so that fit can be checked before tens of gigabytes
    are downloaded. Giving active and total parameter counts marks it
    as a mixture-of-experts model.
    """
    facts = ModelFacts(
        name=name,
        file_bytes=int(size_gb * GIB),
        layers=layers,
        kv_heads=kv_heads,
        key_length=head_dim,
        value_length=head_dim,
        total_params=int(total_b * 1e9),
    )

    if total_b and active_b and active_b < total_b:
        facts.active_params = int(active_b * 1e9)
        facts.notes.append(
            "Described from figures, not from the file: experts are "
            f"assumed to be {ASSUMED_EXPERT_SHARE:.0%} of the weights."
        )

    if not layers:
        facts.notes.append(
            "Give --layers, --kv-heads and --head-dim to include the cache."
        )

    return facts


def this_machine(**overrides) -> Machine:
    """This workstation's card and memory, read live."""
    from freya.tools.system_tools import get_gpu_info, get_memory_info

    gpus = get_gpu_info()
    memory = get_memory_info()
    gpu = gpus[0] if gpus else {"memory_total_mib": 0, "name": ""}

    return Machine(
        vram_total_gb=gpu["memory_total_mib"] / 1024,
        ram_total_gb=memory["total_gb"],
        gpu_name=gpu.get("name", ""),
        **{key: value for key, value in overrides.items() if value is not None},
    )


# Measuring what Ollama actually does.

MEASURE_PROMPT = (
    "In about two hundred words, explain how a heat pump moves heat out "
    "of a cold room into a warm one."
)


def measure(client, model, context_tokens, prompt=MEASURE_PROMPT, keep_loaded=False):
    """
    Load `model` the way Freya would (her context size, no layer count
    forced, so the runtime chooses the split), ask one question, and
    report where the model ended up and how fast it wrote.

    This unloads whatever Ollama had loaded, including Freya's own
    model, which Ollama reloads on her next turn.
    """
    started = time.monotonic()
    response = client.generate(
        model=model,
        prompt=prompt,
        options={"num_ctx": context_tokens},
        keep_alive="5m" if keep_loaded else "30s",
    )
    wall = time.monotonic() - started

    placed = None

    for entry in _get(client.ps(), "models") or []:
        if _get(entry, "model", "name") in (model, f"{model}:latest"):
            placed = entry
            break

    def seconds(name):
        value = _get(response, name) or 0
        return value / 1e9

    eval_count = _get(response, "eval_count") or 0
    eval_seconds = seconds("eval_duration")
    prompt_count = _get(response, "prompt_eval_count") or 0
    prompt_seconds = seconds("prompt_eval_duration")

    result = {
        "model": model,
        "context_tokens": context_tokens,
        "load_seconds": round(seconds("load_duration"), 1),
        "wall_seconds": round(wall, 1),
        "reply_tokens": eval_count,
        "tokens_per_second": round(eval_count / eval_seconds, 1) if eval_seconds else 0.0,
        "prompt_tokens_per_second": (
            round(prompt_count / prompt_seconds, 1) if prompt_seconds else 0.0
        ),
    }

    if placed is not None:
        size = _get(placed, "size") or 0
        on_card = _get(placed, "size_vram") or 0
        result["loaded_gb"] = round(size / GIB, 1)
        result["on_card_gb"] = round(on_card / GIB, 1)
        result["card_share_percent"] = int(round(100 * on_card / size)) if size else 0

    return result
