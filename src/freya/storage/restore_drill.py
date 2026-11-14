"""
Time a restore of scratch data.

The drill creates a temporary data directory, writes one made-up row
into each store (control, usage, memory, media, tasks), backs it up,
deletes it, restores it, and checks integrity. It never opens the
owner's data directory. The times are for that scratch data only.
They are not a measurement of a real backup.

    python devtools/restore_drill.py
    python -m freya.storage.restore_drill
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from freya.core.audit.log import SqliteAuditLog
from freya.core.persistence.control_store import ControlStore
from freya.core.usage.ledger import SCOPE_CALL, USAGE_MIGRATIONS, UsageLedger
from freya.media.store import MediaStore
from freya.memory import memory_manager
from freya.memory.conversations import ConversationStore
from freya.memory.deletion_ledger import LEDGER_DIRECTORY, ledger_path_for
from freya.memory.protection import ensure_memory_protection
from freya.paths import data_dir
from freya.storage.backup import BackupRefused, back_up, check
from freya.storage.restore import restore
from freya.tasks.lifecycle import TASK_MIGRATIONS


# Inside the targets ADR-015 records. Scratch data is expected to be
# far under both; the comparison is what the drill reports.
RTO_SECONDS = 4 * 60 * 60
RPO_SECONDS = 24 * 60 * 60

WHEN = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
SCRATCH_FACT = "Scratch fact 4419: the drill bell is blue."

# Fixed queries. The names are the drill's, not anything a caller typed.
ROW_QUERIES: Tuple[Tuple[str, str, str], ...] = (
    ("freya_control.db", "audit_event", "SELECT COUNT(*) FROM audit_event"),
    ("freya_control.db", "policy_state", "SELECT COUNT(*) FROM policy_state"),
    ("freya_usage.db", "usage_limit", "SELECT COUNT(*) FROM usage_limit"),
    ("freya_memory.db", "memories", "SELECT COUNT(*) FROM memories"),
    ("freya_memory.db", "conversation", "SELECT COUNT(*) FROM conversation"),
    ("media/freya_media.db", "media", "SELECT COUNT(*) FROM media"),
    ("freya_tasks.db", "task", "SELECT COUNT(*) FROM task"),
)


@dataclass(frozen=True)
class DrillResult:
    """Timings and checks for one scratch run."""

    ok: bool
    seconds: float
    backup_seconds: float
    restore_seconds: float
    verify_seconds: float
    recovery_point_seconds: float
    rows: Tuple[Tuple[str, int], ...]
    copied: Tuple[str, ...]
    problems: Tuple[str, ...]

    @property
    def inside_rto(self) -> bool:
        return self.seconds < RTO_SECONDS

    @property
    def inside_rpo(self) -> bool:
        return self.recovery_point_seconds < RPO_SECONDS

    def report(self) -> str:
        """Plain text. No row contents, only counts and times."""
        lines = [
            "Restore drill, measured on CI scratch data.",
            f"whole: {self.seconds:.3f} s",
            f"backup: {self.backup_seconds:.3f} s",
            f"restore: {self.restore_seconds:.3f} s",
            f"verify: {self.verify_seconds:.3f} s",
            f"recovery point gap: {self.recovery_point_seconds:.3f} s",
            "inside 4 h RTO: " + ("yes" if self.inside_rto else "no"),
            "inside 24 h RPO: " + ("yes" if self.inside_rpo else "no"),
            "rows: " + ", ".join(f"{name}={count}" for name, count in self.rows),
            "copied: " + ", ".join(self.copied),
        ]

        if self.problems:
            lines.append("problems: " + "; ".join(self.problems))

        lines.append(
            "This is scratch data in a temporary directory, "
            "not the owner's databases."
        )

        return "\n".join(lines)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False

    return True


def _refuse_owner_data(root: Path, data: Path) -> None:
    owner = data_dir().resolve()

    if data == owner or _inside(owner, root) or _inside(root, owner):
        raise BackupRefused(
            "The restore drill does not use the owner's data directory."
        )


def _record(log: SqliteAuditLog, operation_id: str) -> None:
    log.record(
        operation_id=operation_id,
        kind="dispatch",
        actor="local-owner",
        client="cli",
        domain="local",
        capability="system.info",
        provider="system-tools-local",
        decision="allow",
        decision_reason="scratch drill",
        policy_revision=1,
        grant_epoch=1,
    )


def _seed(data: Path, environ) -> None:
    ledger = ensure_memory_protection(environ)
    memory = memory_manager.MemoryManager(ledger)

    try:
        conversations = ConversationStore(memory.connection, memory.ledger)
        memory.add_memory(SCRATCH_FACT, "fact", "drill")
        conversations.add_exchange("scratch hello", "scratch reply")
    finally:
        if memory.ledger is not None:
            memory.ledger.close()

        memory.connection.close()

    control = ControlStore(data / "freya_control.db")

    try:
        log = SqliteAuditLog(control, checkpoint_interval=0)
        log.start()
        _record(log, "scratch-1")
        _record(log, "scratch-2")
    finally:
        control.close()

    usage = ControlStore(data / "freya_usage.db", USAGE_MIGRATIONS)

    try:
        UsageLedger(usage).set_limit(
            "scratch",
            "daily",
            SCOPE_CALL,
            "tokens",
            10,
            now=WHEN,
        )
    finally:
        usage.close()

    media = MediaStore(
        data / "media" / "freya_media.db",
        clock=lambda: "2026-11-13T12:00:00+00:00",
    )

    try:
        media.add_media("local", "scratch-1", "Scratch reel", "video", "owner")
    finally:
        media.close()

    tasks = ControlStore(data / "freya_tasks.db", TASK_MIGRATIONS)

    try:
        tasks.execute(
            "INSERT INTO task ("
            "task_id, title, state, check_name, interrupted, "
            "run_token, detail, created_at, updated_at"
            ") VALUES (?, ?, 'created', 'scratch', 0, ?, '', ?, ?)",
            (
                "scratch-task",
                "Scratch task",
                "scratch-token",
                "2026-11-13T12:00:00Z",
                "2026-11-13T12:00:00Z",
            ),
        )
    finally:
        tasks.close()


def _open_read_only(path: Path):
    return sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )


def _integrity(path: Path) -> Optional[str]:
    if not path.is_file():
        return f"{path.name} is missing."

    connection = _open_read_only(path)

    try:
        verdict = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error:
        return f"{path.name} cannot be opened."
    finally:
        connection.close()

    if verdict != "ok":
        return f"{path.name} fails its own check."

    return None


def _memory_has_fact(path: Path) -> bool:
    if not path.is_file():
        return False

    connection = _open_read_only(path)

    try:
        row = connection.execute(
            "SELECT 1 FROM memories WHERE content = ?",
            (SCRATCH_FACT,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        connection.close()

    return row is not None


def _counts(data: Path, ledger: Path) -> Dict[str, int]:
    found = {}

    for name, table, query in ROW_QUERIES:
        connection = _open_read_only(data / name)

        try:
            found[f"{name}:{table}"] = connection.execute(query).fetchone()[0]
        finally:
            connection.close()

    connection = _open_read_only(ledger)

    try:
        found["deletion_ledger.db:tombstone"] = connection.execute(
            "SELECT COUNT(*) FROM tombstone"
        ).fetchone()[0]
    finally:
        connection.close()

    return found


def _remove_ledger(ledger: Path) -> None:
    """
    Remove the scratch ledger. Only the drill's own directory is
    removed as a tree. Any other path loses just the file.
    """
    if ledger.parent.name == LEDGER_DIRECTORY and ledger.parent.is_dir():
        shutil.rmtree(ledger.parent)
        return

    ledger.unlink(missing_ok=True)


def run_drill(root, now: Optional[datetime] = None, environ=None) -> DrillResult:
    """
    Run one drill under ``root`` and return the timings.

    ``environ`` is passed to the ledger path. The default for this
    function is an empty mapping, so a ``FREYA_DELETION_LEDGER`` in
    the process environment cannot point the drill at another ledger.
    The backup is allowed on the same drive because a temporary
    directory has no second drive. The backup Freya makes as she
    closes still refuses the same drive.
    """
    if environ is None:
        environ = {}

    root = Path(root).resolve()
    data = root / "data"
    _refuse_owner_data(root, data)

    try:
        data.mkdir(parents=True)
    except FileExistsError as exc:
        raise BackupRefused(
            "The drill needs an empty folder. Nothing was deleted."
        ) from exc

    moment = now or WHEN
    saved = (memory_manager.DATA_DIR, memory_manager.DATABASE_PATH)
    memory_manager.DATA_DIR = data
    memory_manager.DATABASE_PATH = data / "freya_memory.db"
    problems: List[str] = []
    started = time.perf_counter()

    try:
        _seed(data, environ)
        ledger = ledger_path_for(data, environ)
        before = _counts(data, ledger)
        backup_started = time.perf_counter()
        # Same drive on purpose: scratch has nowhere else to go.
        folder = back_up(
            data,
            root / "backups",
            moment,
            allow_same_drive=True,
            environ=environ,
        )
        backup_finished = time.perf_counter()
        copied = tuple(
            entry["name"]
            for entry in json.loads(
                (folder / "manifest.json").read_text(encoding="utf-8")
            )["copied"]
        )
        manifest_problems = check(folder)

        if manifest_problems:
            problems.extend(manifest_problems)

        shutil.rmtree(data)
        _remove_ledger(ledger)
        restore_started = time.perf_counter()
        restore(folder, data, environ)
        restore_finished = time.perf_counter()
        verify_started = time.perf_counter()

        for name, _table, _query in ROW_QUERIES:
            problem = _integrity(data / name)

            if problem:
                problems.append(problem)

        ledger_problem = _integrity(ledger)

        if ledger_problem:
            problems.append(ledger_problem)

        if not _memory_has_fact(data / "freya_memory.db"):
            problems.append("The scratch memory row did not survive the restore.")

        if ledger.is_file() and data.is_dir():
            after = _counts(data, ledger)

            if after != before:
                problems.append("Row counts differ after restore.")
        else:
            after = {}
            problems.append("Restore did not bring the stores back.")

        control = data / "freya_control.db"

        if control.is_file():
            store = ControlStore(control)

            try:
                verdict = SqliteAuditLog(store).verify()
            finally:
                store.close()

            expected = before.get("freya_control.db:audit_event")

            if not verdict.ok or verdict.events_checked != expected:
                problems.append("The audit hash chain did not survive the restore.")

        verify_finished = time.perf_counter()
    finally:
        memory_manager.DATA_DIR, memory_manager.DATABASE_PATH = saved

    rows = tuple(sorted(before.items()))
    unique = []

    for problem in problems:
        if problem not in unique:
            unique.append(problem)

    recovery_point = verify_finished - backup_finished
    whole = verify_finished - started
    result = DrillResult(
        ok=not unique and whole < RTO_SECONDS and recovery_point < RPO_SECONDS,
        seconds=whole,
        backup_seconds=backup_finished - backup_started,
        restore_seconds=restore_finished - restore_started,
        verify_seconds=verify_finished - verify_started,
        recovery_point_seconds=recovery_point,
        rows=rows,
        copied=copied,
        problems=tuple(unique),
    )

    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Time a restore of scratch data. "
            "Does not read the owner's data directory."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="Empty folder to use. Default: a temporary directory, removed afterwards.",
    )
    args = parser.parse_args(argv)
    cleanup = None
    root = args.root

    if root is None:
        root = Path(tempfile.mkdtemp(prefix="freya-restore-drill-"))
        cleanup = root

    code = 1

    try:
        result = run_drill(root)
    except BackupRefused as refused:
        print(f"No drill was run: {refused}", file=sys.stderr)
    else:
        print(result.report())
        code = 0 if result.ok else 1
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)

    return code


if __name__ == "__main__":
    raise SystemExit(main())
