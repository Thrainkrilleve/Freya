"""Image requests and memory reads go through the broker, or they do not run.

The runtime checks use Core only, so they run where the foundation
jobs do: pytest and the store libraries, without the image provider.
"""

from pathlib import Path

import pytest

from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import (
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityStatus,
    declare,
)
from freya.core.conversation_memory import memories_for_screening
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.image.request import IMAGE_GENERATE, request_image


ROOT = Path(__file__).resolve().parents[1] / "src" / "freya"


@pytest.fixture
def broker(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    policy = SqlitePolicyEngine(store)
    audit = SqliteAuditLog(store, checkpoint_interval=0)
    audit.start()
    registry = CapabilityRegistry()
    built = CapabilityBroker(registry, policy, audit)
    built.store = store
    yield built
    store.close()


def allow(broker, name, handler):
    broker._registry.register(
        CapabilityDefinition(
            name=name,
            description="Test capability.",
            provider="test-local",
            handler=handler,
            declaration=declare(("none", "test fixture, no resource")),
        )
    )
    broker._policy.add_rule("local-owner", name, "allow", "Test allow.")


def rows(broker, capability):
    return broker.store.execute(
        "SELECT kind, outcome FROM audit_event WHERE capability = ? ORDER BY event_id",
        (capability,),
    ).fetchall()


def test_an_image_request_is_audited(broker):
    seen = []
    allow(broker, IMAGE_GENERATE, lambda prompt: seen.append(prompt) or {"status": "generated"})

    assert request_image(broker, "a made-up fox") is True
    assert seen == ["a made-up fox"]
    assert [row["kind"] for row in rows(broker, IMAGE_GENERATE)] == ["dispatch", "outcome"]
    assert rows(broker, IMAGE_GENERATE)[-1]["outcome"] == "succeeded"


def test_a_denied_image_request_does_not_generate(broker, capsys):
    seen = []
    allow(broker, IMAGE_GENERATE, lambda prompt: seen.append(prompt))
    broker._policy.add_rule("local-owner", IMAGE_GENERATE, "deny", "Test deny.")

    assert request_image(broker, "a made-up fox") is False
    assert seen == []
    assert "a made-up fox" not in capsys.readouterr().out
    assert rows(broker, IMAGE_GENERATE)[-1]["outcome"] == "denied"


def test_an_unavailable_image_request_does_not_generate(broker):
    seen = []
    allow(broker, IMAGE_GENERATE, lambda prompt: seen.append(prompt))
    broker._registry.set_status(IMAGE_GENERATE, CapabilityStatus.UNAVAILABLE)

    assert request_image(broker, "a made-up fox") is False
    assert seen == []
    assert rows(broker, IMAGE_GENERATE)[-1]["outcome"] == "unavailable"


def test_a_failed_image_handler_is_audited(broker, capsys):
    def fail(prompt):
        raise RuntimeError("The image could not be generated.")

    allow(broker, IMAGE_GENERATE, fail)

    assert request_image(broker, "a made-up fox") is False
    assert "a made-up fox" not in capsys.readouterr().out
    assert rows(broker, IMAGE_GENERATE)[-1]["outcome"] == "failed-known"


def test_a_missing_broker_does_not_generate(capsys):
    assert request_image(None, "a made-up fox") is False
    assert "a made-up fox" not in capsys.readouterr().out


def test_screening_reads_memory_through_the_broker(broker):
    seen = []

    def recall(query=None, for_reply=False, for_screening=False):
        seen.append(for_screening)
        return [("7", "a made-up fact")]

    allow(broker, "conversation.recall", recall)

    assert memories_for_screening(broker) == [("7", "a made-up fact")]
    assert seen == [True]
    recorded = rows(broker, "conversation.recall")
    assert [row["kind"] for row in recorded] == ["dispatch", "outcome"]
    assert recorded[-1]["outcome"] == "succeeded"

    broker._policy.add_rule("local-owner", "conversation.recall", "deny", "Test deny.")
    with pytest.raises(RuntimeError, match="could not be read"):
        memories_for_screening(broker)
    assert seen == [True]
    assert rows(broker, "conversation.recall")[-1]["outcome"] == "denied"


def test_a_missing_broker_does_not_read_memories_for_screening():
    with pytest.raises(RuntimeError, match="could not be read"):
        memories_for_screening(None)


def test_direct_image_and_memory_bypasses_are_rejected():
    image_calls = []
    memory_calls = []
    allowed_image = {
        "main.py",
        "image/image_generator.py",
        "providers/image/comfyui.py",
        "gpu/resource_manager.py",
    }
    allowed_memory = {
        "memory/memory_manager.py",
        "core/memory_commands.py",
        "core/conversation_memory.py",
    }
    image_name = (
        ROOT.joinpath("providers/image/comfyui.py")
        .read_text(encoding="utf-8")
        .split("IMAGE_GENERATE = ", 1)[1]
        .splitlines()[0]
    )
    request_name = (
        ROOT.joinpath("image/request.py")
        .read_text(encoding="utf-8")
        .split("IMAGE_GENERATE = ", 1)[1]
        .splitlines()[0]
    )
    assert image_name == request_name

    for path in ROOT.rglob("*.py"):
        if "evaluation" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()

        if relative not in allowed_image and (
            "image_generator.generate" in text
            or "image_generator.ensure_running" in text
            or "_queue_prompt(" in text
        ):
            image_calls.append(relative)

        if relative == "main.py":
            before, _, handler_and_after = text.partition("def generate_image_tool")
            handler, _, after = handler_and_after.partition("\n    def ")
            if before.count("run_image_generation(") != 1:
                image_calls.append("main.py outside the image handler")
            if "run_image_generation(" in after:
                image_calls.append("main.py after the image handler")
            if "run_image_generation(" not in handler:
                image_calls.append("main.py handler does not perform image generation")
            if "get_all_memories(" in text:
                memory_calls.append("main.py")

        if relative not in allowed_memory and "get_all_memories(" in text:
            memory_calls.append(relative)

    assert image_calls == []
    assert memory_calls == []
