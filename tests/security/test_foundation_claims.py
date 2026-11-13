"""
Explicit tests for the local security claims Freya already makes.

Section 12 of the Foundation (distributed nodes) is recorded in
docs/security/foundation-section-12.md. Most of it is not proven here.
What this file proves is deny-by-default, an audit entry for every
brokered call, a hash chain that shows a rewrite, the gateway's
one-time key and cookie, and a loopback bind.
"""

import sqlite3
from pathlib import Path

from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import (
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
)
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.events import EventBus
from freya.gateway.server import LOOPBACK, SESSION_COOKIE, Gateway
from freya.image.gallery import ImageGallery
from freya.window import WindowActions


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "freya"

# Schedulers section 12 says a single node must not require.
SCHEDULERS = ("ray", "nats", "nomad", "k3s", "kubernetes")


def _plane(tmp_path, rules=()):
    store = ControlStore(tmp_path / "control.db")
    registry = CapabilityRegistry()
    called = []

    def handler(*args, **kwargs):
        called.append((args, kwargs))
        return {"ok": True}

    registry.register(
        CapabilityDefinition(
            name="system.info",
            description="Reads a made-up fact for this test.",
            provider="test-provider",
            handler=handler,
        )
    )
    policy = SqlitePolicyEngine(store)

    for capability in rules:
        policy.add_rule("local-owner", capability, "allow", "Test grant.")

    audit = SqliteAuditLog(store, checkpoint_interval=0)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit)
    return store, broker, called, audit


def _events(store):
    return [
        dict(row)
        for row in store.execute(
            "SELECT * FROM audit_event ORDER BY event_id"
        ).fetchall()
    ]


def test_a_registered_capability_is_denied_without_a_rule(tmp_path):
    store, broker, called, _audit = _plane(tmp_path)

    result = broker.invoke("system.info")

    assert result.success is False
    assert result.outcome == "denied"
    assert called == []
    assert "No policy rule allows" in result.error
    store.close()


def test_every_brokered_call_has_an_audit_entry(tmp_path):
    store, broker, called, _audit = _plane(tmp_path, rules=("system.info",))

    denied = broker.invoke("missing.tool")
    allowed = broker.invoke("system.info", "detail")

    rows = _events(store)
    by_operation = {row["operation_id"] for row in rows}

    assert denied.operation_id in by_operation
    assert allowed.operation_id in by_operation
    assert any(
        row["operation_id"] == denied.operation_id and row["outcome"] == "denied"
        for row in rows
    )
    assert any(
        row["operation_id"] == allowed.operation_id and row["kind"] == "dispatch"
        for row in rows
    )
    assert any(
        row["operation_id"] == allowed.operation_id and row["outcome"] == "succeeded"
        for row in rows
    )
    assert called
    assert len(rows) >= 3
    store.close()


def test_a_rewritten_audit_row_breaks_the_chain(tmp_path):
    store, broker, _called, audit = _plane(tmp_path, rules=("system.info",))
    broker.invoke("system.info")
    assert audit.verify().ok is True

    path = store.path
    store.close()

    connection = sqlite3.connect(path)
    try:
        try:
            connection.execute(
                "UPDATE audit_event SET decision_reason = 'changed' "
                "WHERE event_id = 1"
            )
        except sqlite3.DatabaseError as exc:
            assert "append-only" in str(exc)
        else:
            raise AssertionError("an ordinary update of the audit log was accepted")

        connection.execute("DROP TRIGGER audit_event_no_update")
        connection.execute(
            "UPDATE audit_event SET decision_reason = 'changed' "
            "WHERE event_id = 1"
        )
        connection.commit()
    finally:
        connection.close()

    reopened = ControlStore(path)
    checked = SqliteAuditLog(reopened).verify()
    reopened.close()

    assert checked.ok is False
    assert any("altered" in problem for problem in checked.problems)


def _actions():
    return WindowActions(
        say=lambda text: None,
        generate_image=lambda prompt: None,
        set_muted=lambda muted: None,
        interrupt=lambda: None,
        status=lambda: {},
        set_volume=lambda value: None,
        run_command=lambda text: None,
        console_history=lambda: [],
    )


def test_window_key_is_single_use_and_a_cookie_is_required(tmp_path):
    gateway = Gateway(
        EventBus(),
        ImageGallery(tmp_path / "images"),
        _actions(),
        port=0,
    )
    gateway.start()

    try:
        path = gateway.new_window_url().split(f":{gateway.port}", 1)[1]
        import http.client

        def get(target, cookie=None):
            connection = http.client.HTTPConnection(LOOPBACK, gateway.port, timeout=5)
            headers = {"Cookie": cookie} if cookie else {}
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            body = response.read()
            cookie_out = response.getheader("Set-Cookie")
            status = response.status
            connection.close()
            return status, body, cookie_out

        bare, _body, _cookie = get("/")
        first, _body, cookie = get(path)
        second, body, _cookie = get(path)
        page, page_body, _cookie = get("/", cookie=cookie.split(";", 1)[0])
        forged, _body, _cookie = get("/", cookie=f"{SESSION_COOKIE}=invented")

        assert bare == 403
        assert first == 302
        assert "HttpOnly" in cookie
        assert second == 403
        assert b"already been used" in body
        assert page == 200
        assert b"<" in page_body
        assert forged == 403
    finally:
        gateway.stop()


def test_the_gateway_binds_loopback_only(tmp_path):
    assert LOOPBACK == "127.0.0.1"

    gateway = Gateway(
        EventBus(),
        ImageGallery(tmp_path / "images"),
        _actions(),
        port=0,
    )
    listener = gateway._bind()

    try:
        address, port = listener.getsockname()[:2]
        assert address == "127.0.0.1"
        assert port != 0
    finally:
        listener.close()


def test_single_node_tree_does_not_import_a_scheduler():
    offenders = []

    for path in SOURCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")

        for name in SCHEDULERS:
            if f"import {name}" in text or f"from {name}" in text:
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")

    assert offenders == []
