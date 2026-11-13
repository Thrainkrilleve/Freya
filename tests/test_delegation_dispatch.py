from datetime import datetime, timedelta, timezone

import pytest

from freya.core.persistence import ControlStore
from freya.core.usage import SCOPE_PERIOD, USAGE_MIGRATIONS, UsageLedger
from freya.delegation.compose import (
    TASK_KINDS,
    AvailableInput,
    PlanRefused,
    compose_brief,
    parse_plan,
    writer_messages,
)
from freya.delegation.dispatch import (
    AUTHORIZATION_SECONDS,
    DispatchRefused,
    Dispatcher,
    Route,
    SentBriefs,
)
from freya.delegation.material import (
    ALLOWED,
    KIND_CODE,
    KIND_CONVERSATION,
    KIND_MEMORY,
    KIND_REQUEST,
    CloudSettings,
)
from freya.delegation.screening import Corrections
from freya.evaluation.delegation_tasks import KINDS as EVALUATED_KINDS


VENDOR = "anthropic"
ROUTE = "anthropic-plan"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)

# Made-up memories about made-up people.
MEMORIES = [
    ("1", "The user's sister Oriel Vantongeren lives in Tromso."),
    ("2", "The backup server answers on port 48213."),
]

AVAILABLE = [
    AvailableInput("request", KIND_REQUEST, "Review this function for mistakes."),
    AvailableInput("code:add", KIND_CODE, "def add(a, b): return a - b"),
    AvailableInput("turn:1", KIND_CONVERSATION, "We were talking about the adder."),
    AvailableInput("memory:1", KIND_MEMORY, MEMORIES[0][1]),
]


class Reply:
    def __init__(self, text="A worker's answer.", tokens=120):
        self.text, self.tokens = text, tokens


class RouteFailure(Exception):
    def __init__(self, sent):
        super().__init__("the route failed")
        self.sent = sent


class Harness:
    def __init__(self, tmp_path):
        self.settings = CloudSettings(tmp_path / "cloud_settings.json")
        self.settings.set_provider(VENDOR, True)
        self.settings.set_material(KIND_CODE, ALLOWED)
        self.store = ControlStore(tmp_path / "usage.db", USAGE_MIGRATIONS)
        self.ledger = UsageLedger(self.store)
        self.ledger.set_limit(
            ROUTE, "week", SCOPE_PERIOD, "tokens", 100_000,
            period_seconds=7 * 86400, resets_at=NOW + timedelta(days=7),
            hard=True, now=NOW,
        )
        self.corrections = Corrections(tmp_path / "corrections.json")
        self.sent = SentBriefs(tmp_path / "sent_briefs.jsonl")
        self.asked = []
        self.fail = None
        self.time = 1000.0
        self.writer_saw = []
        self.dispatcher = Dispatcher(
            self.settings, self.ledger,
            [Route(ROUTE, VENDOR, self.ask)],
            lambda: MEMORIES, self.sent, self.corrections,
            clock=lambda: self.time,
        )

    def ask(self, text):
        self.asked.append(text)
        if self.fail is not None:
            raise self.fail
        return Reply()

    def writer(self, messages):
        self.writer_saw.append(messages)
        return "Find the mistake in the function and name the line."

    def compose(self, plan=None, approved=frozenset(), available=AVAILABLE):
        plan = plan or parse_plan(
            {"kind": "code-review", "inputs": ["code:add"]}, available,
        )
        return compose_brief(
            plan, available, VENDOR, self.settings, self.writer, approved,
        )

    def used(self):
        return self.ledger.status(ROUTE, now=NOW)[0].used


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.store.close()


# The plan

def test_the_kinds_of_task_match_what_is_evaluated():
    assert TASK_KINDS == EVALUATED_KINDS


def test_a_plan_holds_choices_and_ids_and_nothing_else():
    plan = parse_plan(
        '{"kind": "analysis", "value": "high", "urgent": true, "inputs": ["code:add"]}',
        AVAILABLE,
    )

    assert (plan.kind, plan.inputs) == ("analysis", ("code:add",))
    # What the model thinks the task is worth is only a suggestion.
    assert (plan.suggested_value, plan.suggested_urgent) == ("high", True)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        {"kind": "analysis", "note": "her sister lives in Tromso"},
        {"kind": "analysis", "instructions": "mention Oriel"},
        {"kind": "writing poems"},
        {"kind": "The user's sister Oriel lives in Tromso"},
        {"kind": "analysis", "value": "urgent"},
        {"kind": "analysis", "urgent": "yes"},
        {"kind": "analysis", "inputs": "code:add"},
        {"kind": "analysis", "inputs": ["something not offered"]},
        {"kind": "analysis", "inputs": ["Oriel lives in Tromso"]},
        {"kind": "analysis", "inputs": ["code:add", "code:add"]},
        {"kind": "analysis", "inputs": [{"id": "code:add"}]},
    ],
)
def test_a_plan_with_room_for_prose_is_refused(raw):
    with pytest.raises(PlanRefused):
        parse_plan(raw, AVAILABLE)


