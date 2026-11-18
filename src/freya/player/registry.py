"""
Which adapters exist, and which schema actions each one may perform.

An adapter that does not declare an action cannot be asked for it.
Registering an adapter here does not register a capability with the
control plane, and it does not allow a target.
"""

import re
from dataclasses import dataclass

from freya.player.schema import ACTION_NAMES, PermissionRefused


_ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")


@dataclass(frozen=True)
class AdapterSpec:
    """What one adapter says it can do. The schema is the outer bound."""

    adapter_id: str
    actions: frozenset
    summary: str


class AdapterRegistry:
    """The adapters this process knows how to build. It starts empty."""

    def __init__(self):
        self._specs = {}
        self._factories = {}

    def register(self, spec: AdapterSpec, factory) -> None:
        if not isinstance(spec, AdapterSpec):
            raise ValueError("An adapter must describe itself.")
        if not _ID.match(spec.adapter_id):
            raise ValueError("The adapter id is not acceptable.")
        if not isinstance(spec.summary, str) or not spec.summary.strip():
            raise ValueError("The adapter needs a summary.")
        actions = spec.actions
        if not isinstance(actions, frozenset) or not actions:
            raise ValueError("The adapter must name the actions it performs.")
        if not actions <= ACTION_NAMES:
            raise ValueError("The adapter names an action that is not in the schema.")
        if "stop" not in actions:
            raise ValueError("The adapter must be able to stop.")
        if spec.adapter_id in self._specs:
            raise ValueError("That adapter is already registered.")
        self._specs[spec.adapter_id] = spec
        self._factories[spec.adapter_id] = factory

    def get(self, adapter_id: str):
        return self._specs.get(adapter_id)

    def create(self, adapter_id: str):
        factory = self._factories.get(adapter_id)
        if factory is None:
            raise PermissionRefused("That adapter is not registered.")
        return factory()

    def permits(self, adapter_id: str, action: str) -> bool:
        spec = self._specs.get(adapter_id)
        return spec is not None and action in spec.actions

    def ids(self):
        return tuple(sorted(self._specs))
