import pytest

from freya.core.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityStatus,
    declare,
)
from freya.core.providers import (
    ProviderDefinition,
    ProviderHealth,
    ProviderRegistry,
    ProviderState,
)
from freya.tools.system_tools import (
    SYSTEM_TOOL_CAPABILITIES,
    SYSTEM_TOOLS_PROVIDER,
    create_system_tools_provider,
    register_system_tools,
)


def failing():
    raise RuntimeError("handler exploded")


@pytest.fixture
def providers():
    registry = ProviderRegistry()
    registry.register(
        ProviderDefinition(
            provider_id="ollama-local",
            name="Test provider",
            capabilities=["llm.chat"],
            state=ProviderState.RUNNING,
            health=ProviderHealth.HEALTHY,
        )
    )
    return registry


@pytest.fixture
def calls():
    return []


@pytest.fixture
def capabilities(providers, calls):
    registry = CapabilityRegistry(providers)
    registry.register(
        CapabilityDefinition(
            name="llm.chat",
            description="Test capability.",
            provider="ollama-local",
            handler=lambda: calls.append(1) or "ok",
            declaration=declare(("none", "test fixture, no resource")),
        )
    )
    return registry


def test_available_when_provider_running_and_healthy(capabilities, calls):
    assert capabilities.get_status("llm.chat") == CapabilityStatus.AVAILABLE
    assert capabilities.is_available("llm.chat")

    result = capabilities.execute("llm.chat")

    assert result.success is True
    assert calls == [1]


@pytest.mark.parametrize(
    "state, health, expected",
    [
        (ProviderState.RUNNING, ProviderHealth.HEALTHY, CapabilityStatus.AVAILABLE),
        (ProviderState.RUNNING, ProviderHealth.DEGRADED, CapabilityStatus.DEGRADED),
        (ProviderState.RUNNING, ProviderHealth.UNKNOWN, CapabilityStatus.UNAVAILABLE),
        (ProviderState.RUNNING, ProviderHealth.UNHEALTHY, CapabilityStatus.UNAVAILABLE),
        (ProviderState.RUNNING, ProviderHealth.OFFLINE, CapabilityStatus.UNAVAILABLE),
        (ProviderState.STOPPED, ProviderHealth.HEALTHY, CapabilityStatus.UNAVAILABLE),
        (ProviderState.STARTING, ProviderHealth.HEALTHY, CapabilityStatus.UNAVAILABLE),
        (ProviderState.STOPPING, ProviderHealth.HEALTHY, CapabilityStatus.UNAVAILABLE),
        (ProviderState.FAILED, ProviderHealth.HEALTHY, CapabilityStatus.UNAVAILABLE),
        (ProviderState.DISABLED, ProviderHealth.HEALTHY, CapabilityStatus.UNAVAILABLE),
    ],
)
def test_status_follows_provider_state_and_health(
    capabilities, providers, calls, state, health, expected
):
    providers.set_state("ollama-local", state)
    providers.set_health("ollama-local", health)

    assert capabilities.get_status("llm.chat") == expected
    assert capabilities.is_available("llm.chat") is (
        expected == CapabilityStatus.AVAILABLE
    )
    assert capabilities.describe_capabilities()[0]["status"] == expected.value

    result = capabilities.execute("llm.chat")

    if expected == CapabilityStatus.AVAILABLE:
        assert result.success is True
    else:
        assert calls == []
        assert result.success is False
        assert "ollama-local" in result.error
        assert state.value in result.error
        assert health.value in result.error


def test_unregistered_provider_makes_capability_unavailable(providers, calls):
    capabilities = CapabilityRegistry(providers)
    capabilities.register(
        CapabilityDefinition(
            name="image.generate",
            description="Test capability.",
            provider="comfyui-local",
            handler=lambda: calls.append(1),
            declaration=declare(("none", "test fixture, no resource")),
        )
    )

    result = capabilities.execute("image.generate")

    assert capabilities.get_status("image.generate") == CapabilityStatus.UNAVAILABLE
    assert calls == []
    assert result.success is False
    assert "Provider is not registered: comfyui-local" in result.error


