"""
Background model calls turn thinking off.

A thinking model's own default spends each unseen call (memory
decisions, comparisons, summaries) on its reasoning. Replies are
left alone. Ollama rejects `think` for some models that cannot
think; that refusal is tried once more without the field.
"""

import pytest

from freya.config import MODEL, background_think
from freya.control_plane import build_local_control_plane
from freya.core import llm
from freya.core.model_runtime import (
    ROLE_BACKGROUND,
    ModelRuntimeUnavailable,
    get_model_runtime,
)


MESSAGES = [{"role": "user", "content": "hi"}]
REMEMBERED = {
    "type": "fact",
    "key": "tea",
    "content": "User likes tea.",
}
REMEMBERED_JSON = (
    '{"remember": true, "type": "fact", "key": "tea", '
    '"content": "User likes tea."}'
)


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


def think_of(call):
    return call[4].get("think", "absent")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", False),
        ("off", False),
        ("OFF", False),
        (" off ", False),
        ("false", False),
        ("0", False),
        ("yes", False),
        ("on", True),
        ("ON", True),
        (" low ", "low"),
        ("medium", "medium"),
        ("HIGH", "high"),
        ("xhigh", "xhigh"),
    ],
)
def test_background_think_words(monkeypatch, raw, expected):
    monkeypatch.setenv("FREYA_BACKGROUND_THINK", raw)

    assert background_think() == expected
    if isinstance(expected, bool):
        assert background_think() is expected


def test_background_think_defaults_off(monkeypatch):
    monkeypatch.delenv("FREYA_BACKGROUND_THINK", raising=False)

    assert background_think() is False


def test_background_requests_send_think_false(plane, fake_ollama):
    llm.evaluate_memory("What is the capital of France?")
    llm.summarize_conversation(
        "",
        [{"role": "user", "content": "We talked about the garden."}],
    )
    list(get_model_runtime().chat_stream(MESSAGES, role=ROLE_BACKGROUND))

    assert [think_of(call) for call in fake_ollama.calls] == [False, False, False]


def test_reply_requests_do_not_send_think(plane, fake_ollama, monkeypatch):
    monkeypatch.setenv("FREYA_BACKGROUND_THINK", "on")

    llm.chat(MESSAGES)
    list(llm.chat_stream(MESSAGES))

    assert [think_of(call) for call in fake_ollama.calls] == ["absent", "absent"]

    # A reply that names a setting keeps it. Nothing is forced off.
    get_model_runtime().chat(MESSAGES, think=True)
    assert think_of(fake_ollama.calls[-1]) is True


def test_background_think_env_is_sent(plane, fake_ollama, monkeypatch):
    monkeypatch.setenv("FREYA_BACKGROUND_THINK", "high")
    llm.evaluate_memory("What is the capital of France?")
    assert think_of(fake_ollama.calls[0]) == "high"

    fake_ollama.calls.clear()
    monkeypatch.setenv("FREYA_BACKGROUND_THINK", "on")
    llm.evaluate_memory("What is the capital of France?")
    assert think_of(fake_ollama.calls[0]) is True

    fake_ollama.calls.clear()
    monkeypatch.setenv("FREYA_BACKGROUND_THINK", "nope")
    llm.evaluate_memory("What is the capital of France?")
    assert think_of(fake_ollama.calls[0]) is False


def test_caller_think_wins_over_the_env(plane, fake_ollama, monkeypatch):
    monkeypatch.setenv("FREYA_BACKGROUND_THINK", "on")

    get_model_runtime().chat(MESSAGES, role=ROLE_BACKGROUND, think="low")
    get_model_runtime().chat(MESSAGES, role=ROLE_BACKGROUND, think=False)

    assert think_of(fake_ollama.calls[0]) == "low"
    assert think_of(fake_ollama.calls[1]) is False


def test_a_separate_background_model_also_thinks_off(
    tmp_path, fake_ollama, monkeypatch,
):
    fake_ollama.installed.append("small-model:latest")
    monkeypatch.setattr(
        "freya.control_plane.BACKGROUND_MODEL",
        "small-model",
    )
    plane = build_local_control_plane(tmp_path / "control.db", None)

    try:
        llm.evaluate_memory("What is the capital of France?")
        llm.chat(MESSAGES)
    finally:
        plane.close()

    background, reply = fake_ollama.calls
    assert background[1] == "small-model"
    assert think_of(background) is False
    assert reply[1] == MODEL
    assert think_of(reply) == "absent"


def _rejecting_chat(fake_ollama, message, refuse_stream=False):
    import ollama

    original = fake_ollama.chat

    def chat(model, messages, stream=False, **kwargs):
        if "think" in kwargs:
            fake_ollama.calls.append(
                ("chat", model, messages, stream, kwargs)
            )
            error = ollama.ResponseError(message, 400)
            if stream and refuse_stream:
                def blocked():
                    raise error
                    yield None
                return blocked()
            raise error
        return original(model, messages, stream=stream, **kwargs)

    fake_ollama.chat = chat


