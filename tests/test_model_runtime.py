import json

import pytest

from freya.config import MODEL
from freya.control_plane import build_local_control_plane
from freya.core import llm
from freya.core.capabilities import CapabilityStatus
from freya.core.model_runtime import (
    ModelRuntimeUnavailable,
    get_model_runtime,
)
from freya.core.providers import ProviderHealth
from freya.providers.model.ollama import (
    OLLAMA_PROVIDER,
    check_ollama_health,
)


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


def audit_rows(plane):
    return [
        dict(row)
        for row in plane.store.execute(
            "SELECT * FROM audit_event ORDER BY event_id"
        ).fetchall()
    ]


def test_health_reflects_server_and_installed_models(fake_ollama):
    models = [MODEL, "nomic-embed-text"]

    assert check_ollama_health(models) == ProviderHealth.HEALTHY

    fake_ollama.installed = [MODEL]
    assert check_ollama_health(models) == ProviderHealth.UNHEALTHY

    fake_ollama.reachable = False
    assert check_ollama_health(models) == ProviderHealth.OFFLINE


def test_the_real_client_is_pinned_to_this_machine(monkeypatch):
    import importlib

    monkeypatch.setenv("OLLAMA_HOST", "http://models.example.org:11434")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.org:8080")
    from freya.providers.model import ollama as provider

    provider = importlib.reload(provider)

    assert str(provider.ollama._client.base_url).rstrip("/") == (
        "http://127.0.0.1:11434"
    )
    assert provider.ollama._client.trust_env is False


def test_a_model_that_runs_elsewhere_is_not_a_local_model(fake_ollama):
    from freya.providers.model.ollama import runs_elsewhere

    fake_ollama.installed = ["big-model:cloud", "nomic-embed-text:latest"]
    assert runs_elsewhere("big-model:cloud")
    assert check_ollama_health(["big-model:cloud", "nomic-embed-text"]) == (
        ProviderHealth.UNHEALTHY
    )

    fake_ollama.installed = ["plain-name:latest", "nomic-embed-text:latest"]
    fake_ollama.show = lambda model: (
        {"remote_host": "https://elsewhere.example"}
        if model == "plain-name" else {}
    )
    assert check_ollama_health(["plain-name", "nomic-embed-text"]) == (
        ProviderHealth.UNHEALTHY
    )
    assert check_ollama_health(["nomic-embed-text"]) == ProviderHealth.HEALTHY

    # If where a model runs cannot be found out, it is not local.
    def broken(model):
        raise RuntimeError("show failed")

    fake_ollama.show = broken
    assert check_ollama_health(["nomic-embed-text"]) == ProviderHealth.UNHEALTHY


def test_runtime_is_unavailable_until_configured():
    with pytest.raises(ModelRuntimeUnavailable):
        get_model_runtime()

    with pytest.raises(ModelRuntimeUnavailable):
        llm.chat([{"role": "user", "content": "hi"}])


def test_chat_goes_through_the_broker_and_is_audited(plane, fake_ollama):
    messages = [{"role": "user", "content": "a private question"}]

    reply = llm.chat(messages)

    rows = audit_rows(plane)

    assert reply == "Hello from the fake model."
    assert fake_ollama.calls == [
        ("chat", MODEL, messages, False, {"options": {"num_ctx": 16384}}),
    ]
    assert [(r["kind"], r["capability"]) for r in rows] == [
        ("dispatch", "llm.chat"),
        ("outcome", "llm.chat"),
    ]
    assert rows[1]["outcome"] == "succeeded"
    assert rows[0]["provider"] == OLLAMA_PROVIDER
    assert rows[0]["actor"] == "local-owner"
    # Prompts are never written to the audit log.
    assert "a private question" not in json.dumps(rows)


def test_streamed_chat_yields_chunks_and_records_outcome(plane, fake_ollama):
    chunks = list(llm.chat_stream([{"role": "user", "content": "hi"}]))

    rows = audit_rows(plane)

    assert "".join(chunks).strip() == "Hello from the fake model."
    assert fake_ollama.calls[0][3] is True
    assert [(r["kind"], r["capability"]) for r in rows] == [
        ("dispatch", "llm.chat_stream"),
        ("outcome", "llm.chat_stream"),
    ]
    assert rows[1]["outcome"] == "succeeded"


