import json
import sqlite3

import pytest

from freya.control_plane import CONVERSATION_RECALL, build_local_control_plane
from freya.core import conversation_memory
from freya.core.assistant import FreyaAssistant
from freya.memory import memory_manager
from freya.memory.conversations import (
    BACKUP_FOLDER,
    BACKUP_NAME,
    ConversationStore,
    search_terms,
)


# -- the store ---------------------------------------------------------------


@pytest.fixture
def store():
    return ConversationStore(sqlite3.connect(":memory:"))


def talk(store, *exchanges):
    for said, answer in exchanges:
        store.add_exchange(said, answer)


def test_nothing_is_kept_until_something_is_said(store):
    assert store.current is None
    assert store.recent() == []

    talk(store, ("hello", "hi there"))

    conversation = store.recent()[0]
    assert conversation["turns"] == 2 and conversation["current"]
    assert [turn["role"] for turn in store.turns(conversation["id"])] == ["user", "assistant"]
    assert store.turns(conversation["id"], after=1)[0]["content"] == "hi there"


def test_exchanges_are_kept_word_for_word_with_how_they_arrived(store):
    store.add_exchange("What's the plan for Tuesday?", "Dentist at nine.", "voice")

    user, freya = store.turns(store.current)
    assert user["content"] == "What's the plan for Tuesday?"
    assert freya["content"] == "Dentist at nine."
    assert user["source"] == "voice" and user["at"]


def test_search_finds_passages_with_what_was_said_around_them(store):
    talk(
        store,
        ("my sister is called Marguerite", "Noted."),
        ("what should the avatar wear", "Something blue."),
        ("book the dentist", "Done."),
    )

    found = store.search("avatar")
    assert len(found) == 1
    passage = found[0]
    assert passage["conversation"] == store.current
    said = [turn["content"] for turn in passage["turns"]]
    # The message before, the match, and its answer.
    assert said == ["Noted.", "what should the avatar wear", "Something blue."]
    assert passage["turns"][1]["id"] == passage["match"]

    assert store.search("marguerite")[0]["turns"][0]["content"].endswith("Marguerite")
    assert store.search("nothing like this") == []
    assert store.search("") == []


@pytest.mark.parametrize(
    "query",
    ['"', "avatar OR", "NEAR(", "a* b*", "col:x", "-", "((((", "avatar AND NOT wear", "'; DROP TABLE x"],
)
def test_search_words_cannot_be_read_as_instructions(store, query):
    talk(store, ("what should the avatar wear", "Something blue."))

    # Whatever is typed, it is searched for as plain words and never fails.
    assert isinstance(store.search(query), list)
    assert store.recent()[0]["turns"] == 2


def test_search_terms_are_plain_words():
    assert search_terms("What's the AVATAR'S plan?") == ["what's", "avatar's", "plan"]
    assert search_terms("a b c") == []
    # Words too common to find anything by are left out...
    assert search_terms("Have I ever mentioned anything about a boat?") == ["boat"]
    # ...unless they are all there is.
    assert search_terms("what did you say") == ["what", "did", "you", "say"]
    assert len(search_terms(" ".join(f"word{n}" for n in range(40)))) == 12


def test_deleting_a_message_removes_it_and_clears_the_summary(store):
    talk(store, ("my password hint is tulips", "Okay."), ("what is two plus two", "Four."))
    conversation = store.current
    store.set_summary(conversation, "The user gave a password hint: tulips.", 4)
    secret = store.search("tulips")[0]["match"]

    assert store.delete_turn(secret) == conversation
    assert store.search("tulips") == []
    assert all("tulips" not in turn["content"] for turn in store.turns(conversation))

    after = store.get(conversation)
    assert after["summary"] == "" and after["summarized_turns"] == 0
    assert after["turns"] == 3
    assert store.delete_turn(secret) is None


def test_deleting_an_exchange_removes_the_question_and_the_answer(store):
    talk(
        store,
        ("hello", "hi"),
        ("the garage code is 7731", "Got it, the garage code is 7731."),
        ("thanks", "Any time."),
    )
    conversation = store.current
    question, answer = [t["id"] for t in store.turns(conversation)][2:4]

    # From either half, both go, and nothing else does.
    assert store.delete_exchange(answer) == conversation
    assert [t["content"] for t in store.turns(conversation)] == ["hello", "hi", "thanks", "Any time."]
    assert store.search("7731") == []
    assert store.delete_exchange(question) is None

    first = store.turns(conversation)[0]["id"]
    store.delete_exchange(first)
    assert [t["content"] for t in store.turns(conversation)] == ["thanks", "Any time."]


