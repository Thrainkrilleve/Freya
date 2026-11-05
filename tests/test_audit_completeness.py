"""
UIA-14 (provisional): every protected invocation leaves a complete,
verifiable audit trail, and no record holds what was asked.

Each case runs through the real broker on a temporary control
database; Ollama is the fake from conftest.

As amended by the owner (2026-11-04), the dispatch row is the record
of an allow decision: an allowed call leaves a dispatch and an outcome,
and only denials and unavailability leave a separate decision row.
"""

import json

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.capabilities import CapabilityDefinition
from freya.providers.model.ollama import (
    LLM_CHAT,
    LLM_CHAT_STREAM,
    LLM_EMBED,
    OLLAMA_PROVIDER,
)
from freya.tools.model_tools import MODEL_TOOL


# Text that must never reach the audit log.
SECRET = "uia14-secret-prompt-text"
MESSAGES = [{"role": "user", "content": f"Tell me {SECRET}"}]
TOOL_NAME = "test.echo"


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    plane.capabilities.register(
        CapabilityDefinition(
            name=TOOL_NAME,
            description="Test tool that echoes its argument.",
            provider="system-tools-local",
            handler=lambda text: {"echo": text},
            metadata={
                MODEL_TOOL: True,
                "tool_parameters": {
                    "text": {"description": "Anything.", "max_length": 100},
                },
            },
        )
    )
    plane.policy.add_rule("local-owner", TOOL_NAME, "allow", "Test allow.")
    yield plane
    plane.close()


def rows_for(plane, operation_id):
    return [
        dict(row)
        for row in plane.store.execute(
            "SELECT * FROM audit_event WHERE operation_id = ? "
            "ORDER BY event_id",
            (operation_id,),
        ).fetchall()
    ]


def all_rows(plane):
    return [
        dict(row)
        for row in plane.store.execute(
            "SELECT * FROM audit_event ORDER BY event_id"
        ).fetchall()
    ]


def kinds(rows):
    return [row["kind"] for row in rows]


def latest_operation(plane, capability):
    """
    The one operation recorded for a capability. `ModelToolHost.run`
    returns only the tool's JSON, so the operation id is found from
    the log; the test insists there is exactly one dispatch for the
    capability, so a second, unaudited run inside the host could not
    hide behind the first.
    """
    rows = plane.store.execute(
        "SELECT DISTINCT operation_id FROM audit_event "
        "WHERE capability = ? AND kind = 'dispatch'",
        (capability,),
    ).fetchall()
    assert len(rows) == 1, f"expected one dispatch for {capability}, found {len(rows)}"
    return rows[0]["operation_id"]


def run_allowed_cases(plane):
    """Run every allowed case and return (label, operation_id) pairs."""
    operations = []

    result = plane.broker.invoke(LLM_CHAT, MESSAGES)
    assert result.outcome == "succeeded"
    operations.append(("chat", result.operation_id))

    result = plane.broker.invoke(LLM_CHAT_STREAM, MESSAGES)
    assert result.outcome == "streaming"
    list(result.result)
    operations.append(("stream consumed", result.operation_id))

    result = plane.broker.invoke(LLM_CHAT_STREAM, MESSAGES)
    stream = result.result
    next(stream)
    stream.close()
    operations.append(("stream abandoned", result.operation_id))

    result = plane.broker.invoke(LLM_EMBED, SECRET)
    assert result.outcome == "succeeded"
    operations.append(("embedding", result.operation_id))

    # The model names tools with underscores, as the host offers them.
    reply = json.loads(
        plane.model_tools.run(TOOL_NAME.replace(".", "_"), {"text": SECRET})
    )
    assert reply == {"echo": SECRET}
    operations.append(("model tool", latest_operation(plane, TOOL_NAME)))

    return operations


def test_each_allowed_invocation_has_a_dispatch_and_an_outcome(plane):
    for label, operation_id in run_allowed_cases(plane):
        rows = rows_for(plane, operation_id)
        assert kinds(rows) == ["dispatch", "outcome"], label
        dispatch, outcome = rows
        # The dispatch record carries the policy decision it acted on.
        assert dispatch["decision"] == "allow", label
        assert dispatch["decision_reason"], label
        assert outcome["decision"] == "allow", label
        assert outcome["outcome"], label


def test_streams_record_how_they_ended(plane):
    outcomes = {
        label: rows_for(plane, operation_id)[-1]["outcome"]
        for label, operation_id in run_allowed_cases(plane)
    }
    assert outcomes["stream consumed"] == "succeeded"
    assert outcomes["stream abandoned"] == "failed-known"


def test_a_denied_call_is_recorded_and_never_dispatched(plane):
    plane.capabilities.register(
        CapabilityDefinition(
            name="test.unruled",
            description="Test tool with no policy rule.",
            provider="system-tools-local",
            handler=lambda: "ran",
        )
    )

    result = plane.broker.invoke("test.unruled")

    assert result.outcome == "denied"
    rows = rows_for(plane, result.operation_id)
    assert kinds(rows) == ["decision"]
    assert rows[0]["decision"] == "deny"
    assert rows[0]["outcome"] == "denied"


def test_an_unavailable_call_is_recorded_and_never_dispatched(
    plane, fake_ollama,
):
    fake_ollama.reachable = False
    plane.providers.check_health(OLLAMA_PROVIDER)

    result = plane.broker.invoke(LLM_EMBED, SECRET)

    assert result.outcome == "unavailable"
    rows = rows_for(plane, result.operation_id)
    assert kinds(rows) == ["decision"]
    assert rows[0]["decision"] == "allow"
    assert rows[0]["outcome"] == "unavailable"


def test_the_chain_verifies_and_holds_no_payloads(plane, fake_ollama):
    run_allowed_cases(plane)
    plane.broker.invoke("test.not-registered")
    fake_ollama.reachable = False
    plane.providers.check_health(OLLAMA_PROVIDER)
    plane.broker.invoke(LLM_CHAT, MESSAGES)

    verification = plane.audit.verify()
    assert verification.ok, verification.problems

    rows = all_rows(plane)
    assert rows
    for row in rows:
        for column, value in row.items():
            assert SECRET not in str(value), column
        digest = row["arguments_digest"]
        assert digest is None or len(digest) == 64
