"""
Audit failure handling: which audit writes must stop an action, and how
records that cannot be written after the fact are kept rather than lost.
"""

import logging
import sqlite3
import threading

import pytest

from freya.core.audit import AuditUnavailable, SqliteAuditLog
from freya.core.capabilities import (
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityStatus,
)
from freya.core.capabilities import broker as broker_module
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine


class FlakyAuditLog(SqliteAuditLog):
    """A real audit log whose writes can be made to fail on demand."""

    def __init__(self, store, **kwargs):
        super().__init__(store, checkpoint_interval=0, **kwargs)
        self.failing_kinds = set()

    def fail(self, *kinds):
        self.failing_kinds = set(kinds or ("decision", "dispatch", "outcome"))

    def recover(self):
        self.failing_kinds = set()

    def record(self, **fields):
        if fields["kind"] in self.failing_kinds:
            raise AuditUnavailable("storage is unavailable")
        return super().record(**fields)


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    yield store
    store.close()


@pytest.fixture
def calls():
    return []


@pytest.fixture
def registry(calls):
    registry = CapabilityRegistry()

    def read_time():
        calls.append("time")
        return "12:00"

    def stream_words():
        calls.append("stream")
        yield "one"
        yield "two"

    for name, handler in [
        ("system.time", read_time),
        ("system.stream", stream_words),
        ("system.secret", read_time),
    ]:
        registry.register(
            CapabilityDefinition(
                name=name,
                description="Test capability.",
                provider="test-provider",
                handler=handler,
            )
        )

    registry.register(
        CapabilityDefinition(
            name="system.offline",
            description="Test capability.",
            provider="test-provider",
            handler=read_time,
            status=CapabilityStatus.UNAVAILABLE,
        )
    )
    return registry


@pytest.fixture
def policy(store):
    policy = SqlitePolicyEngine(store)
    for name in ("system.time", "system.stream", "system.offline"):
        policy.add_rule("local-owner", name, "allow", "Test allow.")
    # system.secret has no rule, so it is denied by default.
    return policy


@pytest.fixture
def audit(store):
    audit = FlakyAuditLog(store)
    assert audit.start().verification.ok
    return audit


@pytest.fixture
def broker(registry, policy, audit):
    return CapabilityBroker(registry, policy, audit)


def events(store):
    return [
        dict(row)
        for row in store.execute(
            "SELECT * FROM audit_event ORDER BY event_id"
        ).fetchall()
    ]


# --- Required before acting: the dispatch record -------------------------


def test_unavailable_storage_stops_execution(registry, policy, store, calls):
    audit = SqliteAuditLog(store, checkpoint_interval=0)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit)
    store.close()

    result = broker.invoke("system.time")

    # Policy shares the store, so it is the first to refuse; the
    # denial itself cannot be recorded either, and that is reported.
    assert result.outcome == "denied"
    assert result.audit_recorded is False
    assert broker.pending_audit_records == 1
    assert calls == []


def test_dispatch_write_failure_stops_execution(broker, audit, store, calls):
    audit.fail("dispatch")

    result = broker.invoke("system.time")

    assert result.outcome == "denied"
    assert calls == []
    assert events(store) == []


# --- Required after the fact: denials, unavailability, outcomes ----------


def test_denial_that_cannot_be_recorded_is_reported_not_swallowed(
    broker, audit, store, calls, caplog
):
    audit.fail("decision")

    with caplog.at_level(logging.ERROR, logger=broker_module.__name__):
        result = broker.invoke("system.secret")

    assert result.outcome == "denied"
    assert result.audit_recorded is False
    assert calls == []
    assert broker.pending_audit_records == 1
    assert any(
        result.operation_id in message for message in caplog.messages
    )


def test_unrecorded_denial_blocks_dispatch_until_written(
    broker, audit, store, calls
):
    audit.fail("decision")
    denied = broker.invoke("system.secret")

    # Storage is still refusing the queued denial, so nothing may run,
    # even though this capability is allowed.
    blocked = broker.invoke("system.time")

    assert blocked.outcome == "denied"
    assert "earlier audit records" in blocked.error
    assert calls == []

    audit.recover()
    allowed = broker.invoke("system.time")

    rows = events(store)
    assert allowed.outcome == "succeeded"
    assert calls == ["time"]
    assert broker.pending_audit_records == 0
    # The trail keeps the order in which things were decided.
    assert [(r["operation_id"], r["kind"]) for r in rows] == [
        (denied.operation_id, "decision"),
        (allowed.operation_id, "dispatch"),
        (allowed.operation_id, "outcome"),
    ]
    assert rows[0]["outcome"] == "denied"
    assert audit.verify().ok


