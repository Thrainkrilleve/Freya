import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional


BACKUP_FOLDER = "backups"
BACKUP_NAME = "freya_memory-before-conversations.db"

MAX_TURN_CHARACTERS = 20000
MAX_SEARCH_TERMS = 12
MAX_PASSAGE_CHARACTERS = 1200


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# Words too common to find anything by. A search made only of these
# is run with them anyway.
COMMON_WORDS = frozenset(
    """
    a about again all already also am an and any anything are as at be been before
    but by can could did do does done ever for from had has have having he her
    here him his how i i'm i've if in into is it it's its just know last like me
    mention mentioned mentioning my no not of on once or our out previously recall
    remember said say saying she so some something tell that the their them then
    there they thing things this time to told us was we we've were what when where
    which who why will with would you you've your
    """.split()
)


def search_terms(query) -> List[str]:
    """
    The words of a search, without anything the search index would
    read as an instruction, and without words too common to help.
    """
    words = re.findall(r"[\w']+", str(query or "").lower())
    seen = []

    for word in words:
        word = word.strip("'")

        if len(word) > 1 and word not in seen:
            seen.append(word)

    useful = [word for word in seen if word not in COMMON_WORDS]

    return (useful or seen)[:MAX_SEARCH_TERMS]


class ConversationStore:
    """
    Everything said between the owner and Freya, kept word for word,
    with a short summary of each conversation.

    It lives in the memory database beside her saved memories and is
    as personal: it never leaves this computer. A conversation is one
    run of Freya. Messages are searched by the words in them. Deleting
    a message or a conversation removes it for good, and clears that
    conversation's summary so that nothing deleted lingers in it.

    It uses the memory manager's database connection, and like it is
    only ever used by one turn at a time.
    """

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.current: Optional[int] = None

        if not self._exists("conversation"):
            self._back_up()

        self._create()

    # -- setting up ----------------------------------------------------------

    def _exists(self, table: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    def _back_up(self) -> None:
        """
        Copy the memory database before its first change for
        conversations. A database with nothing in it yet is not copied.
        """
        try:
            row = self.connection.execute("PRAGMA database_list").fetchone()
            path = Path(row[2]) if row and row[2] else None

            if path is None or not path.is_file():
                return

            if not self.connection.execute("SELECT 1 FROM memories LIMIT 1").fetchone():
                return

            folder = path.parent / BACKUP_FOLDER
            target = folder / BACKUP_NAME

            if target.exists():
                return

            folder.mkdir(parents=True, exist_ok=True)
            copy = sqlite3.connect(target)

            try:
                self.connection.backup(copy)
            finally:
                copy.close()
        except (OSError, sqlite3.Error):
            # A backup that cannot be made must not stop Freya; the new
            # tables only add to the database and change nothing in it.
            pass

    def _create(self) -> None:
        # Deleted text is overwritten in the file, not just unlinked.
        self.connection.execute("PRAGMA secure_delete = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                last_at TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                summarized_turns INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS conversation_turn (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'typed',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS conversation_turn_by_conversation
                ON conversation_turn (conversation_id, id);

            CREATE VIRTUAL TABLE IF NOT EXISTS conversation_search
                USING fts5(content, tokenize = 'unicode61 remove_diacritics 2');
            """
        )
        self.connection.commit()
        self._forget_deleted_words()

    def _forget_deleted_words(self) -> None:
        """
        The search index keeps its own copy of every word. By default
        it only marks deleted messages as gone and keeps their words
        until a later merge, which may never happen, so they stay in
        the file. Its secure-delete option removes them on delete.

        The option is recorded in the index itself. When it is not yet
        on, the index is rebuilt from the stored messages once, which
        clears words left by deletions made before.
        """
        on = self.connection.execute(
            "SELECT v FROM conversation_search_config WHERE k = 'secure-delete'"
        ).fetchone()

        if on is not None and on[0]:
            return

        try:
            self.connection.execute(
                "INSERT INTO conversation_search(conversation_search, rank) "
                "VALUES('secure-delete', 1)"
            )
        except sqlite3.OperationalError:
            # SQLite before 3.42 has no such option. Deleting still
            # removes the messages; their words may stay in the index.
            return

        self.connection.execute(
            "INSERT INTO conversation_search(conversation_search) VALUES('rebuild')"
        )
        self.connection.commit()

    # -- writing ---------------------------------------------------------------

    def add_exchange(self, user_text: str, reply: str, source: str = "typed") -> None:
        """
        Keep one thing the owner said and Freya's answer. The
        conversation is created with its first exchange, so a run in
        which nothing is said leaves nothing behind.
        """
        moment = now()

        if self.current is None:
            self.current = self.connection.execute(
                "INSERT INTO conversation (started_at, last_at) VALUES (?, ?)",
                (moment, moment),
            ).lastrowid

        for role, content in (("user", user_text), ("assistant", reply)):
            content = str(content)[:MAX_TURN_CHARACTERS]
            turn_id = self.connection.execute(
                "INSERT INTO conversation_turn "
                "(conversation_id, role, content, source, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.current, role, content, str(source)[:20], moment),
            ).lastrowid
            self.connection.execute(
                "INSERT INTO conversation_search (rowid, content) VALUES (?, ?)",
                (turn_id, content),
            )

        self.connection.execute(
            "UPDATE conversation SET last_at = ? WHERE id = ?", (moment, self.current)
        )
        self.connection.commit()

    def set_summary(self, conversation_id: int, summary: str, turns: int) -> None:
        self.connection.execute(
            "UPDATE conversation SET summary = ?, summarized_turns = ? WHERE id = ?",
            (str(summary).strip(), int(turns), conversation_id),
        )
        self.connection.commit()

    # -- reading ---------------------------------------------------------------

    def _turn(self, row) -> dict:
        return {
            "id": row[0],
            "conversation": row[1],
            "role": row[2],
            "content": row[3],
            "source": row[4],
            "at": row[5],
        }

    def turns(self, conversation_id: int, after: int = 0) -> List[dict]:
        """
        A conversation's messages in order, leaving out the first
        `after` of them.
        """
        rows = self.connection.execute(
            "SELECT id, conversation_id, role, content, source, created_at "
            "FROM conversation_turn WHERE conversation_id = ? ORDER BY id "
            "LIMIT -1 OFFSET ?",
            (conversation_id, max(int(after), 0)),
        ).fetchall()

        return [self._turn(row) for row in rows]

    def get(self, conversation_id) -> Optional[dict]:
        row = self.connection.execute(
            "SELECT c.id, c.started_at, c.last_at, c.summary, c.summarized_turns, "
            "(SELECT COUNT(*) FROM conversation_turn t WHERE t.conversation_id = c.id) "
            "FROM conversation c WHERE c.id = ?",
            (conversation_id,),
        ).fetchone()

        return self._conversation(row) if row else None

    def _conversation(self, row) -> dict:
        return {
            "id": row[0],
            "started_at": row[1],
            "last_at": row[2],
            "summary": row[3],
            "summarized_turns": row[4],
            "turns": row[5],
            "current": row[0] == self.current,
        }

    def recent(self, limit: int = 20, include_current: bool = True) -> List[dict]:
        """
        The latest conversations, newest first.
        """
        rows = self.connection.execute(
            "SELECT c.id, c.started_at, c.last_at, c.summary, c.summarized_turns, "
            "(SELECT COUNT(*) FROM conversation_turn t WHERE t.conversation_id = c.id) "
            "FROM conversation c ORDER BY c.id DESC LIMIT ?",
            (int(limit) + 1,),
        ).fetchall()
        found = [self._conversation(row) for row in rows]

        if not include_current:
            found = [entry for entry in found if not entry["current"]]

        return found[:limit]

    def unsummarized(self) -> List[dict]:
        """
        Earlier conversations with messages their summary does not yet
        cover, oldest first.
        """
        return [
            entry
            for entry in reversed(self.recent(limit=50, include_current=False))
            if entry["turns"] > entry["summarized_turns"]
        ]

    def search(self, query, limit: int = 5, context: int = 1) -> List[dict]:
        """
        The passages that best match the words of a search, best first.
        Each is the matching message with the messages around it, so
        that an answer comes with the question it answered.
        """
        terms = search_terms(query)

        if not terms:
            return []

        match = " OR ".join(f'"{term}"' for term in terms)

        try:
            rows = self.connection.execute(
                "SELECT rowid FROM conversation_search WHERE conversation_search MATCH ? "
                "ORDER BY bm25(conversation_search) LIMIT ?",
                (match, int(limit) * 4),
            ).fetchall()
        except sqlite3.Error:
            return []

        passages = []
        covered = set()

        for (turn_id,) in rows:
            if turn_id in covered:
                continue

            found = self.connection.execute(
                "SELECT conversation_id FROM conversation_turn WHERE id = ?", (turn_id,)
            ).fetchone()

            if found is None:
                continue

            conversation_id = found[0]
            before = self.connection.execute(
                "SELECT id, conversation_id, role, content, source, created_at "
                "FROM conversation_turn WHERE conversation_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, turn_id, context),
            ).fetchall()
            after = self.connection.execute(
                "SELECT id, conversation_id, role, content, source, created_at "
                "FROM conversation_turn WHERE conversation_id = ? AND id >= ? "
                "ORDER BY id LIMIT ?",
                (conversation_id, turn_id, context + 1),
            ).fetchall()
            turns = [self._turn(row) for row in list(reversed(before)) + after]
            covered.update(turn["id"] for turn in turns)
            conversation = self.get(conversation_id)
            passages.append(
                {
                    "conversation": conversation_id,
                    "started_at": conversation["started_at"] if conversation else "",
                    "match": turn_id,
                    "turns": turns,
                }
            )

            if len(passages) >= limit:
                break

        return passages

    # -- forgetting --------------------------------------------------------------

    def _forget_turns(self, turn_ids) -> None:
        for turn_id in turn_ids:
            self.connection.execute(
                "DELETE FROM conversation_search WHERE rowid = ?", (turn_id,)
            )
            self.connection.execute(
                "DELETE FROM conversation_turn WHERE id = ?", (turn_id,)
            )

    def delete_turn(self, turn_id) -> Optional[int]:
        """
        Remove one message for good. Returns the conversation it was
        in, or None when there is no such message. The conversation's
        summary is cleared, because it may repeat what was removed; a
        conversation left with no messages is removed too.
        """
        row = self.connection.execute(
            "SELECT conversation_id FROM conversation_turn WHERE id = ?", (turn_id,)
        ).fetchone()

        if row is None:
            return None

        conversation_id = row[0]
        self._forget_turns([turn_id])
        remaining = self.connection.execute(
            "SELECT COUNT(*) FROM conversation_turn WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()[0]

        if remaining == 0 and conversation_id != self.current:
            self.connection.execute(
                "DELETE FROM conversation WHERE id = ?", (conversation_id,)
            )
        else:
            self.connection.execute(
                "UPDATE conversation SET summary = '', summarized_turns = 0 WHERE id = ?",
                (conversation_id,),
            )

        self.connection.commit()

        return conversation_id

    def delete_exchange(self, turn_id) -> Optional[int]:
        """
        Remove a message together with the other half of its exchange:
        the answer to something the owner said, or the thing Freya was
        answering. Her answers usually repeat what she was told, so
        removing one half would leave it behind in the other. Returns
        the conversation, or None when there is no such message.
        """
        row = self.connection.execute(
            "SELECT conversation_id, role FROM conversation_turn WHERE id = ?", (turn_id,)
        ).fetchone()

        if row is None:
            return None

        conversation_id, role = row

        if role == "user":
            other = self.connection.execute(
                "SELECT id, role FROM conversation_turn "
                "WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT 1",
                (conversation_id, turn_id),
            ).fetchone()
        else:
            other = self.connection.execute(
                "SELECT id, role FROM conversation_turn "
                "WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
                (conversation_id, turn_id),
            ).fetchone()

        if other is not None and other[1] != role:
            self.delete_turn(other[0])

        self.delete_turn(turn_id)

        return conversation_id

    def delete_conversation(self, conversation_id) -> bool:
        """
        Remove a whole conversation and its summary for good.
        """
        if self.get(conversation_id) is None:
            return False

        ids = [
            row[0]
            for row in self.connection.execute(
                "SELECT id FROM conversation_turn WHERE conversation_id = ?",
                (conversation_id,),
            )
        ]
        self._forget_turns(ids)

        if conversation_id == self.current:
            # The run goes on; what is said next starts a new record.
            self.current = None

        self.connection.execute("DELETE FROM conversation WHERE id = ?", (conversation_id,))
        self.connection.commit()

        return True

    def delete_all(self) -> int:
        """
        Remove every conversation for good. Returns how many there were.
        """
        count = self.connection.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
        self.connection.execute("DELETE FROM conversation_search")
        self.connection.execute("DELETE FROM conversation_turn")
        self.connection.execute("DELETE FROM conversation")
        self.connection.commit()
        self.current = None

        return count


def passage_text(turns: List[dict], limit: int = MAX_PASSAGE_CHARACTERS) -> List[dict]:
    """
    A passage as who said what, with long messages cut short.
    """
    return [
        {
            "who": "the user" if turn["role"] == "user" else "Freya",
            "said": turn["content"][:limit],
        }
        for turn in turns
    ]
