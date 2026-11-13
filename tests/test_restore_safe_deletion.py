import sqlite3
from types import SimpleNamespace

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.assistant import FreyaAssistant
from freya.memory import deletion_ledger, memory_manager
from freya.memory.conversations import ConversationStore
from freya.memory.deletion_ledger import (
    TABLES,
    ProtectionSetupError,
    StorageQuarantined,
    initialize_protection,
)
from freya.memory.memory_manager import MemoryManager


# Everything stored here is made up for the tests.
SECRET = "quillfield7731"


class Home:
    """
    A memory database, its deletion ledger, and its backup, each in a
    folder of its own, and the managers opened on them.
    """

    def __init__(self, root):
        self.database = root / "store" / "memory.db"
        self.ledger = root / "ledger" / "deletions.db"
        self.backup = root / "backups" / "before-protection.db"
        self._opened = []

        for path in (self.database, self.ledger, self.backup):
            path.parent.mkdir()

    def open(self, protected=True):
        memory = MemoryManager(self.ledger if protected else None)
        self._opened.append(memory)

        return memory, ConversationStore(memory.connection, memory.ledger)

    def close(self):
        for memory in self._opened:
            memory.connection.close()

            if memory.ledger is not None:
                memory.ledger.close()

        self._opened.clear()

    def protect(self):
        self.close()

        return initialize_protection(self.database, self.ledger, self.backup)

    def snapshot(self) -> bytes:
        """A copy of the memory database, as a backup would take it."""
        self.close()

        return self.database.read_bytes()

    def restore(self, snapshot: bytes) -> None:
        self.close()
        self.database.write_bytes(snapshot)

    def tombstones(self) -> list:
        connection = sqlite3.connect(self.ledger)

        try:
            return connection.execute(
                "SELECT artifact_id, kind FROM tombstone ORDER BY seq"
            ).fetchall()
        finally:
            connection.close()


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


@pytest.fixture
def home(plane, tmp_path, monkeypatch):
    home = Home(tmp_path)
    # Never touch the owner's real memory database.
    monkeypatch.setattr(memory_manager, "DATA_DIR", home.database.parent)
    monkeypatch.setattr(memory_manager, "DATABASE_PATH", home.database)
    yield home
    home.close()


@pytest.fixture
def protected(home):
    """A memory database with Freya's tables, protected while empty."""
    home.open(protected=False)
    home.protect()

    return home


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


def schema(connection) -> list:
    return connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()


def identifiers(connection) -> list:
    return [
        row[0]
        for table in TABLES.values()
        for row in connection.execute(f"SELECT artifact_id FROM {table}")
    ]


def well_formed(value) -> bool:
    return isinstance(value, str) and len(value) == 32 and set(value) <= set("0123456789abcdef")


def applied(connection) -> int:
    return int(
        connection.execute(
            "SELECT value FROM storage_meta WHERE key = 'ledger_seq'"
        ).fetchone()[0]
    )


def columns(connection, table) -> set:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def proposal(memory_id, content, key=None):
    return {
        "id": memory_id, "current_type": "fact", "current_key": key, "content": content,
        "proposed_type": "fact", "proposed_key": f"key_{memory_id}",
        "blocked": False, "block_reason": None,
    }


def embeds(fake_ollama) -> list:
    return [call for call in fake_ollama.calls if call[0] == "embed"]


# -- without a ledger ----------------------------------------------------------


def test_without_a_ledger_nothing_is_different(home):
    memory, store = home.open(protected=False)
    assert memory.ledger is None and store.ledger is None

    forgotten = memory.add_memory("The spare key is under the heron statue.", "fact")
    store.add_exchange(f"the garage code is {SECRET}", "Noted.")
    assert memory.delete_memory(forgotten) is True
    assert store.delete_all() == 1

    assert "storage_meta" not in [row[1] for row in schema(memory.connection)]
    assert "artifact_id" not in columns(memory.connection, "memories")
    assert not home.ledger.exists() and not home.backup.exists()


# -- protecting a database -------------------------------------------------------


