import pytest

from freya.core.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityStatus,
    declare,
)


def make_capability(name="llm.chat", handler=None, **overrides):
    if "declaration" in overrides:
        declaration = overrides.pop("declaration")
    else:
        declaration = declare(
            ("none", "test fixture, no resource"),
            requires=("gpu",) if overrides.get("requires_gpu") else (),
            effect=(
                "consequential" if overrides.get("destructive") else "read-only"
            ),
        )

    return CapabilityDefinition(
        name=name,
        description="Test capability.",
        provider="test-provider",
        handler=handler or (lambda *args, **kwargs: "ok"),
        declaration=declaration,
        **overrides,
    )


@pytest.fixture
def registry():
    return CapabilityRegistry()


def test_register_normalizes_name(registry):
    capability = make_capability("  LLM.Chat ")

    registry.register(capability)

    assert capability.name == "llm.chat"
    assert registry.exists("llm.chat")
    assert registry.exists(" LLM.CHAT ")
    assert registry.get("Llm.Chat") is capability


@pytest.mark.parametrize("name", ["", "   "])
def test_register_rejects_empty_name(registry, name):
    with pytest.raises(ValueError):
        registry.register(make_capability(name))


def test_register_rejects_duplicate_name(registry):
    first = make_capability()
    registry.register(first)

    with pytest.raises(ValueError):
        registry.register(make_capability(" LLM.CHAT "))

    assert registry.get("llm.chat") is first


def test_register_with_replace_swaps_capability_and_aliases(registry):
    first = make_capability(aliases=["chat", "talk"])
    second = make_capability(aliases=["chat", "converse"])

    registry.register(first)
    registry.register(second, replace=True)

    assert registry.list_capabilities() == [second]
    assert registry.get("chat") is second
    assert registry.get("converse") is second
    assert not registry.exists("talk")


def test_replace_true_registers_new_capability(registry):
    capability = make_capability()

    registry.register(capability, replace=True)

    assert registry.get("llm.chat") is capability


def test_lookup_by_alias(registry):
    capability = make_capability(
        aliases=["Chat", " talk to model ", "", "chat", "LLM.Chat"]
    )

    registry.register(capability)

    assert capability.aliases == ["chat", "talk to model"]
    assert registry.get("chat") is capability
    assert registry.get("TALK TO MODEL") is capability
    assert registry.exists("Chat")
    assert registry.is_available("chat")


def test_alias_cannot_shadow_registered_capability(registry):
    registry.register(make_capability("system.time"))

    with pytest.raises(ValueError):
        registry.register(make_capability("system.clock", aliases=["system.time"]))


def test_alias_cannot_be_registered_twice(registry):
    registry.register(make_capability("system.time", aliases=["clock"]))

    with pytest.raises(ValueError):
        registry.register(make_capability("system.clock", aliases=["Clock"]))


def test_name_cannot_shadow_registered_alias(registry):
    first = make_capability("system.time", aliases=["clock"])
    registry.register(first)

    with pytest.raises(ValueError):
        registry.register(make_capability("clock"))

    assert registry.get("clock") is first


def test_failed_registration_leaves_registry_unchanged(registry):
    first = make_capability("system.time", aliases=["clock"])
    registry.register(first)

    with pytest.raises(ValueError):
        registry.register(
            make_capability("system.disk", aliases=["disk", "clock"])
        )

    assert registry.list_capabilities() == [first]
    assert not registry.exists("system.disk")
    assert not registry.exists("disk")


def test_failed_replace_keeps_previous_capability(registry):
    first = make_capability("system.time", aliases=["clock"])
    other = make_capability("system.disk", aliases=["disk"])
    registry.register(first)
    registry.register(other)

    with pytest.raises(ValueError):
        registry.register(
            make_capability("system.time", aliases=["disk"]),
            replace=True,
        )

    assert registry.get("system.time") is first
    assert registry.get("clock") is first
    assert registry.get("disk") is other


def test_unregister_removes_capability_and_its_aliases(registry):
    registry.register(make_capability("system.time", aliases=["clock", "time"]))
    registry.register(make_capability("system.disk", aliases=["disk"]))

    assert registry.unregister("Clock") is True

    assert not registry.exists("system.time")
    assert not registry.exists("clock")
    assert not registry.exists("time")
    assert registry.exists("disk")
    assert registry.unregister("system.time") is False


def test_alias_can_be_reused_after_unregister(registry):
    registry.register(make_capability("system.time", aliases=["clock"]))
    registry.unregister("system.time")
    replacement = make_capability("system.clock", aliases=["clock"])

    registry.register(replacement)

    assert registry.get("clock") is replacement


