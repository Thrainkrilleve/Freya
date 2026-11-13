import pytest

from freya.control_plane import build_local_control_plane
from freya.core.audit import AuditUnavailable, SqliteAuditLog
from freya.core.capabilities import (
    LOCAL_OWNER,
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
    CapabilityStatus,
    Principal,
)
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine


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

    def read_time(*args, **kwargs):
        calls.append((args, kwargs))
        return "12:00"

    def explode():
        calls.append("explode")
        raise RuntimeError("handler exploded")

    for name, handler, extra in [
        ("system.time", read_time, {"aliases": ["clock"]}),
        ("system.broken", explode, {}),
        ("files.delete", explode, {"destructive": True}),
    ]:
        registry.register(
            CapabilityDefinition(
                name=name,
                description="Test capability.",
                provider="test-provider",
                handler=handler,
                **extra,
            )
        )

    return registry


@pytest.fixture
def policy(store):
    policy = SqlitePolicyEngine(store)
    for name in ("system.time", "system.broken", "files.delete"):
        policy.add_rule("local-owner", name, "allow", "Test allow.")
    return policy


@pytest.fixture
def audit(store):
    audit = SqliteAuditLog(store, checkpoint_interval=0)
    audit.start()
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


def test_allowed_invocation_is_recorded_before_and_after(broker, store, calls):
    result = broker.invoke("Clock", "utc", fmt="short")

    rows = events(store)

    assert result.success is True
    assert result.outcome == "succeeded"
    assert result.capability_name == "system.time"
    assert result.provider == "test-provider"
    assert result.result == "12:00"
    assert calls == [(("utc",), {"fmt": "short"})]

    assert [row["kind"] for row in rows] == ["dispatch", "outcome"]
    assert {row["operation_id"] for row in rows} == {result.operation_id}
    for row in rows:
        assert row["actor"] == "local-owner"
        assert row["client"] == "cli"
        assert row["domain"] == "local"
        assert row["capability"] == "system.time"
        assert row["provider"] == "test-provider"
        assert row["decision"] == "allow"
        assert row["decision_reason"] == "Test allow."
        assert row["policy_revision"] == 3
        assert row["grant_epoch"] == 1
        assert len(row["arguments_digest"]) == 64
    assert rows[0]["outcome"] is None
    assert rows[1]["outcome"] == "succeeded"


def test_arguments_are_stored_only_as_a_digest(broker, store):
    broker.invoke("system.time", "a-very-secret-argument", token="hunter2")
    broker.invoke("system.time", "something-else")

    rows = events(store)
    stored = " ".join(str(value) for row in rows for value in row.values())

    assert "a-very-secret-argument" not in stored
    assert "hunter2" not in stored
    assert rows[0]["arguments_digest"] != rows[2]["arguments_digest"]


def test_denied_by_default_and_handler_not_called(
    registry, store, audit, calls
):
    broker = CapabilityBroker(registry, SqlitePolicyEngine(store), audit)

    result = broker.invoke("system.time")

    rows = events(store)

    assert result.success is False
    assert result.outcome == "denied"
    assert "No policy rule allows" in result.error
    assert calls == []
    assert len(rows) == 1
    assert rows[0]["kind"] == "decision"
    assert rows[0]["decision"] == "deny"
    assert rows[0]["outcome"] == "denied"
    assert rows[0]["policy_revision"] == 0


def test_identity_comes_from_the_broker_not_the_caller(
    registry, policy, audit, store, calls
):
    guest = Principal(actor="guest", client="web", domain="local")
    broker = CapabilityBroker(registry, policy, audit, guest)

    # Extra keyword arguments are handler arguments, never identity.
    result = broker.invoke("system.time", actor="local-owner")

    rows = events(store)

    assert result.outcome == "denied"
    assert calls == []
    assert rows[0]["actor"] == "guest"
    assert rows[0]["client"] == "web"


