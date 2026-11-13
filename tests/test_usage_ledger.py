from datetime import datetime, timedelta, timezone

import pytest

from freya.core.persistence import ControlStore
from freya.core.usage import (
    ANY_ROUTE,
    ASK,
    GO,
    REFUSE,
    SCOPE_CALL,
    SCOPE_PERIOD,
    SCOPE_TASK,
    STALE_RESERVATION_SECONDS,
    USAGE_MIGRATIONS,
    VALUE_HIGH,
    VALUE_LOW,
    WAIT,
    UsageLedger,
)


WEEK = 7 * 86400
PLAN = "fake-plan"
PAID = "fake-api"

# A provider's week that began on a Wednesday afternoon, so no boundary
# falls on local or UTC midnight.
WEEK_START = datetime(2026, 10, 7, 15, 30, tzinfo=timezone.utc)
RESET = WEEK_START + timedelta(days=7)


def day(number, hours=1):
    """A moment `hours` into the numbered day of the provider's week."""
    return WEEK_START + timedelta(days=number - 1, hours=hours)


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path / "usage.db", USAGE_MIGRATIONS)
    yield store
    store.close()


@pytest.fixture
def ledger(store):
    return UsageLedger(store)


@pytest.fixture
def weekly(ledger):
    ledger.set_limit(
        PLAN, "week", SCOPE_PERIOD, "units", 700,
        period_seconds=WEEK, resets_at=RESET, hard=True,
        source="provider", now=day(1, 0),
    )
    return ledger


def spend(ledger, amount, now, task="t", route=PLAN, unit="units", **kwargs):
    # As the owner would: going past the day's share takes their yes.
    kwargs.setdefault("owner_agreed_to_exceed", True)
    result = ledger.reserve(route, unit, amount, task, now=now, **kwargs)
    assert result.verdict == GO, result.reason
    ledger.settle(result.event_id, amount)
    return result


def test_the_usage_store_is_its_own_database(tmp_path, store):
    tables = {
        row[0]
        for row in store.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }

    assert {"usage_limit", "usage_event"} <= tables
    assert "audit_event" not in tables
    assert store.schema_version == len(USAGE_MIGRATIONS)


def test_a_route_with_no_limit_cannot_be_used(ledger):
    result = ledger.reserve(PLAN, "units", 1, "t", now=day(1))

    assert result.verdict == REFUSE
    assert result.event_id is None


def test_times_without_a_zone_are_rejected(ledger):
    with pytest.raises(ValueError):
        ledger.assess(PLAN, "units", 1, "t", now=datetime(2026, 10, 9))


def test_a_zero_money_cap_refuses_any_cost_and_cannot_be_overridden(ledger):
    ledger.set_limit(PAID, "task", SCOPE_TASK, "usd-micros", 0)

    paid = ledger.reserve(
        PAID, "usd-micros", 1, "t",
        owner_agreed_to_exceed=True, now=day(1),
    )
    free = ledger.reserve(PAID, "usd-micros", 0, "t", now=day(1))

    assert paid.verdict == REFUSE
    assert paid.event_id is None
    # Zero means off: a call estimated free could still be charged.
    assert free.verdict == REFUSE
    assert free.event_id is None


def test_call_and_task_caps_ask_and_the_owner_may_exceed_once(ledger):
    ledger.set_limit(PAID, "call", SCOPE_CALL, "usd-micros", 100)
    ledger.set_limit(PAID, "task", SCOPE_TASK, "usd-micros", 150)

    assert ledger.assess(PAID, "usd-micros", 101, "a").verdict == ASK

    spend(ledger, 100, day(1), task="a", route=PAID, unit="usd-micros")

    # The task cap counts only this task's use.
    assert ledger.assess(PAID, "usd-micros", 60, "a", now=day(1)).verdict == ASK
    assert ledger.assess(PAID, "usd-micros", 60, "b", now=day(1)).verdict == GO

    refused = ledger.reserve(PAID, "usd-micros", 60, "a", now=day(1))
    agreed = ledger.reserve(
        PAID, "usd-micros", 60, "a",
        owner_agreed_to_exceed=True, now=day(1),
    )

    assert refused.event_id is None
    assert agreed.verdict == GO and agreed.event_id is not None
    # Agreeing covered that reservation only.
    assert ledger.assess(PAID, "usd-micros", 1, "a", now=day(1)).verdict == ASK


