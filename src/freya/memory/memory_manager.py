import json
import math
import sqlite3

from freya.core.model_runtime import (
    ModelRuntimeUnavailable,
    get_model_runtime,
)
from freya.memory.deletion_ledger import (
    DeletionLedger,
    StorageQuarantined,
    is_protected,
)
from freya.paths import data_dir


DATA_DIR = data_dir()
DATABASE_PATH = DATA_DIR / "freya_memory.db"

DEFAULT_MEMORY_LIMIT = 3
DEFAULT_SIMILARITY_THRESHOLD = 0.50


class MemoryManager:
    def __init__(self, ledger_path=None):
        """
        With `ledger_path`, the database is opened in protected mode:
        it and the deletion ledger at that path are checked against
        each other, and every deletion on record is applied, before
        anything else is done. Either failing raises
        StorageQuarantined with nothing changed. The database must
        already have been through `initialize_protection`.
        """
        # The record of deletions, in protected mode only.
        self.ledger = None

        if ledger_path is None:
            DATA_DIR.mkdir(exist_ok=True)
        elif not DATABASE_PATH.is_file():
            # Connecting would create an empty one.
            raise StorageQuarantined(
                "There is no memory database to open."
            )

        # Typed turns and spoken turns run on different threads. The
        # turn gate lets only one run at a time, so sharing this
        # connection between them is safe.
        self.connection = sqlite3.connect(
            DATABASE_PATH,
            check_same_thread=False,
        )

        # Forgotten text is overwritten in the file, not just unlinked.
        self.connection.execute(
            "PRAGMA secure_delete = ON"
        )

        try:
            if ledger_path is not None:
                self.ledger = DeletionLedger.open(
                    ledger_path,
                    self.connection,
                )
            elif is_protected(self.connection):
                # Deleting from it without its ledger would leave
                # deletions that a restore could undo.
                raise StorageQuarantined(
                    "This memory database is protected and "
                    "must be opened with its deletion ledger."
                )
        except BaseException:
            self.connection.close()
            raise

        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_type TEXT DEFAULT 'other',
                memory_key TEXT,
                content TEXT NOT NULL,
                embedding TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                memory_type TEXT,
                memory_key TEXT,
                content TEXT NOT NULL,
                original_created_at TIMESTAMP,
                original_updated_at TIMESTAMP,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_plan (
                memory_id INTEGER PRIMARY KEY,
                original_type TEXT,
                original_key TEXT,
                original_content TEXT NOT NULL,
                proposed_type TEXT,
                proposed_key TEXT,
                blocked INTEGER NOT NULL DEFAULT 0,
                block_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        self._upgrade_database()
        self.connection.commit()

        self._embed_existing_memories()

    def _upgrade_database(self):
        cursor = self.connection.execute(
            "PRAGMA table_info(memories)"
        )

        columns = [
            row[1]
            for row in cursor.fetchall()
        ]

        if "embedding" not in columns:
            self.connection.execute(
                """
                ALTER TABLE memories
                ADD COLUMN embedding TEXT
                """
            )

        if "memory_type" not in columns:
            self.connection.execute(
                """
                ALTER TABLE memories
                ADD COLUMN memory_type TEXT
                DEFAULT 'other'
                """
            )

        if "memory_key" not in columns:
            self.connection.execute(
                """
                ALTER TABLE memories
                ADD COLUMN memory_key TEXT
                """
            )

        if "updated_at" not in columns:
            self.connection.execute(
                """
                ALTER TABLE memories
                ADD COLUMN updated_at TIMESTAMP
                """
            )

        self.connection.execute(
            """
            UPDATE memories
            SET memory_type = 'other'
            WHERE memory_type IS NULL
               OR TRIM(memory_type) = ''
            """
        )

        self.connection.execute(
            """
            UPDATE memories
            SET updated_at = created_at
            WHERE updated_at IS NULL
            """
        )

        self.connection.commit()

    def _create_embedding(self, text):
        return get_model_runtime().embed(
            text
        )

    def _stored_embedding(self, text):
        """
        The embedding as it is stored, or None when the embedding
        model cannot be reached. A memory kept without one is still
        found by its words, and is embedded once the model is back.
        """
        try:
            return json.dumps(
                self._create_embedding(
                    text
                )
            )
        except ModelRuntimeUnavailable:
            return None

    def _embed_existing_memories(self):
        cursor = self.connection.execute(
            """
            SELECT id, content
            FROM memories
            WHERE embedding IS NULL
            """
        )

        for memory_id, content in cursor.fetchall():
            embedding = self._stored_embedding(
                content
            )

            if embedding is None:
                # The embedding model is away. The rest are tried
                # again at the next search and the next start.
                break

            self.connection.execute(
                """
                UPDATE memories
                SET embedding = ?
                WHERE id = ?
                """,
                (
                    embedding,
                    memory_id,
                ),
            )

        self.connection.commit()

    def _archive_memory(
        self,
        memory_id,
        action,
    ):
        memory = self.get_memory(
            memory_id
        )

        if memory is None:
            return False

        self.connection.execute(
            """
            INSERT INTO memory_history (
                memory_id,
                action,
                memory_type,
                memory_key,
                content,
                original_created_at,
                original_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory["id"],
                action,
                memory["type"],
                memory["key"],
                memory["content"],
                memory["created_at"],
                memory["updated_at"],
            ),
        )

        return True

    def add_memory(
        self,
        content,
        memory_type="other",
        memory_key=None,
    ):
        existing = self.connection.execute(
            """
            SELECT id
            FROM memories
            WHERE LOWER(content) = LOWER(?)
            """,
            (content,),
        ).fetchone()

        if existing:
            return existing[0]

        embedding = self._stored_embedding(
            content
        )

        cursor = self.connection.execute(
            """
            INSERT INTO memories (
                memory_type,
                memory_key,
                content,
                embedding,
                updated_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                memory_type,
                memory_key,
                content,
                embedding,
            ),
        )

        self.connection.commit()

        return cursor.lastrowid

    def update_memory(
        self,
        memory_id,
        content,
        memory_type=None,
        memory_key=None,
        action="update",
    ):
        existing = self.get_memory(
            memory_id
        )

        if existing is None:
            return False

        if memory_type is None:
            memory_type = existing["type"]

        if memory_key is None:
            memory_key = existing["key"]

        embedding = self._stored_embedding(
            content
        )

        try:
            self.connection.execute(
                "BEGIN"
            )

            self._archive_memory(
                memory_id,
                action,
            )

            cursor = self.connection.execute(
                """
                UPDATE memories
                SET memory_type = ?,
                    memory_key = ?,
                    content = ?,
                    embedding = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    memory_type,
                    memory_key,
                    content,
                    embedding,
                    memory_id,
                ),
            )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

        return cursor.rowcount > 0

    def get_memory(self, memory_id):
        cursor = self.connection.execute(
            """
            SELECT
                id,
                memory_type,
                memory_key,
                content,
                created_at,
                updated_at
            FROM memories
            WHERE id = ?
            """,
            (memory_id,),
        )

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "id": row[0],
            "type": row[1],
            "key": row[2],
            "content": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }

    def get_memories_by_key(
        self,
        memory_key,
    ):
        cursor = self.connection.execute(
            """
            SELECT
                id,
                memory_type,
                memory_key,
                content,
                created_at,
                updated_at
            FROM memories
            WHERE LOWER(memory_key) = LOWER(?)
            ORDER BY updated_at DESC, id ASC
            """,
            (memory_key,),
        )

        rows = cursor.fetchall()

        return [
            {
                "id": row[0],
                "type": row[1],
                "key": row[2],
                "content": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]

    def get_memory_by_key(
        self,
        memory_key,
    ):
        memories = self.get_memories_by_key(
            memory_key
        )

        if not memories:
            return None

        return memories[0]

    def get_all_memories(self):
        cursor = self.connection.execute(
            """
            SELECT
                id,
                memory_type,
                memory_key,
                content,
                created_at,
                updated_at
            FROM memories
            ORDER BY id ASC
            """
        )

        return cursor.fetchall()

    def get_history_entry(
        self,
        history_id,
    ):
        cursor = self.connection.execute(
            """
            SELECT
                history_id,
                memory_id,
                action,
                memory_type,
                memory_key,
                content,
                original_created_at,
                original_updated_at,
                archived_at
            FROM memory_history
            WHERE history_id = ?
            """,
            (history_id,),
        )

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "history_id": row[0],
            "memory_id": row[1],
            "action": row[2],
            "type": row[3],
            "key": row[4],
            "content": row[5],
            "original_created_at": row[6],
            "original_updated_at": row[7],
            "archived_at": row[8],
        }

    def get_memory_history(
        self,
        memory_id,
    ):
        cursor = self.connection.execute(
            """
            SELECT
                history_id,
                memory_id,
                action,
                memory_type,
                memory_key,
                content,
                original_created_at,
                original_updated_at,
                archived_at
            FROM memory_history
            WHERE memory_id = ?
            ORDER BY history_id ASC
            """,
            (memory_id,),
        )

        return cursor.fetchall()

    def delete_memory(
        self,
        memory_id,
    ):
        """
        Forget a memory because the owner asked: the memory with its
        embedding, every earlier version of it in the history, any
        other history entry holding the same words, and its line in
        a saved migration plan all go together. Nothing is archived.

        A memory that is no longer live but still has history (one
        removed as a duplicate, say) is forgotten the same way.
        Returns False when there was nothing to forget.
        """
        existing = self.get_memory(
            memory_id
        )

        content = (
            existing["content"]
            if existing is not None
            else None
        )

        if self.ledger is not None:
            doomed = self._doomed(
                memory_id,
                content,
            )

            if doomed:
                # Recorded in the ledger first, then removed; see
                # DeletionLedger. May raise StorageQuarantined.
                self.ledger.forget(
                    self.connection,
                    doomed,
                    plan=(memory_id, content),
                )

                return True

        try:
            self.connection.execute(
                "BEGIN"
            )

            removed = self.connection.execute(
                """
                DELETE FROM memories
                WHERE id = ?
                """,
                (memory_id,),
            ).rowcount

            removed += self.connection.execute(
                """
                DELETE FROM memory_history
                WHERE memory_id = ?
                   OR LOWER(content) = LOWER(?)
                """,
                (
                    memory_id,
                    content,
                ),
            ).rowcount

            self.connection.execute(
                """
                DELETE FROM migration_plan
                WHERE memory_id = ?
                   OR LOWER(original_content) = LOWER(?)
                """,
                (
                    memory_id,
                    content,
                ),
            )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

        return removed > 0

    def _doomed(
        self,
        memory_id,
        content,
    ):
        """
        The identifier and kind of every row that forgetting a memory
        removes: the memory, its earlier versions, and any other
        history entry holding the same words.
        """
        doomed = self.connection.execute(
            """
            SELECT artifact_id, 'memory'
            FROM memories
            WHERE id = ?
            """,
            (memory_id,),
        ).fetchall()

        if not doomed:
            # No longer live, so only its history is here. A copy of
            # the database in which it was still live holds it under
            # the identifier its history entries remember.
            doomed = self.connection.execute(
                """
                SELECT DISTINCT memory_artifact_id, 'memory'
                FROM memory_history
                WHERE memory_id = ?
                  AND memory_artifact_id IS NOT NULL
                """,
                (memory_id,),
            ).fetchall()

        return doomed + self.connection.execute(
            """
            SELECT artifact_id, 'history'
            FROM memory_history
            WHERE memory_id = ?
               OR LOWER(content) = LOWER(?)
            """,
            (
                memory_id,
                content,
            ),
        ).fetchall()

    def resolve_duplicate(
        self,
        keep_id,
        remove_id,
        memory_type,
        memory_key,
    ):
        if keep_id == remove_id:
            return False

        keep_memory = self.get_memory(
            keep_id
        )

        remove_memory = self.get_memory(
            remove_id
        )

        if (
            keep_memory is None
            or remove_memory is None
        ):
            return False

        try:
            self.connection.execute(
                "BEGIN"
            )

            keep_memory = self.get_memory(
                keep_id
            )

            remove_memory = self.get_memory(
                remove_id
            )

            if (
                keep_memory is None
                or remove_memory is None
            ):
                self.connection.rollback()
                return False

            collision = self.connection.execute(
                """
                SELECT id
                FROM memories
                WHERE memory_key IS NOT NULL
                  AND LOWER(memory_key) = LOWER(?)
                  AND id NOT IN (?, ?)
                LIMIT 1
                """,
                (
                    memory_key,
                    keep_id,
                    remove_id,
                ),
            ).fetchone()

            if collision is not None:
                raise RuntimeError(
                    "Another live memory already "
                    "uses the canonical key."
                )

            self._archive_memory(
                keep_id,
                "duplicate_resolution_keep",
            )

            self._archive_memory(
                remove_id,
                "duplicate_resolution_remove",
            )

            update_cursor = (
                self.connection.execute(
                    """
                    UPDATE memories
                    SET memory_type = ?,
                        memory_key = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        memory_type,
                        memory_key,
                        keep_id,
                    ),
                )
            )

            if update_cursor.rowcount != 1:
                raise RuntimeError(
                    "Could not update the "
                    "kept memory."
                )

            delete_cursor = (
                self.connection.execute(
                    """
                    DELETE FROM memories
                    WHERE id = ?
                    """,
                    (remove_id,),
                )
            )

            if delete_cursor.rowcount != 1:
                raise RuntimeError(
                    "Could not delete the "
                    "duplicate memory."
                )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

        return True

    def restore_memory(
        self,
        memory_id,
        history_id,
    ):
        current = self.get_memory(
            memory_id
        )

        if current is None:
            return {
                "success": False,
                "reason": "memory_not_found",
            }

        history = self.get_history_entry(
            history_id
        )

        if history is None:
            return {
                "success": False,
                "reason": "history_not_found",
            }

        if history["memory_id"] != memory_id:
            return {
                "success": False,
                "reason": "history_mismatch",
            }

        restored_key = history["key"]

        if restored_key:
            collision = self.connection.execute(
                """
                SELECT id
                FROM memories
                WHERE memory_key IS NOT NULL
                  AND LOWER(memory_key) = LOWER(?)
                  AND id != ?
                LIMIT 1
                """,
                (
                    restored_key,
                    memory_id,
                ),
            ).fetchone()

            if collision is not None:
                return {
                    "success": False,
                    "reason": "key_collision",
                    "collision_id": collision[0],
                }

        restored_embedding = (
            self._stored_embedding(
                history["content"]
            )
        )

        try:
            self.connection.execute(
                "BEGIN"
            )

            current = self.get_memory(
                memory_id
            )

            history = self.get_history_entry(
                history_id
            )

            if current is None:
                raise RuntimeError(
                    "Memory disappeared before "
                    "restore."
                )

            if history is None:
                raise RuntimeError(
                    "History entry disappeared "
                    "before restore."
                )

            if (
                history["memory_id"]
                != memory_id
            ):
                raise RuntimeError(
                    "History ownership changed "
                    "before restore."
                )

            restored_key = history["key"]

            if restored_key:
                collision = (
                    self.connection.execute(
                        """
                        SELECT id
                        FROM memories
                        WHERE memory_key IS NOT NULL
                          AND LOWER(memory_key) = LOWER(?)
                          AND id != ?
                        LIMIT 1
                        """,
                        (
                            restored_key,
                            memory_id,
                        ),
                    ).fetchone()
                )

                if collision is not None:
                    raise RuntimeError(
                        "A semantic-key collision "
                        "appeared before restore."
                    )

            archived = self._archive_memory(
                memory_id,
                "restore_checkpoint",
            )

            if not archived:
                raise RuntimeError(
                    "Could not archive the "
                    "current memory."
                )

            cursor = self.connection.execute(
                """
                UPDATE memories
                SET memory_type = ?,
                    memory_key = ?,
                    content = ?,
                    embedding = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    history["type"],
                    history["key"],
                    history["content"],
                    restored_embedding,
                    memory_id,
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Could not restore memory."
                )

            self.connection.commit()

        except Exception as error:
            self.connection.rollback()

            return {
                "success": False,
                "reason": "transaction_failed",
                "error": str(error),
            }

        restored = self.get_memory(
            memory_id
        )

        return {
            "success": True,
            "memory": restored,
            "source_history_id": history_id,
        }

    def process_candidate(
        self,
        candidate,
    ):
        memory_key = candidate["key"]
        memory_type = candidate["type"]
        content = candidate["content"]

        existing_memories = (
            self.get_memories_by_key(
                memory_key
            )
        )

        if not existing_memories:
            memory_id = self.add_memory(
                content=content,
                memory_type=memory_type,
                memory_key=memory_key,
            )

            return {
                "action": "add",
                "memory_id": memory_id,
            }

        for existing in existing_memories:
            if (
                existing["content"].strip().lower()
                == content.strip().lower()
            ):
                return {
                    "action": "skip",
                    "memory_id": existing["id"],
                }

        existing = existing_memories[0]

        self.update_memory(
            memory_id=existing["id"],
            content=content,
            memory_type=memory_type,
            memory_key=memory_key,
            action="automatic_update",
        )

        return {
            "action": "update",
            "memory_id": existing["id"],
        }

    def save_migration_plan(
        self,
        proposals,
    ):
        try:
            self.connection.execute(
                "BEGIN"
            )

            self.connection.execute(
                "DELETE FROM migration_plan"
            )

            for proposal in proposals:
                self.connection.execute(
                    """
                    INSERT INTO migration_plan (
                        memory_id,
                        original_type,
                        original_key,
                        original_content,
                        proposed_type,
                        proposed_key,
                        blocked,
                        block_reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal["id"],
                        proposal["current_type"],
                        proposal["current_key"],
                        proposal["content"],
                        proposal["proposed_type"],
                        proposal["proposed_key"],
                        1 if proposal["blocked"] else 0,
                        proposal["block_reason"],
                    ),
                )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

    def get_migration_plan(self):
        cursor = self.connection.execute(
            """
            SELECT
                memory_id,
                original_type,
                original_key,
                original_content,
                proposed_type,
                proposed_key,
                blocked,
                block_reason,
                created_at
            FROM migration_plan
            ORDER BY memory_id ASC
            """
        )

        rows = cursor.fetchall()

        return [
            {
                "id": row[0],
                "current_type": row[1],
                "current_key": row[2],
                "content": row[3],
                "proposed_type": row[4],
                "proposed_key": row[5],
                "blocked": bool(row[6]),
                "block_reason": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]

    def clear_migration_plan(self):
        self.connection.execute(
            "DELETE FROM migration_plan"
        )

        self.connection.commit()

    def apply_migration_plan(self):
        plan = self.get_migration_plan()

        if not plan:
            return {
                "applied": [],
                "blocked": [],
                "stale": [],
            }

        applied = []
        blocked = []
        stale = []

        try:
            self.connection.execute(
                "BEGIN"
            )

            for proposal in plan:
                memory_id = proposal["id"]

                if proposal["blocked"]:
                    blocked.append(
                        memory_id
                    )
                    continue

                if (
                    not proposal["proposed_type"]
                    or not proposal["proposed_key"]
                ):
                    blocked.append(
                        memory_id
                    )
                    continue

                current = self.get_memory(
                    memory_id
                )

                if current is None:
                    stale.append(
                        memory_id
                    )
                    continue

                if (
                    current["type"]
                    != proposal["current_type"]
                    or current["key"]
                    != proposal["current_key"]
                    or current["content"]
                    != proposal["content"]
                ):
                    stale.append(
                        memory_id
                    )
                    continue

                self._archive_memory(
                    memory_id,
                    "legacy_migration",
                )

                self.connection.execute(
                    """
                    UPDATE memories
                    SET memory_type = ?,
                        memory_key = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        proposal["proposed_type"],
                        proposal["proposed_key"],
                        memory_id,
                    ),
                )

                applied.append(
                    memory_id
                )

            self.connection.execute(
                "DELETE FROM migration_plan"
            )

            self.connection.commit()

        except Exception:
            self.connection.rollback()
            raise

        return {
            "applied": applied,
            "blocked": blocked,
            "stale": stale,
        }

    def search_memory_candidates(
        self,
        query,
        limit=5,
        threshold=DEFAULT_SIMILARITY_THRESHOLD,
    ):
        return self.search_memories(
            query=query,
            limit=limit,
            threshold=threshold,
        )

    def search_memories(
        self,
        query,
        limit=DEFAULT_MEMORY_LIMIT,
        threshold=DEFAULT_SIMILARITY_THRESHOLD,
    ):
        query_embedding = self._create_embedding(
            query
        )

        # The embedding model is answering, so memories saved while
        # it was away can be embedded now and take part in this search.
        self._embed_existing_memories()

        cursor = self.connection.execute(
            """
            SELECT
                id,
                memory_type,
                memory_key,
                content,
                embedding
            FROM memories
            WHERE embedding IS NOT NULL
            """
        )

        scored_memories = []

        for (
            memory_id,
            memory_type,
            memory_key,
            content,
            embedding_json,
        ) in cursor.fetchall():

            memory_embedding = json.loads(
                embedding_json
            )

            similarity = self._cosine_similarity(
                query_embedding,
                memory_embedding,
            )

            if similarity >= threshold:
                scored_memories.append(
                    (
                        similarity,
                        memory_id,
                        memory_type,
                        memory_key,
                        content,
                    )
                )

        scored_memories.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        return [
            {
                "id": memory_id,
                "type": memory_type,
                "key": memory_key,
                "content": content,
                "similarity": similarity,
            }
            for (
                similarity,
                memory_id,
                memory_type,
                memory_key,
                content,
            ) in scored_memories[:limit]
        ]

    @staticmethod
    def _cosine_similarity(
        vector_a,
        vector_b,
    ):
        dot_product = sum(
            a * b
            for a, b in zip(
                vector_a,
                vector_b,
            )
        )

        magnitude_a = math.sqrt(
            sum(
                a * a
                for a in vector_a
            )
        )

        magnitude_b = math.sqrt(
            sum(
                b * b
                for b in vector_b
            )
        )

        if magnitude_a == 0 or magnitude_b == 0:
            return 0.0

        return dot_product / (
            magnitude_a * magnitude_b
        )