def test_unregistered_capability_is_denied_and_recorded(broker, store):
    result = broker.invoke(" Files.Format ")

    rows = events(store)

    assert result.outcome == "denied"
    assert result.capability_name == "files.format"
    assert "not registered" in result.error
    assert rows[0]["decision"] == "deny"
    assert rows[0]["provider"] is None


def test_policy_failure_denies(registry, audit, store, calls):
    class BrokenPolicy:
        def decide(self, subject, action):
            raise RuntimeError("policy store is offline")

    broker = CapabilityBroker(registry, BrokenPolicy(), audit)

    result = broker.invoke("system.time")

    assert result.outcome == "denied"
    assert "Policy state is unavailable" in result.error
    assert calls == []
    assert events(store)[0]["decision"] == "deny"


def test_unavailable_capability_is_not_executed(
    broker, registry, store, calls
):
    registry.set_status("system.time", CapabilityStatus.DISABLED)

    result = broker.invoke("system.time")

    rows = events(store)

    assert result.success is False
    assert result.outcome == "unavailable"
    assert calls == []
    assert [row["kind"] for row in rows] == ["decision"]
    assert rows[0]["decision"] == "allow"
    assert rows[0]["outcome"] == "unavailable"


def test_nothing_runs_when_the_audit_record_cannot_be_written(
    registry, policy, calls
):
    class FullAuditLog:
        def record(self, **fields):
            raise AuditUnavailable("disk is full")

    broker = CapabilityBroker(registry, policy, FullAuditLog())

    result = broker.invoke("system.time")

    assert result.success is False
    assert result.outcome == "denied"
    assert "audit log" in result.error
    assert calls == []


def test_nothing_runs_when_the_audit_log_failed_verification(
    registry, policy, store, calls
):
    audit = SqliteAuditLog(store)  # never started, so never verified
    broker = CapabilityBroker(registry, policy, audit)

    result = broker.invoke("system.time")

    assert result.outcome == "denied"
    assert calls == []
    assert events(store) == []


def test_handler_failure_is_recorded_as_failed_known(broker, store, calls):
    result = broker.invoke("system.broken")

    rows = events(store)

    assert result.success is False
    assert result.outcome == "failed-known"
    assert result.error == "handler exploded"
    assert calls == ["explode"]
    assert rows[1]["outcome"] == "failed-known"
    assert rows[1]["error_summary"] == "handler exploded"


def test_failed_destructive_action_has_unknown_outcome(broker, store):
    result = broker.invoke("files.delete")

    assert result.outcome == "unknown-outcome"
    assert events(store)[1]["outcome"] == "unknown-outcome"


def test_lost_outcome_record_is_reconciled_on_next_start(
    registry, policy, store, calls
):
    class LosesOutcome(SqliteAuditLog):
        def record(self, **fields):
            if fields["kind"] == "outcome":
                raise AuditUnavailable("crashed before the outcome was written")
            return super().record(**fields)

    audit = LosesOutcome(store, checkpoint_interval=0)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit)

    result = broker.invoke("system.time")

    assert result.success is True
    assert [row["kind"] for row in events(store)] == ["dispatch"]

    startup = SqliteAuditLog(store).start()
    rows = events(store)

    assert startup.reconciled == 1
    assert rows[1]["kind"] == "outcome"
    assert rows[1]["outcome"] == "unknown-outcome"
    assert rows[1]["operation_id"] == result.operation_id


def test_revoking_a_rule_takes_effect_on_the_next_invocation(
    broker, policy, store, calls
):
    assert broker.invoke("system.time").success is True

    rule = next(
        r for r in policy.list_rules() if r["capability"] == "system.time"
    )
    policy.remove_rule(rule["rule_id"])

    result = broker.invoke("system.time")

    assert result.outcome == "denied"
    assert len(calls) == 1
    assert events(store)[-1]["grant_epoch"] == 2


def test_audit_log_verifies_after_mixed_activity(broker, audit):
    broker.invoke("system.time")
    broker.invoke("system.broken")
    broker.invoke("missing")

    assert audit.verify().ok is True


