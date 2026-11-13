import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from freya.core.persistence.control_store import DATA_DIR


USAGE_DATABASE_PATH = DATA_DIR / "freya_usage.db"

# Each entry is one schema version. Never edit an entry that has shipped;
# append a new one instead.
USAGE_MIGRATIONS = [
    """
    CREATE TABLE usage_limit (
        limit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        route TEXT NOT NULL,
        name TEXT NOT NULL,
        scope TEXT NOT NULL CHECK (scope IN ('call', 'task', 'period')),
        unit TEXT NOT NULL,
        capacity INTEGER NOT NULL CHECK (capacity >= 0),
        period_seconds INTEGER,
        period_start TEXT,
        resets_at TEXT,
        exhausted_until TEXT,
        hard INTEGER NOT NULL CHECK (hard IN (0, 1)),
        source TEXT NOT NULL
            CHECK (source IN ('owner', 'provider', 'estimate')),
        updated_at TEXT NOT NULL,
        UNIQUE (route, name)
    );

    CREATE TABLE usage_event (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        route TEXT NOT NULL,
        task_id TEXT NOT NULL,
        unit TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        estimated INTEGER NOT NULL CHECK (estimated >= 0),
        reported INTEGER CHECK (reported IS NULL OR reported >= 0),
        state TEXT NOT NULL
            CHECK (state IN ('reserved', 'settled', 'unknown', 'released'))
    );

    CREATE INDEX usage_event_route
        ON usage_event (route, recorded_at);
    """,
]

# A limit on this route applies to every route that counts in its unit.
ANY_ROUTE = "*"

SCOPE_CALL = "call"
SCOPE_TASK = "task"
SCOPE_PERIOD = "period"

VALUE_LOW = "low"
VALUE_NORMAL = "normal"
VALUE_HIGH = "high"
VALUES = (VALUE_LOW, VALUE_NORMAL, VALUE_HIGH)

# Verdicts, mildest first. The most severe one among the limits decides.
GO = "go"
ASK = "ask"
WAIT = "wait"
REFUSE = "refuse"
_SEVERITY = {GO: 0, ASK: 1, WAIT: 2, REFUSE: 3}

DAY_SECONDS = 86400
WARNING_FRACTION = 0.8

# A reservation is settled, released or marked unknown within minutes
# of being made; one older than this was left behind by a Freya that
# died mid-send. It is counted like an unknown outcome from then on,
# in the period it was made in, instead of against every period for
# ever. Far longer than any send can take (the authorization window,
# the program's sign-in check and its run come to a few minutes).
STALE_RESERVATION_SECONDS = 2 * 3600

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# What an event counts for: the provider's figure once it is known,
# the full estimate while it is not, and nothing if it was never sent.
_COUNTED = """
    CASE state
        WHEN 'settled' THEN reported
        WHEN 'released' THEN 0
        ELSE estimated
    END
"""