def test_execute_by_alias_reports_canonical_name(registry):
    registry.register(
        make_capability("system.time", handler=lambda: "12:00", aliases=["clock"])
    )

    result = registry.execute("Clock")

    assert result.success is True
    assert result.capability_name == "system.time"
    assert result.result == "12:00"


def test_get_unknown_returns_none(registry):
    assert registry.get("missing") is None
    assert not registry.exists("missing")


def test_unregister(registry):
    registry.register(make_capability())

    assert registry.unregister(" LLM.CHAT ") is True
    assert not registry.exists("llm.chat")
    assert registry.unregister("llm.chat") is False


def test_list_capabilities_returns_copy(registry):
    registry.register(make_capability("a"))
    registry.register(make_capability("b"))

    listed = registry.list_capabilities()
    listed.clear()

    assert [c.name for c in registry.list_capabilities()] == ["a", "b"]


def test_new_capability_is_available_by_default(registry):
    registry.register(make_capability())

    assert registry.is_available("llm.chat")


def test_is_available_false_for_unknown(registry):
    assert registry.is_available("missing") is False


@pytest.mark.parametrize(
    "status",
    [
        CapabilityStatus.DEGRADED,
        CapabilityStatus.UNAVAILABLE,
        CapabilityStatus.DISABLED,
    ],
)
def test_only_available_status_counts_as_available(registry, status):
    registry.register(make_capability())

    assert registry.set_status("llm.chat", status) is True
    assert registry.get("llm.chat").status == status
    assert registry.is_available("llm.chat") is False


def test_set_status_unknown_returns_false(registry):
    assert registry.set_status("missing", CapabilityStatus.DISABLED) is False


def test_execute_passes_arguments_and_returns_result(registry):
    calls = []

    def handler(*args, **kwargs):
        calls.append((args, kwargs))
        return {"answer": 42}

    registry.register(make_capability(handler=handler))

    result = registry.execute(" LLM.Chat ", "hello", temperature=0)

    assert calls == [(("hello",), {"temperature": 0})]
    assert result.success is True
    assert result.capability_name == "llm.chat"
    assert result.provider == "test-provider"
    assert result.result == {"answer": 42}
    assert result.error is None


def test_execute_unknown_capability(registry):
    result = registry.execute(" Missing ")

    assert result.success is False
    assert result.capability_name == "missing"
    assert result.provider is None
    assert result.error == "Capability is not registered."


@pytest.mark.parametrize(
    "status",
    [
        CapabilityStatus.DEGRADED,
        CapabilityStatus.UNAVAILABLE,
        CapabilityStatus.DISABLED,
    ],
)
def test_execute_does_not_call_handler_unless_available(registry, status):
    calls = []
    registry.register(
        make_capability(handler=lambda: calls.append(1), status=status)
    )

    result = registry.execute("llm.chat")

    assert calls == []
    assert result.success is False
    assert result.provider == "test-provider"
    assert status.value in result.error


def test_execute_contains_handler_exceptions(registry):
    def handler():
        raise RuntimeError("provider exploded")

    registry.register(make_capability(handler=handler))

    result = registry.execute("llm.chat")

    assert result.success is False
    assert result.capability_name == "llm.chat"
    assert result.provider == "test-provider"
    assert result.result is None
    assert result.error == "provider exploded"


def test_describe_capabilities_is_serializable_snapshot(registry):
    registry.register(
        make_capability(
            category="llm",
            status=CapabilityStatus.DEGRADED,
            requires_gpu=True,
            destructive=True,
            aliases=["chat"],
            optional_dependencies=["gpu"],
            metadata={"model": "test"},
        )
    )

    described = registry.describe_capabilities()

    assert described == [
        {
            "name": "llm.chat",
            "description": "Test capability.",
            "provider": "test-provider",
            "category": "llm",
            "status": "degraded",
            "requires_gpu": True,
            "destructive": True,
            "declaration": declare(
                ("none", "test fixture, no resource"),
                requires=("gpu",),
                effect="consequential",
            ).as_dict(),
            "aliases": ["chat"],
            "optional_dependencies": ["gpu"],
            "metadata": {"model": "test"},
        }
    ]

    described[0]["aliases"].append("changed")
    described[0]["optional_dependencies"].append("changed")
    described[0]["metadata"]["model"] = "changed"
    capability = registry.get("llm.chat")

    assert capability.aliases == ["chat"]
    assert capability.optional_dependencies == ["gpu"]
    assert capability.metadata == {"model": "test"}


def test_safety_flags_default_to_false(registry):
    registry.register(make_capability())
    capability = registry.get("llm.chat")

    assert capability.requires_gpu is False
    assert capability.destructive is False
    assert capability.aliases == []
