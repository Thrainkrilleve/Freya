import json
import sqlite3

import pytest

from freya.control_plane import CONVERSATION_RECALL, build_local_control_plane
from freya.core import conversation_memory, llm
from freya.core.assistant import FreyaAssistant
from freya.core.model_runtime import ModelRuntimeUnavailable
from freya.kernel.context import ContextScope
from freya.memory import memory_manager
from freya.memory.conversations import ConversationStore
from freya.memory.memory_manager import MemoryManager


# Everything stored here is made up for the tests.
MEMORY = "The user's locker code is ZEBRA-4471."
SAID = "My sister Ottoline keeps bees in Quillfield."
ANSWERED = "Ottoline's bees sound delightful."
SUMMARY = "The user spoke about a sister and her beehives."
STORED = ("ZEBRA-4471", "Quillfield", "bees sound delightful", "her beehives")

# The stand-in model's answer to a ranking request: keep memory 1.
KEEP_FIRST_MEMORY = '{"relevant_ids": [1], "remember": false}'


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


@pytest.fixture
def new_assistant(plane, tmp_path, monkeypatch):
    # Never touch the owner's real memory database.
    monkeypatch.setattr(memory_manager, "DATA_DIR", tmp_path)
    monkeypatch.setattr(memory_manager, "DATABASE_PATH", tmp_path / "memory.db")
    made = []

    def make():
        assistant = FreyaAssistant(tool_host=plane.model_tools, broker=plane.broker)
        if not plane.broker.exists(CONVERSATION_RECALL):
            plane.register_conversation_recall(lambda **request: made[-1].recall(**request))
        made.append(assistant)
        return assistant

    yield make

    for assistant in made:
        assistant.memory.connection.close()


@pytest.fixture
def remembering(new_assistant, fake_ollama):
    """
    Freya on a later run: a memory is saved, and an earlier
    conversation is stored and summarised.
    """
    first = new_assistant()
    first.memory.add_memory(MEMORY, "fact", "locker_code")
    fake_ollama.stream_script = [ANSWERED]
    say(first, SAID)
    fake_ollama.reply = SUMMARY
    first.finish_conversation()

    fake_ollama.reply = "Hello from the fake model."
    fake_ollama.calls.clear()

    return new_assistant()


def say(assistant, text):
    return "".join(assistant.respond_stream(text)).strip()


def withdraw_recall(plane):
    rules = {rule["capability"]: rule["rule_id"] for rule in plane.policy.list_rules()}
    assert plane.policy.remove_rule(rules[CONVERSATION_RECALL])


def recall_audit(plane):
    return [
        dict(row)
        for row in plane.store.execute(
            "SELECT * FROM audit_event WHERE capability = ? ORDER BY event_id",
            (CONVERSATION_RECALL,),
        ).fetchall()
    ]


def given_to_model(fake_ollama) -> str:
    """Everything every model call was given: chat, ranking, embedding."""
    return json.dumps([call[2] for call in fake_ollama.calls], default=str)


def packets(messages) -> list:
    data = next(m["content"] for m in messages if m["content"].startswith("Freya context data"))
    return json.loads(data.split("\n", 1)[1])


class Reads:
    """Records every statement run on the memory database."""

    def __init__(self, assistant):
        self._connection = assistant.memory.connection
        self.statements = []

    def __enter__(self):
        self._connection.set_trace_callback(self.statements.append)
        return self.statements

    def __exit__(self, *error):
        self._connection.set_trace_callback(None)


def stored_text(connection) -> str:
    """
    Every value of every table in a database, in lower case: the
    tables themselves and the search index's own storage.
    """
    tables = [
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]

    return repr(
        [connection.execute(f'SELECT * FROM "{table}"').fetchall() for table in tables]
    ).lower()


# -- reading for the model is authorized and audited ------------------------------


