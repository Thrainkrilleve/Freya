import json
from freya.storage.sqlite import IntegrityError as StoreIntegrityError

import pytest

from freya.core.audit import (
    AuditUnavailable,
    FileCheckpointSink,
    SqliteAuditLog,
)
from freya.core.audit.log import GENESIS_HASH
from freya.core.persistence import ControlStore


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    yield store
    store.close()


@pytest.fixture
def sink(tmp_path):
    return FileCheckpointSink(tmp_path / "offhost" / "checkpoints.jsonl")


@pytest.fixture
def audit(store):
    log = SqliteAuditLog(store, checkpoint_interval=0)
    log.start()
    return log


def record(log, operation_id="op-1", kind="dispatch", **overrides):
    fields = {
        "operation_id": operation_id,
        "kind": kind,
        "actor": "local-owner",
        "client": "cli",
        "domain": "local",
        "capability": "system.info",
        "provider": "system-tools-local",
        "decision": "allow",
        "decision_reason": "test",
        "policy_revision": 1,
        "grant_epoch": 1,
    }
    fields.update(overrides)
    return log.record(**fields)


def tamper(store, sql, parameters=()):
    """Simulate an attacker who removes the append-only triggers."""
    store.execute("DROP TRIGGER IF EXISTS audit_event_no_update")
    store.execute("DROP TRIGGER IF EXISTS audit_event_no_delete")
    store.execute(sql, parameters)


def test_cannot_record_before_start(store):
    log = SqliteAuditLog(store)

    with pytest.raises(AuditUnavailable):
        record(log)


def test_events_are_numbered_and_chained(store, audit):
    first = record(audit, "op-1")
    second = record(audit, "op-1", kind="outcome", outcome="succeeded")

    rows = store.execute(
        "SELECT * FROM audit_event ORDER BY event_id"
    ).fetchall()

    assert (first, second) == (1, 2)
    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert rows[0]["recorded_at"].endswith("+00:00")
    assert audit.head() == {
        "last_event_id": 2,
        "row_hash": rows[1]["row_hash"],
    }
    assert audit.verify().ok is True
    assert audit.verify().events_checked == 2


def test_error_summary_is_truncated(store, audit):
    record(audit, kind="outcome", outcome="failed-known", error_summary="x" * 5000)

    row = store.execute("SELECT error_summary FROM audit_event").fetchone()

    assert len(row["error_summary"]) == 200


def test_rows_cannot_be_updated_or_deleted(store, audit):
    record(audit)

    with pytest.raises(StoreIntegrityError):
        store.execute("UPDATE audit_event SET actor = 'someone-else'")

    with pytest.raises(StoreIntegrityError):
        store.execute("DELETE FROM audit_event")

    assert audit.verify().ok is True


def test_verify_detects_an_edited_row(store, audit):
    record(audit, "op-1")
    record(audit, "op-2")
    record(audit, "op-3")

    tamper(
        store,
        "UPDATE audit_event SET decision_reason = 'forged' WHERE event_id = 2",
    )

    result = audit.verify()

    assert result.ok is False
    assert any("Event 2 has been altered" in p for p in result.problems)


def test_verify_detects_a_deleted_row(store, audit):
    record(audit, "op-1")
    record(audit, "op-2")
    record(audit, "op-3")

    tamper(store, "DELETE FROM audit_event WHERE event_id = 2")

    result = audit.verify()

    assert result.ok is False
    assert any("Event 2 is missing" in p for p in result.problems)
    assert any("does not link" in p for p in result.problems)


def test_verify_detects_truncation_through_a_checkpoint(store, audit):
    record(audit, "op-1")
    record(audit, "op-2")
    audit.checkpoint()

    # Without the checkpoint, removing the newest rows leaves a valid chain.
    tamper(store, "DELETE FROM audit_event WHERE event_id = 2")

    result = audit.verify()

    assert result.ok is False
    assert any("no longer in the log" in p for p in result.problems)