def test_a_weekly_allowance_is_split_across_the_days_left(weekly):
    status = weekly.status(PLAN, now=day(1))[0]

    assert status.paced
    assert status.today_share == 100
    assert status.resets_at == RESET
    # Days are counted from the provider's reset, not from midnight.
    assert status.next_share_at == WEEK_START + timedelta(days=1)


def test_normal_work_asks_before_passing_todays_share(weekly):
    spend(weekly, 90, day(1))

    within = weekly.assess(PLAN, "units", 10, "t", now=day(1))
    beyond = weekly.assess(PLAN, "units", 11, "t", now=day(1))

    assert within.verdict == GO
    assert beyond.verdict == ASK
    assert "today's share" in beyond.reason


def test_unused_share_carries_forward(weekly):
    spend(weekly, 40, day(1))

    status = weekly.status(PLAN, now=day(2))[0]

    # 660 left over six days.
    assert status.today_share == 110
    assert status.used_today == 0


def test_overspending_one_day_shrinks_the_days_after(weekly):
    spend(weekly, 220, day(1), value=VALUE_HIGH)

    assert weekly.status(PLAN, now=day(2))[0].today_share == 80


def test_low_value_work_waits_unless_allowance_is_plentiful(weekly):
    assert weekly.assess(
        PLAN, "units", 50, "t", value=VALUE_LOW, now=day(1)
    ).verdict == GO

    held = weekly.assess(PLAN, "units", 51, "t", value=VALUE_LOW, now=day(1))

    assert held.verdict == WAIT


def test_nothing_passes_todays_share_without_the_owners_yes(weekly):
    # Owner decision, 2026-10-11: however much the work matters.
    spend(weekly, 100, day(1))

    for more in ({"value": VALUE_HIGH}, {"urgent": True}, {}):
        asked = weekly.assess(PLAN, "units", 50, "t", now=day(1), **more)
        held = weekly.reserve(PLAN, "units", 50, "t", now=day(1), **more)

        assert asked.verdict == ASK and asked.over_share
        assert "today's share" in asked.reason
        assert held.verdict == ASK and held.event_id is None

    agreed = weekly.reserve(
        PLAN, "units", 50, "t",
        value=VALUE_HIGH, owner_agreed_to_exceed=True, now=day(1),
    )

    assert agreed.verdict == GO and agreed.over_share


def test_urgent_work_of_low_value_is_not_held_back_at_half_the_share(weekly):
    assert weekly.assess(
        PLAN, "units", 60, "t", value=VALUE_LOW, urgent=True, now=day(1)
    ).verdict == GO


def test_nothing_passes_a_hard_allowance_not_even_urgent_work(weekly):
    spend(weekly, 690, day(1), value=VALUE_HIGH)

    result = weekly.reserve(
        PLAN, "units", 11, "t",
        urgent=True, owner_agreed_to_exceed=True, now=day(1),
    )

    assert result.verdict == WAIT
    assert result.event_id is None
    assert result.limit.resets_at == RESET


def test_work_larger_than_a_hard_allowance_is_refused_not_told_to_wait(weekly):
    result = weekly.assess(PLAN, "units", 701, "t", urgent=True, now=day(1))

    assert result.verdict == REFUSE


def test_a_call_still_in_flight_counts_after_the_reset(weekly):
    pending = weekly.reserve(
        PLAN, "units", 600, "t", value=VALUE_HIGH, now=day(7, 23),
    )
    after = RESET + timedelta(minutes=5)

    assert weekly.status(PLAN, now=after)[0].used == 600
    assert weekly.assess(
        PLAN, "units", 200, "t", value=VALUE_HIGH, now=after
    ).verdict == WAIT

    # Once its outcome is known it belongs to the period it ran in.
    weekly.settle(pending.event_id, 600)

    assert weekly.status(PLAN, now=after)[0].used == 0