def test_an_allowed_read_is_labelled_audited_once_and_not_kept(remembering, plane, fake_ollama):
    assistant = remembering
    fake_ollama.json_reply = KEEP_FIRST_MEMORY
    fake_ollama.stream_script = ["Your sister keeps bees."]
    before = len(recall_audit(plane))

    assert say(assistant, "Tell me about Ottoline.") == "Your sister keeps bees."

    sent = [call[2] for call in fake_ollama.calls if call[0] == "chat" and call[3]][-1]
    by_source = {packet["source"]: packet for packet in packets(sent)}

    memory = by_source["saved-memory"]
    assert (memory["id"], memory["trust"]) == ("memory:1", "legacy-unverified")
    assert MEMORY in memory["content"]

    passage = by_source["conversation-passage"]
    assert (passage["id"], passage["trust"]) == ("conversation:1:turns:1,2", "quoted-conversation")
    assert f"the user: {SAID}" in passage["content"]
    assert f"Freya: {ANSWERED}" in passage["content"]

    summary = by_source["conversation-summary"]
    assert (summary["id"], summary["trust"]) == ("conversation:1", "model-inference")
    assert SUMMARY in summary["content"]

    for packet in by_source.values():
        assert (packet["owner"], packet["domain"]) == ("local-owner", "local")

    # One read for the turn, recorded without what was read or asked.
    rows = recall_audit(plane)[before:]
    assert [(row["kind"], row["outcome"]) for row in rows] == [
        ("dispatch", None),
        ("outcome", "succeeded"),
    ]
    assert "Ottoline" not in json.dumps(rows, default=str)

    # What was read was for this reply only.
    assert assistant.messages[-2:] == [
        {"role": "user", "content": "Tell me about Ottoline."},
        {"role": "assistant", "content": "Your sister keeps bees."},
    ]
    kept = json.dumps(assistant.messages)
    assert not [text for text in STORED if text in kept]


def test_without_permission_the_stores_are_not_read(remembering, plane):
    assistant = remembering
    withdraw_recall(plane)

    with Reads(assistant) as statements:
        plain = assistant._prepare_context("Tell me about Ottoline.")
        asked = assistant._prepare_context("Do you remember my locker code?")

    assert statements == []

    for messages in (plain, asked):
        sent = json.dumps(messages)
        assert not [text for text in STORED if text in sent]
        assert "could not be consulted" in sent
        assert "Nothing was found" not in sent


def test_without_permission_nothing_stored_reaches_the_model_by_any_path(
    remembering, plane, fake_ollama
):
    assistant = remembering
    withdraw_recall(plane)
    before = len(recall_audit(plane))
    # The model tries the tool as well.
    fake_ollama.stream_script = [
        [("conversation_recall", {"query": "Ottoline bees locker"})],
        "I cannot look that up.",
    ]

    assert say(assistant, "Do you remember what I said about Ottoline?") == "I cannot look that up."
    assert say(assistant, "What is my locker code?")

    everything = given_to_model(fake_ollama)
    assert not [text for text in STORED if text in everything]
    assert "Not authorized" in everything

    outcomes = [row["outcome"] for row in recall_audit(plane)[before:]]
    assert outcomes == ["denied"] * 3


def test_withdrawing_permission_takes_effect_during_a_run(remembering, plane, fake_ollama):
    assistant = remembering
    fake_ollama.json_reply = KEEP_FIRST_MEMORY

    # While it is allowed, she has been given all of it.
    sent = json.dumps(assistant._prepare_context("Tell me about Ottoline."))
    assert [text for text in STORED if text not in sent] == []

    withdraw_recall(plane)

    sent = json.dumps(assistant._prepare_context("Tell me about Ottoline."))
    assert not [text for text in STORED if text in sent]


def test_denied_recall_does_not_summarize_old_conversations_at_start(
    new_assistant, plane, fake_ollama
):
    first = new_assistant()
    first.conversations.add_exchange(SAID, ANSWERED)
    withdraw_recall(plane)
    fake_ollama.calls.clear()

    second = new_assistant()
    second._prepare_context("Do you remember Ottoline?")

    assert not [call for call in fake_ollama.calls if call[0] == "chat"]
    assert second.conversations.unsummarized()[0]["summary"] == ""


def test_deleting_old_text_does_not_summarize_without_recall_permission(
    remembering, plane, fake_ollama
):
    assistant = remembering
    turn = assistant.find("Quillfield")["passages"][0]["match"]
    conversation = assistant.conversations.recent()[0]["id"]
    withdraw_recall(plane)
    fake_ollama.calls.clear()

    assert assistant.memory_request({"action": "turn-delete", "id": turn})["error"] is None
    assert not [call for call in fake_ollama.calls if call[0] == "chat"]
    assert assistant.conversations.get(conversation) is None