# Composing

def test_the_writer_sees_only_material_cleared_to_leave(h):
    plan = parse_plan(
        {"kind": "code-review", "inputs": ["code:add", "turn:1", "memory:1"]},
        AVAILABLE,
    )

    waiting = h.compose(plan)
    assert not waiting.ready and waiting.text is None
    assert h.writer_saw == []

    brief = h.compose(plan, approved=frozenset({KIND_CONVERSATION, KIND_MEMORY}))
    shown = str(h.writer_saw)

    assert brief.ready
    assert "def add(a, b)" in shown and "talking about the adder" in shown
    # Memories are set never to leave, so the writer never sees one.
    assert "Oriel" not in shown and "Tromso" not in shown
    assert "Oriel" not in brief.text
    assert [part.kind for part in brief.review.withheld] == [KIND_MEMORY]


def test_the_brief_is_assembled_by_code_from_cleared_parts(h):
    brief = h.compose()

    assert brief.text == (
        "TASK\nFind the mistake in the function and name the line.\n\n"
        "MATERIAL\n[1]\nReview this function for mistakes.\n\n"
        "[2]\ndef add(a, b): return a - b"
    )
    assert writer_messages("code-review", brief.review.send)[1]["content"].count("[") == 2


def test_nothing_is_composed_for_a_provider_that_is_off(h):
    h.settings.set_provider(VENDOR, False)

    brief = h.compose()

    assert not brief.ready
    assert h.writer_saw == []


def test_a_writer_that_returns_nothing_is_an_error(h):
    h.writer = lambda messages: "   "

    with pytest.raises(PlanRefused):
        h.compose()


# Sending

def test_the_models_view_of_value_never_reaches_the_ledger(h):
    h.ledger.set_limit(
        ROUTE, "week", SCOPE_PERIOD, "tokens", 70_000,
        period_seconds=7 * 86400, resets_at=NOW + timedelta(days=7),
        hard=True, now=NOW,
    )
    eager = parse_plan(
        {"kind": "code-review", "value": "high", "urgent": True, "inputs": ["code:add"]},
        AVAILABLE,
    )
    brief = h.compose(eager)
    # A normal task may use today's share of 10,000; this estimate is
    # under it, so pad the day first.
    first = h.dispatcher.authorize(brief, ROUTE, "t", now=NOW)
    h.dispatcher.send(first, brief.text)
    h.ledger.settle(
        h.ledger.reserve(ROUTE, "tokens", 9_000, "pad", now=NOW).event_id, 9_000,
    )

    with pytest.raises(DispatchRefused) as paced:
        h.dispatcher.authorize(h.compose(eager), ROUTE, "t", now=NOW)

    assert paced.value.assessment.verdict == "ask"

    # Nor does the owner's own "urgent" pass the day's share: only
    # their yes to going over does (owner decision, 2026-10-11).
    with pytest.raises(DispatchRefused) as urgent:
        h.dispatcher.authorize(h.compose(eager), ROUTE, "t", urgent=True, now=NOW)

    assert urgent.value.assessment.verdict == "ask"
    assert h.dispatcher.authorize(
        h.compose(eager), ROUTE, "t", owner_agreed_to_exceed=True, now=NOW,
    )


def test_a_brief_composed_under_old_settings_cannot_be_authorized(h):
    brief = h.compose()

    h.settings.set_material(KIND_CODE, "never")

    with pytest.raises(DispatchRefused):
        h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)
    assert h.used() == 0


