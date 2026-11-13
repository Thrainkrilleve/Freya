"""
The local task journal: every transition, cancellation, a restart,
and an audit entry for each brokered step.
"""

from pathlib import Path

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.capabilities.broker import LOCAL_OWNER
from freya.tasks.lifecycle import (
    TASK_CAPABILITIES,
    TaskError,
    TaskLifecycle,
    register_task_lifecycle,
)


def _grant(plane):
    for name in TASK_CAPABILITIES:
        plane.policy.add_rule(LOCAL_OWNER.actor, name, "allow", "Test grant.")


def _open(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db")
    life = TaskLifecycle(tmp_path / "freya_tasks.db")
    # FREYA_TASKS=1 already registered a journal beside the control
    # database. These tests use their own file, so the handlers move.
    register_task_lifecycle(
        plane,
        life,
        replace=plane.capabilities.get("task.create") is not None,
    )
    return plane, life


def _audit(plane, capability):
    return [
        dict(row)
        for row in plane.store.execute(
            """
            SELECT kind, decision, outcome
            FROM audit_event
            WHERE capability = ?
            ORDER BY event_id
            """,
            (capability,),
        ).fetchall()
    ]


def test_the_journal_is_off_and_code_run_stays_unregistered(tmp_path, monkeypatch, fake_ollama):
    monkeypatch.delenv("FREYA_TASKS", raising=False)
    plane = build_local_control_plane(tmp_path / "off.db")

    try:
        assert plane.broker.exists("task.create") is False
        assert plane.broker.exists("code.run") is False
        assert plane.broker.exists("decide") is False
    finally:
        plane.close()

    monkeypatch.setenv("FREYA_TASKS", "1")
    enabled = build_local_control_plane(tmp_path / "on.db")

    try:
        assert enabled.broker.exists("task.authorize") is True
        denied = enabled.broker.invoke("task.authorize", "missing")
        assert denied.outcome == "denied"
        assert enabled.broker.exists("code.run") is False
        assert "model_tool" not in (
            enabled.capabilities.get("task.verify").metadata
        )
    finally:
        enabled.close()


def test_a_task_is_denied_until_a_rule_allows_it(tmp_path, fake_ollama):
    plane, life = _open(tmp_path, fake_ollama)
    life.add_check("holds", lambda task_id: True)

    try:
        with pytest.raises(TaskError, match="Not authorized"):
            life.create("Paint the door", "holds")

        assert life._store.execute("SELECT COUNT(*) FROM task").fetchone()[0] == 0
        assert _audit(plane, "task.create")
        assert _audit(plane, "task.create")[0]["outcome"] == "denied"
    finally:
        plane.close()


def test_every_transition_is_audited_and_the_check_runs_first(tmp_path, fake_ollama):
    plane, life = _open(tmp_path, fake_ollama)
    seen = []
    life.add_check("holds", lambda task_id: seen.append(task_id) or True)
    _grant(plane)

    try:
        task_id = life.create("Paint the door", "holds")
        assert life.authorize(task_id)["state"] == "authorized"
        assert life.start(task_id)["state"] == "running"
        done = life.verify(task_id)

        assert seen == [task_id]
        assert done["state"] == "done"
        assert [step["to_state"] for step in life.events(task_id)] == [
            "created", "authorized", "running", "verifying", "done",
        ]

        for name in ("task.create", "task.authorize", "task.start", "task.verify", "task.finish"):
            rows = _audit(plane, name)
            assert rows, name
            assert any(row["decision"] == "allow" and row["outcome"] == "succeeded" for row in rows)
    finally:
        plane.close()


def test_a_failed_check_finishes_as_failed_without_a_model(tmp_path, fake_ollama):
    plane, life = _open(tmp_path, fake_ollama)
    life.add_check("holds", lambda task_id: False)
    _grant(plane)

    try:
        task_id = life.create("Paint the door", "holds")
        life.authorize(task_id)
        life.start(task_id)
        failed = life.verify(task_id)

        assert failed["state"] == "failed"
        assert failed["detail"] == "check-returned-false"
        assert _audit(plane, "task.fail")
    finally:
        plane.close()


def test_a_check_that_raises_is_a_failed_task(tmp_path, fake_ollama):
    plane, life = _open(tmp_path, fake_ollama)

    def blows(task_id):
        raise RuntimeError("secret title should not be copied")

    life.add_check("holds", blows)
    _grant(plane)

    try:
        task_id = life.create("Paint the door", "holds")
        life.authorize(task_id)
        life.start(task_id)
        failed = life.verify(task_id)

        assert failed["state"] == "failed"
        assert failed["detail"] == "check-failed"
        text = " ".join(
            (row["decision_reason"] or "") + (row["error_summary"] or "")
            for row in plane.store.execute("SELECT decision_reason, error_summary FROM audit_event").fetchall()
        )
        assert "secret title" not in text
        assert "Paint the door" not in text
    finally:
        plane.close()


@pytest.mark.parametrize("stop_at", ["created", "authorized", "running", "verifying"])
def test_cancel_from_every_open_state(tmp_path, fake_ollama, stop_at):
    plane, life = _open(tmp_path, fake_ollama)
    life.add_check("holds", lambda task_id: True)
    _grant(plane)

    try:
        task_id = life.create("Paint the door", "holds")

        if stop_at != "created":
            life.authorize(task_id)

        if stop_at in ("running", "verifying"):
            life.start(task_id)

        if stop_at == "verifying":
            life._store.execute(
                "UPDATE task SET state = 'verifying' WHERE task_id = ?",
                (task_id,),
            )

        assert life.cancel(task_id)["state"] == "cancelled"

        with pytest.raises(TaskError, match="not available"):
            life.cancel(task_id)
    finally:
        plane.close()


def test_illegal_transitions_are_refused(tmp_path, fake_ollama):
    plane, life = _open(tmp_path, fake_ollama)
    life.add_check("holds", lambda task_id: True)
    _grant(plane)

    try:
        task_id = life.create("Paint the door", "holds")

        with pytest.raises(TaskError, match="not available"):
            life.start(task_id)

        with pytest.raises(TaskError, match="not available"):
            life.verify(task_id)

        with pytest.raises(TaskError, match="not available"):
            life.resume(task_id)

        life.authorize(task_id)

        with pytest.raises(TaskError, match="not available"):
            life.authorize(task_id)

        life.start(task_id)
        life.verify(task_id)

        with pytest.raises(TaskError, match="not available"):
            life.cancel(task_id)
    finally:
        plane.close()


def test_an_interrupted_task_resumes_after_a_restart(tmp_path, fake_ollama):
    plane, life = _open(tmp_path, fake_ollama)
    life.add_check("holds", lambda task_id: True)
    _grant(plane)
    task_id = life.create("Paint the door", "holds")
    life.authorize(task_id)
    life.start(task_id)
    path = life._store.path
    life.close()

    restored = TaskLifecycle(path)
    register_task_lifecycle(plane, restored, replace=True)
    restored.add_check("holds", lambda task_id: True)

    try:
        found = restored.get(task_id)
        assert found["state"] == "running"
        assert found["interrupted"] is True

        with pytest.raises(TaskError, match="not available"):
            restored.verify(task_id)

        resumed = restored.resume(task_id)
        assert resumed["state"] == "running"
        assert resumed["interrupted"] is False
        assert restored.verify(task_id)["state"] == "done"
        assert any(row["outcome"] == "succeeded" for row in _audit(plane, "task.resume"))
    finally:
        plane.close()


def test_a_restart_during_verification_runs_the_check_again(tmp_path, fake_ollama):
    plane, life = _open(tmp_path, fake_ollama)
    calls = []
    life.add_check("holds", lambda task_id: calls.append("first") or True)
    _grant(plane)
    task_id = life.create("Paint the door", "holds")
    life.authorize(task_id)
    life.start(task_id)
    life._store.execute(
        """
        UPDATE task
        SET state = 'verifying', run_token = 'dead', interrupted = 0
        WHERE task_id = ?
        """,
        (task_id,),
    )
    path = life._store.path
    life.close()

    restored = TaskLifecycle(path)

    def second(task_id):
        calls.append("second")
        return True

    register_task_lifecycle(plane, restored, replace=True)
    restored.add_check("holds", second)

    try:
        assert restored.get(task_id)["state"] == "verifying"
        assert restored.get(task_id)["interrupted"] is True
        assert restored.resume(task_id)["state"] == "done"
        assert calls == ["second"]
    finally:
        plane.close()


def test_the_package_imports_neither_a_model_nor_federation():
    source = Path(__file__).resolve().parents[1] / "src" / "freya" / "tasks"
    text = "\n".join(path.read_text(encoding="utf-8") for path in source.glob("*.py"))

    assert "freya.federation" not in text
    assert "freya.core.llm" not in text
    assert "import ollama" not in text
