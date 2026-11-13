import json
import math
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional


SCHEMA_VERSION = 1

# Each entry is one schema version. Never edit an entry that has
# shipped; append a new one instead.
MIGRATIONS = [
    """
    CREATE TABLE media (
        media_id TEXT PRIMARY KEY,
        source_type TEXT NOT NULL DEFAULT 'media',
        provider TEXT NOT NULL
            CHECK (provider IN ('local', 'jellyfin', 'youtube')),
        source_ref TEXT NOT NULL,
        source_url TEXT,
        acquisition TEXT NOT NULL,
        title TEXT NOT NULL,
        creator TEXT,
        kind TEXT NOT NULL,
        media_type TEXT,
        duration REAL,
        root TEXT,
        relative TEXT,
        size INTEGER,
        modified_ns INTEGER,
        fingerprint TEXT,
        trust TEXT NOT NULL DEFAULT 'untrusted-source',
        created_at TEXT NOT NULL,
        analyzed_at TEXT,
        analysis_model TEXT,
        transcriber TEXT,
        coverage TEXT,
        UNIQUE (provider, source_ref)
    );

    CREATE TABLE media_tag (
        media_id TEXT NOT NULL REFERENCES media (media_id) ON DELETE CASCADE,
        tag TEXT NOT NULL,
        PRIMARY KEY (media_id, tag)
    );

    CREATE TABLE media_note (
        note_id INTEGER PRIMARY KEY,
        media_id TEXT NOT NULL REFERENCES media (media_id) ON DELETE CASCADE,
        at REAL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE media_skip (
        skip_id INTEGER PRIMARY KEY,
        media_id TEXT NOT NULL REFERENCES media (media_id) ON DELETE CASCADE,
        start REAL NOT NULL,
        end REAL NOT NULL,
        label TEXT NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        CHECK (end > start)
    );

    CREATE TABLE media_segment (
        segment_id INTEGER PRIMARY KEY,
        media_id TEXT NOT NULL REFERENCES media (media_id) ON DELETE CASCADE,
        start REAL NOT NULL,
        end REAL NOT NULL,
        text TEXT NOT NULL,
        origin TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT 'speech'
    );

    CREATE INDEX media_segment_time ON media_segment (media_id, start);

    CREATE TABLE media_summary (
        summary_id INTEGER PRIMARY KEY,
        media_id TEXT NOT NULL REFERENCES media (media_id) ON DELETE CASCADE,
        scope TEXT NOT NULL CHECK (scope IN ('chunk', 'whole')),
        start REAL,
        end REAL,
        text TEXT NOT NULL,
        model TEXT,
        evidence TEXT NOT NULL DEFAULT 'inference',
        created_at TEXT NOT NULL
    );

    CREATE TABLE media_job (
        job_id TEXT PRIMARY KEY,
        media_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('queued', 'running', 'completed', 'failed', 'cancelled')),
        stage TEXT,
        progress REAL NOT NULL DEFAULT 0,
        detail TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT
    );

    CREATE INDEX media_job_media ON media_job (media_id);

    CREATE TABLE media_tombstone (
        media_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        deleted_at TEXT NOT NULL
    );

    CREATE TABLE media_setting (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE VIRTUAL TABLE media_search USING fts5(
        text,
        media_id UNINDEXED,
        kind UNINDEXED,
        ref UNINDEXED,
        start UNINDEXED,
        tokenize = 'unicode61'
    );
    """,
]

PROVIDERS = ("local", "jellyfin", "youtube")

# Seven days: longer than any recording Freya is asked about.
MAX_SECONDS = 7 * 24 * 3600.0

MAX_TITLE = 300
MAX_NOTE = 2000
MAX_TAG = 40
MAX_TAGS = 20
MAX_SKIPS = 100
MAX_NOTES = 1000
MAX_SEGMENT_TEXT = 2000
MAX_SEGMENTS = 20000
MAX_SUMMARY = 4000
MAX_SEARCH_RESULTS = 50
MAX_LIST = 500

JOB_STATES = ("queued", "running", "completed", "failed", "cancelled")
FINISHED_STATES = ("completed", "failed", "cancelled")