def test_embedding_goes_through_the_broker(plane, fake_ollama):
    embedding = get_model_runtime().embed("remember this")

    assert embedding == [0.1, 0.2, 0.3]
    assert fake_ollama.calls == [
        ("embed", "nomic-embed-text", "remember this")
    ]
    assert audit_rows(plane)[0]["capability"] == "llm.embed"


def test_json_helpers_use_the_runtime(plane, fake_ollama):
    fake_ollama.reply = '{"relevant_ids": [2], "reason": "It answers it."}'
    candidates = [
        {
            "id": i,
            "type": "fact",
            "key": f"k{i}",
            "similarity": 0.5,
            "content": f"Example fact {i}.",
        }
        for i in (1, 2)
    ]

    result = llm.rerank_memory_candidates("a question", candidates)

    assert result["relevant_ids"] == [2]
    assert fake_ollama.calls[0][4] == {
        "format": "json", "options": {"num_ctx": 16384},
    }

    fake_ollama.reply = '{"remember": false}'
    assert llm.evaluate_memory("What is the capital of France?") is None


def test_offline_ollama_is_reported_and_recovers(tmp_path, fake_ollama):
    fake_ollama.reachable = False
    plane = build_local_control_plane(tmp_path / "control.db", None)

    assert any("Ollama is not reachable" in m for m in plane.startup_messages())
    assert plane.capabilities.get_status("llm.chat") == (
        CapabilityStatus.UNAVAILABLE
    )

    with pytest.raises(ModelRuntimeUnavailable) as error:
        llm.chat([{"role": "user", "content": "hi"}])

    assert "not available" in str(error.value)
    assert fake_ollama.calls == []

    # Ollama is started later: the next request re-checks and succeeds.
    fake_ollama.reachable = True

    assert llm.chat([{"role": "user", "content": "hi"}]) == fake_ollama.reply
    assert plane.providers.get(OLLAMA_PROVIDER).health == ProviderHealth.HEALTHY
    plane.close()


def test_missing_model_is_reported(tmp_path, fake_ollama):
    fake_ollama.installed = [MODEL]
    plane = build_local_control_plane(tmp_path / "control.db", None)

    assert any("model is not installed" in m for m in plane.startup_messages())
    with pytest.raises(ModelRuntimeUnavailable):
        llm.chat([{"role": "user", "content": "hi"}])
    plane.close()


def test_ollama_going_away_marks_provider_offline(plane, fake_ollama):
    assert llm.chat([{"role": "user", "content": "hi"}])

    fake_ollama.reachable = False

    with pytest.raises(ModelRuntimeUnavailable):
        llm.chat([{"role": "user", "content": "hi"}])

    rows = audit_rows(plane)

    assert rows[-1]["outcome"] == "failed-known"
    assert plane.providers.get(OLLAMA_PROVIDER).health == ProviderHealth.OFFLINE
    assert plane.capabilities.get_status("llm.chat") == (
        CapabilityStatus.UNAVAILABLE
    )


def test_stream_failure_mid_reply_is_reported_and_recorded(plane, fake_ollama):
    fake_ollama.fail_mid_stream = True
    received = []

    with pytest.raises(ModelRuntimeUnavailable):
        for chunk in llm.chat_stream([{"role": "user", "content": "hi"}]):
            received.append(chunk)

    rows = audit_rows(plane)

    assert received == ["Hello ", "from "]
    assert rows[-1]["kind"] == "outcome"
    assert rows[-1]["outcome"] == "failed-known"


def test_revoking_policy_stops_model_use(plane, fake_ollama):
    rule = next(
        r for r in plane.policy.list_rules() if r["capability"] == "llm.chat"
    )
    plane.policy.remove_rule(rule["rule_id"])

    with pytest.raises(ModelRuntimeUnavailable) as error:
        llm.chat([{"role": "user", "content": "hi"}])

    assert "Not authorized" in str(error.value)
    assert fake_ollama.calls == []
    assert audit_rows(plane)[-1]["outcome"] == "denied"