def test_protecting_backs_up_first_and_gives_every_row_an_identifier(home):
    memory, store = home.open(protected=False)
    edited = memory.add_memory("The user prefers tea.", "preference", "drink")
    memory.update_memory(edited, "The user prefers green tea.")
    store.add_exchange("hello", "hi")
    before = stored_text(memory.connection)

    store_id = home.protect()

    # The backup is the database as it was, and is sound.
    backup = sqlite3.connect(home.backup)
    assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert stored_text(backup) == before
    assert "artifact_id" not in columns(backup, "memories")
    backup.close()

    ledger = sqlite3.connect(home.ledger)
    assert ledger.execute("SELECT key, value FROM ledger_meta").fetchall() == [
        ("store_id", store_id)
    ]
    assert ledger.execute("SELECT COUNT(*) FROM tombstone").fetchone() == (0,)
    ledger.close()

    memory, store = home.open()
    assert dict(memory.connection.execute("SELECT key, value FROM storage_meta")) == {
        "store_id": store_id,
        "ledger_seq": "0",
    }

    # One memory, one earlier version, one conversation, two messages.
    found = identifiers(memory.connection)
    assert len(found) == len(set(found)) == 5
    assert all(well_formed(value) for value in found)
    assert memory.connection.execute(
        "SELECT h.memory_artifact_id = m.artifact_id FROM memory_history h "
        "JOIN memories m ON m.id = h.memory_id"
    ).fetchall() == [(1,)]

    # Nothing that was stored has changed.
    assert memory.get_memory(edited)["content"] == "The user prefers green tea."
    assert [turn["content"] for turn in store.turns(1)] == ["hello", "hi"]


def test_a_database_is_protected_only_once(protected, tmp_path):
    again = tmp_path / "ledger" / "second.db"
    copy = tmp_path / "backups" / "second.db"

    with pytest.raises(ProtectionSetupError, match="already protected"):
        initialize_protection(protected.database, again, copy)

    with pytest.raises(ProtectionSetupError, match="already exists"):
        protected.protect()

    assert not again.exists() and not copy.exists()


def assert_not_protected(home, before: bytes) -> None:
    assert home.database.read_bytes() == before
    assert not home.ledger.exists()
    # It still opens the old way.
    memory, _ = home.open(protected=False)
    assert memory.get_all_memories()[0][3] == "The user prefers tea."


@pytest.fixture
def unprotected(home):
    memory, _ = home.open(protected=False)
    memory.add_memory("The user prefers tea.", "preference", "drink")

    return home, home.snapshot()


def test_an_existing_backup_is_never_overwritten(unprotected):
    home, before = unprotected
    home.backup.write_bytes(b"an earlier backup")

    with pytest.raises(ProtectionSetupError, match="already exists"):
        home.protect()

    assert home.backup.read_bytes() == b"an earlier backup"
    assert_not_protected(home, before)


def test_a_backup_that_cannot_be_written_stops_protection(unprotected, tmp_path):
    home, before = unprotected
    home.backup = tmp_path / "no-such-folder" / "backup.db"

    with pytest.raises(ProtectionSetupError):
        home.protect()

    assert_not_protected(home, before)


def test_a_backup_that_does_not_match_stops_protection(unprotected, monkeypatch):
    home, before = unprotected
    monkeypatch.setattr(deletion_ledger, "_same_contents", lambda first, second: False)

    with pytest.raises(ProtectionSetupError, match="does not match"):
        home.protect()

    # A copy that failed its check is not left lying about.
    assert not home.backup.exists()
    assert_not_protected(home, before)


def test_a_failure_after_the_backup_undoes_every_change(unprotected, monkeypatch):
    home, before = unprotected
    connection = sqlite3.connect(home.database)
    was = schema(connection), stored_text(connection)
    connection.close()
    add = deletion_ledger._add_identities

    def add_then_fail(source, store_id):
        add(source, store_id)
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(deletion_ledger, "_add_identities", add_then_fail)

    with pytest.raises(ProtectionSetupError, match="disk full"):
        home.protect()

    connection = sqlite3.connect(home.database)
    assert (schema(connection), stored_text(connection)) == was
    connection.close()

    # No ledger is left to be mistaken for this database's. The checked
    # backup is a true copy and stays.
    assert not home.ledger.exists() and home.backup.is_file()
    memory, _ = home.open(protected=False)
    assert memory.ledger is None