def test_a_brief_whose_text_was_swapped_cannot_be_authorized(h):
    import dataclasses

    brief = h.compose()
    swapped = dataclasses.replace(
        brief, text=brief.text + "\n\n[3]\nOriel lives in Tromso.",
    )
    relabelled = dataclasses.replace(
        brief,
        parts=brief.parts + (
            type(brief.parts[0])(KIND_REQUEST, MEMORIES[1][1], "request-2"),
        ),
    )

    with pytest.raises(DispatchRefused):
        h.dispatcher.authorize(swapped, ROUTE, "task-1", now=NOW)
    with pytest.raises(DispatchRefused):
        h.dispatcher.authorize(relabelled, ROUTE, "task-1", now=NOW)
    assert h.used() == 0


def test_an_always_private_memory_cannot_be_released_with_one_click(h):
    h.corrections.mark_always_private(memory_id="1")
    available = [
        AvailableInput("request", KIND_REQUEST, "Draft a note to Oriel about the move."),
    ]
    brief = h.compose(parse_plan({"kind": "rewriting"}, available), available=available)

    with pytest.raises(DispatchRefused) as held:
        h.dispatcher.authorize(
            brief, ROUTE, "task-1", owner_released_hold=True, now=NOW,
        )

    assert {m.kind for m in held.value.screening.matches} == {"always-private"}


def test_a_mark_made_after_authorizing_still_stops_the_send(h):
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    h.corrections.mark_always_private(phrase="function for mistakes")

    with pytest.raises(DispatchRefused) as held:
        h.dispatcher.send(authorization, brief.text)

    assert held.value.screening.held
    assert h.asked == [] and h.used() == 0


def test_a_wrong_copy_does_not_cancel_the_real_authorization(h):
    brief = h.compose()
    real = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)
    altered = type(real)(
        "0" * 64, real.route_id, real.provider, real.settings_revision,
        real.event_id, real.issued, real.serial,
    )

    with pytest.raises(DispatchRefused):
        h.dispatcher.send(altered, brief.text)

    assert h.dispatcher.send(real, brief.text).text == "A worker's answer."


def test_authorizations_nobody_used_give_their_usage_back(h):
    brief = h.compose()
    h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)
    assert h.used() > 0

    h.time += AUTHORIZATION_SECONDS + 1
    h.dispatcher.authorize(h.compose(), ROUTE, "task-2", now=NOW)

    assert h.used() == len(brief.text.encode("utf-8")) + 4096


def test_a_reply_without_a_usage_figure_keeps_the_estimate(h):
    class NoFigure:
        text, tokens = "An answer.", None

    h.ask = lambda text: NoFigure()
    h.dispatcher = Dispatcher(
        h.settings, h.ledger, [Route(ROUTE, VENDOR, h.ask)],
        lambda: MEMORIES, h.sent, h.corrections, clock=lambda: h.time,
    )
    brief = h.compose()

    h.dispatcher.send(
        h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW), brief.text,
    )

    assert h.used() == len(brief.text.encode("utf-8")) + 4096
    assert [e["outcome"] for e in h.sent.entries()] == ["answered"]


def test_the_dispatcher_can_tell_a_route_what_it_is_sending(h):
    brief = h.compose()
    seen = []
    h.ask = lambda text: seen.append(
        (h.dispatcher.in_flight(text), h.dispatcher.in_flight(text + "x"))
    ) or Reply()
    h.dispatcher = Dispatcher(
        h.settings, h.ledger, [Route(ROUTE, VENDOR, h.ask)],
        lambda: MEMORIES, h.sent, h.corrections, clock=lambda: h.time,
    )

    assert not h.dispatcher.in_flight(brief.text)
    h.dispatcher.send(
        h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW), brief.text,
    )

    assert seen == [(True, False)]
    assert not h.dispatcher.in_flight(brief.text)


def test_one_send_carries_one_permit(h):
    brief = h.compose()
    taken = []

    def ask(text):
        # The route takes its permit; a second caller with the same
        # text at the same moment finds none.
        taken.append(h.dispatcher.take_permit(text))
        taken.append(h.dispatcher.take_permit(text))
        return Reply()

    h.dispatcher = Dispatcher(
        h.settings, h.ledger, [Route(ROUTE, VENDOR, ask)],
        lambda: MEMORIES, h.sent, h.corrections, clock=lambda: h.time,
    )

    assert not h.dispatcher.take_permit(brief.text)
    h.dispatcher.send(
        h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW), brief.text,
    )

    assert taken == [True, False]
    assert not h.dispatcher.take_permit(brief.text)


def test_a_permit_nobody_took_does_not_stay_behind(h):
    brief = h.compose()

    h.dispatcher.send(
        h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW), brief.text,
    )

    assert not h.dispatcher.take_permit(brief.text)