def test_an_assistant_with_no_broker_is_given_nothing_stored(remembering):
    assistant = remembering
    assistant.broker = None

    with Reads(assistant) as statements:
        sent = json.dumps(assistant._prepare_context("Do you remember my locker code?"))

    assert statements == []
    assert not [text for text in STORED if text in sent]


def test_the_model_cannot_ask_for_more_than_passages(remembering, plane):
    result = json.loads(
        plane.model_tools.run("conversation_recall", {"query": "Ottoline", "for_reply": True})
    )

    assert set(result) == {"query", "found", "passages", "note"}
    assert result["found"] == 1
    assert "ZEBRA-4471" not in json.dumps(result)


# -- whose records they are ----------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    [
        ContextScope(actor="another-user"),
        ContextScope(domain="work"),
        ContextScope(purpose="training"),
    ],
    ids=["actor", "domain", "purpose"],
)
def test_a_scope_the_stores_cannot_serve_is_refused_before_reading(remembering, plane, scope):
    assistant = remembering
    assistant.context.scope = scope

    with Reads(assistant) as statements:
        tool = plane.broker.invoke(CONVERSATION_RECALL, query="Ottoline")
        reply = plane.broker.invoke(CONVERSATION_RECALL, query="Ottoline", for_reply=True)
        sent = json.dumps(assistant._prepare_context("Do you remember my locker code?"))

    assert (tool.success, reply.success) == (False, False)
    assert tool.outcome == "failed-known"
    assert statements == []
    assert not [text for text in STORED if text in sent]


# -- forgetting ------------------------------------------------------------------


@pytest.fixture
def memory(plane, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_manager, "DATA_DIR", tmp_path)
    monkeypatch.setattr(memory_manager, "DATABASE_PATH", tmp_path / "memory.db")
    manager = MemoryManager()
    yield manager
    manager.connection.close()


def proposal(memory_id, content):
    return {
        "id": memory_id, "current_type": "fact", "current_key": None, "content": content,
        "proposed_type": "fact", "proposed_key": f"key_{memory_id}",
        "blocked": False, "block_reason": None,
    }


def test_a_forgotten_memory_leaves_no_copy_behind(memory):
    forgotten = memory.add_memory("The spare key is under the heron statue.", "fact", "spare_key")
    memory.update_memory(forgotten, "The spare key is under the blue pot.")
    kept = memory.add_memory("The user prefers tea.", "preference", "drink")
    memory.update_memory(kept, "The user prefers green tea.")
    memory.save_migration_plan(
        [
            proposal(forgotten, "The spare key is under the blue pot."),
            proposal(kept, "The user prefers green tea."),
        ]
    )

    assert memory.delete_memory(forgotten) is True

    text = stored_text(memory.connection)
    assert "heron statue" not in text and "blue pot" not in text
    assert memory.get_memory(forgotten) is None
    assert memory.get_memory_history(forgotten) == []
    assert [entry["id"] for entry in memory.get_migration_plan()] == [kept]
    assert [found["id"] for found in memory.search_memories("spare key", limit=10)] == [kept]
    assert memory.delete_memory(forgotten) is False

    # An edit of another memory keeps its history and can be undone.
    history = memory.get_memory_history(kept)
    assert [(entry[2], entry[5]) for entry in history] == [("update", "The user prefers tea.")]
    assert memory.restore_memory(kept, history[0][0])["success"] is True


def test_resolving_a_duplicate_still_archives_and_forgetting_clears_the_archive(memory):
    keep = memory.add_memory("The user's bank is Larkspur Mutual.", "fact")
    duplicate = memory.add_memory("User banks with Larkspur Mutual.", "fact")

    assert memory.resolve_duplicate(keep, duplicate, "fact", "bank") is True
    assert [entry[2] for entry in memory.get_memory_history(keep)] == ["duplicate_resolution_keep"]
    assert [entry[2] for entry in memory.get_memory_history(duplicate)] == [
        "duplicate_resolution_remove"
    ]

    # The removed duplicate is no longer live, but its archived words
    # can still be forgotten by its number.
    assert memory.delete_memory(duplicate) is True
    assert memory.delete_memory(keep) is True
    assert "larkspur" not in stored_text(memory.connection)


def test_a_copy_of_the_forgotten_words_under_another_memory_goes_too(memory):
    first = memory.add_memory("The alarm code is 2468.", "fact", "alarm")
    memory.update_memory(first, "The alarm is serviced in May.")
    second = memory.add_memory("the alarm code is 2468.", "fact", "alarm_code")

    assert memory.delete_memory(second) is True

    assert "2468" not in stored_text(memory.connection)
    assert memory.get_memory(first)["content"] == "The alarm is serviced in May."