@pytest.mark.parametrize(
    "ledger, backup",
    [
        ("store/memory.db", "backups/b.db"),
        ("store/deletions.db", "backups/b.db"),
        ("store/deeper/deletions.db", "backups/b.db"),
        ("ledger/l.db", "store/memory.db"),
        ("ledger/l.db", "store/memory.db-journal"),
        ("ledger/l.db", "ledger/l.db"),
        ("ledger/l.db", "ledger/../ledger/l.db"),
    ],
)
def test_paths_that_overlap_are_refused(unprotected, tmp_path, ledger, backup):
    home, before = unprotected
    home.ledger, home.backup = tmp_path / ledger, tmp_path / backup

    with pytest.raises(ProtectionSetupError):
        home.protect()

    assert home.database.read_bytes() == before
    # Nothing was created.
    assert [path.name for path in (tmp_path / "store").iterdir()] == ["memory.db"]
    assert not list((tmp_path / "ledger").iterdir())
    assert not list((tmp_path / "backups").iterdir())


def test_a_database_without_freyas_tables_cannot_be_protected(home):
    connection = sqlite3.connect(home.database)
    connection.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(ProtectionSetupError, match="memory_history"):
        home.protect()

    assert not home.ledger.exists() and not home.backup.exists()


# -- identifiers -------------------------------------------------------------------


def test_every_way_of_adding_a_row_gives_it_its_own_identifier(protected):
    memory, store = protected.open()
    connection = memory.connection

    edited = memory.add_memory("The user prefers tea.", "preference", "drink")
    memory.update_memory(edited, "The user prefers green tea.")
    memory.process_candidate(
        {"key": "drink", "type": "preference", "content": "The user prefers oolong."}
    )
    earliest = memory.get_memory_history(edited)[0][0]
    assert memory.restore_memory(edited, earliest)["success"] is True

    keep = memory.add_memory("The user's bank is Larkspur Mutual.", "fact")
    duplicate = memory.add_memory("User banks with Larkspur Mutual.", "fact")
    assert memory.resolve_duplicate(keep, duplicate, "fact", "bank") is True
    memory.save_migration_plan([proposal(keep, "The user's bank is Larkspur Mutual.", "bank")])
    assert memory.apply_migration_plan()["applied"] == [keep]

    store.add_exchange("hello", "hi", "voice")
    store.current = None
    store.add_exchange("good morning", "Good morning.")

    # A row added behind the managers' backs gets one as well.
    connection.execute("INSERT INTO memories (content) VALUES ('Added directly.')")
    connection.commit()

    found = identifiers(connection)
    counts = [
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in TABLES.values()
    ]
    # Memories, earlier versions, conversations, messages.
    assert counts == [3, 6, 2, 4]
    assert len(found) == len(set(found)) == sum(counts)
    assert all(well_formed(value) for value in found)

    # Each earlier version remembers which memory it was a version of.
    known = dict(connection.execute("SELECT id, artifact_id FROM memories"))
    for memory_id, remembered in connection.execute(
        "SELECT memory_id, memory_artifact_id FROM memory_history"
    ).fetchall():
        assert well_formed(remembered)
        assert known.get(memory_id, remembered) == remembered


@pytest.mark.parametrize("table", TABLES.values())
def test_a_row_cannot_bring_or_change_its_identifier(protected, table):
    memory, store = protected.open()
    connection = memory.connection
    memory.update_memory(memory.add_memory("The user prefers tea."), "The user prefers coffee.")
    store.add_exchange("hello", "hi")
    taken = connection.execute(f"SELECT artifact_id FROM {table}").fetchone()[0]
    names = ", ".join(
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})")
        if row[5] == 0
    )

    for claimed in (taken, "0" * 32):
        with pytest.raises(sqlite3.Error):
            # A copy of an existing row, claiming an identifier.
            connection.execute(
                f"INSERT INTO {table} ({names}) "
                f"SELECT {names.replace('artifact_id', '?', 1)} FROM {table} LIMIT 1",
                (claimed,),
            )

        connection.rollback()

    with pytest.raises(sqlite3.Error):
        connection.execute(f"UPDATE {table} SET artifact_id = ?", ("f" * 32,))

    connection.rollback()
    assert [row[0] for row in connection.execute(f"SELECT artifact_id FROM {table}")].count(
        taken
    ) == 1


# -- the ledger ----------------------------------------------------------------------