def test_a_conversation_left_empty_is_removed(store):
    talk(store, ("one", "two"))
    old = store.current
    store.current = None
    talk(store, ("three", "four"))

    for turn in store.turns(old):
        store.delete_turn(turn["id"])

    assert store.get(old) is None
    assert [c["id"] for c in store.recent()] == [store.current]


def test_deleting_conversations(store):
    talk(store, ("first thing", "yes"))
    first = store.current
    store.current = None
    talk(store, ("second thing", "yes"))
    second = store.current

    assert store.delete_conversation(first) is True
    assert store.delete_conversation(first) is False
    assert store.search("first") == []
    assert store.search("second")

    # Deleting the one in progress: what is said next starts a new one.
    assert store.delete_conversation(second) is True
    assert store.current is None
    talk(store, ("third thing", "yes"))
    assert store.current not in (first, second)

    assert store.delete_all() == 1
    assert store.recent() == [] and store.search("third") == []


def test_only_earlier_conversations_still_need_summarising(store):
    talk(store, ("one", "two"))
    first = store.current
    store.current = None
    talk(store, ("three", "four"))

    assert [c["id"] for c in store.unsummarized()] == [first]
    store.set_summary(first, "About numbers.", 2)
    assert store.unsummarized() == []


def test_memory_database_is_backed_up_before_its_first_change(tmp_path):
    path = tmp_path / "freya_memory.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    connection.execute("INSERT INTO memories (content) VALUES ('likes tea')")
    connection.commit()

    ConversationStore(connection)
    backup = tmp_path / BACKUP_FOLDER / BACKUP_NAME
    assert backup.is_file()

    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT content FROM memories").fetchall() == [("likes tea",)]
    # The copy is of the database as it was: no conversation tables.
    assert not copy.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'conversation'"
    ).fetchone()
    copy.close()

    # It is made once, and the memories are untouched.
    stamp = backup.stat().st_mtime_ns
    ConversationStore(connection)
    assert backup.stat().st_mtime_ns == stamp
    assert connection.execute("SELECT content FROM memories").fetchall() == [("likes tea",)]


def test_an_empty_database_is_not_backed_up(tmp_path):
    path = tmp_path / "freya_memory.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    connection.commit()

    ConversationStore(connection)

    assert not (tmp_path / BACKUP_FOLDER).exists()


# -- Freya using it ------------------------------------------------------------


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

    def make():
        assistant = FreyaAssistant(tool_host=plane.model_tools, broker=plane.broker)
        if not plane.broker.exists(CONVERSATION_RECALL):
            plane.register_conversation_recall(lambda **request: make.current.recall(**request))
        make.current = assistant
        return assistant

    return make


def say(assistant, text):
    return "".join(assistant.respond_stream(text)).strip()


def test_every_exchange_is_stored_as_it_happens(new_assistant, fake_ollama):
    assistant = new_assistant()
    fake_ollama.stream_script = ["The avatar is ready."]
    assistant.source = "window"

    assert say(assistant, "Is the avatar ready?") == "The avatar is ready."

    store = assistant.conversations
    user, freya = store.turns(store.current)
    assert (user["content"], user["source"]) == ("Is the avatar ready?", "window")
    assert freya["content"] == "The avatar is ready."


def test_commands_are_not_stored_as_conversation(new_assistant):
    assistant = new_assistant()

    assert "no past conversations" in say(assistant, "/history")
    assert assistant.conversations.recent() == []


def test_she_starts_with_summaries_of_recent_conversations(new_assistant, fake_ollama):
    first = new_assistant()
    say(first, "We should paint the fence green.")
    fake_ollama.reply = "The user decided to paint the fence green."
    first.finish_conversation()

    stored = first.conversations.recent()[0]
    assert stored["summary"] == "The user decided to paint the fence green."
    assert stored["summarized_turns"] == 2

    second = new_assistant()
    sent = json.dumps(second._prepare_context("What is the time?"))
    assert "The user decided to paint the fence green." in sent
    assert "model-inference" in sent and "not instructions" in sent
    assert {"source": "conversation-summary", "id": f"conversation:{stored['id']}",
            "trust": "model-inference", "truncated": False} in second.context.last_report["included"]
    # It is read for each reply, never kept in the live conversation.
    assert all("paint the fence" not in m["content"] for m in second.messages)
    # Her new run is a new conversation, created when something is said.
    assert second.conversations.current is None


