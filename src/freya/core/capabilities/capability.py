from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


# What a capability may say it touches. "none" means it touches no
# filesystem, network, GPU, device, secret, memory store, or process,
# and it cannot be combined with another kind.
ACCESS_KINDS = frozenset({
    "none",
    "filesystem",
    "network",
    "gpu",
    "device",
    "secret",
    "memory",
    "process",
})

EFFECT_READ_ONLY = "read-only"
EFFECT_CONSEQUENTIAL = "consequential"
EFFECTS = frozenset({EFFECT_READ_ONLY, EFFECT_CONSEQUENTIAL})


@dataclass(frozen=True)
class ResourceAccess:
    """
    One kind of data or resource a capability touches, and what
    specifically: a folder, a host, a device, a secret, or none.
    """

    kind: str
    detail: str

    def __post_init__(self):
        kind = str(self.kind).strip().lower()
        detail = str(self.detail).strip()

        if kind not in ACCESS_KINDS:
            raise ValueError(f"Unknown access kind: {kind!r}.")

        if not detail:
            raise ValueError("An access needs a detail of what is touched.")

        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "detail", detail)


@dataclass(frozen=True)
class CapabilityDeclaration:
    """
    What a capability does to the machine, checked at registration.

    ``effect`` is ``read-only`` or ``consequential``. Consequential
    means it modifies software, hardware, files, or an external
    system. Read-only still needs an allow rule.
    """

    accesses: Tuple[Any, ...]
    requires: Tuple[str, ...] = ()
    effect: str = EFFECT_READ_ONLY

    def __post_init__(self):
        checked = []

        for access in self.accesses:
            if isinstance(access, ResourceAccess):
                checked.append(access)
            elif isinstance(access, tuple) and len(access) == 2:
                checked.append(ResourceAccess(access[0], access[1]))
            else:
                raise ValueError("Each access must name a kind and a detail.")

        if not checked:
            raise ValueError("A declaration must say what it accesses.")

        kinds = {item.kind for item in checked}

        if "none" in kinds and kinds != {"none"}:
            raise ValueError(
                "An access of 'none' cannot be combined with another."
            )

        requires = tuple(str(item).strip().lower() for item in self.requires)

        if any(not item for item in requires):
            raise ValueError("A required resource must be named.")

        effect = str(self.effect).strip().lower()

        if effect not in EFFECTS:
            raise ValueError(
                "Effect must be 'read-only' or 'consequential'."
            )

        object.__setattr__(self, "accesses", tuple(checked))
        object.__setattr__(self, "requires", requires)
        object.__setattr__(self, "effect", effect)

    @property
    def consequential(self) -> bool:
        return self.effect == EFFECT_CONSEQUENTIAL

    def as_dict(self) -> dict:
        return {
            "effect": self.effect,
            "requires": list(self.requires),
            "accesses": [
                {"kind": access.kind, "detail": access.detail}
                for access in self.accesses
            ],
        }


def declare(*accesses, requires=(), effect=EFFECT_READ_ONLY) -> CapabilityDeclaration:
    """
    Build a declaration. Each access is a (kind, detail) pair.
    """
    return CapabilityDeclaration(accesses, requires, effect)


class CapabilityStatus(str, Enum):
    """
    Runtime availability state of a capability.
    """

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass
class CapabilityDefinition:
    """
    Describes one capability available to Freya.

    A capability represents WHAT Freya can do.

    Examples:
        llm.chat
        memory.search
        speech.transcribe
        speech.synthesize
        image.generate
        tools.execute

    The implementation behind a capability may later be
    local, remote, hardware-specific, or replaceable.
    """

    name: str
    description: str

    provider: str
    handler: Callable[..., Any]

    category: str = "general"

    status: CapabilityStatus = CapabilityStatus.AVAILABLE

    # Interim safety/resource flags. ``declaration`` is the checked
    # record of what the capability touches and whether it modifies
    # anything. ``requires_gpu`` and ``destructive`` stay, and
    # registration refuses a declaration that contradicts them.
    requires_gpu: bool = False
    destructive: bool = False
    declaration: Optional[CapabilityDeclaration] = None

    # Alternative lookup names. The capability name stays canonical.
    aliases: List[str] = field(
        default_factory=list
    )

    optional_dependencies: List[str] = field(
        default_factory=list
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )


@dataclass
class CapabilityExecutionResult:
    """
    Standard result returned when Freya invokes a capability.
    """

    success: bool
    capability_name: str

    provider: Optional[str] = None

    result: Any = None
    error: Optional[str] = None