def test_a_soft_period_cap_offers_the_choice_to_go_over(ledger):
    ledger.set_limit(
        PAID, "day", SCOPE_PERIOD, "usd-micros", 100,
        period_seconds=86400, resets_at=day(2, 0), now=day(1, 0),
    )
    spend(ledger, 95, day(1), route=PAID, unit="usd-micros")

    assert ledger.assess(PAID, "usd-micros", 10, "t", now=day(1)).verdict == ASK


def test_usage_starts_again_after_the_reset(weekly):
    spend(weekly, 700, day(1), value=VALUE_HIGH)
    assert weekly.assess(PLAN, "units", 1, "t", now=day(7)).verdict == WAIT

    after = weekly.status(PLAN, now=RESET + timedelta(hours=1))[0]

    assert after.used == 0
    assert after.resets_at == RESET + timedelta(days=7)
    assert after.today_share == 100


def test_a_providers_notice_overrides_freyas_own_count(weekly):
    spend(weekly, 10, day(2))
    sooner = day(4, 0)

    weekly.note_limit_reached(PLAN, "week", sooner, now=day(2))

    status = weekly.status(PLAN, now=day(3))[0]
    assert status.remaining == 0
    assert status.resets_at == sooner
    assert status.source == "provider"
    assert weekly.assess(
        PLAN, "units", 1, "t", urgent=True, now=day(3)
    ).verdict == WAIT

    # Once the provider's reset has passed, the allowance is open again.
    assert weekly.assess(PLAN, "units", 1, "t", now=day(4, 1)).verdict == GO


def test_moving_the_reset_does_not_move_what_was_already_used(weekly):
    spend(weekly, 100, day(1))
    spend(weekly, 50, day(3))
    later = RESET + timedelta(days=3)

    # The provider says this period runs three days longer than thought.
    weekly.set_period(PLAN, "week", WEEK_START, later, now=day(4))

    status = weekly.status(PLAN, now=day(4))[0]
    assert status.used == 150
    assert status.resets_at == later

    weekly.note_limit_reached(PLAN, "week", later, now=day(5))
    after = weekly.status(PLAN, now=later + timedelta(hours=1))[0]
    assert after.used == 0
    assert after.resets_at == later + timedelta(days=7)


def test_a_billing_month_need_not_be_a_fixed_number_of_days(ledger):
    october = datetime(2026, 10, 1, tzinfo=timezone.utc)
    november = datetime(2026, 11, 1, tzinfo=timezone.utc)
    ledger.set_limit(
        PAID, "month", SCOPE_PERIOD, "usd-micros", 1000,
        period_seconds=30 * 86400, period_start=october, resets_at=november,
        now=october,
    )
    spend(ledger, 400, october + timedelta(hours=2), route=PAID, unit="usd-micros", value=VALUE_HIGH)

    # Thirty days back from 1 November is 2 October, which would have
    # dropped the first day's spending.
    late = ledger.status(PAID, now=datetime(2026, 10, 31, 12, tzinfo=timezone.utc))[0]

    assert late.used == 400

    december = datetime(2026, 12, 1, tzinfo=timezone.utc)
    ledger.set_period(PAID, "month", november, december, now=november)
    assert ledger.status(PAID, now=november + timedelta(hours=1))[0].used == 0


def test_a_period_must_start_before_it_resets(ledger, weekly):
    with pytest.raises(ValueError):
        ledger.set_limit(
            PLAN, "week", SCOPE_PERIOD, "units", 700,
            period_seconds=WEEK, period_start=RESET, resets_at=RESET,
        )
    with pytest.raises(ValueError):
        weekly.set_period(PLAN, "week", RESET, WEEK_START, now=day(1))
    with pytest.raises(KeyError):
        weekly.set_period(PLAN, "month", WEEK_START, RESET, now=day(1))


def test_a_notice_for_an_unknown_limit_is_an_error(ledger):
    with pytest.raises(KeyError):
        ledger.note_limit_reached(PLAN, "week", RESET, now=day(1))