def test_a_checked_brief_is_sent_recorded_and_counted(h):
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    reply = h.dispatcher.send(authorization, brief.text)

    assert reply.text == "A worker's answer."
    assert h.asked == [brief.text]
    assert h.used() == 120
    assert [(e["provider"], e["outcome"], e["text"]) for e in h.sent.entries()] == [
        (VENDOR, "answered", brief.text),
    ]


def test_no_other_text_can_be_sent_with_an_authorization(h):
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    with pytest.raises(DispatchRefused):
        h.dispatcher.send(authorization, brief.text + " Also, Oriel lives in Tromso.")

    assert h.asked == []
    assert h.used() == 0
    assert h.sent.entries() == []


def test_an_authorization_works_once(h):
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)
    h.dispatcher.send(authorization, brief.text)

    with pytest.raises(DispatchRefused):
        h.dispatcher.send(authorization, brief.text)

    assert len(h.asked) == 1


def test_a_provider_hold_stops_a_brief_at_both_checks(h):
    # Two briefs are authorized; then the provider says its limit is
    # reached. The second must not go, and its usage is given back.
    waiting = {}
    h.dispatcher._held = lambda provider: waiting.get(provider)
    brief = h.compose()
    first = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)
    second = h.dispatcher.authorize(brief, ROUTE, "task-2", now=NOW)
    h.dispatcher.send(first, brief.text)
    used = h.used()
    waiting[VENDOR] = "The plan's limit is reached."

    with pytest.raises(DispatchRefused) as refused:
        h.dispatcher.send(second, brief.text)

    assert refused.value.reason == "The plan's limit is reached."
    assert len(h.asked) == 1
    assert h.used() < used

    with pytest.raises(DispatchRefused):
        h.dispatcher.authorize(brief, ROUTE, "task-3", now=NOW)


def test_a_made_up_authorization_is_refused(h):
    brief = h.compose()
    real = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)
    forged = type(real)(
        real.brief_hash, real.route_id, real.provider,
        real.settings_revision, real.event_id, real.issued, real.serial + 50,
    )
    h.dispatcher.send(real, brief.text)
    h.asked.clear()
    altered = type(real)(
        "0" * 64, real.route_id, real.provider,
        real.settings_revision, real.event_id, real.issued, real.serial,
    )

    with pytest.raises(DispatchRefused):
        h.dispatcher.send(forged, brief.text)
    with pytest.raises(DispatchRefused):
        h.dispatcher.send(altered, brief.text)

    assert h.asked == []


def test_an_old_authorization_is_refused_and_its_usage_given_back(h):
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)
    h.time += AUTHORIZATION_SECONDS + 1

    with pytest.raises(DispatchRefused):
        h.dispatcher.send(authorization, brief.text)

    assert h.asked == [] and h.used() == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.set_provider(VENDOR, False),
        lambda s: s.set_material(KIND_CODE, "never"),
        lambda s: s.set_provider("openai", True),
    ],
)
def test_changing_the_settings_cancels_an_authorization(h, change):
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    change(h.settings)

    with pytest.raises(DispatchRefused):
        h.dispatcher.send(authorization, brief.text)

    assert h.asked == [] and h.used() == 0


def test_a_provider_that_is_off_cannot_be_authorized(h):
    brief = h.compose()
    h.settings.set_provider(VENDOR, False)

    with pytest.raises(DispatchRefused):
        h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)


def test_a_brief_waiting_for_permission_cannot_be_authorized(h):
    plan = parse_plan({"kind": "code-review", "inputs": ["turn:1"]}, AVAILABLE)

    with pytest.raises(DispatchRefused) as refused:
        h.dispatcher.authorize(h.compose(plan), ROUTE, "task-1", now=NOW)

    assert refused.value.review.ask == (KIND_CONVERSATION,)


def test_a_brief_for_another_provider_or_route_is_refused(h):
    brief = h.compose()

    with pytest.raises(DispatchRefused):
        h.dispatcher.authorize(brief, "no-such-route", "task-1", now=NOW)

    other = Dispatcher(
        h.settings, h.ledger, [Route(ROUTE, "openai", h.ask)],
        lambda: MEMORIES, h.sent,
    )
    with pytest.raises(DispatchRefused):
        other.authorize(brief, ROUTE, "task-1", now=NOW)