def test_streamed_result_is_audited_when_the_stream_ends(
    registry, policy, audit, store
):
    def count():
        yield 1
        yield 2

    registry.register(
        CapabilityDefinition(
            name="stream.count",
            description="Test capability.",
            provider="test-provider",
            handler=count,
        )
    )
    policy.add_rule("local-owner", "stream.count", "allow", "Test allow.")
    broker = CapabilityBroker(registry, policy, audit)

    result = broker.invoke("stream.count")

    assert result.success is True
    assert result.outcome == "streaming"
    assert [row["kind"] for row in events(store)] == ["dispatch"]

    assert list(result.result) == [1, 2]

    rows = events(store)
    assert [row["kind"] for row in rows] == ["dispatch", "outcome"]
    assert rows[1]["outcome"] == "succeeded"
    assert rows[1]["operation_id"] == result.operation_id


def test_stream_that_fails_or_is_abandoned_is_recorded(
    registry, policy, audit, store
):
    def breaks():
        yield 1
        raise RuntimeError("stream exploded")

    registry.register(
        CapabilityDefinition(
            name="stream.breaks",
            description="Test capability.",
            provider="test-provider",
            handler=breaks,
        )
    )
    policy.add_rule("local-owner", "stream.breaks", "allow", "Test allow.")
    broker = CapabilityBroker(registry, policy, audit)

    failing = broker.invoke("stream.breaks").result
    assert next(failing) == 1
    with pytest.raises(RuntimeError):
        next(failing)

    abandoned = broker.invoke("stream.breaks").result
    assert next(abandoned) == 1
    abandoned.close()

    outcomes = [
        (row["outcome"], row["error_summary"])
        for row in events(store)
        if row["kind"] == "outcome"
    ]
    assert outcomes == [
        ("failed-known", "stream exploded"),
        ("failed-known", "Stream closed before completion."),
    ]


def test_local_control_plane_runs_system_tools(tmp_path, fake_ollama):
    database = tmp_path / "control.db"
    checkpoints = tmp_path / "offhost" / "checkpoints.jsonl"

    plane = build_local_control_plane(database, checkpoints)

    assert plane.startup_messages() == []
    assert plane.broker.exists("gpu")

    result = plane.broker.invoke("system.info")

    assert result.success is True
    assert result.provider == "system-tools-local"
    assert result.result["python_version"]
    assert {r["capability"] for r in plane.policy.list_rules()} == {
        "system.info",
        "system.disk",
        "system.gpu",
        "system.cpu",
        "system.memory",
        "system.hardware",
        "system.sensors",
        "llm.chat",
        "llm.chat_stream",
        "llm.embed",
        "security.self_assessment",
    }
    assert all(
        r["subject"] == LOCAL_OWNER.actor for r in plane.policy.list_rules()
    )

    plane.close()

    reopened = build_local_control_plane(database, checkpoints)

    assert reopened.audit_startup.verification.ok is True
    assert reopened.audit_startup.verification.events_checked == 2
    assert reopened.policy.state() == (3, 1)
    assert checkpoints.exists()
    reopened.close()


def test_local_control_plane_disables_tools_when_log_is_tampered(
    tmp_path, fake_ollama
):
    database = tmp_path / "control.db"

    plane = build_local_control_plane(database, None)
    plane.broker.invoke("system.info")
    plane.close()

    store = ControlStore(database)
    store.execute("DROP TRIGGER audit_event_no_update")
    store.execute("UPDATE audit_event SET actor = 'forged' WHERE event_id = 1")
    store.close()

    reopened = build_local_control_plane(database, None)
    result = reopened.broker.invoke("system.info")

    assert reopened.audit_startup.verification.ok is False
    assert "AUDIT LOG FAILED VERIFICATION" in reopened.startup_messages()[0]
    assert result.success is False
    assert result.outcome == "denied"
    reopened.close()
