from typing import Dict, List, Optional

from freya.core.capabilities.capability import (
    CapabilityDeclaration,
    CapabilityDefinition,
    CapabilityExecutionResult,
    CapabilityStatus,
)
from freya.core.providers.provider import (
    ProviderHealth,
    ProviderState,
)
from freya.core.providers.registry import (
    ProviderRegistry,
)


def _require_complete(capability: CapabilityDefinition) -> None:
    """
    Refuse a capability that does not say what it does, what it
    touches, what it needs, and whether it only reads.
    """
    name = capability.name.strip() or "(unnamed)"

    if not capability.description or not capability.description.strip():
        raise ValueError(
            f"Capability {name} needs a description of what it does."
        )

    declaration = capability.declaration

    if not isinstance(declaration, CapabilityDeclaration):
        raise ValueError(
            f"Capability {name} needs a declaration of what it "
            "accesses, what it requires, and whether it is "
            "read-only or consequential."
        )

    if capability.requires_gpu and "gpu" not in declaration.requires:
        raise ValueError(
            f"Capability {name} sets requires_gpu but does not "
            "declare gpu as required."
        )

    if capability.destructive and not declaration.consequential:
        raise ValueError(
            f"Capability {name} is destructive, so its effect "
            "must be consequential."
        )


class CapabilityRegistry:
    """
    Central registry describing what Freya can currently do.

    The registry intentionally depends on capability
    contracts rather than specific implementations.

    Providers may eventually live:
        - in the current process
        - in another local process
        - on another machine
        - on dedicated GPU hardware
        - behind an API

    Freya Core should not need to know the difference.

    When a ProviderRegistry is supplied, a capability is
    only available while its provider is registered there,
    running, and healthy. Availability is derived from the
    provider on every query rather than copied, so the two
    registries cannot disagree.
    """

    def __init__(
        self,
        providers: Optional[ProviderRegistry] = None,
    ):
        self._providers = providers

        self._capabilities: Dict[
            str,
            CapabilityDefinition,
        ] = {}

        self._aliases: Dict[
            str,
            str,
        ] = {}

    @staticmethod
    def _normalize_name(
        name: str,
    ) -> str:
        return name.strip().lower()

    def _resolve_name(
        self,
        name: str,
    ) -> Optional[str]:
        name = self._normalize_name(name)

        if name in self._capabilities:
            return name

        return self._aliases.get(name)

    def register(
        self,
        capability: CapabilityDefinition,
        replace: bool = False,
    ) -> None:
        """
        Register a capability.

        A name that is already registered is rejected
        unless replace=True, so one provider cannot
        silently take over another's capability.

        Registration is all-or-nothing: when the declaration
        is incomplete, or the name or any alias conflicts, the
        registry is unchanged.
        """

        _require_complete(capability)

        name = self._normalize_name(
            capability.name
        )

        if not name:
            raise ValueError(
                "Capability name cannot be empty."
            )

        if (
            name in self._capabilities
            and not replace
        ):
            raise ValueError(
                "Capability is already registered: "
                f"{name}"
            )

        if self._aliases.get(name) is not None:
            raise ValueError(
                "Capability name conflicts with "
                "a registered alias: "
                f"{name}"
            )

        aliases = []

        for alias in capability.aliases:
            alias = self._normalize_name(alias)

            if (
                not alias
                or alias == name
                or alias in aliases
            ):
                continue

            if alias in self._capabilities:
                raise ValueError(
                    "Capability alias conflicts with "
                    "a registered capability: "
                    f"{alias}"
                )

            if self._aliases.get(alias, name) != name:
                raise ValueError(
                    "Capability alias is already "
                    f"registered: {alias}"
                )

            aliases.append(alias)

        self._remove_aliases(name)

        capability.name = name
        capability.aliases = aliases

        self._capabilities[name] = capability

        for alias in aliases:
            self._aliases[alias] = name

    def _remove_aliases(
        self,
        name: str,
    ) -> None:
        for alias in [
            alias
            for alias, target
            in self._aliases.items()
            if target == name
        ]:
            del self._aliases[alias]

    def unregister(
        self,
        name: str,
    ) -> bool:
        """
        Remove a capability, and its aliases, from the
        registry.
        """

        name = self._resolve_name(name)

        if name is None:
            return False

        del self._capabilities[name]

        self._remove_aliases(name)

        return True

    def exists(
        self,
        name: str,
    ) -> bool:
        """
        Return True when a capability is registered
        under this name or alias.
        """

        return self._resolve_name(name) is not None

    def get(
        self,
        name: str,
    ) -> Optional[CapabilityDefinition]:
        """
        Retrieve a capability definition by name or alias.
        """

        name = self._resolve_name(name)

        if name is None:
            return None

        return self._capabilities[name]

    def list_capabilities(
        self,
    ) -> List[CapabilityDefinition]:
        """
        Return all registered capabilities.
        """

        return list(
            self._capabilities.values()
        )

    def set_status(
        self,
        name: str,
        status: CapabilityStatus,
    ) -> bool:
        """
        Change the runtime status of a capability.
        """

        capability = self.get(name)

        if capability is None:
            return False

        capability.status = status

        return True

    def _effective_status(
        self,
        capability: CapabilityDefinition,
    ) -> CapabilityStatus:
        """
        Combine the capability's own status with the
        state and health of its provider.
        """

        if (
            capability.status
            != CapabilityStatus.AVAILABLE
            or self._providers is None
        ):
            return capability.status

        provider = self._providers.get(
            capability.provider
        )

        if (
            provider is None
            or provider.state
            != ProviderState.RUNNING
        ):
            return CapabilityStatus.UNAVAILABLE

        if (
            provider.health
            == ProviderHealth.HEALTHY
        ):
            return CapabilityStatus.AVAILABLE

        if (
            provider.health
            == ProviderHealth.DEGRADED
        ):
            return CapabilityStatus.DEGRADED

        return CapabilityStatus.UNAVAILABLE

    def _unavailable_reason(
        self,
        capability: CapabilityDefinition,
    ) -> str:
        if (
            capability.status
            != CapabilityStatus.AVAILABLE
            or self._providers is None
        ):
            return (
                "Current status: "
                f"{capability.status.value}"
            )

        provider = self._providers.get(
            capability.provider
        )

        if provider is None:
            return (
                "Provider is not registered: "
                f"{capability.provider}"
            )

        return (
            f"Provider {provider.provider_id} is "
            f"{provider.state.value} with health "
            f"{provider.health.value}."
        )

    def get_status(
        self,
        name: str,
    ) -> Optional[CapabilityStatus]:
        """
        Return the capability's effective status, taking
        its provider into account.
        """

        capability = self.get(name)

        if capability is None:
            return None

        return self._effective_status(
            capability
        )

    def is_available(
        self,
        name: str,
    ) -> bool:
        """
        Return True only when a capability exists, is
        marked AVAILABLE, and its provider is usable.
        """

        return (
            self.get_status(name)
            == CapabilityStatus.AVAILABLE
        )

    def execute(
        self,
        name: str,
        *args,
        **kwargs,
    ) -> CapabilityExecutionResult:
        """
        Execute an available capability.

        Exceptions from one provider are contained here
        and returned as structured failures rather than
        being allowed to crash unrelated Freya systems.
        """

        capability = self.get(name)

        if capability is None:
            return CapabilityExecutionResult(
                success=False,
                capability_name=(
                    self._normalize_name(name)
                ),
                error=(
                    "Capability is not registered."
                ),
            )

        if (
            self._effective_status(capability)
            != CapabilityStatus.AVAILABLE
        ):
            return CapabilityExecutionResult(
                success=False,
                capability_name=capability.name,
                provider=capability.provider,
                error=(
                    "Capability is not available. "
                    + self._unavailable_reason(
                        capability
                    )
                ),
            )

        try:
            result = capability.handler(
                *args,
                **kwargs,
            )

            return CapabilityExecutionResult(
                success=True,
                capability_name=capability.name,
                provider=capability.provider,
                result=result,
            )

        except Exception as exc:
            return CapabilityExecutionResult(
                success=False,
                capability_name=capability.name,
                provider=capability.provider,
                error=str(exc),
            )

    def describe_capabilities(
        self,
    ) -> List[dict]:
        """
        Return serializable capability metadata.

        This will later be useful for:
            - Freya planning
            - diagnostics
            - remote nodes
            - admin interfaces
            - capability discovery
        """

        descriptions = []

        for capability in (
            self.list_capabilities()
        ):
            descriptions.append(
                {
                    "name": capability.name,
                    "description": (
                        capability.description
                    ),
                    "provider": (
                        capability.provider
                    ),
                    "category": (
                        capability.category
                    ),
                    "status": (
                        self._effective_status(
                            capability
                        ).value
                    ),
                    "requires_gpu": (
                        capability.requires_gpu
                    ),
                    "destructive": (
                        capability.destructive
                    ),
                    "declaration": (
                        capability.declaration.as_dict()
                        if capability.declaration is not None
                        else None
                    ),
                    "aliases": list(
                        capability.aliases
                    ),
                    "optional_dependencies": list(
                        capability.optional_dependencies
                    ),
                    "metadata": dict(
                        capability.metadata
                    ),
                }
            )

        return descriptions