def test_the_ledger_holds_no_words_of_what_was_deleted(protected):
    memory, store = protected.open()
    forgotten = memory.add_memory("The spare key is under the heron statue.", "fact", "spare_key")
    memory.update_memory(forgotten, "The spare key is under the blue pot.")
    store.add_exchange(f"the garage code is {SECRET}", f"Noted: {SECRET}.")
    store.set_summary(store.current, f"The user gave the garage code {SECRET}.", 2)

    assert memory.delete_memory(forgotten) is True
    assert store.delete_conversation(store.current) is True
    assert applied(memory.connection) == 5
    protected.close()

    ledger = sqlite3.connect(protected.ledger)
    assert {row[1] for row in schema(ledger) if row[0] == "table"} == {
        "ledger_meta", "tombstone", "sqlite_sequence",
    }
    assert [row[1] for row in ledger.execute("PRAGMA table_info(tombstone)")] == [
        "seq", "artifact_id", "kind", "deleted_at",
    ]
    assert [row[0] for row in ledger.execute("SELECT key FROM ledger_meta")] == ["store_id"]

    rows = ledger.execute("SELECT seq, artifact_id, kind, deleted_at FROM tombstone").fetchall()
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
    assert sorted(row[2] for row in rows) == ["conversation", "history", "memory", "turn", "turn"]
    assert all(well_formed(row[1]) and len(row[3]) == 25 for row in rows)

    # Tombstones are only ever added.
    for statement in ("DELETE FROM tombstone", "UPDATE tombstone SET kind = 'turn'"):
        with pytest.raises(sqlite3.Error):
            ledger.execute(statement)
        ledger.rollback()

    ledger.close()

    raw = protected.ledger.read_bytes().lower()
    for words in (b"heron", b"blue pot", b"spare", b"garage", SECRET.encode()):
        assert words not in raw


# -- restoring an older copy -----------------------------------------------------------


def test_a_forgotten_memory_stays_forgotten_when_an_older_copy_is_restored(protected):
    memory, _ = protected.open()
    first = memory.add_memory("The alarm code is 2468.", "fact", "alarm")
    memory.update_memory(first, "The alarm is serviced in May.")
    second = memory.add_memory("the alarm code is 2468.", "fact", "alarm_code")
    third = memory.add_memory("The spare key is under the heron statue.", "fact", "spare_key")
    kept = memory.add_memory("The user prefers tea.", "preference", "drink")
    memory.save_migration_plan(
        [
            proposal(second, "the alarm code is 2468.", "alarm_code"),
            proposal(third, "The spare key is under the heron statue.", "spare_key"),
            proposal(kept, "The user prefers tea.", "drink"),
        ]
    )
    older = protected.snapshot()
    gone = ("2468", "heron statue", "blue pot")

    # After the copy was made: one is edited and then forgotten, the
    # other forgotten as it is.
    memory, _ = protected.open()
    memory.update_memory(third, "The spare key is under the blue pot.")
    assert memory.delete_memory(third) is True
    assert memory.delete_memory(second) is True
    text = stored_text(memory.connection)
    assert not [words for words in gone if words in text]
    assert [entry["id"] for entry in memory.get_migration_plan()] == [kept]
    assert memory.delete_memory(third) is False

    # The copy holds the edited one as it was before the edit.
    protected.restore(older)
    restored = sqlite3.connect(protected.database)
    text = stored_text(restored)
    assert "2468" in text and "heron statue" in text
    restored.close()

    memory, _ = protected.open()
    text = stored_text(memory.connection)
    assert not [words for words in gone if words in text]
    assert memory.get_memory(second) is None and memory.get_memory(third) is None
    # The first memory's old version held the same words as the
    # second: gone too, by its own identifier. The memory itself stays.
    assert memory.get_memory_history(first) == []
    assert memory.get_memory(first)["content"] == "The alarm is serviced in May."
    assert memory.get_memory(kept)["content"] == "The user prefers tea."
    # The saved plan held copies of the forgotten words.
    assert memory.get_migration_plan() == []
    assert {found["id"] for found in memory.search_memories("alarm", limit=10)} == {first, kept}
    # Two memories, and an old version of each of two memories.
    assert applied(memory.connection) == len(protected.tombstones()) == 4


def test_forgetting_a_memory_that_only_has_history_survives_a_restore(protected):
    memory, _ = protected.open()
    keep = memory.add_memory("The user's bank is Larkspur Mutual.", "fact")
    duplicate = memory.add_memory("User banks with Larkspur.", "fact")
    older = protected.snapshot()

    # After the copy: removed as a duplicate, which archives it, and
    # then forgotten by its number.
    memory, _ = protected.open()
    assert memory.resolve_duplicate(keep, duplicate, "fact", "bank") is True
    assert memory.get_memory(duplicate) is None
    assert memory.delete_memory(duplicate) is True
    assert "user banks with" not in stored_text(memory.connection)

    # In the older copy it is still a live memory.
    protected.restore(older)
    memory, _ = protected.open()
    assert memory.get_memory(duplicate) is None
    assert "user banks with" not in stored_text(memory.connection)
    assert memory.get_memory(keep)["content"] == "The user's bank is Larkspur Mutual."


