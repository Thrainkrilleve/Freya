"""
The decider as a provider and one capability, `decide` (proposed NEW-CK).

Every decision goes through the broker, so it is policy-checked and
audited. The broker records only a digest of the arguments, so the
text decided about never reaches the audit log (proposed NEW-CP).

`decide` is not offered to the model as a tool, is in no default policy
seed, and `build_local_control_plane` does not register it: nothing in
Freya asks it anything yet. A test checks all three.
"""
import time
from typing import Dict, Optional

from freya.core.capabilities import CapabilityDefinition
from freya.core.providers import ProviderDefinition
from freya.decider.contract import (
    Decider,
    DecisionError,
    DecisionPoint,
    checked_decision,
    checked_state,
)
from freya.decider.points import POINTS


DECIDER_PROVIDER = "decider-local"
DECIDE = "decide"


class DeciderUnavailable(RuntimeError):
    """
    The decider failed. `detail` may hold what it said, which can quote
    the text, so it is kept out of the message the broker records.
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


def _ready(decider) -> bool:
    try:
        return bool(decider.ready())
    except Exception:
        return False


def create_decider_provider(decider: Decider) -> ProviderDefinition:
    return ProviderDefinition(
        provider_id=DECIDER_PROVIDER,
        name="Local decider",
        capabilities=[DECIDE],
        start_handler=lambda: None,
        health_handler=lambda: _ready(decider),
        metadata={"runs_on": "this machine"},
    )


def register_decide(registry, decider: Decider, points: Optional[Dict[str, DecisionPoint]] = None) -> None:
    points = dict(POINTS if points is None else points)

    def decide(point: str, state: str) -> dict:
        found = points.get(point) if isinstance(point, str) else None

        if found is None:
            raise DecisionError("There is no such decision point.")

        text = checked_state(state)
        started = time.perf_counter()

        try:
            distribution = decider.decide(found, text)
        except Exception as exc:
            raise DeciderUnavailable(
                "The decider could not answer.", detail=str(exc)[:2000],
            ) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        return checked_decision(found, distribution, decider.decider_id, elapsed_ms).as_dict()

    registry.register(
        CapabilityDefinition(
            name=DECIDE,
            description=(
                "Answers one registered question with a fixed set of "
                "answers about the given text, with a probability for "
                "each answer, or abstains. It chooses between paths "
                "Freya's code already allows and never authorizes anything."
            ),
            provider=DECIDER_PROVIDER,
            handler=decide,
            category="decision",
        )
    )