def test_warm_models_loads_both_models(plane, fake_ollama):
    plane.warm_models()

    assert [call[:2] for call in fake_ollama.calls] == [
        ("generate", MODEL),
        ("embed", "nomic-embed-text"),
    ]
    # Warm-up is provider housekeeping, not a capability invocation.
    assert audit_rows(plane) == []

    fake_ollama.reachable = False
    plane.warm_models()


@pytest.fixture
def split_plane(tmp_path, fake_ollama, monkeypatch):
    fake_ollama.installed.append("small-model:latest")
    monkeypatch.setattr(
        "freya.control_plane.BACKGROUND_MODEL",
        "small-model",
    )
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


def test_background_work_uses_the_reply_model_by_default(plane, fake_ollama):
    llm.evaluate_memory("What is the capital of France?")

    assert fake_ollama.calls[0][1] == MODEL
    assert plane.capabilities.get("llm.background.chat") is None
    assert audit_rows(plane)[0]["capability"] == "llm.chat"


def test_background_work_goes_to_its_own_model(split_plane, fake_ollama):
    llm.evaluate_memory("What is the capital of France?")
    llm.chat([{"role": "user", "content": "hi"}])
    list(llm.chat_stream([{"role": "user", "content": "hi"}]))

    assert [call[1] for call in fake_ollama.calls] == [
        "small-model",
        MODEL,
        MODEL,
    ]
    assert [
        row["capability"]
        for row in audit_rows(split_plane)
        if row["kind"] == "dispatch"
    ] == ["llm.background.chat", "llm.chat", "llm.chat_stream"]


def test_summaries_are_background_work(split_plane, fake_ollama):
    llm.summarize_conversation(
        "",
        [{"role": "user", "content": "We talked about the garden."}],
    )

    assert fake_ollama.calls[0][1] == "small-model"


def test_a_missing_background_model_is_reported(tmp_path, fake_ollama, monkeypatch):
    monkeypatch.setattr(
        "freya.control_plane.BACKGROUND_MODEL",
        "small-model",
    )
    plane = build_local_control_plane(tmp_path / "control.db", None)

    try:
        assert any(
            "required model is not installed" in m
            for m in plane.startup_messages()
        )
        with pytest.raises(ModelRuntimeUnavailable):
            llm.evaluate_memory("Remember that I like tea.")
        assert fake_ollama.calls == []
    finally:
        plane.close()


def test_both_models_are_warmed(split_plane, fake_ollama):
    split_plane.warm_models()

    assert [
        call[1] for call in fake_ollama.calls if call[0] == "generate"
    ] == [MODEL, "small-model"]


def test_every_request_asks_for_the_same_context_size(plane, fake_ollama):
    # Ollama reloads a model, taking seconds, when the size changes
    # between requests, so replies and background work must agree.
    llm.chat([{"role": "user", "content": "hi"}], options={"num_predict": 50})
    llm.evaluate_memory("What is the capital of France?")
    list(llm.chat_stream([{"role": "user", "content": "hi"}]))
    llm.summarize_conversation("", [{"role": "user", "content": "A talk."}])

    sizes = [call[4]["options"]["num_ctx"] for call in fake_ollama.calls]

    assert sizes == [16384] * 4
    assert fake_ollama.calls[0][4]["options"]["num_predict"] == 50


def test_a_request_that_names_its_own_size_keeps_it(plane, fake_ollama):
    llm.chat([{"role": "user", "content": "hi"}], options={"num_ctx": 4096})

    assert fake_ollama.calls[0][4]["options"] == {"num_ctx": 4096}


def test_models_are_warmed_at_the_size_they_will_be_used_at(plane, fake_ollama):
    plane.warm_models()

    warmed = [call for call in fake_ollama.calls if call[0] == "generate"]

    assert warmed == [("generate", MODEL, {"options": {"num_ctx": 16384}})]


def test_an_unknown_role_is_refused(plane):
    with pytest.raises(ValueError):
        get_model_runtime().chat([], role="somewhere-else")