def test_unavailable_decision_that_cannot_be_recorded_is_queued(
    broker, audit, calls
):
    audit.fail("decision")

    result = broker.invoke("system.offline")

    assert result.outcome == "unavailable"
    assert result.audit_recorded is False
    assert broker.pending_audit_records == 1
    assert calls == []


def test_lost_outcome_is_kept_and_written_before_the_next_dispatch(
    broker, audit, store, calls
):
    audit.fail("outcome")

    first = broker.invoke("system.time")

    # The action ran and cannot be undone; the result says the outcome
    # is not yet on record.
    assert first.outcome == "succeeded"
    assert first.audit_recorded is False
    assert calls == ["time"]

    blocked = broker.invoke("system.time")
    assert blocked.outcome == "denied"
    assert calls == ["time"]

    audit.recover()
    second = broker.invoke("system.time")

    rows = events(store)
    assert second.outcome == "succeeded"
    assert [(r["operation_id"], r["kind"]) for r in rows] == [
        (first.operation_id, "dispatch"),
        (first.operation_id, "outcome"),
        (second.operation_id, "dispatch"),
        (second.operation_id, "outcome"),
    ]
    assert rows[1]["outcome"] == "succeeded"
    assert audit.verify().ok


def test_lost_stream_outcome_is_queued(broker, audit, store, calls):
    result = broker.invoke("system.stream")
    audit.fail("outcome")

    assert list(result.result) == ["one", "two"]
    assert broker.pending_audit_records == 1

    audit.recover()
    assert broker.invoke("system.time").outcome == "succeeded"
    kinds = [r["kind"] for r in events(store)]
    assert kinds == ["dispatch", "outcome", "dispatch", "outcome"]


def test_full_queue_counts_and_logs_lost_records(
    broker, audit, monkeypatch, caplog
):
    monkeypatch.setattr(broker_module, "PENDING_AUDIT_LIMIT", 2)
    audit.fail("decision")

    with caplog.at_level(logging.CRITICAL, logger=broker_module.__name__):
        for _ in range(3):
            broker.invoke("system.secret")

    assert broker.pending_audit_records == 2
    assert broker.lost_audit_records == 1
    assert any("lost" in message for message in caplog.messages)


def test_log_messages_do_not_contain_arguments(
    broker, audit, caplog
):
    audit.fail("decision")

    with caplog.at_level(logging.DEBUG):
        broker.invoke("system.secret", "hunter2", token="s3cret-token")

    assert "hunter2" not in caplog.text
    assert "s3cret-token" not in caplog.text


# --- Corrupted or unreadable audit chains --------------------------------


def test_tampered_chain_disables_every_capability(
    registry, policy, store, audit, calls
):
    CapabilityBroker(registry, policy, audit).invoke("system.time")
    store.execute("DROP TRIGGER audit_event_no_update")
    store.execute("UPDATE audit_event SET actor = 'forged' WHERE event_id = 1")

    restarted = SqliteAuditLog(store)
    startup = restarted.start()
    broker = CapabilityBroker(registry, policy, restarted)

    assert startup.verification.ok is False
    for name in ("system.time", "system.stream"):
        assert broker.invoke(name).outcome == "denied"
    assert calls == ["time"]


