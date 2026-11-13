import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.usage import SCOPE_PERIOD
from freya.delegation.compose import AvailableInput, compose_brief, parse_plan
from freya.delegation.dispatch import DispatchRefused
from freya.delegation.material import KIND_REQUEST
from freya.delegation.service import RouteUnavailable, build_cloud_delegation
from freya.providers.model import claude_plan
from freya.providers.model.claude_plan import (
    ANTHROPIC_PLAN_PROVIDER,
    CLOUD_ANTHROPIC_PLAN_ASK,
)
from test_claude_plan_route import FAKE_PROGRAM, reply


NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
VENDOR = "anthropic"
AVAILABLE = [
    AvailableInput("request", KIND_REQUEST, "Add 14, 9 and 23 and give the total."),
]


class Setup:
    def __init__(self, tmp_path):
        script = tmp_path / "fake_claude.py"
        script.write_text(FAKE_PROGRAM, encoding="utf-8")
        self.folder = tmp_path
        self.program = [sys.executable, str(script)]
        self.behave(output=reply("46"))
        claude_plan._sign_in_checked.clear()
        self.plane = build_local_control_plane(tmp_path / "control.db", None)
        self.cloud = build_cloud_delegation(
            self.plane, lambda: [], tmp_path / "data", self.program,
        )

    def behave(self, **behaviour):
        (self.folder / "behaviour.json").write_text(
            json.dumps(behaviour), encoding="utf-8",
        )

    def ran(self):
        return (self.folder / "seen.json").exists()

    def open_everything(self):
        """What the owner would do, by hand, to let the route be used."""
        self.cloud.settings.set_provider(VENDOR, True)
        self.plane.policy.add_rule(
            "local-owner", CLOUD_ANTHROPIC_PLAN_ASK, "allow", "test",
        )
        self.cloud.ledger.set_limit(
            ANTHROPIC_PLAN_PROVIDER, "week", SCOPE_PERIOD, "tokens", 500_000,
            period_seconds=7 * 86400, resets_at=NOW + timedelta(days=7),
            hard=True, now=NOW,
        )

    def brief(self):
        return compose_brief(
            parse_plan({"kind": "reasoning"}, AVAILABLE), AVAILABLE, VENDOR,
            self.cloud.settings, lambda messages: "Add the numbers.",
        )

    def send(self):
        brief = self.brief()
        authorization = self.cloud.dispatcher.authorize(
            brief, ANTHROPIC_PLAN_PROVIDER, "task-1", now=NOW,
        )
        return self.cloud.dispatcher.send(authorization, brief.text)

    def audit(self):
        return [
            dict(row) for row in self.plane.store.execute(
                "SELECT * FROM audit_event WHERE capability = ? ORDER BY event_id",
                (CLOUD_ANTHROPIC_PLAN_ASK,),
            )
        ]

    def close(self):
        self.cloud.close()
        self.plane.close()


@pytest.fixture
def s(tmp_path, fake_ollama):
    setup = Setup(tmp_path)
    yield setup
    setup.close()


def test_assembling_it_allows_nothing_and_creates_no_rule(s):
    assert s.cloud.settings.enabled_providers() == []
    assert s.plane.capabilities.get(CLOUD_ANTHROPIC_PLAN_ASK) is not None
    assert not s.plane.policy.decide("local-owner", CLOUD_ANTHROPIC_PLAN_ASK).allow
    assert s.cloud.ledger.status(ANTHROPIC_PLAN_PROVIDER, now=NOW) == []
    assert not s.brief().ready
    assert not s.ran()


def test_each_of_the_owners_three_locks_stops_a_brief_by_itself(s, tmp_path):
    # Only the provider's switch is open.
    s.cloud.settings.set_provider(VENDOR, True)
    with pytest.raises(DispatchRefused):       # no usage limit recorded
        s.send()

    # The switch and a limit, but no policy rule.
    s.cloud.ledger.set_limit(
        ANTHROPIC_PLAN_PROVIDER, "week", SCOPE_PERIOD, "tokens", 500_000,
        period_seconds=7 * 86400, resets_at=NOW + timedelta(days=7),
        hard=True, now=NOW,
    )
    with pytest.raises(RouteUnavailable) as denied:
        s.send()

    assert denied.value.sent is False
    assert not s.ran()
    assert s.cloud.ledger.status(ANTHROPIC_PLAN_PROVIDER, now=NOW)[0].used == 0
    assert s.cloud.sent.entries() == []

    # A rule and a limit, but the switch turned off again.
    s.plane.policy.add_rule(
        "local-owner", CLOUD_ANTHROPIC_PLAN_ASK, "allow", "test",
    )
    s.cloud.settings.set_provider(VENDOR, False)
    assert not s.brief().ready
    assert not s.ran()


def test_with_all_three_open_a_brief_goes_through_the_broker(s):
    s.open_everything()

    answer = s.send()

    assert answer.text == "46"
    assert s.cloud.ledger.status(ANTHROPIC_PLAN_PROVIDER, now=NOW)[0].used == 49
    sent = s.cloud.sent.entries()
    assert [(e["provider"], e["outcome"]) for e in sent] == [(VENDOR, "answered")]
    assert "Add 14, 9 and 23" in sent[0]["text"]

    rows = s.audit()
    assert [row["kind"] for row in rows] == ["dispatch", "outcome"]
    assert rows[1]["outcome"] == "succeeded"
    # The audit log shows that a brief went, never what it said.
    assert "Add 14" not in json.dumps(rows)


def test_the_capability_cannot_be_called_around_the_dispatcher(s):
    s.open_everything()

    direct = s.plane.broker.invoke(
        CLOUD_ANTHROPIC_PLAN_ASK, "A brief nobody checked.",
    )

    assert not direct.success
    assert not s.ran()
    assert s.cloud.sent.entries() == []


def test_a_failure_after_sending_is_counted_and_recorded(s):
    s.open_everything()
    s.behave(
        output=json.dumps({"is_error": True, "result": "echo of the brief"}),
        exit=1,
    )

    with pytest.raises(RouteUnavailable) as failed:
        s.send()

    assert failed.value.sent is True
    assert "echo of the brief" not in str(failed.value)
    assert "echo of the brief" not in json.dumps(s.audit())
    assert s.cloud.ledger.status(ANTHROPIC_PLAN_PROVIDER, now=NOW)[0].used > 4000
    assert [e["outcome"] for e in s.cloud.sent.entries()] == ["outcome unknown"]


def test_a_failure_before_sending_uses_nothing(s):
    s.open_everything()
    s.behave(output=reply("46"), sign_in={"loggedIn": False})

    with pytest.raises(RouteUnavailable) as failed:
        s.send()

    assert failed.value.sent is False
    assert not s.ran()
    assert s.cloud.ledger.status(ANTHROPIC_PLAN_PROVIDER, now=NOW)[0].used == 0
    assert s.cloud.sent.entries() == []


def test_the_model_is_not_offered_the_route_as_a_tool(s):
    s.open_everything()

    names = [
        tool["function"]["name"] for tool in s.plane.model_tools.definitions()
    ] if hasattr(s.plane.model_tools, "definitions") else []

    assert all("cloud" not in name for name in names)
    assert not s.plane.capabilities.get(CLOUD_ANTHROPIC_PLAN_ASK).metadata.get("model_tool")
