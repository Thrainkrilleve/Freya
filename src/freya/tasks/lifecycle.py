"""
A local task journal.

States run created, authorized, running, verifying, then done or failed.
Cancelled is available until the task has finished. Nothing here asks a
model what to do. A check is a function this process registered, and it
runs before the task can be done. The journal is one SQLite file on
this machine. It does not schedule work onto another node.

Every transition is a capability on the broker, so it is denied unless
a rule allows it and the audit log records it. The package stays out of
the control plane unless FREYA_TASKS=1.
"""

import os
import re
import threading
import uuid
from datetime import datetime, timezone

from freya.core.capabilities import CapabilityDefinition
from freya.core.persistence.control_store import DATA_DIR, ControlStore
from freya.core.providers import ProviderDefinition


TASK_PROVIDER = "task-local"
TASK_DATABASE_NAME = "freya_tasks.db"
TASK_DATABASE_PATH = DATA_DIR / TASK_DATABASE_NAME

CREATED = "created"
AUTHORIZED = "authorized"
RUNNING = "running"
VERIFYING = "verifying"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = frozenset({DONE, FAILED, CANCELLED})
OPEN = frozenset({CREATED, AUTHORIZED, RUNNING, VERIFYING})
INTERRUPTED = frozenset({RUNNING, VERIFYING})

TASK_CREATE = "task.create"
TASK_AUTHORIZE = "task.authorize"
TASK_START = "task.start"
TASK_VERIFY = "task.verify"
TASK_FINISH = "task.finish"
TASK_FAIL = "task.fail"
TASK_CANCEL = "task.cancel"
TASK_RESUME = "task.resume"

TASK_CAPABILITIES = (
    TASK_CREATE,
    TASK_AUTHORIZE,
    TASK_START,
    TASK_VERIFY,
    TASK_FINISH,
    TASK_FAIL,
    TASK_CANCEL,
    TASK_RESUME,
)

_CHECK_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,40}$")
_TITLE_LIMIT = 200

TASK_MIGRATIONS = [
    """
    CREATE TABLE task (
        task_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        state TEXT NOT NULL,
        check_name TEXT NOT NULL,
        interrupted INTEGER NOT NULL CHECK (interrupted IN (0, 1)),
        run_token TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE task_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (task_id) REFERENCES task(task_id)
    );
    """,
]


class TaskError(Exception):
    """A transition was refused. The message names no task title."""


def tasks_enabled() -> bool:
    return os.environ.get("FREYA_TASKS") == "1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _title(value) -> str:
    if not isinstance(value, str):
        raise TaskError("A task needs a title.")

    text = value.strip()

    if not text or len(text) > _TITLE_LIMIT or any(ord(char) < 32 for char in text):
        raise TaskError("A task needs a title.")

    return text


def _check_name(value) -> str:
    if not isinstance(value, str) or _CHECK_NAME.fullmatch(value) is None:
        raise TaskError("That check is not registered.")

    return value