def talk(store) -> int:
    """Two conversations; the first holds the secret and a summary of it."""
    store.add_exchange("hello", "hi")
    store.add_exchange(f"the garage code is {SECRET}", f"Noted: {SECRET}.")
    first = store.current
    store.set_summary(first, f"The user gave the garage code {SECRET}.", 4)
    store.current = None
    store.add_exchange("the weather is fine", "It is.")
    store.current = None

    return first


def said(store, conversation) -> list:
    return [turn["id"] for turn in store.turns(conversation) if SECRET in turn["content"]]


WAYS = {
    "turn": lambda store, first: [store.delete_turn(turn) for turn in said(store, first)],
    "exchange": lambda store, first: store.delete_exchange(said(store, first)[1]),
    "conversation": lambda store, first: store.delete_conversation(first),
    "all": lambda store, first: store.delete_all(),
}


@pytest.mark.parametrize("way", WAYS)
def test_deleted_conversation_text_stays_deleted_when_an_older_copy_is_restored(protected, way):
    _, store = protected.open()
    first = talk(store)
    older = protected.snapshot()

    def check(memory, store):
        assert SECRET not in stored_text(memory.connection)
        assert store.search(SECRET) == [] and store.search("garage") == []

        if way in ("turn", "exchange"):
            # The rest of the conversation is kept and can be found;
            # its summary repeated the secret, so it is cleared.
            assert [turn["content"] for turn in store.turns(first)] == ["hello", "hi"]
            assert store.get(first)["summary"] == ""
            assert store.get(first)["summarized_turns"] == 0
            assert store.search("hello")
        else:
            assert store.get(first) is None
            assert store.search("hello") == []

        assert bool(store.search("weather")) == (way != "all")
        assert len(store.recent()) == {"turn": 2, "exchange": 2, "conversation": 1, "all": 0}[way]

    memory, store = protected.open()
    WAYS[way](store, first)
    check(memory, store)

    protected.restore(older)
    restored = sqlite3.connect(protected.database)
    assert SECRET in stored_text(restored)
    restored.close()

    memory, store = protected.open()
    check(memory, store)
    assert applied(memory.connection) == len(protected.tombstones())


def test_a_conversation_emptied_by_a_deletion_is_recorded_too(protected):
    _, store = protected.open()
    store.add_exchange(f"the garage code is {SECRET}", "Noted.")
    emptied = store.current
    store.set_summary(emptied, f"A garage code, {SECRET}.", 2)
    older = protected.snapshot()

    memory, store = protected.open()
    assert store.delete_exchange(store.turns(emptied)[0]["id"]) == emptied
    assert store.get(emptied) is None
    assert sorted(kind for _, kind in protected.tombstones()) == ["conversation", "turn", "turn"]

    protected.restore(older)
    memory, store = protected.open()
    assert store.get(emptied) is None
    assert SECRET not in stored_text(memory.connection)


def test_opening_again_changes_nothing_more(protected):
    _, store = protected.open()
    first = talk(store)
    older = protected.snapshot()

    _, store = protected.open()
    store.delete_exchange(said(store, first)[0])
    protected.restore(older)

    # The first open after the restore removes it again.
    memory, store = protected.open()
    store.set_summary(first, "They greeted each other.", 2)
    once = stored_text(memory.connection), schema(memory.connection), protected.tombstones()
    protected.close()

    for _ in range(2):
        memory, store = protected.open()
        assert (
            stored_text(memory.connection), schema(memory.connection), protected.tombstones()
        ) == once
        # A summary written since is not cleared a second time.
        assert store.get(first)["summary"] == "They greeted each other."
        protected.close()


# -- numbers given out again ---------------------------------------------------------


