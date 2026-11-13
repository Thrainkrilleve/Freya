"""Durable, append-only state transitions for a local coding proposal.

Development-only module. No subprocesses, code execution, or privileged actions.
The caller remains responsible for identity/policy authorization.
"""
from __future__ import annotations

import json
import contextlib
import threading
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_JOURNAL_LOCKS = {}


def _journal_lock(path: Path):
    # Process-local serialization; cross-process locking remains a separate requirement.
    with _LOCKS_GUARD:
        return _JOURNAL_LOCKS.setdefault(str(path), threading.RLock())


@contextlib.contextmanager
def _process_lock(path: Path):
    """Cross-process exclusive lock using standard-library OS file locking."""
    lock_path = path.with_suffix(".lock")
    # Initialize once without inspecting a byte that another process may lock.
    # The lock file itself is persistent; never delete it on unlock.
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            # Windows byte-range locks cover even an empty file. Avoid reads
            # before acquiring the lock: another process may hold that byte.
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


STATES = ("requested", "generating", "proposed", "verified", "rejected", "failed")
ALLOWED = {
    "requested": {"generating", "rejected", "failed"},
    "generating": {"proposed", "failed"},
    "proposed": {"verified", "rejected", "failed"},
    "verified": set(),
    "rejected": set(),
    "failed": set(),
}


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    sequence: int
    state: str
    timestamp: float
    details: str = ""


class TaskJournal:
    """One journal per task; atomic replace and fsync, no silent overwrites."""

    def __init__(self, directory: Path, task_id: str):
        self.directory = directory.resolve()
        if not task_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in task_id):
            raise ValueError("Invalid task ID")
        self.task_id = task_id
        self.path = self.directory / f"{task_id}.json"

    @classmethod
    def create(cls, directory: Path) -> "TaskJournal":
        journal = cls(directory, str(uuid.uuid4()))
        journal.directory.mkdir(parents=True, exist_ok=True)
        event = TaskEvent(journal.task_id, 0, "requested", time.time())
        with journal.path.open("x", encoding="utf-8") as file:
            json.dump([asdict(event)], file)
            file.flush()
            os.fsync(file.fileno())
        return journal

    def read(self) -> list[TaskEvent]:
        events = [TaskEvent(**event) for event in json.loads(self.path.read_text(encoding="utf-8"))]
        if not events or events[0].task_id != self.task_id or events[0].state != "requested":
            raise ValueError("Invalid journal")
        for i, event in enumerate(events):
            if event.task_id != self.task_id or event.sequence != i or event.state not in STATES:
                raise ValueError("Invalid event")
            if i and event.state not in ALLOWED[events[i - 1].state]:
                raise ValueError("Invalid transition history")
        return events

    def transition(self, state: str, details: str = "") -> TaskEvent:
        with _journal_lock(self.path):
            with _process_lock(self.path):
                return self._transition_locked(state, details)

    def _transition_locked(self, state: str, details: str = "") -> TaskEvent:
        if state not in STATES:
            raise ValueError("Unknown task state")
        events = self.read()
        if state not in ALLOWED[events[-1].state]:
            raise ValueError(f"Transition {events[-1].state} -> {state} not allowed")
        if len(details) > 2000:
            raise ValueError("Details too long")
        event = TaskEvent(self.task_id, len(events), state, time.time(), details)
        updated = [asdict(item) for item in [*events, event]]
        # Unique names isolate crash leftovers and other writers' temporary files.
        # Simultaneous transitions on one journal still require serialization.
        temporary = self.directory / f"{self.task_id}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(updated, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return event
