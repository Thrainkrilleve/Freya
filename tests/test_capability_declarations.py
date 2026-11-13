"""
Every registered capability carries a complete declaration, and a
consequential one needs a rule that names it.
"""

from types import SimpleNamespace

from freya.control_plane import build_local_control_plane
from freya.core.capabilities import (
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
    declare,
)
from freya.core.audit import SqliteAuditLog
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.core.providers import ProviderDefinition, ProviderRegistry
from freya.decider.service import register_decide
from freya.media.service import MediaService
from freya.providers.model.claude_plan import register_claude_plan_capability
from freya.sandbox.contract import SandboxProfile
from freya.sandbox.service import register_code_run
from freya.spatial_avatar.service import AvatarService
from freya.tools.file_tools import register_file_reader_capabilities
from freya.tools.world_view import (
    WORLD_CAMERAS,
    WORLD_MILITARY,
    WORLD_SHOW,
    register_world_view_capabilities,
)


def test_registration_rejects_an_incomplete_declaration():
    registry = CapabilityRegistry()
    incomplete = CapabilityDefinition(
        name="system.info",
        description="Reads a fact.",
        provider="test",
        handler=lambda: None,
    )

    try:
        registry.register(incomplete)
    except ValueError as exc:
        assert "declaration" in str(exc)
    else:
        raise AssertionError("an incomplete declaration was registered")

    assert registry.list_capabilities() == []

    blank = CapabilityDefinition(
        name="system.info",
        description="   ",
        provider="test",
        handler=lambda: None,
        declaration=declare(("none", "nothing")),
    )

    try:
        registry.register(blank)
    except ValueError as exc:
        assert "description" in str(exc)
    else:
        raise AssertionError("a blank description was registered")

    contradictory = CapabilityDefinition(
        name="files.delete",
        description="Deletes a file.",
        provider="test",
        handler=lambda: None,
        destructive=True,
        declaration=declare(("filesystem", "a file"), effect="read-only"),
    )

    try:
        registry.register(contradictory)
    except ValueError as exc:
        assert "consequential" in str(exc)
    else:
        raise AssertionError("a destructive read-only capability was registered")

    assert registry.list_capabilities() == []


def _assert_complete(capability):
    declaration = capability.declaration
    assert capability.description.strip()
    assert declaration is not None
    assert declaration.effect in ("read-only", "consequential")
    assert declaration.accesses
    assert all(access.kind and access.detail for access in declaration.accesses)
    if capability.requires_gpu:
        assert "gpu" in declaration.requires
    if capability.destructive:
        assert declaration.consequential
    described = declaration.as_dict()
    assert described["effect"] == declaration.effect
    assert described["accesses"]


def test_voice_and_task_capabilities_are_declared_at_startup(tmp_path, monkeypatch, fake_ollama):
    """The paths main actually registers: the voice gate, and tasks when enabled."""
    from freya.tasks.lifecycle import TASK_CAPABILITIES
    from freya.voice.gate import (
        VOICE_CAPABILITIES,
        VOICE_SPEAK,
        VOICE_STUDIO,
        VOICE_TURN,
        VOICE_WAKE,
        install_voice_gate,
    )

    class Voice:
        def speak(self, text):
            return None

    monkeypatch.delenv("FREYA_TASKS", raising=False)
    plain = build_local_control_plane(tmp_path / "off.db")

    try:
        install_voice_gate(plain, Voice(), lambda message: {"state": {}})
        found = {item.name: item for item in plain.capabilities.list_capabilities()}

        assert set(VOICE_CAPABILITIES) <= set(found)
        assert "task.create" not in found
        assert "code.run" not in found

        for name in VOICE_CAPABILITIES:
            _assert_complete(found[name])

        assert not found[VOICE_WAKE].declaration.consequential
        assert not found[VOICE_TURN].declaration.consequential
        assert found[VOICE_SPEAK].declaration.consequential
        assert found[VOICE_STUDIO].declaration.consequential
    finally:
        plain.close()

    monkeypatch.setenv("FREYA_TASKS", "1")
    enabled = build_local_control_plane(tmp_path / "on.db")

    try:
        found = {item.name: item for item in enabled.capabilities.list_capabilities()}

        assert set(TASK_CAPABILITIES) <= set(found)
        assert "code.run" not in found
        assert "decide" not in found

        for name in TASK_CAPABILITIES:
            _assert_complete(found[name])
            assert found[name].declaration.consequential
    finally:
        enabled.close()


