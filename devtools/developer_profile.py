"""Conservative permission policy for the supervised developer alpha.

This standalone standard-library module is a reference policy, not a substitute
for Freya's authenticated broker, approvals, audit trail or OS sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeveloperAction(str, Enum):
    READ_SOURCE = "read_source"
    PROPOSE_PATCH = "propose_patch"
    VALIDATE_PATCH = "validate_patch"
    RUN_TESTS = "run_tests"
    APPLY_PATCH = "apply_patch"
    CREATE_PR = "create_pr"
    MERGE = "merge"
    DEPLOY = "deploy"
    CHANGE_POLICY = "change_policy"
    EDIT_SECRETS = "edit_secrets"


@dataclass(frozen=True)
class DeveloperProfile:
    """Immutable, fail-closed capabilities for a proposal-only trial."""

    name: str = "supervised-proposal-only"
    permitted_actions: frozenset[DeveloperAction] = frozenset({
        DeveloperAction.READ_SOURCE,
        DeveloperAction.PROPOSE_PATCH,
    })

    def __post_init__(self) -> None:
        try:
            actions = frozenset(DeveloperAction(action) for action in self.permitted_actions)
        except (TypeError, ValueError):
            raise ValueError("Expected a collection of known developer actions.") from None
        object.__setattr__(self, "permitted_actions", actions)

    def allows(self, action: DeveloperAction | str) -> bool:
        try:
            requested = DeveloperAction(action)
        except ValueError:
            return False
        return requested in self.permitted_actions


DEFAULT_PROFILE = DeveloperProfile()