def _stamp(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("Times given to the usage ledger need a time zone.")
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def _now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Times given to the usage ledger need a time zone.")
    return now


@dataclass(frozen=True)
class LimitStatus:
    """
    One limit and how much of it is used at a given moment.

    For a paced limit, `today_share` is what may be used between
    `day_start` and the next day boundary so that the rest lasts
    until `resets_at`; days are counted back from the reset, so they
    follow the provider's clock rather than local midnight.
    """

    route: str
    name: str
    scope: str
    unit: str
    capacity: int
    used: int
    hard: bool
    source: str
    resets_at: Optional[datetime] = None
    paced: bool = False
    today_share: Optional[int] = None
    used_today: Optional[int] = None
    next_share_at: Optional[datetime] = None

    @property
    def remaining(self) -> int:
        return max(0, self.capacity - self.used)


@dataclass(frozen=True)
class Assessment:
    """
    Whether a piece of work may use a route now.

    go: within every limit. ask: it would pass a limit the owner may
    choose to exceed. wait: it would pass a limit that only time
    lifts. refuse: not possible at any time under the present limits.
    """

    verdict: str
    reason: str
    limit: Optional[LimitStatus] = None
    over_share: bool = False
    near: Tuple[LimitStatus, ...] = ()
    event_id: Optional[int] = None


class UsageLedger:
    """
    Records what each route has used and decides whether more fits.

    A route is one way of reaching one model. Amounts are whole
    numbers in a unit the route counts in (micro-dollars for money, or
    Freya's own count for a plan allowance). The ledger never contacts
    a provider and holds no prompts or replies.
    """

    def __init__(self, store, warning_fraction=WARNING_FRACTION):
        self._store = store
        self._warning_fraction = warning_fraction

    # Limits

    def set_limit(
        self,
        route,
        name,
        scope,
        unit,
        capacity,
        *,
        period_seconds=None,
        resets_at=None,
        period_start=None,
        hard=False,
        source="owner",
        now=None,
    ) -> None:
        """
        Create or replace the limit called `name` on a route.

        A period limit needs its length and the moment it next resets.
        Its present period runs from `period_start`, which is one
        length before the reset unless stated, to `resets_at`; the two
        are kept separately, so a billing month need not be a fixed
        number of days. Later periods are assumed to follow at the
        stated length until `set_period` says otherwise.

        A hard limit is one that cannot be exceeded whatever the owner
        answers, such as a plan allowance the vendor enforces.
        """
        if scope == SCOPE_PERIOD:
            if not period_seconds or period_seconds <= 0 or resets_at is None:
                raise ValueError(
                    "A period limit needs period_seconds and resets_at."
                )
            if period_start is None:
                period_start = resets_at - timedelta(seconds=period_seconds)
            if period_start >= resets_at:
                raise ValueError("A period must start before it resets.")
        elif (
            period_seconds is not None
            or resets_at is not None
            or period_start is not None
        ):
            raise ValueError("Only a period limit has a length or a reset.")

        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO usage_limit (
                    route, name, scope, unit, capacity, period_seconds,
                    period_start, resets_at, exhausted_until, hard, source,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT (route, name) DO UPDATE SET
                    scope = excluded.scope,
                    unit = excluded.unit,
                    capacity = excluded.capacity,
                    period_seconds = excluded.period_seconds,
                    period_start = excluded.period_start,
                    resets_at = excluded.resets_at,
                    exhausted_until = NULL,
                    hard = excluded.hard,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    route,
                    name,
                    scope,
                    unit,
                    capacity,
                    period_seconds,
                    _stamp(period_start) if period_start is not None else None,
                    _stamp(resets_at) if resets_at is not None else None,
                    1 if hard else 0,
                    source,
                    _stamp(_now(now)),
                ),
            )

    def set_period(self, route, name, period_start, resets_at, now=None) -> None:
        """
        State exactly when a limit's present period began and when it
        resets, as the provider reports it. What was used stays where
        it was recorded; only the boundaries move.
        """
        if period_start >= resets_at:
            raise ValueError("A period must start before it resets.")

        with self._store.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE usage_limit
                SET period_start = ?, resets_at = ?, exhausted_until = NULL,
                    source = 'provider', updated_at = ?
                WHERE route = ? AND name = ? AND scope = 'period'
                """,
                (
                    _stamp(period_start),
                    _stamp(resets_at),
                    _stamp(_now(now)),
                    route,
                    name,
                ),
            ).rowcount

        if not changed:
            raise KeyError(f"No period limit {name!r} on route {route!r}.")

    def note_limit_reached(self, route, name, resets_at, now=None) -> None:
        """
        Record a provider's notice that a limit is used up until
        `resets_at`, whatever Freya's own count says. The period's
        start is left alone, so earlier usage is not lost from it.
        """
        with self._store.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE usage_limit
                SET resets_at = ?, exhausted_until = ?,
                    source = 'provider', updated_at = ?
                WHERE route = ? AND name = ? AND scope = 'period'
                """,
                (
                    _stamp(resets_at),
                    _stamp(resets_at),
                    _stamp(_now(now)),
                    route,
                    name,
                ),
            ).rowcount

        if not changed:
            raise KeyError(f"No period limit {name!r} on route {route!r}.")

    def status(self, route, now=None) -> List[LimitStatus]:
        """
        Every limit that applies to a route, with its present use.
        """
        with self._store.transaction() as connection:
            return self._statuses(connection, route, None, None, _now(now))

    # Deciding and recording

    def assess(
        self,
        route,
        unit,
        estimate,
        task_id,
        *,
        value=VALUE_NORMAL,
        urgent=False,
        now=None,
    ) -> Assessment:
        """
        Say whether `estimate` more may be used on a route, without
        recording anything.
        """
        with self._store.transaction() as connection:
            return self._assess(
                connection, route, unit, estimate, task_id,
                value, urgent, _now(now),
            )

    def reserve(
        self,
        route,
        unit,
        estimate,
        task_id,
        *,
        value=VALUE_NORMAL,
        urgent=False,
        owner_agreed_to_exceed=False,
        now=None,
    ) -> Assessment:
        """
        Assess, and if the work may go, record its estimate against
        the route in the same transaction. The returned assessment
        carries the event to settle afterwards.

        `owner_agreed_to_exceed` turns an "ask" into a go for this one
        reservation. It never lifts a "wait" or a "refuse".
        """
        moment = _now(now)

        with self._store.transaction() as connection:
            assessment = self._assess(
                connection, route, unit, estimate, task_id,
                value, urgent, moment,
            )
            allowed = assessment.verdict == GO or (
                assessment.verdict == ASK and owner_agreed_to_exceed
            )

            if not allowed:
                return assessment

            event_id = connection.execute(
                """
                INSERT INTO usage_event (
                    route, task_id, unit, recorded_at, estimated, state
                )
                VALUES (?, ?, ?, ?, ?, 'reserved')
                """,
                (route, task_id, unit, _stamp(moment), estimate),
            ).lastrowid

        return Assessment(
            verdict=GO,
            reason=assessment.reason,
            limit=assessment.limit,
            over_share=assessment.over_share,
            near=assessment.near,
            event_id=event_id,
        )

    def settle(self, event_id, reported) -> None:
        """
        Replace an estimate with the amount the provider reported.
        """
        self._finish(event_id, "settled", reported)

    def mark_unknown(self, event_id) -> None:
        """
        The call's outcome is not known. It keeps counting at its
        full estimate.
        """
        self._finish(event_id, "unknown", None)

    def release(self, event_id) -> None:
        """
        The call was never sent, so it used nothing.
        """
        self._finish(event_id, "released", None)

    def _finish(self, event_id, state, reported):
        if state == "settled" and (reported is None or reported < 0):
            raise ValueError("A settled event needs the reported amount.")

        with self._store.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE usage_event
                SET state = ?, reported = ?
                WHERE event_id = ? AND state = 'reserved'
                """,
                (state, reported, event_id),
            ).rowcount

        if not changed:
            raise KeyError(f"Usage event {event_id} is not awaiting an outcome.")

    # Internals

    def _assess(
        self, connection, route, unit, estimate, task_id, value, urgent, now,
    ):
        if value not in VALUES:
            raise ValueError(f"Unknown task value: {value!r}")
        if estimate < 0:
            raise ValueError("An estimate cannot be negative.")

        statuses = self._statuses(connection, route, unit, task_id, now)

        if not statuses:
            # A route nobody has set a limit for is not a free route.
            return Assessment(
                REFUSE,
                f"No limit is recorded for {route}, so it cannot be used.",
            )

        worst = Assessment(GO, "Within every limit.")
        over_share = False
        near = []

        for status in statuses:
            verdict, reason, shared = self._judge(
                status, estimate, value, urgent,
            )
            over_share = over_share or shared

            if _SEVERITY[verdict] > _SEVERITY[worst.verdict]:
                worst = Assessment(verdict, reason, status)

            if (
                status.scope == SCOPE_PERIOD
                and status.capacity > 0
                and status.used + estimate
                >= self._warning_fraction * status.capacity
            ):
                near.append(status)

        return Assessment(
            worst.verdict,
            worst.reason,
            worst.limit,
            over_share=over_share,
            near=tuple(near),
        )

    @staticmethod
    def _judge(status, estimate, value, urgent):
        """
        One limit's verdict on the estimate: (verdict, reason, whether
        it goes past today's share).
        """
        label = f"the {status.name} limit on {status.route}"

        if status.capacity == 0:
            # Zero means off. A call estimated to cost nothing could
            # still be charged, so it is not let through either.
            return REFUSE, f"{label} is zero.", False

        if estimate > status.capacity and status.hard:
            # No reset can make this fit, so waiting would never end.
            return REFUSE, f"This is larger than {label} allows.", False

        if status.used + estimate > status.capacity:
            if status.scope != SCOPE_PERIOD:
                if status.hard:
                    return REFUSE, f"This would pass {label}.", False
                return ASK, f"This would pass {label}.", False
            if status.hard:
                return WAIT, f"This would pass {label} before it resets.", False
            return ASK, f"This would pass {label} before it resets.", False

        if not status.paced:
            return GO, "", False

        beyond_share = status.used_today + estimate > status.today_share

        if value == VALUE_LOW and not urgent:
            # Low-value work only uses allowance that is plentiful:
            # half of today's share at most.
            if status.used_today + estimate > status.today_share // 2:
                return WAIT, f"Low-value work is held back by {label}.", False
            return GO, "", False

        if beyond_share:
            # Whatever the work is worth and however urgent, going past
            # the day's share is the owner's to approve (owner decision,
            # 2026-10-11). Freya may suggest it; she may not decide it.
            return ASK, f"This would use more than today's share of {label}.", True

        return GO, "", False

    def _statuses(self, connection, route, unit, task_id, now):
        rows = connection.execute(
            """
            SELECT * FROM usage_limit
            WHERE route IN (?, ?)
            ORDER BY limit_id
            """,
            (route, ANY_ROUTE),
        ).fetchall()

        statuses = []

        for row in rows:
            if unit is not None and row["unit"] != unit:
                continue
            statuses.append(
                self._status(connection, row, route, task_id, now)
            )

        return statuses

    def _status(self, connection, row, route, task_id, now):
        # A limit on every route counts every route's use of its unit.
        routes = None if row["route"] == ANY_ROUTE else route

        # A reservation still open counts in every window asked about,
        # because the send it stands for is happening now. One left
        # open past STALE_RESERVATION_SECONDS belongs to a send that
        # died, and counts where it was recorded, like an unknown.
        stale = _stamp(now - timedelta(seconds=STALE_RESERVATION_SECONDS))

        def used(since=None, until=None, task=None):
            clauses, parameters = ["unit = ?"], [row["unit"]]

            if routes is not None:
                clauses.append("route = ?")
                parameters.append(routes)
            if since is not None:
                clauses.append(
                    "(recorded_at >= ? OR (state = 'reserved' AND recorded_at >= ?))"
                )
                parameters.extend([_stamp(since), stale])
            if until is not None:
                clauses.append(
                    "(recorded_at < ? AND (state != 'reserved' OR recorded_at < ?))"
                )
                parameters.extend([_stamp(until), stale])
            if task is not None:
                clauses.append("task_id = ?")
                parameters.append(task)

            return connection.execute(
                f"SELECT COALESCE(SUM({_COUNTED}), 0) FROM usage_event "
                f"WHERE {' AND '.join(clauses)}",
                parameters,
            ).fetchone()[0]

        common = dict(
            route=row["route"],
            name=row["name"],
            scope=row["scope"],
            unit=row["unit"],
            capacity=row["capacity"],
            hard=bool(row["hard"]),
            source=row["source"],
        )

        if row["scope"] == SCOPE_CALL:
            return LimitStatus(used=0, **common)

        if row["scope"] == SCOPE_TASK:
            return LimitStatus(
                used=used(task=task_id) if task_id is not None else 0,
                **common,
            )

        period = timedelta(seconds=row["period_seconds"])
        resets_at = _parse(row["resets_at"])

        start = _parse(row["period_start"])

        if now >= resets_at:
            # Until the provider says otherwise, the next period
            # follows straight on from the last, at the stated length.
            start = resets_at + ((now - resets_at) // period) * period
            resets_at = start + period
        total = used(since=start)

        exhausted = row["exhausted_until"]
        if exhausted is not None and now < _parse(exhausted):
            total = max(total, row["capacity"])

        if row["period_seconds"] <= DAY_SECONDS:
            return LimitStatus(used=total, resets_at=resets_at, **common)

        days_left = max(
            1,
            math.ceil((resets_at - now).total_seconds() / DAY_SECONDS),
        )
        day_start = max(start, resets_at - timedelta(days=days_left))
        used_before_today = used(since=start, until=day_start)
        used_today = total - used_before_today

        return LimitStatus(
            used=total,
            resets_at=resets_at,
            paced=True,
            today_share=max(0, row["capacity"] - used_before_today) // days_left,
            used_today=used_today,
            next_share_at=day_start + timedelta(days=1),
            **common,
        )