# Who stands behind each kind of text, as recall reports it.
EVIDENCE_TRUST = {
    "title": "untrusted-source",
    "speech": "untrusted-source",
    "inference": "model-inference",
    "owner-note": "owner",
}


class MediaGone(LookupError):
    """
    The media record was deleted. Work for it is dropped.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def check_seconds(value, name="time", allow_none=False) -> Optional[float]:
    """
    A finite time in seconds from the start of a recording, within
    MAX_SECONDS.
    """
    if value is None and allow_none:
        return None

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"The {name} must be a number of seconds.")

    value = float(value)

    if not math.isfinite(value) or value < 0 or value > MAX_SECONDS:
        raise ValueError(f"The {name} must be between 0 and {int(MAX_SECONDS)} seconds.")

    return round(value, 3)


def check_text(value, name, limit, required=True) -> Optional[str]:
    if value is None and not required:
        return None

    if not isinstance(value, str):
        raise ValueError(f"The {name} must be text.")

    value = value.replace("\x00", "").strip()

    if required and not value:
        raise ValueError(f"The {name} is empty.")

    if len(value) > limit:
        raise ValueError(f"The {name} can be at most {limit} characters.")

    return value


def search_expression(query) -> Optional[str]:
    """
    A safe FTS5 expression for what was typed: each word is quoted,
    so nothing typed is read as search syntax.
    """
    words = re.findall(r"\w+", str(query or ""))[:8]

    if not words:
        return None

    return " ".join(f'"{word}"*' for word in words)


class MediaStore:
    """
    Freya's media catalog, in a database of its own (not the personal
    memory database). Every change happens in one transaction; derived
    text for a deleted record cannot be written afterwards.
    """

    def __init__(self, path, clock=_now):
        self.path = Path(path)

        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._clock = clock
        self._lock = threading.RLock()
        self._job_context = threading.local()
        self._connection = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA secure_delete = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    # -- plumbing ------------------------------------------------------

    def _migrate(self):
        with self._lock:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._connection.execute("SELECT version FROM schema_version").fetchone()
            version = row["version"] if row else 0

            if version > len(MIGRATIONS):
                raise RuntimeError(
                    "The media database was made by a newer Freya and cannot be opened."
                )

            for number in range(version, len(MIGRATIONS)):
                with self.transaction() as connection:
                    for statement in _statements(MIGRATIONS[number]):
                        connection.execute(statement)

                    if number == 0 and row is None:
                        connection.execute(
                            "INSERT INTO schema_version (version) VALUES (?)", (number + 1,)
                        )
                    else:
                        connection.execute(
                            "UPDATE schema_version SET version = ?", (number + 1,)
                        )

    @contextmanager
    def transaction(self):
        with self._lock:
            scope = getattr(self._job_context, "scope", None)
            if scope:
                job_id, authorize = scope
                job = self._connection.execute("SELECT state FROM media_job WHERE job_id=?", (job_id,)).fetchone()
                if job is None or job["state"] != "running" or not authorize():
                    raise MediaGone("This analysis is no longer authorized to publish.")
            self._connection.execute("BEGIN IMMEDIATE")

            try:
                yield _Connection(self._connection)
                if scope and not scope[1]():
                    raise MediaGone("This analysis was cancelled before publication.")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    @contextmanager
    def job_scope(self, job_id, authorize):
        self._job_context.scope = (job_id, authorize)
        try:
            yield
        finally:
            self._job_context.scope = None

    def _rows(self, sql, parameters=()) -> List[dict]:
        with self._lock:
            return [dict(row) for row in self._connection.execute(sql, parameters)]

    def _row(self, sql, parameters=()) -> Optional[dict]:
        rows = self._rows(sql, parameters)
        return rows[0] if rows else None

    def close(self):
        with self._lock:
            self._connection.close()

    @staticmethod
    def _live(connection, media_id):
        if connection.execute(
            "SELECT 1 FROM media WHERE media_id = ?", (media_id,)
        ).fetchone() is None:
            raise MediaGone("That media record no longer exists.")

    # -- media records -----------------------------------------------

    def add_media(self, provider, source_ref, title, kind, acquisition, **fields) -> dict:
        """
        Add a record, or return the existing one for the same source.
        Returns the record with `created` True when it is new.
        """
        if provider not in PROVIDERS:
            raise ValueError("Unknown media provider.")

        title = check_text(title, "title", MAX_TITLE)
        source_ref = check_text(source_ref, "source", 2000)
        duration = check_seconds(fields.get("duration"), "duration", allow_none=True)

        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT media_id FROM media WHERE provider = ? AND source_ref = ?",
                (provider, source_ref),
            ).fetchone()

            if existing is not None:
                record = self.get(existing["media_id"])
                record["created"] = False
                return record

            media_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO media (
                    media_id, provider, source_ref, source_url, acquisition,
                    title, creator, kind, media_type, duration, root, relative,
                    size, modified_ns, fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    media_id, provider, source_ref, fields.get("source_url"),
                    acquisition, title,
                    check_text(fields.get("creator"), "creator", MAX_TITLE, required=False),
                    kind, fields.get("media_type"), duration, fields.get("root"),
                    fields.get("relative"), fields.get("size"),
                    fields.get("modified_ns"), fields.get("fingerprint"),
                    self._clock(),
                ),
            )
            connection.execute(
                "INSERT INTO media_search (text, media_id, kind, ref, start) "
                "VALUES (?, ?, 'title', ?, NULL)",
                (title, media_id, media_id),
            )

        record = self.get(media_id)
        record["created"] = True
        return record

    def get(self, media_id) -> Optional[dict]:
        if not isinstance(media_id, str) or not re.fullmatch(r"[0-9a-f]{32}", media_id):
            return None

        record = self._row("SELECT * FROM media WHERE media_id = ?", (media_id,))

        if record is None:
            return None

        record["coverage"] = json.loads(record["coverage"]) if record["coverage"] else None
        record["tags"] = [
            row["tag"]
            for row in self._rows(
                "SELECT tag FROM media_tag WHERE media_id = ? ORDER BY tag", (media_id,)
            )
        ]
        return record

    def find_source(self, provider, source_ref) -> Optional[dict]:
        row = self._row(
            "SELECT media_id FROM media WHERE provider = ? AND source_ref = ?",
            (provider, source_ref),
        )
        return self.get(row["media_id"]) if row else None

    def list_media(self, provider=None, tag=None, limit=MAX_LIST) -> List[dict]:
        sql = "SELECT media.* FROM media"
        where, parameters = [], []

        if tag:
            sql += " JOIN media_tag USING (media_id)"
            where.append("media_tag.tag = ?")
            parameters.append(str(tag)[:MAX_TAG].lower())

        if provider in PROVIDERS:
            where.append("media.provider = ?")
            parameters.append(provider)

        if where:
            sql += " WHERE " + " AND ".join(where)

        sql += " ORDER BY media.created_at DESC LIMIT ?"
        parameters.append(max(1, min(int(limit), MAX_LIST)))

        records = self._rows(sql, parameters)

        for record in records:
            record["coverage"] = json.loads(record["coverage"]) if record["coverage"] else None

        return records

    def set_duration(self, media_id, seconds):
        seconds = check_seconds(seconds, "duration")

        with self.transaction() as connection:
            self._live(connection, media_id)
            connection.execute(
                "UPDATE media SET duration = ? WHERE media_id = ?", (seconds, media_id)
            )

    def set_file_identity(self, media_id, size, modified_ns, fingerprint):
        with self.transaction() as connection:
            self._live(connection, media_id)
            connection.execute(
                "UPDATE media SET size = ?, modified_ns = ?, fingerprint = ? "
                "WHERE media_id = ?",
                (size, modified_ns, fingerprint, media_id),
            )

    def set_tags(self, media_id, tags: Iterable[str]) -> List[str]:
        if not isinstance(tags, (list, tuple)):
            raise ValueError("Tags are a list of words.")

        cleaned = []

        for tag in tags[:MAX_TAGS]:
            tag = check_text(tag, "tag", MAX_TAG).lower()

            if tag not in cleaned:
                cleaned.append(tag)

        with self.transaction() as connection:
            self._live(connection, media_id)
            connection.execute("DELETE FROM media_tag WHERE media_id = ?", (media_id,))

            for tag in cleaned:
                connection.execute(
                    "INSERT INTO media_tag (media_id, tag) VALUES (?, ?)", (media_id, tag)
                )

        return cleaned

    def delete_media(self, media_id) -> bool:
        """
        Remove a record and everything derived from it, at once, and
        leave a tombstone. The original file is never touched.
        """
        with self.transaction() as connection:
            record = connection.execute(
                "SELECT provider FROM media WHERE media_id = ?", (media_id,)
            ).fetchone()

            if record is None:
                return False

            connection.execute("DELETE FROM media_search WHERE media_id = ?", (media_id,))
            connection.execute(
                "UPDATE media_job SET state = 'cancelled', finished_at = ?, "
                "error = 'The media record was deleted.' "
                "WHERE media_id = ? AND state IN ('queued', 'running')",
                (self._clock(), media_id),
            )
            connection.execute("DELETE FROM media WHERE media_id = ?", (media_id,))
            connection.execute(
                "INSERT OR REPLACE INTO media_tombstone (media_id, provider, deleted_at) "
                "VALUES (?, ?, ?)",
                (media_id, record["provider"], self._clock()),
            )

        return True

    def is_deleted(self, media_id) -> bool:
        return self._row(
            "SELECT 1 AS gone FROM media_tombstone WHERE media_id = ?", (media_id,)
        ) is not None

    # -- notes, skips --------------------------------------------------

    def add_note(self, media_id, text, at=None) -> dict:
        text = check_text(text, "note", MAX_NOTE)
        at = check_seconds(at, "note time", allow_none=True)

        with self.transaction() as connection:
            self._live(connection, media_id)

            count = connection.execute(
                "SELECT COUNT(*) AS n FROM media_note WHERE media_id = ?", (media_id,)
            ).fetchone()["n"]

            if count >= MAX_NOTES:
                raise ValueError(f"A media record can have at most {MAX_NOTES} notes.")

            note_id = connection.execute(
                "INSERT INTO media_note (media_id, at, text, created_at) VALUES (?, ?, ?, ?)",
                (media_id, at, text, self._clock()),
            ).lastrowid
            connection.execute(
                "INSERT INTO media_search (text, media_id, kind, ref, start) "
                "VALUES (?, ?, 'owner-note', ?, ?)",
                (text, media_id, note_id, at),
            )

        return {"id": note_id, "at": at, "text": text}

    def delete_note(self, media_id, note_id) -> bool:
        with self.transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM media_note WHERE media_id = ? AND note_id = ?",
                (media_id, note_id),
            ).rowcount
            connection.execute(
                "DELETE FROM media_search WHERE media_id = ? AND kind = 'owner-note' "
                "AND ref = ?",
                (media_id, note_id),
            )

        return bool(deleted)

    def notes(self, media_id) -> List[dict]:
        return self._rows(
            "SELECT note_id AS id, at, text, created_at FROM media_note "
            "WHERE media_id = ? ORDER BY COALESCE(at, 0), note_id",
            (media_id,),
        )

    def add_skip(self, media_id, start, end, label="Skip", confirmed=True) -> dict:
        start = check_seconds(start, "start")
        end = check_seconds(end, "end")
        label = check_text(label or "Skip", "label", 60)

        if end <= start:
            raise ValueError("A skipped part must end after it starts.")

        with self.transaction() as connection:
            self._live(connection, media_id)

            count = connection.execute(
                "SELECT COUNT(*) AS n FROM media_skip WHERE media_id = ?", (media_id,)
            ).fetchone()["n"]

            if count >= MAX_SKIPS:
                raise ValueError(f"A media record can have at most {MAX_SKIPS} skipped parts.")

            skip_id = connection.execute(
                "INSERT INTO media_skip (media_id, start, end, label, confirmed, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (media_id, start, end, label, 1 if confirmed else 0, self._clock()),
            ).lastrowid

        return {"id": skip_id, "start": start, "end": end, "label": label,
                "confirmed": bool(confirmed)}

    def delete_skip(self, media_id, skip_id) -> bool:
        with self.transaction() as connection:
            return bool(
                connection.execute(
                    "DELETE FROM media_skip WHERE media_id = ? AND skip_id = ?",
                    (media_id, skip_id),
                ).rowcount
            )

    def skips(self, media_id) -> List[dict]:
        rows = self._rows(
            "SELECT skip_id AS id, start, end, label, confirmed FROM media_skip "
            "WHERE media_id = ? ORDER BY start",
            (media_id,),
        )

        for row in rows:
            row["confirmed"] = bool(row["confirmed"])

        return rows

    # -- transcript and analysis ---------------------------------------

    def segment_count(self, media_id) -> int:
        return self._row(
            "SELECT COUNT(*) AS n FROM media_segment WHERE media_id = ?", (media_id,)
        )["n"]

    def add_segments(self, media_id, segments, origin) -> int:
        """
        Store transcript segments as (start, end, text). Segments with
        an impossible time are refused; empty text is skipped.
        """
        origin = check_text(origin, "origin", 120)
        cleaned = []

        for start, end, text in segments:
            start = check_seconds(start, "segment start")
            end = check_seconds(end, "segment end")
            text = str(text or "").replace("\x00", "").strip()[:MAX_SEGMENT_TEXT]

            if end < start:
                raise ValueError("A transcript segment ends before it starts.")

            if text:
                cleaned.append((start, end, text))

        with self.transaction() as connection:
            self._live(connection, media_id)

            count = connection.execute(
                "SELECT COUNT(*) AS n FROM media_segment WHERE media_id = ?", (media_id,)
            ).fetchone()["n"]

            cleaned = cleaned[: max(0, MAX_SEGMENTS - count)]

            for start, end, text in cleaned:
                segment_id = connection.execute(
                    "INSERT INTO media_segment (media_id, start, end, text, origin) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (media_id, start, end, text, origin),
                ).lastrowid
                connection.execute(
                    "INSERT INTO media_search (text, media_id, kind, ref, start) "
                    "VALUES (?, ?, 'speech', ?, ?)",
                    (text, media_id, segment_id, start),
                )

        return len(cleaned)

    def clear_segments(self, media_id, origin_prefix=None):
        with self.transaction() as connection:
            if origin_prefix:
                ids = [
                    row["segment_id"]
                    for row in connection.execute(
                        "SELECT segment_id FROM media_segment WHERE media_id = ? "
                        "AND origin LIKE ? ESCAPE '\\'",
                        (media_id, origin_prefix.replace("%", "\\%").replace("_", "\\_") + "%"),
                    )
                ]
            else:
                ids = [
                    row["segment_id"]
                    for row in connection.execute(
                        "SELECT segment_id FROM media_segment WHERE media_id = ?", (media_id,)
                    )
                ]

            for segment_id in ids:
                connection.execute(
                    "DELETE FROM media_segment WHERE segment_id = ?", (segment_id,)
                )
                connection.execute(
                    "DELETE FROM media_search WHERE media_id = ? AND kind = 'speech' "
                    "AND ref = ?",
                    (media_id, segment_id),
                )

    def replace_transcript(self, media_id, segments, origin):
        origin = check_text(origin, "origin", 120)
        cleaned = []
        for start, end, text in segments:
            start, end = check_seconds(start), check_seconds(end)
            if end < start:
                raise ValueError("A transcript segment ends before it starts.")
            text = str(text or "").replace("\x00", "").strip()[:MAX_SEGMENT_TEXT]
            if text:
                cleaned.append((start, end, text))
            if len(cleaned) >= MAX_SEGMENTS:
                break
        if not cleaned:
            raise ValueError("The transcript contains no usable text.")
        with self.transaction() as connection:
            self._live(connection, media_id)
            connection.execute("UPDATE media_job SET state='cancelled', finished_at=? WHERE media_id=? AND state IN ('queued','running')", (self._clock(), media_id))
            connection.execute("DELETE FROM media_segment WHERE media_id=?", (media_id,))
            connection.execute("DELETE FROM media_summary WHERE media_id=?", (media_id,))
            connection.execute("DELETE FROM media_search WHERE media_id=? AND kind IN ('speech','inference')", (media_id,))
            connection.execute("UPDATE media SET analyzed_at=NULL, coverage=NULL, analysis_model=NULL, transcriber=NULL WHERE media_id=?", (media_id,))
            for start, end, text in cleaned:
                identifier = connection.execute("INSERT INTO media_segment(media_id,start,end,text,origin) VALUES(?,?,?,?,?)", (media_id,start,end,text,origin)).lastrowid
                connection.execute("INSERT INTO media_search(text,media_id,kind,ref,start) VALUES(?,?,'speech',?,?)", (text,media_id,identifier,start))
        return len(cleaned)

    def segments(self, media_id, limit=MAX_SEGMENTS) -> List[dict]:
        return self._rows(
            "SELECT segment_id AS id, start, end, text, origin, evidence "
            "FROM media_segment WHERE media_id = ? ORDER BY start, segment_id LIMIT ?",
            (media_id, max(1, min(int(limit), MAX_SEGMENTS))),
        )

    def replace_summaries(self, media_id, summaries, model) -> None:
        """
        Store the analysis summaries for a record, replacing earlier
        ones: each is (scope, start, end, text).
        """
        with self.transaction() as connection:
            self._live(connection, media_id)
            connection.execute(
                "DELETE FROM media_search WHERE media_id = ? AND kind = 'inference'",
                (media_id,),
            )
            connection.execute("DELETE FROM media_summary WHERE media_id = ?", (media_id,))

            for scope, start, end, text in summaries:
                text = check_text(text, "summary", MAX_SUMMARY)
                start = check_seconds(start, "summary start", allow_none=True)
                end = check_seconds(end, "summary end", allow_none=True)
                summary_id = connection.execute(
                    "INSERT INTO media_summary (media_id, scope, start, end, text, model, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (media_id, scope, start, end, text, model, self._clock()),
                ).lastrowid
                connection.execute(
                    "INSERT INTO media_search (text, media_id, kind, ref, start) "
                    "VALUES (?, ?, 'inference', ?, ?)",
                    (text, media_id, summary_id, start),
                )

    def summaries(self, media_id) -> List[dict]:
        return self._rows(
            "SELECT summary_id AS id, scope, start, end, text, model, evidence, created_at "
            "FROM media_summary WHERE media_id = ? "
            "ORDER BY scope = 'chunk', COALESCE(start, 0)",
            (media_id,),
        )

    def record_analysis(self, media_id, model, transcriber, coverage) -> None:
        with self.transaction() as connection:
            self._live(connection, media_id)
            connection.execute(
                "UPDATE media SET analyzed_at = ?, analysis_model = ?, transcriber = ?, "
                "coverage = ? WHERE media_id = ?",
                (self._clock(), model, transcriber, json.dumps(coverage), media_id),
            )

    # -- jobs ----------------------------------------------------------

    def add_job(self, media_id) -> dict:
        job_id = uuid.uuid4().hex

        with self.transaction() as connection:
            self._live(connection, media_id)
            active = connection.execute(
                "SELECT job_id FROM media_job WHERE media_id = ? "
                "AND state IN ('queued', 'running')",
                (media_id,),
            ).fetchone()

            if active is not None:
                return self.job(active["job_id"])

            connection.execute(
                "INSERT INTO media_job (job_id, media_id, state, stage, created_at) "
                "VALUES (?, ?, 'queued', 'waiting', ?)",
                (job_id, media_id, self._clock()),
            )

        return self.job(job_id)

    def job(self, job_id) -> Optional[dict]:
        return self._row("SELECT * FROM media_job WHERE job_id = ?", (job_id,))

    def jobs(self, limit=50) -> List[dict]:
        return self._rows(
            "SELECT media_job.*, media.title FROM media_job "
            "LEFT JOIN media USING (media_id) "
            "ORDER BY media_job.created_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )

    def update_job(self, job_id, **fields) -> bool:
        """
        Change a job, unless it has already finished. Returns False when
        it had (for example, it was cancelled meanwhile).
        """
        allowed = {"state", "stage", "progress", "detail", "error", "started_at", "finished_at"}
        fields = {name: value for name, value in fields.items() if name in allowed}

        if fields.get("state") is not None and fields["state"] not in JOB_STATES:
            raise ValueError("Unknown job state.")

        if fields.get("state") in FINISHED_STATES:
            fields.setdefault("finished_at", self._clock())

        if fields.get("state") == "running":
            fields.setdefault("started_at", self._clock())

        if not fields:
            return False

        assignments = ", ".join(f"{name} = ?" for name in fields)

        with self.transaction() as connection:
            return bool(
                connection.execute(
                    f"UPDATE media_job SET {assignments} WHERE job_id = ? "
                    "AND state IN ('queued', 'running')",
                    (*fields.values(), job_id),
                ).rowcount
            )

    def reconcile_jobs(self) -> dict:
        """
        After a restart: a job that was running was interrupted, and is
        marked failed (what it had stored is kept); queued jobs are
        returned so they can be queued again.
        """
        with self.transaction() as connection:
            interrupted = connection.execute(
                "UPDATE media_job SET state = 'failed', finished_at = ?, "
                "error = 'Interrupted when Freya stopped. Work already saved is kept; "
                "analyze again to finish.' WHERE state = 'running'",
                (self._clock(),),
            ).rowcount

        queued = self._rows(
            "SELECT job_id, media_id FROM media_job WHERE state = 'queued' "
            "ORDER BY created_at"
        )

        return {"interrupted": interrupted, "queued": queued}

    # -- settings ------------------------------------------------------

    def setting(self, name, default=None):
        row = self._row("SELECT value FROM media_setting WHERE name = ?", (name,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, name, value):
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO media_setting (name, value) VALUES (?, ?)",
                (name, json.dumps(value)),
            )

    # -- recall --------------------------------------------------------

    def search(self, query, limit=20, media_id=None) -> List[dict]:
        """
        Passages matching the words typed, each with where it came from
        and the time in the original recording it was said.
        """
        expression = search_expression(query)

        if expression is None:
            return []

        sql = (
            "SELECT media_search.media_id, media_search.kind, media_search.ref, "
            "media_search.start, "
            "snippet(media_search, 0, '', '', '…', 24) AS excerpt, "
            "media.title, media.provider, media.source_url, media.trust "
            "FROM media_search JOIN media USING (media_id) "
            "WHERE media_search MATCH ?"
        )
        parameters = [expression]

        if media_id:
            sql += " AND media_search.media_id = ?"
            parameters.append(media_id)

        sql += " ORDER BY rank LIMIT ?"
        parameters.append(max(1, min(int(limit), MAX_SEARCH_RESULTS)))

        try:
            rows = self._rows(sql, parameters)
        except sqlite3.OperationalError:
            return []

        results = []

        for row in rows:
            start = row["start"]
            results.append(
                {
                    "media_id": row["media_id"],
                    "title": row["title"],
                    "provider": row["provider"],
                    "source_url": row["source_url"],
                    "evidence": row["kind"],
                    "trust": EVIDENCE_TRUST.get(row["kind"], "untrusted-source"),
                    "start": float(start) if start is not None else None,
                    "text": row["excerpt"],
                    "ref": str(row["ref"]),
                }
            )

        return results


def _statements(script):
    """
    Split a migration into statements, so each runs inside the
    migration's transaction (executescript would commit first).
    """
    statement = ""

    for line in script.splitlines(keepends=True):
        statement += line

        if sqlite3.complete_statement(statement):
            if statement.strip():
                yield statement.strip()

            statement = ""

    if statement.strip():
        yield statement.strip()


class _Connection:
    """
    The connection inside a transaction. Only execute is offered.
    """

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        return self._connection.execute(sql, parameters)