def test_a_model_that_rejects_think_is_asked_again_without_it(plane, fake_ollama):
    _rejecting_chat(fake_ollama, '"plain" does not support thinking')

    assert llm.evaluate_memory("What is the capital of France?") is None
    assert think_of(fake_ollama.calls[0]) is False
    assert think_of(fake_ollama.calls[1]) == "absent"
    assert len(fake_ollama.calls) == 2


def test_a_stream_that_rejects_think_is_asked_again_without_it(plane, fake_ollama):
    _rejecting_chat(
        fake_ollama,
        '"plain" does not support thinking',
        refuse_stream=True,
    )

    assert list(get_model_runtime().chat_stream(
        MESSAGES, role=ROLE_BACKGROUND,
    ))
    assert think_of(fake_ollama.calls[0]) is False
    assert fake_ollama.calls[0][3] is True
    assert think_of(fake_ollama.calls[1]) == "absent"
    assert len(fake_ollama.calls) == 2


def test_an_unrelated_ollama_error_is_not_retried(plane, fake_ollama):
    _rejecting_chat(fake_ollama, "model runner has unexpectedly stopped")

    with pytest.raises(ModelRuntimeUnavailable):
        llm.evaluate_memory("What is the capital of France?")

    assert len(fake_ollama.calls) == 1
    assert think_of(fake_ollama.calls[0]) is False


def test_a_second_refusal_is_not_tried_again(plane, fake_ollama):
    import ollama

    def chat(model, messages, stream=False, **kwargs):
        fake_ollama.calls.append(("chat", model, messages, stream, kwargs))
        raise ollama.ResponseError("does not support thinking", 400)

    fake_ollama.chat = chat

    with pytest.raises(ModelRuntimeUnavailable):
        llm.evaluate_memory("What is the capital of France?")

    assert len(fake_ollama.calls) == 2
    assert think_of(fake_ollama.calls[1]) == "absent"


def test_a_stream_that_fails_after_a_chunk_is_not_sent_again(plane, fake_ollama):
    fake_ollama.fail_mid_stream = True
    received = []

    with pytest.raises(ModelRuntimeUnavailable):
        for chunk in get_model_runtime().chat_stream(
            MESSAGES, role=ROLE_BACKGROUND,
        ):
            received.append(chunk)

    assert received == ["Hello ", "from "]
    assert len(fake_ollama.calls) == 1


def _fence(body, tag="json", newline="\n"):
    return f"```{tag}{newline}{body}{newline}```"


def test_a_markdown_fence_around_memory_json_is_read():
    assert llm._parse_memory_result(_fence(REMEMBERED_JSON)) == REMEMBERED
    assert llm._parse_memory_result(_fence(REMEMBERED_JSON, tag="JSON")) == REMEMBERED
    assert llm._parse_memory_result(_fence(REMEMBERED_JSON, tag="")) == REMEMBERED
    assert llm._parse_memory_result(
        _fence(REMEMBERED_JSON, newline="\r\n")
    ) == REMEMBERED
    assert llm._parse_memory_result(REMEMBERED_JSON) == REMEMBERED
    assert llm._parse_memory_result(_fence('{"remember": false}')) is None


def test_prose_around_memory_json_is_still_refused():
    loose = [
        f"Sure:\n{_fence(REMEMBERED_JSON)}",
        f"{_fence(REMEMBERED_JSON)}\nthanks",
        f"The memory is {REMEMBERED_JSON}",
        f"{REMEMBERED_JSON}\n{REMEMBERED_JSON}",
        "```json {\"remember\": false} ```",
        "```json\nnot json\n```",
    ]

    for text in loose:
        assert llm._parse_memory_result(text) is None


def test_fenced_background_json_is_read_and_loose_text_is_not(plane, fake_ollama):
    fake_ollama.reply = _fence(
        '{"relationship": "duplicate", "reason": "Same fact.", '
        '"suggested_key": "tea"}'
    )
    pair = (
        {"id": 1, "type": "fact", "key": "tea", "content": "User likes tea."},
        {"id": 2, "type": "fact", "key": "tea", "content": "User likes tea."},
    )

    compared = llm.compare_memories(*pair)

    assert compared["relationship"] == "duplicate"
    assert compared["suggested_key"] == "tea"
    assert think_of(fake_ollama.calls[0]) is False

    fake_ollama.reply = (
        'I think they match: {"relationship": "duplicate", '
        '"reason": "Same fact.", "suggested_key": "tea"}'
    )
    assert llm.compare_memories(*pair)["relationship"] == "uncertain"

    fake_ollama.reply = _fence('{"relevant_ids": [2], "reason": "It answers it."}')
    candidates = [
        {
            "id": number,
            "type": "fact",
            "key": f"k{number}",
            "similarity": 0.5,
            "content": f"Example fact {number}.",
        }
        for number in (1, 2)
    ]
    assert llm.rerank_memory_candidates("a question", candidates)["relevant_ids"] == [2]

    fake_ollama.reply = 'Use 2. {"relevant_ids": [2], "reason": "It answers it."}'
    assert llm.rerank_memory_candidates("a question", candidates)["relevant_ids"] == []

    fake_ollama.reply = _fence(REMEMBERED_JSON)
    assert llm.evaluate_memory("Remember that I like tea.") == REMEMBERED