def test_the_control_plane_registry_is_fully_declared(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db")

    try:
        found = plane.capabilities.list_capabilities()
        assert found
        assert {item.name for item in found}.isdisjoint({"code.run", "decide"})

        for capability in found:
            _assert_complete(capability)
    finally:
        plane.close()


def test_every_other_producer_declares_its_capabilities():
    registry = CapabilityRegistry()
    reader = SimpleNamespace(
        list=lambda **kwargs: {},
        read=lambda **kwargs: {},
        search=lambda **kwargs: {},
    )
    register_file_reader_capabilities(registry, reader)

    view = SimpleNamespace(**{
        name: (lambda *args, **kwargs: {})
        for name in (
            "overview", "aircraft", "ships", "satellite_pass",
            "earthquakes", "fires", "storms", "launches", "cameras",
            "military", "show",
        )
    })
    register_world_view_capabilities(registry, view)

    for capability in MediaService.capabilities(object.__new__(MediaService)):
        registry.register(capability)

    for capability in AvatarService.capabilities(object.__new__(AvatarService)):
        registry.register(capability)

    class Sandbox:
        def profile(self):
            return SandboxProfile("test", False, False, False, True, "container")

        def ready(self):
            return True

        def run(self, request):
            raise AssertionError("the declaration test must not run code")

    register_code_run(registry, Sandbox())

    class Decider:
        decider_id = "test"

        def ready(self):
            return True

        def decide(self, point, text):
            raise AssertionError("the declaration test must not decide")

    register_decide(registry, Decider())
    register_claude_plan_capability(registry, lambda text: None)

    names = {item.name for item in registry.list_capabilities()}
    assert {"files.list", "files.read", "files.search", WORLD_SHOW, WORLD_CAMERAS, WORLD_MILITARY} <= names
    assert "code.run" in names
    assert "decide" in names
    assert "cloud.anthropic_plan.ask" in names
    assert "media.delete" in names
    assert "avatar.prepare" in names

    for capability in registry.list_capabilities():
        _assert_complete(capability)

    show = registry.get(WORLD_SHOW)
    cameras = registry.get(WORLD_CAMERAS)
    assert show.declaration.consequential
    assert not cameras.declaration.consequential
    assert registry.get("code.run").declaration.consequential
    assert not registry.get("decide").declaration.consequential
    assert registry.get("media.delete").destructive


def test_consequential_needs_an_exact_rule_and_read_only_stays_deny_by_default(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    providers = ProviderRegistry()
    providers.register(
        ProviderDefinition(
            provider_id="test",
            name="Test",
            capabilities=["files.read", "files.delete"],
            start_handler=lambda: None,
            health_handler=lambda: True,
        )
    )
    providers.start("test")
    providers.check_health("test")
    registry = CapabilityRegistry(providers)
    called = []

    def read():
        called.append("read")
        return {"ok": True}

    def delete():
        called.append("delete")
        return {"ok": True}

    registry.register(CapabilityDefinition(
        name="files.read",
        description="Reads one file.",
        provider="test",
        handler=read,
        declaration=declare(("filesystem", "one opened file")),
    ))
    registry.register(CapabilityDefinition(
        name="files.delete",
        description="Deletes one file.",
        provider="test",
        handler=delete,
        destructive=True,
        declaration=declare(
            ("filesystem", "one file"),
            effect="consequential",
        ),
    ))
    policy = SqlitePolicyEngine(store)
    audit = SqliteAuditLog(store, checkpoint_interval=0)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit)

    assert broker.invoke("files.read").outcome == "denied"
    assert broker.invoke("files.delete").outcome == "denied"
    assert called == []

    policy.add_rule("local-owner", "*", "allow", "Everything.")
    policy.add_rule("*", "files.delete", "allow", "Anyone may delete.")

    assert broker.invoke("files.read").outcome == "succeeded"
    refused = broker.invoke("files.delete")
    assert refused.outcome == "denied"
    assert "consequential" in refused.error
    assert called == ["read"]

    policy.add_rule("local-owner", "files.delete", "allow", "This owner may delete.")
    assert broker.invoke("files.delete").outcome == "succeeded"
    assert called == ["read", "delete"]
    store.close()
