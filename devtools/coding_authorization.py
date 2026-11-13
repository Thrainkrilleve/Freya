"""Policy-backed cooperative revocation checks for long-running coding proposals.

The streaming model client calls the checkpoint at least every
local_coding_agent.POLL_INTERVAL seconds and closes the connection when it
raises; every later proposal stage checks it again. A custom, non-streaming
`generate` function passed to the runner is not interrupted mid-call.
"""
from __future__ import annotations

from typing import Callable

from coding_capability_contract import CODING_PROPOSE


class CodingAuthorizationRevoked(PermissionError):
    """The proposal is no longer authorized to continue."""


def policy_checkpoint(policy, actor: str, *,
                      cancelled: Callable[[], bool] | None = None) -> Callable[[], None]:
    """Return a fail-closed callback bound to an authenticated actor.

    Caller must supply actor from a trusted broker/session, never model input.
    Re-evaluates policy on every checkpoint, including after model generation.
    """
    if not actor or not actor.strip():
        raise ValueError("An authenticated actor is required")

    def check() -> None:
        if cancelled is not None:
            try:
                if cancelled():
                    raise CodingAuthorizationRevoked("Coding task cancelled")
            except CodingAuthorizationRevoked:
                raise
            except Exception as exc:
                raise CodingAuthorizationRevoked("Cancellation state unavailable") from exc
        try:
            decision = policy.decide(actor, CODING_PROPOSE)
        except Exception as exc:
            raise CodingAuthorizationRevoked("Policy state unavailable") from exc
        if getattr(decision, "allow", None) is not True:
            raise CodingAuthorizationRevoked("Coding permission revoked")

    return check