class TaskLifecycle:
    """
    The journal and the transitions. Handlers do the writing. Public
    methods only call the broker.
    """

    def __init__(self, path=TASK_DATABASE_PATH):
        self._store = ControlStore(path, TASK_MIGRATIONS)
        self._broker = None
        self._checks = {}
        self._token = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._recover()

    def close(self) -> None:
        self._store.close()

    def bind(self, broker) -> None:
        self._broker = broker

    def add_check(self, name, hook) -> None:
        """
        Register the mechanical check `name`. `hook` is called with
        the task id and returns True when the task holds.
        """
        checked = _check_name(name)

        if not callable(hook):
            raise TaskError("That check is not registered.")

        self._checks[checked] = hook

    def definitions(self):
        described = {
            TASK_CREATE: "Records a new local task. It does not run it.",
            TASK_AUTHORIZE: "Authorizes one local task. It does not run it.",
            TASK_START: "Marks one authorized task as running on this machine.",
            TASK_VERIFY: "Runs the task's mechanical check. No model is asked.",
            TASK_FINISH: "Records that a check passed.",
            TASK_FAIL: "Records that a check failed.",
            TASK_CANCEL: "Cancels a task that has not finished.",
            TASK_RESUME: "Continues a task this process found interrupted.",
        }
        handlers = {
            TASK_CREATE: self._create,
            TASK_AUTHORIZE: self._authorize,
            TASK_START: self._start,
            TASK_VERIFY: self._verify,
            TASK_FINISH: self._finish,
            TASK_FAIL: self._fail,
            TASK_CANCEL: self._cancel,
            TASK_RESUME: self._resume,
        }

        for name in TASK_CAPABILITIES:
            yield CapabilityDefinition(
                name=name,
                description=described[name],
                provider=TASK_PROVIDER,
                handler=handlers[name],
                category="task",
            )

    def create(self, title, check_name) -> str:
        return self._call(TASK_CREATE, title, check_name)["task_id"]

    def authorize(self, task_id) -> dict:
        return self._call(TASK_AUTHORIZE, task_id)

    def start(self, task_id) -> dict:
        return self._call(TASK_START, task_id)

    def verify(self, task_id) -> dict:
        return self._call(TASK_VERIFY, task_id)

    def cancel(self, task_id) -> dict:
        return self._call(TASK_CANCEL, task_id)

    def resume(self, task_id) -> dict:
        return self._call(TASK_RESUME, task_id)

    def get(self, task_id):
        with self._lock:
            return self._read(task_id)

    def events(self, task_id):
        with self._lock:
            return [
                dict(row)
                for row in self._store.execute(
                    """
                    SELECT from_state, to_state
                    FROM task_event
                    WHERE task_id = ?
                    ORDER BY event_id
                    """,
                    (task_id,),
                ).fetchall()
            ]

    def _call(self, name, *args):
        if self._broker is None:
            raise TaskError("Not authorized.")

        result = self._broker.invoke(name, *args)

        if result.outcome == "denied" or (
            result.error and "Not authorized" in result.error
        ):
            raise TaskError("Not authorized.")

        if not result.success:
            raise TaskError(_fixed(result.error or ""))

        return result.result

    def _recover(self) -> None:
        with self._lock:
            self._store.execute(
                """
                UPDATE task
                SET interrupted = 1, updated_at = ?
                WHERE state IN (?, ?) AND run_token != ?
                """,
                (_now(), RUNNING, VERIFYING, self._token),
            )

    def _create(self, title, check_name):
        text = _title(title)
        name = _check_name(check_name)

        if name not in self._checks:
            raise TaskError("That check is not registered.")

        task_id = uuid.uuid4().hex
        moment = _now()

        with self._lock:
            self._store.execute(
                """
                INSERT INTO task (
                    task_id, title, state, check_name, interrupted,
                    run_token, detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, '', '', ?, ?)
                """,
                (task_id, text, CREATED, name, moment, moment),
            )
            self._note(task_id, "", CREATED, moment)

        return {"task_id": task_id, "state": CREATED}

    def _authorize(self, task_id):
        return self._move(task_id, {CREATED}, AUTHORIZED)

    def _start(self, task_id):
        return self._move(
            task_id,
            {AUTHORIZED},
            RUNNING,
            run_token=self._token,
            interrupted=0,
        )

    def _verify(self, task_id):
        with self._lock:
            row = self._require(task_id)

            if row["state"] != RUNNING or row["interrupted"]:
                raise TaskError("That transition is not available.")

        self._move(
            task_id,
            {RUNNING},
            VERIFYING,
            run_token=self._token,
            interrupted=0,
        )
        return self._run_check(task_id)

    def _resume(self, task_id):
        with self._lock:
            row = self._require(task_id)

            if row["state"] not in INTERRUPTED or not row["interrupted"]:
                raise TaskError("That transition is not available.")

            state = row["state"]

        self._move(
            task_id,
            {state},
            state,
            run_token=self._token,
            interrupted=0,
            same_state=True,
        )

        if state == VERIFYING:
            return self._run_check(task_id)

        return self._read(task_id)

    def _run_check(self, task_id):
        with self._lock:
            row = self._require(task_id)

            if row["state"] != VERIFYING:
                raise TaskError("That transition is not available.")

            hook = self._checks.get(row["check_name"])

        if hook is None:
            return self._through(TASK_FAIL, task_id, "check-not-registered")

        try:
            passed = hook(task_id) is True
        except Exception:
            return self._through(TASK_FAIL, task_id, "check-failed")

        if passed:
            return self._through(TASK_FINISH, task_id)

        return self._through(TASK_FAIL, task_id)

    def _through(self, name, *args):
        result = self._broker.invoke(name, *args)

        if not result.success:
            raise TaskError(_fixed(result.error or ""))

        return result.result

    def _finish(self, task_id):
        return self._move(task_id, {VERIFYING}, DONE, detail="passed")

    def _fail(self, task_id, detail="check-returned-false"):
        if not isinstance(detail, str) or len(detail) > 80:
            detail = "check-failed"

        return self._move(task_id, {VERIFYING}, FAILED, detail=detail)

    def _cancel(self, task_id):
        return self._move(task_id, set(OPEN), CANCELLED, interrupted=0)

    def _move(self, task_id, expected, new_state, same_state=False, **sets):
        with self._lock:
            row = self._require(task_id)

            if row["state"] not in expected:
                raise TaskError("That transition is not available.")

            if row["state"] == new_state and not same_state:
                raise TaskError("That transition is not available.")

            if any(
                column not in ("run_token", "interrupted", "detail")
                for column in sets
            ):
                raise TaskError("That transition is not available.")

            moment = _now()
            self._store.execute(
                """
                UPDATE task
                SET state = ?, updated_at = ?, run_token = ?,
                    interrupted = ?, detail = ?
                WHERE task_id = ?
                """,
                (
                    new_state,
                    moment,
                    sets.get("run_token", row["run_token"]),
                    int(sets.get("interrupted", row["interrupted"])),
                    sets.get("detail", row["detail"]),
                    task_id,
                ),
            )
            self._note(task_id, row["state"], new_state, moment)
            return self._read(task_id)

    def _require(self, task_id):
        row = self._read(task_id)

        if row is None:
            raise TaskError("That transition is not available.")

        return row

    def _read(self, task_id):
        if not isinstance(task_id, str) or not task_id.strip():
            return None

        row = self._store.execute(
            """
            SELECT task_id, title, state, check_name, interrupted,
                   run_token, detail
            FROM task WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()

        if row is None:
            return None

        found = dict(row)
        found["interrupted"] = bool(found["interrupted"])
        return found

    def _note(self, task_id, from_state, to_state, moment):
        self._store.execute(
            """
            INSERT INTO task_event (task_id, from_state, to_state, recorded_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, from_state, to_state, moment),
        )