def test_external_checkpoint_detects_a_fully_rewritten_log(store, sink):
    audit = SqliteAuditLog(store, sink=sink, checkpoint_interval=0)
    audit.start()
    record(audit, "op-1")
    record(audit, "op-2")
    audit.checkpoint()

    # Rewrite history, then rebuild the chain and local checkpoints so the
    # database is self-consistent again.
    tamper(store, "DELETE FROM audit_event")
    store.execute("DELETE FROM audit_checkpoint")
    record(audit, "op-forged")
    record(audit, "op-forged-2")
    rebuilt = SqliteAuditLog(store, checkpoint_interval=0)

    assert rebuilt.verify().ok is True

    checked_against_sink = SqliteAuditLog(store, sink=sink)
    result = checked_against_sink.verify()

    assert result.ok is False
    assert any("External checkpoint" in p for p in result.problems)


def test_checkpoint_writes_to_sink_and_skips_when_unchanged(store, sink):
    audit = SqliteAuditLog(store, sink=sink, checkpoint_interval=0)
    audit.start()
    record(audit)

    first = audit.checkpoint()
    second = audit.checkpoint()

    lines = sink.path.read_text(encoding="utf-8").splitlines()
    latest = json.loads(lines[-1])

    assert first["sink_status"] == "written"
    assert second is None
    assert latest["last_event_id"] == 1
    assert latest["row_hash"] == audit.head()["row_hash"]
    assert set(latest) == {"created_at", "last_event_id", "row_hash"}
    assert sink.read_latest() == latest


def test_automatic_checkpoint_every_interval(store):
    audit = SqliteAuditLog(store, checkpoint_interval=3)
    audit.start()

    for index in range(7):
        record(audit, f"op-{index}")

    checkpointed = [
        row["last_event_id"]
        for row in store.execute(
            "SELECT last_event_id FROM audit_checkpoint ORDER BY checkpoint_id"
        ).fetchall()
    ]

    assert checkpointed == [0, 3, 6]


def test_unreachable_sink_is_a_warning_not_a_failure(store):
    class BrokenSink:
        name = "broken"

        def write(self, checkpoint):
            raise OSError("share is offline")

        def read_latest(self):
            raise OSError("share is offline")

    audit = SqliteAuditLog(store, sink=BrokenSink(), checkpoint_interval=0)
    startup = audit.start()
    record(audit)
    checkpoint = audit.checkpoint()
    result = audit.verify()

    assert startup.verification.ok is True
    assert checkpoint["sink_status"].startswith("failed: share is offline")
    assert result.ok is True
    assert any("could not be read" in w for w in result.warnings)
    assert any("could not be written" in w for w in result.warnings)


def test_start_reconciles_interrupted_operations(store, audit):
    record(audit, "finished")
    record(audit, "finished", kind="outcome", outcome="succeeded")
    record(audit, "interrupted", arguments_digest="abc")
    record(audit, "denied", kind="decision", decision="deny", outcome="denied")

    restarted = SqliteAuditLog(store, checkpoint_interval=0)
    startup = restarted.start()

    rows = store.execute(
        "SELECT * FROM audit_event WHERE operation_id = 'interrupted' "
        "ORDER BY event_id"
    ).fetchall()

    assert startup.reconciled == 1
    assert [row["kind"] for row in rows] == ["dispatch", "outcome"]
    assert rows[1]["outcome"] == "unknown-outcome"
    assert rows[1]["arguments_digest"] == "abc"
    assert restarted.verify().ok is True
    assert SqliteAuditLog(store).start().reconciled == 0


def test_start_refuses_a_tampered_log(store, audit):
    record(audit, "op-1")
    record(audit, "op-2")
    tamper(store, "UPDATE audit_event SET actor = 'forged' WHERE event_id = 1")

    restarted = SqliteAuditLog(store)
    startup = restarted.start()

    assert startup.verification.ok is False
    assert restarted.integrity_ok is False
    with pytest.raises(AuditUnavailable):
        record(restarted, "op-3")


def test_record_raises_when_store_is_unavailable(store, audit):
    store.close()

    with pytest.raises(AuditUnavailable):
        record(audit)