def test_forgetting_a_memory_is_all_or_nothing(memory):
    memory_id = memory.add_memory("The alarm code is 2468.", "fact", "alarm")
    memory.update_memory(memory_id, "The alarm code is 1357.")
    memory.connection.execute(
        "CREATE TEMP TRIGGER refuse BEFORE DELETE ON memory_history "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    with pytest.raises(sqlite3.Error):
        memory.delete_memory(memory_id)

    assert memory.get_memory(memory_id)["content"] == "The alarm code is 1357."
    assert len(memory.get_memory_history(memory_id)) == 1


@pytest.fixture
def store():
    return ConversationStore(sqlite3.connect(":memory:"))


def test_a_deleted_exchange_is_gone_from_every_table_and_the_search_index(store):
    for said, answer in (
        ("hello", "hi"),
        ("the garage code is quillfield7731", "Noted: quillfield7731."),
        ("thanks", "Any time."),
    ):
        store.add_exchange(said, answer)

    conversation = store.current
    store.set_summary(conversation, "The user gave the garage code quillfield7731.", 6)
    assert "quillfield7731" in stored_text(store.connection)

    assert store.delete_exchange(store.search("quillfield7731")[0]["match"]) == conversation

    assert "quillfield7731" not in stored_text(store.connection)
    assert store.search("quillfield7731") == [] and store.search("garage") == []
    assert store.get(conversation)["summary"] == ""
    assert [turn["content"] for turn in store.turns(conversation)] == [
        "hello", "hi", "thanks", "Any time.",
    ]
    # What is left can still be found.
    assert store.search("thanks")


def test_deleted_conversations_are_gone_from_every_table_and_the_search_index(store):
    store.add_exchange("my doctor is Dr Wrenfold", "Noted.")
    first = store.current
    store.set_summary(first, "The user's doctor is Dr Wrenfold.", 2)
    store.current = None
    store.add_exchange("the wifi password is plumtree99", "Okay.")

    assert store.delete_conversation(first) is True
    text = stored_text(store.connection)
    assert "wrenfold" not in text and "plumtree99" in text

    assert store.delete_all() == 1
    assert "plumtree99" not in stored_text(store.connection)


def test_deleting_an_exchange_is_all_or_nothing(store):
    store.add_exchange("the garage code is 7731", "Got it: 7731.")
    conversation = store.current
    store.set_summary(conversation, "A garage code was given.", 2)
    question = store.turns(conversation)[0]["id"]
    store.connection.execute(
        "CREATE TEMP TRIGGER refuse BEFORE DELETE ON conversation_turn "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    with pytest.raises(sqlite3.Error):
        store.delete_exchange(question)

    assert [turn["role"] for turn in store.turns(conversation)] == ["user", "assistant"]
    assert store.search("7731")
    assert store.get(conversation)["summary"] == "A garage code was given."


def test_what_was_forgotten_is_not_given_to_the_model_again(remembering, fake_ollama):
    assistant = remembering
    assert "ZEBRA-4471" in json.dumps(assistant._prepare_context("Do you remember my locker code?"))

    assert assistant.memory_command("/forget 1") == "Memory 1 has been deleted."
    said = assistant.find("Quillfield")["passages"][0]["match"]
    assert assistant.memory_request({"action": "turn-delete", "id": said})["error"] is None

    for question in ("Do you remember my locker code?", "Tell me about Ottoline."):
        sent = json.dumps(assistant._prepare_context(question))
        assert not [text for text in STORED if text in sent]

    text = stored_text(assistant.memory.connection)
    assert not [gone for gone in STORED if gone.lower() in text]


def test_deleting_from_a_folded_conversation_rewrites_her_summary_of_it(
    new_assistant, fake_ollama, monkeypatch
):
    monkeypatch.setattr(conversation_memory, "MAX_LIVE_MESSAGES", 4)
    monkeypatch.setattr(conversation_memory, "KEEP_LIVE_MESSAGES", 2)
    assistant = new_assistant()
    fake_ollama.reply = "The user gave the gate code PELICAN-9."

    for said in ("The gate code is PELICAN-9.", "It is raining.", "It is still raining."):
        fake_ollama.stream_script = ["Understood."]
        say(assistant, said)

    # The older part is now her summary, and it repeats the code.
    assert "PELICAN-9" in json.dumps(assistant._prepare_context("Go on."))

    fake_ollama.reply = "They talked about the rain."
    said = assistant.find("PELICAN")["passages"][0]["match"]
    assert assistant.memory_request({"action": "turn-delete", "id": said})["error"] is None

    sent = json.dumps(assistant._prepare_context("Go on."))
    assert "PELICAN-9" not in sent and "They talked about the rain." in sent
    assert "pelican" not in stored_text(assistant.memory.connection)


# -- when the model is away -------------------------------------------------------


def test_memories_are_kept_while_the_embedding_model_is_away(memory, fake_ollama):
    fake_ollama.reachable = False

    memory_id = memory.add_memory("The user's cat is called Marzipan.", "fact", "cat_name")
    assert memory.update_memory(memory_id, "The user's cat is called Marzipan the Second.")
    assert memory.get_memory(memory_id)["content"].endswith("the Second.")

    with pytest.raises(ModelRuntimeUnavailable):
        memory.search_memories("cat")

    # Once it is back, what was saved without it takes part in a search.
    fake_ollama.reachable = True
    assert [found["id"] for found in memory.search_memories("cat")] == [memory_id]
    assert memory.connection.execute(
        "SELECT COUNT(*) FROM memories WHERE embedding IS NULL"
    ).fetchone() == (0,)


def test_freya_starts_and_recalls_by_words_without_the_model(new_assistant, fake_ollama):
    first = new_assistant()
    fake_ollama.reachable = False
    first.memory.add_memory("The user's cat is called Marzipan.", "fact", "cat_name")

    # A memory with no embedding, and no model to make one: she starts.
    second = new_assistant()
    sent = json.dumps(second._prepare_context("What is my cat called?"))

    assert "Marzipan" in sent
    notes = " ".join(second.context.last_report["retrieval_notes"])
    assert "matched by their words" in notes and "candidate order" in notes

    # It is embedded the first time the model answers again.
    fake_ollama.reachable = True
    second._prepare_context("What is my cat called?")
    assert second.memory.connection.execute(
        "SELECT COUNT(*) FROM memories WHERE embedding IS NULL"
    ).fetchone() == (0,)


def test_memories_matched_by_their_words_can_be_ranked(plane, fake_ollama):
    fake_ollama.json_reply = '{"relevant_ids": [7]}'

    ranking = llm.rerank_memory_candidates(
        "What is my cat called?",
        [{"id": 7, "type": "fact", "key": "cat_name", "content": "The cat is Marzipan."}],
    )

    assert ranking["relevant_ids"] == [7]


def test_one_conversation_that_cannot_be_summarised_does_not_hold_up_the_rest(
    new_assistant, monkeypatch
):
    assistant = new_assistant()
    store = assistant.conversations
    store.add_exchange("first topic", "yes")
    first, store.current = store.current, None
    store.add_exchange("second topic", "yes")
    second, store.current = store.current, None

    def summarize(previous, turns):
        if turns[0]["content"] == "first topic":
            raise ValueError("This summary batch exceeds its work limit.")
        return "About the second topic."

    monkeypatch.setattr(conversation_memory, "summarize_conversation", summarize)
    assistant._catch_up_summaries()

    assert store.get(first)["summarized_turns"] == 0
    assert store.get(second)["summary"] == "About the second topic."


def test_what_a_reply_is_given_is_bounded(new_assistant):
    assistant = new_assistant()

    for number in range(12):
        assistant.memory.add_memory(f"Plant {number} in the user's house is a fern.", "fact")

    for number in range(9):
        assistant.conversations.add_exchange(f"Note {number}: water the fern.", "x" * 3000)
        assistant.conversations.current = None

    plain = assistant.recall("Tell me about the fern.", for_reply=True)
    assert not plain["asked_about_past"]
    assert (len(plain["memories"]), len(plain["passages"])) == (5, 3)

    asked = assistant.recall("Did I ever mention a fern?", for_reply=True)
    assert asked["asked_about_past"] and asked["searched"] == ["fern"]
    assert (len(asked["memories"]), len(asked["passages"])) == (8, 6)
    assert all(
        len(entry["said"]) <= 400
        for passage in asked["passages"]
        for entry in passage["exchange"]
    )
