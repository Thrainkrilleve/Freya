import json

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.assistant import MAX_TOOL_CALLS, MAX_TOOL_ROUNDS, FreyaAssistant
from freya.core.capabilities import CapabilityDefinition, CapabilityStatus
from freya.memory import memory_manager
from freya.core.providers import ProviderHealth
from freya.providers.image.comfyui import COMFYUI_PROVIDER
from freya.tools.model_tools import (
    MODEL_TOOL,
    TOOL_PARAMETERS,
    ToolArgumentError,
    validate_arguments,
)


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


@pytest.fixture
def assistant(plane, tmp_path, monkeypatch):
    # Never touch the owner's real memory database.
    monkeypatch.setattr(memory_manager, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        memory_manager, "DATABASE_PATH", tmp_path / "memory.db"
    )
    return FreyaAssistant(tool_host=plane.model_tools)


def add_tool(plane, name, handler, **overrides):
    fields = {"metadata": {MODEL_TOOL: True}}
    fields.update(overrides)
    plane.capabilities.register(
        CapabilityDefinition(
            name=name,
            description="Test tool.",
            provider="system-tools-local",
            handler=handler,
            **fields,
        )
    )
    plane.policy.add_rule("local-owner", name, "allow", "Test allow.")


def audit_rows(plane, capability):
    return [
        dict(row)
        for row in plane.store.execute(
            "SELECT * FROM audit_event WHERE capability = ? ORDER BY event_id",
            (capability,),
        ).fetchall()
    ]


def stream_calls(fake_ollama):
    return [call for call in fake_ollama.calls if call[0] == "chat" and call[3]]


def comfyui_can_start(plane):
    # Image generation is offered only while ComfyUI is running or can
    # be started; whether it can depends on Comfy Desktop being
    # installed on this machine, which the tests must not rely on. The
    # health check is replaced too, so no test touches 127.0.0.1:8188.
    provider = plane.providers.get(COMFYUI_PROVIDER)
    provider.metadata["can_start"] = True
    provider.health_handler = lambda: ProviderHealth.UNHEALTHY


def test_only_marked_capabilities_are_offered(plane):
    names = {d["function"]["name"] for d in plane.model_tools.definitions()}

    assert names == {
        "system_info",
        "system_disk",
        "system_gpu",
        "system_cpu",
        "system_memory",
        "system_hardware",
        "system_sensors",
    }
    for definition in plane.model_tools.definitions():
        assert definition["type"] == "function"
        assert definition["function"]["description"]
        assert definition["function"]["parameters"] == {
            "type": "object",
            "properties": {},
        }


def test_destructive_and_unavailable_tools_are_not_offered(plane):
    calls = []
    add_tool(plane, "files.delete", lambda: calls.append(1), destructive=True)
    add_tool(plane, "test.offline", lambda: calls.append(1))
    plane.capabilities.set_status("test.offline", CapabilityStatus.DISABLED)

    names = {d["function"]["name"] for d in plane.model_tools.definitions()}

    assert "files_delete" not in names
    assert "test_offline" not in names
    assert "llm_chat" not in names

    for name in ("files_delete", "test_offline", "llm_chat", "llm.chat"):
        assert json.loads(plane.model_tools.run(name)) == {
            "error": "That tool is not available."
        }
    assert calls == []


@pytest.mark.parametrize("name", ["missing", "", None, 7, "system.info"])
def test_unknown_tool_requests_are_refused(plane, name):
    result = json.loads(plane.model_tools.run(name, {"path": "C:\\"}))

    assert result == {"error": "That tool is not available."}


def test_running_a_tool_goes_through_the_broker(plane):
    received = []
    add_tool(
        plane,
        "test.echo",
        lambda *args, **kwargs: received.append((args, kwargs)) or {"ok": True},
    )

    # Arguments invented by the model are not passed to the tool.
    result = plane.model_tools.run("test_echo", {"command": "rm -rf /"})

    rows = audit_rows(plane, "test.echo")

    assert json.loads(result) == {"ok": True}
    assert received == [((), {})]
    assert [row["kind"] for row in rows] == ["dispatch", "outcome"]
    assert rows[1]["outcome"] == "succeeded"
    assert rows[0]["actor"] == "local-owner"


def test_tool_failure_and_denial_are_returned_as_errors(plane):
    def broken():
        raise RuntimeError("sensor exploded")

    add_tool(plane, "test.broken", broken)

    assert json.loads(plane.model_tools.run("test_broken")) == {
        "error": "sensor exploded"
    }

    rule = next(
        r for r in plane.policy.list_rules() if r["capability"] == "system.cpu"
    )
    plane.policy.remove_rule(rule["rule_id"])

    denied = json.loads(plane.model_tools.run("system_cpu"))

    assert "Not authorized" in denied["error"]
    assert audit_rows(plane, "system.cpu")[-1]["outcome"] == "denied"