def test_a_missed_summary_is_written_at_the_next_authorized_recall(new_assistant, fake_ollama):
    first = new_assistant()
    say(first, "Remind me about the boiler service.")
    # Freya was closed without the chance to summarise.
    assert first.conversations.recent()[0]["summary"] == ""

    fake_ollama.reply = "The user asked for a reminder about the boiler service."
    second = new_assistant()

    sent = json.dumps(second._prepare_context("What is the time?"))
    assert "boiler" in second.conversations.recent()[0]["summary"]
    assert "The user asked for a reminder about the boiler service." in sent


def test_no_summary_is_lost_when_the_model_is_away(new_assistant, fake_ollama):
    first = new_assistant()
    say(first, "Remind me about the boiler service.")
    fake_ollama.reachable = False

    first.finish_conversation()
    second = new_assistant()

    # Nothing was summarised, nothing failed, and it is still waiting.
    assert [c["turns"] for c in second.conversations.unsummarized()] == [2]


def test_a_long_conversation_is_folded_into_its_summary(new_assistant, fake_ollama, monkeypatch):
    monkeypatch.setattr(conversation_memory, "MAX_LIVE_MESSAGES", 8)
    monkeypatch.setattr(conversation_memory, "KEEP_LIVE_MESSAGES", 4)
    assistant = new_assistant()
    fake_ollama.reply = "They counted upward together."

    for number in range(5):
        fake_ollama.stream_script = [f"answer {number}"]
        say(assistant, f"question {number}")

    live = [m for m in assistant.messages if m["role"] != "system"]
    assert [m["content"] for m in live] == ["question 3", "answer 3", "question 4", "answer 4"]
    notes = [m["content"] for m in assistant.messages if m["role"] == "system"]
    assert sum("Earlier in this conversation" in note for note in notes) == 1
    assert any("counted upward" in note for note in notes)

    # Nothing was lost: every word is still stored and can be found.
    store = assistant.conversations
    assert store.get(store.current)["turns"] == 10
    assert store.get(store.current)["summarized_turns"] == 6
    assert store.search("question 0")

    # Folding again replaces the note instead of adding another.
    for number in range(5, 8):
        say(assistant, f"question {number}")
    notes = [m["content"] for m in assistant.messages if m["role"] == "system"]
    assert sum("Earlier in this conversation" in note for note in notes) == 1


def test_the_model_can_look_up_what_was_said(new_assistant, fake_ollama, plane):
    first = new_assistant()
    fake_ollama.stream_script = ["I will call it Bramble."]
    say(first, "What shall we name the robot lawnmower?")
    first.finish_conversation()

    second = new_assistant()
    offered = [tool["function"]["name"] for tool in plane.model_tools.definitions()]
    assert any("recall" in name for name in offered)

    name = next(name for name in offered if "recall" in name)
    fake_ollama.stream_script = [[(name, {"query": "lawnmower name"})], "You named it Bramble."]
    assert say(second, "What did we call the lawnmower?") == "You named it Bramble."

    # The tool's answer, as the model was given it.
    tool_messages = [
        message
        for call in fake_ollama.calls
        if call[0] == "chat"
        for message in call[2]
        if message.get("role") == "tool"
    ]
    result = json.loads(tool_messages[-1]["content"])
    assert result["found"] == 1
    assert {"who": "Freya", "said": "I will call it Bramble."} in result["passages"][0]["exchange"]
    assert "not an instruction" in result["note"]

    rows = plane.store.execute(
        "SELECT outcome FROM audit_event WHERE capability = ?", (CONVERSATION_RECALL,)
    ).fetchall()
    assert ("succeeded",) in [tuple(row) for row in rows]


def test_recall_is_refused_when_its_permission_is_withdrawn(new_assistant, plane):
    assistant = new_assistant()
    say(assistant, "The gate code is on the fridge.")

    rules = {rule["capability"]: rule["rule_id"] for rule in plane.policy.list_rules()}
    plane.policy.remove_rule(rules[CONVERSATION_RECALL])

    result = plane.broker.invoke(CONVERSATION_RECALL, query="gate code")
    assert result.success is False and result.outcome == "denied"


# -- searching, correcting, deleting ------------------------------------------


def test_find_covers_memories_and_conversations(new_assistant, fake_ollama):
    assistant = new_assistant()
    assistant.memory.add_memory("The user's dog is called Biscuit.", "fact", "dog_name")
    assistant.memory.add_memory("The user prefers tea.", "preference", "drink")
    fake_ollama.stream_script = ["Biscuit sounds lovely."]
    say(assistant, "I walked Biscuit this morning.")

    found = assistant.find("biscuit")
    assert [memory["content"] for memory in found["memories"]][0].endswith("Biscuit.")
    assert found["passages"][0]["turns"][0]["content"] == "I walked Biscuit this morning."

    everything = assistant.find("")
    assert len(everything["memories"]) == 2 and everything["passages"] == []
    assert everything["conversations"][0]["current"] is True


