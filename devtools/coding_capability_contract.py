"""Security contract for exposing local coding through Freya's Capability Broker.

This is an integration specification, not an alternate authorization engine.
The development CLI must not be registered as a model tool directly.
"""

CODING_PROPOSE = "development.code.propose"
CODING_VERIFY = "development.code.verify"
CODING_APPLY = "development.code.apply"
CODING_MERGE = "development.code.merge"

# Separate privileges prevent a model's ability to propose a patch from
# silently granting write, execution, or merge authority.
CAPABILITIES = {
    CODING_PROPOSE: {
        "requires": ("authenticated_actor", "authorized_repository", "selected_files"),
        "effects": "read_selected_sources_and_write_proposal_artifacts",
        "default": "deny",
    },
    CODING_VERIFY: {
        "requires": ("authenticated_actor", "authorized_repository", "approved_test_profile"),
        "effects": "disposable_workspace_validation",
        "default": "deny",
    },
    CODING_APPLY: {
        "requires": ("authenticated_actor", "explicit_patch_approval", "verified_patch_digest"),
        "effects": "write_authorized_workspace",
        "default": "deny",
    },
    CODING_MERGE: {
        "requires": ("authenticated_actor", "explicit_merge_approval", "reviewed_commit"),
        "effects": "update_target_branch",
        "default": "deny",
    },
}


def assert_broker_authorized(*, broker_decision: bool, capability: str) -> None:
    """Fail closed at a future broker adapter boundary; not itself a broker."""
    if capability not in CAPABILITIES:
        raise ValueError("Unknown coding capability")
    if broker_decision is not True:
        raise PermissionError(f"Broker authorization required for {capability}")