PROMPT_PARAMETER = {
    TOOL_PARAMETERS: {
        "prompt": {"description": "What to draw.", "max_length": 40},
    },
}


def test_tool_with_arguments_declares_them(plane):
    add_tool(
        plane,
        "test.draw",
        lambda prompt: {"drew": prompt},
        metadata={MODEL_TOOL: True, **PROMPT_PARAMETER},
    )

    definition = next(
        d["function"]
        for d in plane.model_tools.definitions()
        if d["function"]["name"] == "test_draw"
    )

    assert definition["parameters"] == {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to draw."}
        },
        "required": ["prompt"],
    }


def test_declared_arguments_are_cleaned_and_passed(plane):
    received = []
    add_tool(
        plane,
        "test.draw",
        lambda **kwargs: received.append(kwargs) or {"ok": True},
        metadata={MODEL_TOOL: True, **PROMPT_PARAMETER},
    )

    result = plane.model_tools.run(
        "test_draw",
        {"prompt": "  a red fox  ", "size": "huge", "command": "rm -rf /"},
    )

    assert json.loads(result) == {"ok": True}
    # Only the declared argument reaches the tool, trimmed.
    assert received == [{"prompt": "a red fox"}]


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        "a red fox",
        ["a red fox"],
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": 7},
        {"prompt": ["a", "fox"]},
        {"prompt": "x" * 41},
        {"description": "a red fox"},
    ],
)
def test_unacceptable_arguments_are_refused_without_running(plane, arguments):
    calls = []
    add_tool(
        plane,
        "test.draw",
        lambda **kwargs: calls.append(kwargs),
        metadata={MODEL_TOOL: True, **PROMPT_PARAMETER},
    )

    result = json.loads(plane.model_tools.run("test_draw", arguments))

    assert "error" in result
    assert calls == []
    assert audit_rows(plane, "test.draw") == []


def test_validate_arguments_directly():
    rules = {"prompt": {"max_length": 5}, "style": {}}

    assert validate_arguments({}, {"anything": 1}) == {}
    assert validate_arguments(None, None) == {}
    assert validate_arguments(rules, {"prompt": " fox ", "style": "oil"}) == {
        "prompt": "fox",
        "style": "oil",
    }

    with pytest.raises(ToolArgumentError) as error:
        validate_arguments(rules, {"prompt": "fox"})

    assert "style" in str(error.value)


def test_image_generation_is_offered_as_a_tool(plane):
    comfyui_can_start(plane)
    prompts = []
    plane.register_image_generation(
        lambda prompt: prompts.append(prompt) or {"status": "generated"}
    )

    definition = next(
        d["function"]
        for d in plane.model_tools.definitions()
        if d["function"]["name"] == "image_generate"
    )
    result = plane.model_tools.run(
        "image_generate", {"prompt": "a large black dog"}
    )

    assert definition["parameters"]["required"] == ["prompt"]
    assert "picture" in definition["description"]
    assert json.loads(result) == {"status": "generated"}
    assert prompts == ["a large black dog"]

    rows = audit_rows(plane, "image.generate")
    assert [row["kind"] for row in rows] == ["dispatch", "outcome"]
    assert rows[0]["provider"] == "image-service-local"
    # The prompt itself is not written to the audit log.
    assert "black dog" not in json.dumps(rows)


def test_image_tool_failure_is_reported_to_the_model(plane):
    comfyui_can_start(plane)
    def broken(prompt):
        raise RuntimeError("The image could not be generated.")

    plane.register_image_generation(broken)

    result = json.loads(
        plane.model_tools.run("image_generate", {"prompt": "a dog"})
    )

    assert result == {"error": "The image could not be generated."}


def test_assistant_generates_an_image_when_the_model_asks(
    assistant, plane, fake_ollama
):
    comfyui_can_start(plane)
    prompts = []
    plane.register_image_generation(
        lambda prompt: prompts.append(prompt) or {"status": "generated"}
    )
    fake_ollama.stream_script = [
        [("image_generate", {"prompt": "a large black dog"})],
        "Done. It is in the Images tab.",
    ]

    reply = "".join(
        assistant.respond_stream(
            "very good, can you do an image of a large black dog next please?"
        )
    )

    assert prompts == ["a large black dog"]
    assert reply.strip() == "Done. It is in the Images tab."