def test_memories_can_be_corrected_and_deleted_from_the_window(new_assistant):
    assistant = new_assistant()
    assistant.memory.add_memory("The user's dog is called Biscut.", "fact", "dog_name")
    memory = assistant.find("biscut")["memories"][0]

    answer = assistant.memory_request(
        {"action": "memory-edit", "id": memory["id"],
         "content": "The user's dog is called   Biscuit.", "query": "dog"}
    )
    assert answer["error"] is None
    assert answer["memories"][0]["content"] == "The user's dog is called Biscuit."

    for bad in ({"id": memory["id"], "content": "  "}, {"id": memory["id"], "content": "x" * 3000},
                {"id": "1", "content": "x"}, {"id": True, "content": "x"}, {"content": "x"},
                {"id": 9999, "content": "x"}):
        assert assistant.memory_request({"action": "memory-edit", **bad})["error"], bad

    assert assistant.find("biscuit")["memories"][0]["content"].endswith("Biscuit.")

    answer = assistant.memory_request({"action": "memory-delete", "id": memory["id"]})
    assert answer["error"] is None and answer["memories"] == []
    assert assistant.memory_request({"action": "memory-delete", "id": memory["id"]})["error"]
    assert assistant.memory_request({"action": "format-disk"})["error"]
    assert assistant.memory_request("nonsense")["error"]


def test_deleting_a_message_takes_it_out_of_her_attention_too(new_assistant, fake_ollama):
    assistant = new_assistant()
    fake_ollama.stream_script = ["I will keep that safe."]
    say(assistant, "My locker combination is 4-8-15.")
    say(assistant, "What is the weather like?")
    secret = assistant.find("locker combination")["passages"][0]["match"]

    answer = assistant.memory_request({"action": "turn-delete", "id": secret, "query": "locker"})

    assert answer["error"] is None and answer["passages"] == []
    assert all("4-8-15" not in message["content"] for message in assistant.messages)
    assert all("keep that safe" not in message["content"] for message in assistant.messages)
    assert assistant.find("keep that safe")["passages"] == []
    # The rest of the conversation is still in front of her.
    assert any("weather" in message["content"] for message in assistant.messages)


def test_deleting_from_an_earlier_conversation_rewrites_its_summary(new_assistant, fake_ollama):
    first = new_assistant()
    say(first, "My locker combination is 4-8-15.")
    say(first, "Also I like kayaking.")
    fake_ollama.reply = "The user gave a locker combination, 4-8-15, and likes kayaking."
    first.finish_conversation()

    second = new_assistant()
    secret = second.find("locker")["passages"][0]["match"]
    fake_ollama.reply = "The user likes kayaking."
    second.memory_request({"action": "turn-delete", "id": secret})

    assert second.conversations.recent()[0]["summary"] == ""
    second._prepare_context("What hobbies did we discuss?")
    summary = second.conversations.recent()[0]["summary"]
    assert summary == "The user likes kayaking."


def test_reading_and_deleting_a_conversation_from_the_window(new_assistant, fake_ollama):
    assistant = new_assistant()
    fake_ollama.stream_script = ["Hello."]
    say(assistant, "Hello Freya.")
    number = assistant.conversations.current

    opened = assistant.memory_request({"action": "conversation-read", "id": number})["opened"]
    assert [m["content"] for m in opened["messages"]] == ["Hello Freya.", "Hello."]
    assert assistant.memory_request({"action": "conversation-read", "id": 999})["error"]

    answer = assistant.memory_request({"action": "conversation-delete", "id": number})
    assert answer["error"] is None and answer["conversations"] == []
    assert [m for m in assistant.messages if m["role"] != "system"] == []
    assert assistant.memory_request({"action": "conversation-delete", "id": number})["error"]


