"""Image requests and memory reads go through the broker, or they do not run."""

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.conversation_memory import memories_for_screening
from freya.image.request import request_image
from freya.providers.image.comfyui import COMFYUI_PROVIDER, IMAGE_GENERATE


@pytest.fixture
def plane(tmp_path, fake_ollama):
    built = build_local_control_plane(tmp_path / "control.db", None)
    yield built
    built.close()


def audit_rows(plane, capability):
    return plane.store.execute(
        "SELECT kind, outcome FROM audit_event WHERE capability = ? ORDER BY event_id",
        (capability,),
    ).fetchall()


def comfyui_can_start(plane):
    provider = plane.providers.get(COMFYUI_PROVIDER)
    provider.metadata["can_start"] = True
    provider.health_handler = lambda: True
    plane.providers.check_health(COMFYUI_PROVIDER)


def test_an_image_request_is_audited_and_the_handler_is_the_only_generator(plane):
    comfyui_can_start(plane)
    seen = []
    plane.register_image_generation(
        lambda prompt: seen.append(prompt) or {"status": "generated"}
    )

    assert request_image(plane.broker, "a made-up fox") is True
    assert seen == ["a made-up fox"]
    assert [row["kind"] for row in audit_rows(plane, IMAGE_GENERATE)] == [
        "dispatch",
        "outcome",
    ]
    assert audit_rows(plane, IMAGE_GENERATE)[-1]["outcome"] == "succeeded"


def test_a_denied_image_request_does_not_generate(plane, capsys):
    comfyui_can_start(plane)
    seen = []
    plane.register_image_generation(lambda prompt: seen.append(prompt))
    plane.policy.add_rule("local-owner", IMAGE_GENERATE, "deny", "Test deny.")

    assert request_image(plane.broker, "a made-up fox") is False
    assert seen == []
    assert "a made-up fox" not in capsys.readouterr().out
    assert audit_rows(plane, IMAGE_GENERATE)[-1]["outcome"] == "denied"


def test_an_unavailable_image_request_does_not_generate(plane, capsys):
    seen = []
    plane.register_image_generation(lambda prompt: seen.append(prompt))
    provider = plane.providers.get(COMFYUI_PROVIDER)
    provider.metadata["can_start"] = False
    provider.health_handler = lambda: False
    plane.providers.check_health(COMFYUI_PROVIDER)
    plane.providers.check_health("image-service-local")

    assert request_image(plane.broker, "a made-up fox") is False
    assert seen == []
    assert audit_rows(plane, IMAGE_GENERATE)[-1]["outcome"] == "unavailable"


def test_a_failed_image_handler_is_audited_and_not_retried(plane, capsys):
    comfyui_can_start(plane)

    def fail(prompt):
        raise RuntimeError("The image could not be generated.")

    plane.register_image_generation(fail)

    assert request_image(plane.broker, "a made-up fox") is False
    assert "a made-up fox" not in capsys.readouterr().out
    assert audit_rows(plane, IMAGE_GENERATE)[-1]["outcome"] == "failed-known"


def test_a_missing_broker_does_not_generate(capsys):
    assert request_image(None, "a made-up fox") is False
    assert "a made-up fox" not in capsys.readouterr().out


def test_screening_reads_memory_through_the_broker(plane):
    seen = []

    def recall(query=None, for_reply=False, for_screening=False):
        seen.append(for_screening)
        return [("7", "a made-up fact")]

    plane.register_conversation_recall(recall)

    assert memories_for_screening(plane.broker) == [("7", "a made-up fact")]
    assert seen == [True]
    rows = audit_rows(plane, "conversation.recall")
    assert [row["kind"] for row in rows] == ["dispatch", "outcome"]
    assert rows[-1]["outcome"] == "succeeded"

    plane.policy.add_rule(
        "local-owner", "conversation.recall", "deny", "Test deny."
    )
    with pytest.raises(RuntimeError, match="could not be read"):
        memories_for_screening(plane.broker)
    assert seen == [True]
    assert audit_rows(plane, "conversation.recall")[-1]["outcome"] == "denied"


def test_a_missing_broker_does_not_read_memories_for_screening():
    with pytest.raises(RuntimeError, match="could not be read"):
        memories_for_screening(None)


def test_direct_image_and_memory_bypasses_are_rejected():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "freya"
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

    for path in root.rglob("*.py"):
        if "evaluation" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root).as_posix()

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