def test_assistant_answers_without_tools_when_none_are_needed(
    assistant, fake_ollama
):
    fake_ollama.stream_script = ["Paris is the capital."]

    reply = "".join(assistant.respond_stream("What is the capital of France?"))

    first = stream_calls(fake_ollama)[0]

    assert reply.strip() == "Paris is the capital."
    assert len(stream_calls(fake_ollama)) == 1
    assert {t["function"]["name"] for t in first[4]["tools"]} >= {"system_cpu"}
    assert any(
        m["role"] == "system" and "call tools" in m["content"] for m in first[2]
    )


def test_assistant_runs_requested_tool_and_answers_from_its_result(
    assistant, plane, fake_ollama
):
    add_tool(plane, "test.thermometer", lambda: {"temperature_c": 41})
    fake_ollama.stream_script = [
        [("test_thermometer", {})],
        "It is 41 degrees.",
    ]

    chunks = list(assistant.respond_stream("How warm is it in there?"))

    first, second = stream_calls(fake_ollama)
    followup = second[2]

    # Only text reaches the caller, so tool requests are never spoken.
    assert all(isinstance(chunk, str) for chunk in chunks)
    assert "".join(chunks).strip() == "It is 41 degrees."

    assert followup[-2] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "test_thermometer", "arguments": {}}}
        ],
    }
    assert followup[-1]["role"] == "tool"
    assert followup[-1]["tool_name"] == "test_thermometer"
    assert json.loads(followup[-1]["content"]) == {
        "temperature_c": 41,
        "_context": {"source": "tool:test_thermometer", "trust": "untrusted-tool-result", "truncated": False},
    }
    assert [r["outcome"] for r in audit_rows(plane, "test.thermometer")] == [
        None,
        "succeeded",
    ]

    # The conversation history keeps the question and answer, not the
    # tool exchange.
    assert assistant.messages[-2:] == [
        {"role": "user", "content": "How warm is it in there?"},
        {"role": "assistant", "content": "It is 41 degrees."},
    ]
    assert all(m["role"] != "tool" for m in assistant.messages)


def test_assistant_can_use_several_tools_in_one_turn(
    assistant, plane, fake_ollama
):
    add_tool(plane, "test.a", lambda: {"a": 1})
    add_tool(plane, "test.b", lambda: {"b": 2})
    fake_ollama.stream_script = [
        [("test_a", {}), ("test_b", {})],
        "Both checked.",
    ]

    reply = "".join(assistant.respond_stream("Check both."))

    followup = stream_calls(fake_ollama)[1][2]

    assert reply.strip() == "Both checked."
    results = [json.loads(m["content"]) for m in followup if m["role"] == "tool"]
    assert [{key: value for key, value in result.items() if key != "_context"} for result in results] == [{"a": 1}, {"b": 2}]
    assert all(result["_context"]["trust"] == "untrusted-tool-result" for result in results)


def test_assistant_tells_the_model_when_a_tool_is_refused(
    assistant, fake_ollama
):
    fake_ollama.stream_script = [
        [("format_drive", {"drive": "C:"})],
        "I can't do that.",
    ]

    reply = "".join(assistant.respond_stream("Wipe my disk."))

    tool_message = stream_calls(fake_ollama)[1][2][-1]

    assert reply.strip() == "I can't do that."
    result = json.loads(tool_message["content"])
    assert result["error"] == "That tool is not available."
    assert result["_context"]["trust"] == "untrusted-tool-result"


def test_assistant_stops_a_model_that_keeps_calling_tools(
    assistant, plane, fake_ollama
):
    runs = []
    add_tool(plane, "test.loop", lambda: runs.append(1) or {"again": True})
    fake_ollama.stream_script = [[("test_loop", {})]] * MAX_TOOL_ROUNDS + [
        "Here is what I found."
    ]

    reply = "".join(assistant.respond_stream("Keep checking."))

    calls = stream_calls(fake_ollama)

    assert reply.strip() == "Here is what I found."
    assert len(runs) == MAX_TOOL_ROUNDS
    assert len(calls) == MAX_TOOL_ROUNDS + 1
    # On the final round no tools are offered, so the model must answer.
    assert "tools" not in calls[-1][4]


def test_assistant_without_a_tool_host_offers_no_tools(
    plane, fake_ollama, tmp_path, monkeypatch
):
    monkeypatch.setattr(memory_manager, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        memory_manager, "DATABASE_PATH", tmp_path / "memory.db"
    )
    assistant = FreyaAssistant()

    reply = "".join(assistant.respond_stream("Hello"))

    assert reply.strip() == "Hello from the fake model."
    assert "tools" not in stream_calls(fake_ollama)[0][4]