def test_a_new_memory_given_a_forgotten_ones_number_is_not_forgotten(protected):
    memory, _ = protected.open()
    memory.add_memory("The user prefers tea.", "preference", "drink")
    older = protected.snapshot()

    memory, _ = protected.open()
    forgotten = memory.add_memory("The spare key is under the heron statue.", "fact")
    memory.update_memory(forgotten, "The spare key is under the blue pot.")
    assert memory.delete_memory(forgotten) is True

    # The older copy has not counted that far, so the number comes round again.
    protected.restore(older)
    memory, _ = protected.open()
    reused = memory.add_memory("The user's bicycle is green.", "fact")
    assert reused == forgotten
    memory.update_memory(reused, "The user's bicycle is dark green.")

    memory, _ = protected.open()
    assert memory.get_memory(reused)["content"] == "The user's bicycle is dark green."
    assert len(memory.get_memory_history(reused)) == 1

    # And it can be forgotten in its own right.
    assert memory.delete_memory(reused) is True
    assert "bicycle" not in stored_text(memory.connection)


def test_new_messages_given_deleted_ones_numbers_are_not_deleted(protected):
    _, store = protected.open()
    store.add_exchange("hello", "hi")
    older = protected.snapshot()

    _, store = protected.open()
    store.add_exchange(f"the garage code is {SECRET}", "Noted.")
    deleted = store.current
    numbers = [turn["id"] for turn in store.turns(deleted)]
    assert store.delete_conversation(deleted) is True

    protected.restore(older)
    _, store = protected.open()
    store.add_exchange("the pelican is back", "So it is.")
    assert store.current == deleted
    assert [turn["id"] for turn in store.turns(deleted)] == numbers

    _, store = protected.open()
    assert [turn["content"] for turn in store.turns(deleted)] == [
        "the pelican is back", "So it is.",
    ]
    assert store.search("pelican")[0]["match"] == numbers[0]


# -- when a deletion fails -------------------------------------------------------------


def test_deletion_does_not_commit_an_unrelated_open_transaction(protected):
    memory, _ = protected.open()
    identifier = memory.add_memory("Original reminder.")
    memory.connection.execute(
        "UPDATE memories SET content = 'Uncommitted reminder.' WHERE id = ?", (identifier,)
    )

    with pytest.raises(sqlite3.OperationalError, match="current memory transaction"):
        memory.delete_memory(identifier)

    assert memory.connection.in_transaction
    assert protected.tombstones() == []
    memory.connection.rollback()
    assert memory.get_memory(identifier)["content"] == "Original reminder."


@pytest.mark.parametrize("persisted", [False, True])
def test_an_uncertain_ledger_commit_quarantines_until_reopen(protected, persisted):
    memory, _ = protected.open()
    identifier = memory.add_memory("Uncertain commit test.")
    connection = memory.ledger.connection

    class UncertainCommit:
        def execute(self, statement, *arguments):
            if statement == "COMMIT":
                if persisted:
                    connection.execute(statement, *arguments)
                raise sqlite3.OperationalError("Commit acknowledgement lost.")
            return connection.execute(statement, *arguments)

        def __getattr__(self, name):
            return getattr(connection, name)

    memory.ledger.connection = UncertainCommit()
    with pytest.raises(StorageQuarantined, match="commit is uncertain"):
        memory.delete_memory(identifier)
    assert memory.ledger.quarantined
    with pytest.raises(sqlite3.ProgrammingError):
        memory.get_memory(identifier)

    protected.close()
    recovered, _ = protected.open()
    assert (recovered.get_memory(identifier) is None) is persisted