def test_an_unknown_outcome_counts_in_full_and_an_unsent_call_not_at_all(weekly):
    lost = weekly.reserve(PLAN, "units", 30, "t", now=day(1))
    unsent = weekly.reserve(PLAN, "units", 30, "t", now=day(1))
    cheap = weekly.reserve(PLAN, "units", 30, "t", now=day(1))

    assert weekly.status(PLAN, now=day(1))[0].used == 90

    weekly.mark_unknown(lost.event_id)
    weekly.release(unsent.event_id)
    weekly.settle(cheap.event_id, 5)

    assert weekly.status(PLAN, now=day(1))[0].used == 35


def test_an_outcome_is_recorded_once(weekly):
    event = weekly.reserve(PLAN, "units", 30, "t", now=day(1)).event_id
    weekly.settle(event, 10)

    with pytest.raises(KeyError):
        weekly.settle(event, 0)
    with pytest.raises(KeyError):
        weekly.release(event)


def test_a_limit_on_every_route_counts_them_together(ledger):
    ledger.set_limit(
        ANY_ROUTE, "day", SCOPE_PERIOD, "usd-micros", 100,
        period_seconds=86400, resets_at=day(2, 0), now=day(1, 0),
    )
    spend(ledger, 60, day(1), route="api-one", unit="usd-micros")

    assert ledger.assess(
        "api-two", "usd-micros", 41, "t", now=day(1)
    ).verdict == ASK
    # Plan units are not money and do not count against it.
    assert ledger.assess("api-two", "units", 41, "t", now=day(1)).verdict == REFUSE


def test_approaching_a_cap_is_reported(weekly):
    spend(weekly, 500, day(1), value=VALUE_HIGH)

    far = weekly.assess(PLAN, "units", 10, "t", value=VALUE_HIGH, now=day(2))
    close = weekly.assess(PLAN, "units", 60, "t", value=VALUE_HIGH, now=day(2))

    assert far.near == ()
    assert [status.name for status in close.near] == ["week"]


def test_a_period_limit_needs_a_length_and_a_reset(ledger):
    with pytest.raises(ValueError):
        ledger.set_limit(PLAN, "week", SCOPE_PERIOD, "units", 700)
    with pytest.raises(ValueError):
        ledger.set_limit(
            PLAN, "task", SCOPE_TASK, "units", 700, period_seconds=WEEK,
        )


def test_usage_survives_reopening(tmp_path):
    path = tmp_path / "usage.db"
    first = ControlStore(path, USAGE_MIGRATIONS)
    ledger = UsageLedger(first)
    ledger.set_limit(
        PLAN, "week", SCOPE_PERIOD, "units", 700,
        period_seconds=WEEK, resets_at=RESET, hard=True, now=day(1, 0),
    )
    spend(ledger, 70, day(1))
    first.close()

    second = ControlStore(path, USAGE_MIGRATIONS)

    try:
        assert UsageLedger(second).status(PLAN, now=day(1))[0].used == 70
    finally:
        second.close()


def test_an_open_reservation_counts_now_and_a_stale_one_only_where_it_was_made(weekly):
    # A send under way counts in whatever window is asked about.
    open_now = weekly.reserve(PLAN, "units", 60, "t", now=day(1))
    assert open_now.verdict == GO
    assert weekly.status(PLAN, now=day(1, 2))[0].used == 60
    assert weekly.status(PLAN, now=day(1, 2))[0].used_today == 60

    # Freya died mid-send: nothing ever settles it. After the stale
    # window it still counts at its full estimate, like an unknown
    # outcome, but in the period it was made in, not every one after.
    later = day(1) + timedelta(seconds=STALE_RESERVATION_SECONDS + 1)
    assert weekly.status(PLAN, now=later)[0].used == 60

    next_week = RESET + timedelta(days=1)
    assert weekly.status(PLAN, now=next_week)[0].used == 0
    assert weekly.status(PLAN, now=next_week)[0].used_today == 0