def _fixed(message: str) -> str:
    """Keep a handler's fixed sentence, and nothing that follows it."""
    for sentence in (
        "That transition is not available.",
        "That check is not registered.",
        "The check is not registered.",
        "The check failed.",
        "A task needs a title.",
    ):
        if sentence in message:
            return sentence

    return "That transition is not available."


def register_task_lifecycle(plane, lifecycle, *, replace=False) -> None:
    """
    Put the task capabilities on a control plane. No policy seed is
    applied: each transition stays denied until a rule allows it.
    """
    if plane.providers.get(TASK_PROVIDER) is None:
        plane.providers.register(
            ProviderDefinition(
                provider_id=TASK_PROVIDER,
                name="Local task journal",
                capabilities=list(TASK_CAPABILITIES),
                start_handler=lambda: None,
                health_handler=lambda: True,
            )
        )
        plane.providers.start(TASK_PROVIDER)
        plane.providers.check_health(TASK_PROVIDER)

    for definition in lifecycle.definitions():
        plane.capabilities.register(definition, replace=replace)

    lifecycle.bind(plane.broker)
    plane.tasks = lifecycle


def maybe_register_tasks(plane, path=None):
    """
    Register the journal only when FREYA_TASKS=1. Otherwise the
    control plane is unchanged.
    """
    if not tasks_enabled():
        return None

    lifecycle = TaskLifecycle(
        plane.store.path.parent / TASK_DATABASE_NAME if path is None else path
    )
    register_task_lifecycle(plane, lifecycle)
    return lifecycle