def test_a_large_batch_cannot_bypass_the_total_call_limit(
    assistant, plane, fake_ollama
):
    runs = []
    add_tool(plane, "test.batch", lambda: runs.append(1) or {"ok": True})
    fake_ollama.stream_script = [
        [("test_batch", {})] * (MAX_TOOL_CALLS + 4),
        "Some checks could not be completed.",
    ]

    list(assistant.respond_stream("Check everything."))

    assert len(runs) == MAX_TOOL_CALLS
    followup = stream_calls(fake_ollama)[1]
    assert "tools" not in followup[4]
    results = [m for m in followup[2] if m["role"] == "tool"]
    assert len(results) == MAX_TOOL_CALLS + 4
    assert all("budget" in json.loads(m["content"])["error"] for m in results[-4:])


def test_call_budget_is_shared_across_rounds_and_reset_each_turn(
    assistant, plane, fake_ollama
):
    runs = []
    add_tool(plane, "test.batch", lambda: runs.append(1) or {"ok": True})
    fake_ollama.stream_script = [
        [("test_batch", {})] * (MAX_TOOL_CALLS - 1),
        [("test_batch", {})] * 3,
        "Finished the allowed checks.",
        [("test_batch", {})],
        "Checked again.",
    ]

    list(assistant.respond_stream("Check everything."))
    assert len(runs) == MAX_TOOL_CALLS
    list(assistant.respond_stream("Check once more."))
    assert len(runs) == MAX_TOOL_CALLS + 1


def test_refused_requests_also_consume_the_attempt_budget(
    assistant, plane, fake_ollama
):
    runs = []
    add_tool(plane, "test.actual", lambda: runs.append(1) or {"ok": True})
    fake_ollama.stream_script = [
        [("unknown", {})] * MAX_TOOL_CALLS,
        [("test_actual", {})],
        "I could not complete the checks.",
    ]

    list(assistant.respond_stream("Keep checking."))

    assert runs == []
    assert "tools" not in stream_calls(fake_ollama)[1][4]
    last_result = stream_calls(fake_ollama)[2][2][-1]
    assert "budget" in json.loads(last_result["content"])["error"]


def test_final_round_cannot_execute_unsolicited_tools(
    assistant, plane, fake_ollama
):
    runs = []
    add_tool(plane, "test.loop", lambda: runs.append(1) or {"again": True})
    fake_ollama.stream_script = [[("test_loop", {})]] * (MAX_TOOL_ROUNDS + 1)

    reply = "".join(assistant.respond_stream("Keep checking."))

    assert len(runs) == MAX_TOOL_ROUNDS
    assert "tool limit" in reply
    assert "not completed" in reply
    assert "tools" not in stream_calls(fake_ollama)[-1][4]


def test_model_without_a_tool_host_cannot_trigger_unsolicited_calls(
    plane, fake_ollama, tmp_path, monkeypatch
):
    monkeypatch.setattr(memory_manager, "DATA_DIR", tmp_path)
    monkeypatch.setattr(memory_manager, "DATABASE_PATH", tmp_path / "memory.db")
    assistant = FreyaAssistant()
    fake_ollama.stream_script = [[("system_cpu", {})], "I could not check that."]

    reply = "".join(assistant.respond_stream("Check the processor."))

    assert "could not check" in reply
    result = stream_calls(fake_ollama)[1][2][-1]
    assert "not run" in json.loads(result["content"])["error"]


def test_a_reply_with_no_words_is_said_to_be_one_and_kept(assistant, fake_ollama):
    from freya.core.assistant import EMPTY_REPLY_NOTICE

    fake_ollama.stream_script = [""]

    reply = "".join(assistant.respond_stream("Plan the whole week for me."))

    assert reply.strip() == EMPTY_REPLY_NOTICE
    # The question does not vanish from the conversation.
    assert assistant.messages[-2:] == [
        {"role": "user", "content": "Plan the whole week for me."},
        {"role": "assistant", "content": EMPTY_REPLY_NOTICE},
    ]


def test_no_words_after_a_tool_is_also_said(assistant, fake_ollama):
    from freya.core.assistant import EMPTY_REPLY_NOTICE

    fake_ollama.stream_script = [[("system_cpu", {})], ""]

    reply = "".join(assistant.respond_stream("How busy is the processor?"))

    assert reply.strip() == EMPTY_REPLY_NOTICE