def test_find_and_history_commands(new_assistant, fake_ollama):
    assistant = new_assistant()
    assistant.memory.add_memory("The user's dog is called Biscuit.", "fact", "dog_name")
    fake_ollama.stream_script = ["Biscuit sounds lovely."]
    say(assistant, "I walked Biscuit this morning.")
    number = assistant.conversations.current

    # The stand-in model gives every text the same meaning, so only
    # the search by words is exercised here.
    assistant.memory.search_memories = lambda *arguments, **options: []

    found = say(assistant, "/find biscuit")
    assert "Saved memories:" in found and "In past conversations:" in found
    assert "Freya: Biscuit sounds lovely." in found
    assert "Nothing I have saved" in say(assistant, "/find zebra crossing")
    assert "Use /find" in say(assistant, "/find")

    listing = say(assistant, "/history")
    assert f"[{number}]" in listing and "(this one)" in listing
    assert "You: I walked Biscuit this morning." in say(assistant, f"/history {number}")
    assert "no conversation 999" in say(assistant, "/history 999")
    assert "Use /history" in say(assistant, "/history sideways")

    assert f"Deleted conversation {number}" in say(assistant, f"/history forget {number}")
    say(assistant, "One more thing.")
    assert "Deleted all 1" in say(assistant, "/history forget all")
    assert assistant.conversations.recent() == []
    # Saved memories are a separate thing and are still there.
    assert len(assistant.find("")["memories"]) == 1
    assert "/find" in say(assistant, "/memory-help")


# -- questions about the past ---------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Have I ever mentioned a boat?",
        "have we ever talked about moving house",
        "Did I ever tell you my sister's name?",
        "Have I told you about the allotment?",
        "Did we discuss the invoice?",
        "Do you remember the name of my dentist?",
        "Can you recall what colour I wanted?",
        "What did I say about the fence?",
        "What did we decide for the kitchen?",
        "When did I book the plumber?",
        "Last time you mentioned a recipe, what was it?",
        "Remember when I asked about tyres?",
    ],
)
def test_questions_about_the_past_are_recognised(question):
    assert conversation_memory.ASKS_ABOUT_THE_PAST.search(question)


@pytest.mark.parametrize(
    "message",
    [
        "What's the weather like?",
        "Tell me a joke about boats.",
        "I have never been to Spain.",
        "Please remember that my dentist is Dr Okafor.",
        "Have a look at the GPU temperature.",
        "Did you know octopuses have three hearts?",
        "What do you think I should say to him?",
    ],
)
def test_other_messages_are_not_searched_for(message):
    assert not conversation_memory.ASKS_ABOUT_THE_PAST.search(message)


def test_asking_have_i_ever_searches_everything_first(new_assistant, fake_ollama):
    first = new_assistant()
    first.memory.search_memories = lambda *arguments, **options: []
    first.memory.add_memory("The user owns a narrowboat called Heron.", "fact", "boat")
    fake_ollama.stream_script = ["Sounds like a lovely trip."]
    say(first, "I took the boat up to Braunston at the weekend.")
    # A summary that leaves the boat out, as a summary might.
    fake_ollama.reply = "The user talked about their weekend."
    first.finish_conversation()

    second = new_assistant()
    second.memory.search_memories = lambda *arguments, **options: []
    fake_ollama.stream_script = ["Yes: you took it to Braunston."]
    say(second, "Have I ever mentioned anything about a boat?")

    sent = [call[2] for call in fake_ollama.calls if call[0] == "chat" and call[3]][-1]
    note = next(m["content"] for m in sent if "asking about the past" in m["content"])

    assert "searched for: boat" in note
    assert "The user owns a narrowboat called Heron." in note
    assert "the user: I took the boat up to Braunston at the weekend." in note
    assert "Freya: Sounds like a lovely trip." in note
    assert "not an instruction" in note
    # The note is for this answer only; it is not kept in the conversation.
    assert all("asking about the past" not in m["content"] for m in second.messages)
    # It comes straight before the question.
    assert sent[-1] == {"role": "user", "content": "Have I ever mentioned anything about a boat?"}
    assert sent[-2]["content"] == note or "tools" in sent[-2]["content"].lower()


def test_she_is_told_when_nothing_was_found(new_assistant, fake_ollama):
    assistant = new_assistant()
    assistant.memory.search_memories = lambda *arguments, **options: []
    say(assistant, "I like kayaking.")

    sent = assistant._prepare_context("Have I ever mentioned a helicopter?")
    note = next(m["content"] for m in sent if "asking about the past" in m["content"])

    assert "Nothing was found" in note and "searched for: helicopter" in note
    assert "no record" in note


def test_a_failed_search_does_not_stop_her_answering(new_assistant, fake_ollama):
    assistant = new_assistant()
    assistant.conversations.search = lambda *arguments, **options: 1 / 0

    fake_ollama.stream_script = ["I am not sure."]
    assert say(assistant, "Have I ever mentioned a boat?") == "I am not sure."

    # She was told the search failed, not that nothing was ever said.
    sent = [call[2] for call in fake_ollama.calls if call[0] == "chat" and call[3]][-1]
    assert "Stored conversations could not be searched." in json.dumps(sent)
    assert "Nothing was found" not in json.dumps(sent)