def test_a_request_that_names_something_private_is_held(h):
    available = [
        AvailableInput("request", KIND_REQUEST, "Draft a note to Oriel about the move."),
    ]
    brief = h.compose(parse_plan({"kind": "rewriting"}, available), available=available)

    with pytest.raises(DispatchRefused) as held:
        h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    assert [m.text for m in held.value.screening.matches] == ["oriel"]
    assert h.used() == 0

    released = h.dispatcher.authorize(
        brief, ROUTE, "task-1", owner_released_hold=True, now=NOW,
    )
    h.dispatcher.send(released, brief.text)
    assert len(h.asked) == 1


def test_always_private_cannot_be_released_with_one_click(h):
    h.corrections.mark_always_private(phrase="adder")
    available = [
        AvailableInput("request", KIND_REQUEST, "Explain how the adder works."),
    ]
    brief = h.compose(parse_plan({"kind": "analysis"}, available), available=available)

    with pytest.raises(DispatchRefused):
        h.dispatcher.authorize(
            brief, ROUTE, "task-1", owner_released_hold=True, now=NOW,
        )


def test_what_the_writer_adds_is_screened_too(h):
    h.writer = lambda messages: "Review it the way Oriel would."

    with pytest.raises(DispatchRefused) as held:
        h.dispatcher.authorize(h.compose(), ROUTE, "task-1", now=NOW)

    assert held.value.screening.held


def test_the_ledger_can_refuse_and_nothing_is_reserved(h):
    h.ledger.set_limit(
        ROUTE, "week", SCOPE_PERIOD, "tokens", 100,
        period_seconds=7 * 86400, resets_at=NOW + timedelta(days=7),
        hard=True, now=NOW,
    )

    with pytest.raises(DispatchRefused) as refused:
        h.dispatcher.authorize(h.compose(), ROUTE, "task-1", now=NOW)

    assert refused.value.assessment.verdict == "refuse"
    assert h.used() == 0


def test_a_route_with_no_limit_cannot_be_used(h):
    other = Dispatcher(
        h.settings, h.ledger, [Route("unlimited", VENDOR, h.ask)],
        lambda: MEMORIES, h.sent,
    )

    with pytest.raises(DispatchRefused):
        other.authorize(h.compose(), "unlimited", "task-1", now=NOW)


def test_a_failure_before_sending_uses_nothing_and_records_nothing(h):
    h.fail = RouteFailure(sent=False)
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    with pytest.raises(RouteFailure):
        h.dispatcher.send(authorization, brief.text)

    assert h.used() == 0
    assert h.sent.entries() == []


def test_a_failure_after_sending_counts_in_full_and_is_recorded(h):
    h.fail = RouteFailure(sent=True)
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    with pytest.raises(RouteFailure):
        h.dispatcher.send(authorization, brief.text)

    assert h.used() == len(brief.text.encode("utf-8")) + 4096
    assert [e["outcome"] for e in h.sent.entries()] == ["outcome unknown"]


def test_the_settings_revision_rises_with_each_change_and_is_kept(tmp_path):
    path = tmp_path / "cloud_settings.json"
    settings = CloudSettings(path)
    start = settings.revision

    settings.set_provider(VENDOR, True)
    settings.set_provider(VENDOR, True)   # no change, no new revision
    settings.set_material(KIND_CODE, ALLOWED)

    assert settings.revision == start + 2
    assert CloudSettings(path).revision == start + 2


def test_a_record_that_cannot_be_written_still_settles_the_ledger(h, monkeypatch):
    brief = h.compose()
    authorization = h.dispatcher.authorize(brief, ROUTE, "task-1", now=NOW)

    def cannot_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(h.sent, "record", cannot_write)

    with pytest.raises(OSError):
        h.dispatcher.send(authorization, brief.text)

    # The brief left, so it counts at the provider's figure, and the
    # reservation is not left open to count against every later week.
    assert h.asked == [brief.text]
    assert h.used() == 120
    with pytest.raises(KeyError):
        h.ledger.settle(authorization.event_id, 1)


def test_a_damaged_line_in_the_record_hides_only_itself(h):
    h.sent.record(VENDOR, ROUTE, "first", "answered")
    with open(h.sent.path, "a", encoding="utf-8") as output:
        output.write('{"at": "2026-10-11T0')  # cut short by a power loss
    with open(h.sent.path, "a", encoding="utf-8") as output:
        output.write("\n")
    h.sent.record(VENDOR, ROUTE, "second", "answered")

    assert [e["text"] for e in h.sent.entries()] == ["first", "second"]