def test_quarantine_returns_a_safe_memory_tab_error_and_blocks_generation(protected, fake_ollama):
    assistant = FreyaAssistant(memory_ledger_path=protected.ledger)
    identifier = assistant.memory.add_memory("Never serve this quarantined fact.")
    assistant.messages.append({"role": "user", "content": "Never serve this quarantined fact."})
    assistant.memory.connection.execute(
        "CREATE TEMP TRIGGER refuse BEFORE DELETE ON memories "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    fake_ollama.calls.clear()

    result = assistant.memory_request({"action": "memory-delete", "id": identifier})
    assert "quarantined" in result["error"]
    assert result["memories"] == result["passages"] == result["conversations"] == []
    assert assistant.memory_request({"action": "search"})["memories"] == []
    with pytest.raises(StorageQuarantined):
        assistant._prepare_context("What do you remember?")
    assert fake_ollama.calls == []


def test_a_failure_before_the_ledger_has_it_leaves_everything_as_it_was(protected):
    memory, store = protected.open()
    kept = memory.add_memory("The alarm code is 2468.", "fact", "alarm")
    memory.update_memory(kept, "The alarm code is 1357.")
    store.add_exchange(f"the garage code is {SECRET}", "Noted.")
    conversation = store.current
    question = store.turns(conversation)[0]["id"]
    memory.ledger.connection.execute(
        "CREATE TEMP TRIGGER refuse BEFORE INSERT ON tombstone "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    before = stored_text(memory.connection)

    for delete in (
        lambda: memory.delete_memory(kept),
        lambda: store.delete_turn(question),
        lambda: store.delete_exchange(question),
        lambda: store.delete_conversation(conversation),
        store.delete_all,
    ):
        with pytest.raises(sqlite3.Error):
            delete()

    # Nothing was recorded, nothing was removed, and she carries on.
    assert protected.tombstones() == []
    assert stored_text(memory.connection) == before
    assert not memory.ledger.quarantined
    assert memory.get_memory(kept)["content"] == "The alarm code is 1357."
    assert store.search(SECRET) and store.current == conversation


@pytest.mark.parametrize("kind", ["memory", "conversation"])
def test_a_failure_after_the_ledger_has_it_closes_the_database_until_it_is_repaired(
    protected, kind
):
    memory, store = protected.open()
    forgotten = memory.add_memory("The safe combination is 31-41-59.", "fact", "safe")
    store.add_exchange(f"the garage code is {SECRET}", "Noted.")
    store.current = None
    table = "memories" if kind == "memory" else "conversation_turn"
    memory.connection.execute(
        f"CREATE TEMP TRIGGER refuse BEFORE DELETE ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )

    with pytest.raises(StorageQuarantined):
        if kind == "memory":
            memory.delete_memory(forgotten)
        else:
            store.delete_conversation(1)

    # The deletion is on record, so the text must not be served: the
    # connection both of them share is closed.
    assert memory.ledger.quarantined
    assert len(protected.tombstones()) == (1 if kind == "memory" else 3)

    for read in (memory.get_all_memories, lambda: memory.get_memory(forgotten), store.recent):
        with pytest.raises(sqlite3.ProgrammingError):
            read()

    assert store.search(SECRET) == []

    with pytest.raises(StorageQuarantined):
        memory.ledger.forget(memory.connection, [])

    # It was not removed: the file still has it, until the next open.
    protected.close()
    left = sqlite3.connect(protected.database)
    gone = "31-41-59" if kind == "memory" else SECRET
    assert gone in stored_text(left)
    left.close()

    memory, store = protected.open()
    assert gone not in stored_text(memory.connection)
    assert applied(memory.connection) == len(protected.tombstones())

    if kind == "memory":
        assert memory.get_memory(forgotten) is None and store.search(SECRET)
    else:
        assert store.recent() == [] and memory.get_memory(forgotten) is not None


# -- a pair that does not belong together ------------------------------------------------


@pytest.fixture
def at_rest(protected, fake_ollama):
    """
    A protected database and its ledger with one deletion made, closed.
    One memory has no embedding yet, so that an open which gets as far
    as serving would ask the model for one.
    """
    memory, _ = protected.open()
    forgotten = memory.add_memory("The spare key is under the heron statue.", "fact")
    protected.close()
    earlier_ledger = protected.ledger.read_bytes()

    memory, _ = protected.open()
    assert memory.delete_memory(forgotten) is True
    fake_ollama.reachable = False
    memory.add_memory("The user's cat is called Marzipan.", "fact", "cat_name")
    fake_ollama.reachable = True
    protected.close()
    fake_ollama.calls.clear()

    return SimpleNamespace(home=protected, earlier_ledger=earlier_ledger)


def another_stores_ledger(at_rest, tmp_path) -> bytes:
    other = tmp_path / "other"
    other.mkdir()
    (other / "store").mkdir()
    database = other / "store" / "memory.db"
    database.write_bytes(at_rest.home.backup.read_bytes())
    initialize_protection(database, other / "ledger.db", other / "backup.db")

    return (other / "ledger.db").read_bytes()


def drop_a_guard(home) -> None:
    connection = sqlite3.connect(home.database)
    connection.execute("DROP TRIGGER memories_artifact_id_assign")
    connection.commit()
    connection.close()


BROKEN = {
    "missing ledger": lambda at_rest, tmp_path: at_rest.home.ledger.unlink(),
    "empty ledger": lambda at_rest, tmp_path: at_rest.home.ledger.write_bytes(b""),
    "damaged ledger": lambda at_rest, tmp_path: at_rest.home.ledger.write_bytes(
        b"not a database " * 64
    ),
    "another store's ledger": lambda at_rest, tmp_path: at_rest.home.ledger.write_bytes(
        another_stores_ledger(at_rest, tmp_path)
    ),
    "ledger from before the deletion": lambda at_rest, tmp_path: at_rest.home.ledger.write_bytes(
        at_rest.earlier_ledger
    ),
    "database from before protection": lambda at_rest, tmp_path: at_rest.home.database.write_bytes(
        at_rest.home.backup.read_bytes()
    ),
    "database without its guards": lambda at_rest, tmp_path: drop_a_guard(at_rest.home),
    "no ledger named": lambda at_rest, tmp_path: None,
}


@pytest.mark.parametrize("broken", BROKEN)
def test_a_pair_that_fails_its_checks_is_not_opened_or_changed(
    at_rest, tmp_path, fake_ollama, broken
):
    home = at_rest.home
    BROKEN[broken](at_rest, tmp_path)
    database = home.database.read_bytes()
    ledger = home.ledger.read_bytes() if home.ledger.exists() else None

    with pytest.raises(StorageQuarantined):
        MemoryManager(None if broken == "no ledger named" else home.ledger)

    assert home.database.read_bytes() == database
    assert (home.ledger.read_bytes() if home.ledger.exists() else None) == ledger
    # Nothing stored was sent to be embedded.
    assert embeds(fake_ollama) == []


def test_a_missing_database_is_not_created(at_rest):
    home = at_rest.home
    home.database.unlink()

    with pytest.raises(StorageQuarantined):
        MemoryManager(home.ledger)

    assert not home.database.exists()


def test_a_ledger_beside_the_database_is_refused(at_rest):
    home = at_rest.home
    beside = home.database.parent / "deletions.db"
    beside.write_bytes(home.ledger.read_bytes())

    with pytest.raises(StorageQuarantined, match="outside"):
        MemoryManager(beside)


def test_a_sound_pair_opens_and_is_served(at_rest, fake_ollama):
    memory, _ = at_rest.home.open()

    assert len(embeds(fake_ollama)) == 1
    assert "heron" not in stored_text(memory.connection)


def test_rolling_both_files_back_together_is_not_detected(protected):
    """
    The limit of this design, written down rather than hidden: the
    ledger only helps while it is kept apart from what gets restored.
    If an old copy of the ledger is put back along with an old copy of
    the database, the pair is consistent and the deletion is undone.
    """
    memory, _ = protected.open()
    forgotten = memory.add_memory("The spare key is under the heron statue.", "fact")
    older = protected.snapshot(), protected.ledger.read_bytes()

    memory, _ = protected.open()
    assert memory.delete_memory(forgotten) is True

    protected.restore(older[0])
    protected.ledger.write_bytes(older[1])

    memory, _ = protected.open()
    assert memory.get_memory(forgotten)["content"] == "The spare key is under the heron statue."


# -- Freya using it ----------------------------------------------------------------------


def test_the_assistant_and_its_conversations_share_the_checked_ledger(protected):
    assistant = FreyaAssistant(memory_ledger_path=protected.ledger)
    protected._opened.append(assistant.memory)
    assert assistant.conversations.ledger is assistant.memory.ledger is not None

    forgotten = assistant.memory.add_memory("The user's locker code is ZEBRA-4471.", "fact")
    assistant.conversations.add_exchange("the gate code is PELICAN-9", "Noted: PELICAN-9.")
    turn = assistant.find("PELICAN")["passages"][0]["match"]

    assert assistant.memory_request({"action": "turn-delete", "id": turn})["error"] is None
    assert assistant.memory_request({"action": "memory-delete", "id": forgotten})["error"] is None

    # The conversation in progress is kept, empty; its messages and the
    # memory are on record as deleted.
    assert sorted(kind for _, kind in protected.tombstones()) == ["memory", "turn", "turn"]
    text = stored_text(assistant.memory.connection)
    assert "pelican" not in text and "zebra" not in text


def test_the_assistant_is_unprotected_unless_asked(home):
    assistant = FreyaAssistant()
    home._opened.append(assistant.memory)

    assert assistant.memory.ledger is None and assistant.conversations.ledger is None