def test_availability_recovers_with_provider(capabilities, providers):
    providers.set_health("ollama-local", ProviderHealth.UNHEALTHY)
    assert not capabilities.is_available("llm.chat")

    providers.set_health("ollama-local", ProviderHealth.HEALTHY)
    assert capabilities.is_available("llm.chat")


def test_failed_health_check_makes_capability_unavailable(capabilities, providers):
    providers.get("ollama-local").health_handler = failing

    providers.check_health("ollama-local")

    assert capabilities.get_status("llm.chat") == CapabilityStatus.UNAVAILABLE


def test_failed_start_makes_capability_unavailable(capabilities, providers):
    providers.set_state("ollama-local", ProviderState.STOPPED)
    providers.get("ollama-local").start_handler = failing

    providers.start("ollama-local")

    assert capabilities.get_status("llm.chat") == CapabilityStatus.UNAVAILABLE


def test_unregistering_provider_makes_capability_unavailable(
    capabilities, providers
):
    providers.unregister("ollama-local")

    assert not capabilities.is_available("llm.chat")


@pytest.mark.parametrize(
    "status",
    [
        CapabilityStatus.DEGRADED,
        CapabilityStatus.UNAVAILABLE,
        CapabilityStatus.DISABLED,
    ],
)
def test_own_status_wins_over_healthy_provider(capabilities, calls, status):
    capabilities.set_status("llm.chat", status)

    result = capabilities.execute("llm.chat")

    assert capabilities.get_status("llm.chat") == status
    assert calls == []
    assert status.value in result.error


def test_get_status_unknown_capability(capabilities):
    assert capabilities.get_status("missing") is None


def test_without_provider_registry_only_own_status_counts():
    capabilities = CapabilityRegistry()
    capabilities.register(
        CapabilityDefinition(
            name="llm.chat",
            description="Test capability.",
            provider="never-registered",
            handler=lambda: "ok",
            declaration=declare(("none", "test fixture, no resource")),
        )
    )

    assert capabilities.get_status("llm.chat") == CapabilityStatus.AVAILABLE
    assert capabilities.execute("llm.chat").success is True


def test_system_tools_provider_lifecycle_controls_system_tools():
    providers = ProviderRegistry()
    capabilities = CapabilityRegistry(providers)
    register_system_tools(capabilities)

    # Not registered, then registered but not started.
    assert not capabilities.is_available("system.info")
    providers.register(create_system_tools_provider())
    assert not capabilities.is_available("system.info")

    # Running, but health has not been checked yet.
    assert providers.start(SYSTEM_TOOLS_PROVIDER).success
    assert not capabilities.is_available("system.info")

    assert providers.check_health(SYSTEM_TOOLS_PROVIDER).success
    assert all(capabilities.is_available(name) for name in SYSTEM_TOOL_CAPABILITIES)
    assert capabilities.execute("system.info").success is True

    assert providers.stop(SYSTEM_TOOLS_PROVIDER).success
    assert not any(
        capabilities.is_available(name) for name in SYSTEM_TOOL_CAPABILITIES
    )


def test_system_tools_provider_advertises_its_capabilities():
    providers = ProviderRegistry()
    capabilities = CapabilityRegistry(providers)
    providers.register(create_system_tools_provider())
    register_system_tools(capabilities)

    advertised = providers.get(SYSTEM_TOOLS_PROVIDER).capabilities
    registered = [
        c.name
        for c in capabilities.list_capabilities()
        if c.provider == SYSTEM_TOOLS_PROVIDER
    ]

    assert sorted(advertised) == sorted(registered)
    assert providers.providers_for_capability("system.gpu") == [
        providers.get(SYSTEM_TOOLS_PROVIDER)
    ]
