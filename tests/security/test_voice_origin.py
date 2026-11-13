"""
A spoken turn uses the broker for its own gate and the same tool host
as a typed turn. It cannot run a tool the typed path cannot run.
"""

import json

from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import (
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
)
from freya.core.capabilities.broker import LOCAL_OWNER
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.core.providers import ProviderDefinition, ProviderRegistry
from freya.tools.model_tools import MODEL_TOOL, ModelToolHost
from freya.voice.gate import (
    VOICE_SPEAK,
    VOICE_TURN,
    install_voice_gate,
)


class _Voice:
    def __init__(self):
        self.spoken = []

    def speak(self, text):
        self.spoken.append(text)


def _plane(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    providers = ProviderRegistry()
    providers.register(
        ProviderDefinition(
            provider_id="system-tools-local",
            name="Test tools",
            capabilities=["system.info", "code.run"],
            start_handler=lambda: None,
            health_handler=lambda: True,
        )
    )
    providers.start("system-tools-local")
    providers.check_health("system-tools-local")

    registry = CapabilityRegistry(providers)
    called = []

    def info():
        called.append("system.info")
        return {"hostname": "test-host"}

    def code_run():
        called.append("code.run")
        return {"ran": True}

    registry.register(
        CapabilityDefinition(
            name="system.info",
            description="A read-only fact about this computer.",
            provider="system-tools-local",
            handler=info,
            metadata={MODEL_TOOL: True},
        )
    )
    registry.register(
        CapabilityDefinition(
            name="code.run",
            description="Runs code. Not offered to the model.",
            provider="system-tools-local",
            handler=code_run,
            destructive=True,
        )
    )

    policy = SqlitePolicyEngine(store)
    audit = SqliteAuditLog(store, checkpoint_interval=0)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit)

    class Plane:
        pass

    plane = Plane()
    plane.providers = providers
    plane.capabilities = registry
    plane.policy = policy
    plane.broker = broker
    plane.store = store
    return plane, called


def _rows(store, capability):
    return [
        dict(row)
        for row in store.execute(
            "SELECT * FROM audit_event WHERE capability = ? ORDER BY event_id",
            (capability,),
        ).fetchall()
    ]


def test_a_voice_tool_call_is_denied_audited_and_no_wider_than_typed(tmp_path):
    plane, called = _plane(tmp_path)
    voice = _Voice()
    studio_calls = []
    gate = install_voice_gate(
        plane,
        voice,
        lambda message: studio_calls.append(message) or {"state": {}, "clip": None},
        seed=False,
    )
    host = ModelToolHost(plane.capabilities, plane.broker)

    assert gate.accept_wake() is False
    assert gate.accept_turn("what is the cpu") is False
    assert called == []
    assert voice.spoken == []
    assert studio_calls == []
    voice.speak("hello")
    assert voice.spoken == []

    # The model sees system_info; the capability remains system.info.
    typed = json.loads(host.run("system_info"))
    spoken = json.loads(host.run("system_info"))
    hidden = json.loads(host.run("code.run"))

    assert "Not authorized" in typed["error"]
    assert spoken == typed
    assert "not available" in hidden["error"].lower()
    assert called == []

    denied_tool = _rows(plane.store, "system.info")
    assert denied_tool
    assert all(row["outcome"] == "denied" for row in denied_tool if row["kind"] == "decision")
    assert _rows(plane.store, VOICE_TURN)
    assert _rows(plane.store, VOICE_SPEAK)

    # The voice gate may be allowed and the tool still refused.
    plane.policy.add_rule(LOCAL_OWNER.actor, VOICE_TURN, "allow", "Hear.")
    assert gate.accept_turn("what is the cpu") is True
    still_denied = json.loads(host.run("system_info"))
    assert still_denied == typed
    assert called == []

    plane.policy.add_rule(LOCAL_OWNER.actor, "system.info", "allow", "Read.")
    typed_allowed = json.loads(host.run("system_info"))
    spoken_allowed = json.loads(host.run("system_info"))

    assert typed_allowed == spoken_allowed == {"hostname": "test-host"}
    assert called == ["system.info", "system.info"]
    assert json.loads(host.run("code.run"))["error"]
    assert "code.run" not in called

    plane.store.close()


def test_speech_and_the_studio_run_only_after_a_grant(tmp_path):
    plane, _called = _plane(tmp_path)
    voice = _Voice()
    seen = []

    def studio(message):
        seen.append(message["action"])
        return {"state": {"ready": True}, "clip": None, "reference": None, "error": None}

    gate = install_voice_gate(plane, voice, studio, seed=True)

    voice.speak("yes")
    answer = gate.studio({"action": "state"})

    assert voice.spoken == ["yes"]
    assert seen == ["state"]
    assert answer["state"]["ready"] is True
    assert any(row["decision"] == "allow" for row in _rows(plane.store, VOICE_SPEAK))
    plane.store.close()