def test_unreadable_log_fails_closed_instead_of_crashing(
    store, monkeypatch
):
    def broken(*args, **kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(store, "execute", broken)
    log = SqliteAuditLog(store)

    startup = log.start()

    assert startup.verification.ok is False
    assert "could not be read" in startup.verification.problems[0]
    assert log.integrity_ok is False
    with pytest.raises(AuditUnavailable):
        log.record(
            operation_id="op",
            kind="dispatch",
            actor="a",
            client="c",
            domain="d",
            capability="x",
            decision="allow",
            decision_reason="r",
        )


def test_failed_reconciliation_fails_closed(store, audit, broker):
    audit.fail("outcome")
    broker.invoke("system.time")  # leaves a dispatch with no outcome

    restarted = FlakyAuditLog(store)
    restarted.fail("outcome")
    startup = restarted.start()

    assert startup.verification.ok is False
    assert restarted.integrity_ok is False


def test_checkpoint_failure_does_not_unrecord_a_committed_event(
    store, monkeypatch, caplog
):
    log = SqliteAuditLog(store, checkpoint_interval=1)
    log.start()

    def broken_checkpoint():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(log, "checkpoint", broken_checkpoint)

    with caplog.at_level(logging.WARNING):
        event_id = log.record(
            operation_id="op",
            kind="dispatch",
            actor="a",
            client="c",
            domain="d",
            capability="x",
            decision="allow",
            decision_reason="r",
        )

    assert event_id == 1
    assert len(events(store)) == 1
    assert "checkpoint" in caplog.text.lower()


def test_failed_commit_leaves_the_store_usable(store):
    # A deferred foreign key is checked at COMMIT, which makes the
    # commit itself fail deterministically.
    store.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    store.execute(
        "CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) "
        "DEFERRABLE INITIALLY DEFERRED)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as connection:
            connection.execute("INSERT INTO child VALUES (42)")

    with store.transaction() as connection:
        connection.execute("INSERT INTO parent VALUES (1)")

    assert store.execute("SELECT COUNT(*) FROM child").fetchone()[0] == 0
    assert store.execute("SELECT COUNT(*) FROM parent").fetchone()[0] == 1


# --- Concurrency ---------------------------------------------------------


def run_in_threads(count, target):
    start = threading.Barrier(count)
    errors = []

    def worker(index):
        try:
            start.wait(timeout=10)
            target(index)
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []


def test_concurrent_invocations_keep_one_valid_chain(broker, audit, store):
    results = []

    run_in_threads(
        8,
        lambda i: results.extend(
            broker.invoke("system.time") for _ in range(5)
        ),
    )

    rows = events(store)
    assert len(results) == 40
    assert all(r.outcome == "succeeded" for r in results)
    assert [r["event_id"] for r in rows] == list(range(1, 81))
    assert audit.verify().ok


def test_concurrent_denials_during_an_outage_are_all_kept(
    broker, audit, store, calls
):
    audit.fail("decision")

    run_in_threads(8, lambda i: broker.invoke("system.secret"))

    assert broker.pending_audit_records == 8
    assert events(store) == []

    audit.recover()
    assert broker.invoke("system.time").outcome == "succeeded"
    kinds = [r["kind"] for r in events(store)]
    assert kinds == ["decision"] * 8 + ["dispatch", "outcome"]
    assert audit.verify().ok


def test_two_connections_to_one_log_keep_one_valid_chain(tmp_path):
    # Two stores on the same file behave like two processes.
    path = tmp_path / "shared.db"
    stores = [ControlStore(path), ControlStore(path)]
    logs = [SqliteAuditLog(s, checkpoint_interval=0) for s in stores]
    for log in logs:
        log.start()

    def write(index):
        for n in range(10):
            logs[index % 2].record(
                operation_id=f"op-{index}-{n}",
                kind="dispatch",
                actor="a",
                client="c",
                domain="d",
                capability="x",
                decision="allow",
                decision_reason="r",
            )

    try:
        run_in_threads(4, write)
        verification = logs[0].verify()
        assert verification.ok, verification.problems
        assert verification.events_checked == 40
    finally:
        for s in stores:
            s.close()


def test_concurrent_policy_decisions_never_fail_or_mix_up_rows(policy):
    # Before rows were read under the store lock, a decision racing
    # another query on the shared connection could read no row or a
    # row of the wrong shape, and was turned into a spurious denial.
    failures = []

    def decide(index):
        name = "system.time" if index % 2 else "system.secret"
        for _ in range(1500):
            try:
                decision = policy.decide("local-owner", name)
            except Exception as exc:
                failures.append(repr(exc))
                continue
            if decision.allow != (name == "system.time"):
                failures.append(f"wrong decision for {name}")

    run_in_threads(4, decide)

    assert failures == []
