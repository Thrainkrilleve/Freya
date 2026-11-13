import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from freya.core.usage import GO, VALUE_NORMAL, UsageLedger
from freya.delegation.compose import ComposedBrief, assemble_brief
from freya.delegation.material import CloudSettings, review_brief
from freya.delegation.screening import (
    MATCH_ALWAYS_PRIVATE,
    MATCH_UNAVAILABLE,
    Corrections,
    ScreeningResult,
    screen_brief,
)


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
SENT_BRIEFS_PATH = DATA_DIR / "sent_briefs.jsonl"

# How long an authorization stays good. It is meant to be used at once.
AUTHORIZATION_SECONDS = 120
# What a reply may add to the estimate, in the route's unit.
REPLY_ALLOWANCE = 4096


def brief_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """A deliberately high count: every byte a token, plus the reply."""
    return len(text.encode("utf-8")) + REPLY_ALLOWANCE


@dataclass(frozen=True)
class Route:
    """
    One way of reaching one model. `ask` sends a brief and returns
    something with `text` and `tokens`; when it fails, the exception's
    `sent` attribute says whether the brief may have left.
    """

    route_id: str
    provider: str
    ask: Callable[[str], object]
    unit: str = "tokens"
    estimate: Callable[[str], int] = estimate_tokens
    source_name: str = ""


class DispatchRefused(Exception):
    """
    A brief was not authorized or not sent. `review`, `screening` and
    `assessment` carry whichever check said no, for the owner's prompt.
    """

    def __init__(self, reason, *, review=None, screening=None, assessment=None):
        super().__init__(reason)
        self.reason = reason
        self.review = review
        self.screening = screening
        self.assessment = assessment


@dataclass(frozen=True)
class Authorization:
    """
    Proof that one exact brief passed every check for one route. It
    names the brief by its hash, so no other text can be sent with it.
    """

    brief_hash: str
    route_id: str
    provider: str
    settings_revision: int
    event_id: int
    issued: float
    serial: int
    hold_released: bool = False


class SentBriefs:
    """
    The owner's own record of every brief that left this machine, or
    may have. It is personal data and stays here; it is not the audit
    log, which never holds a brief's text.
    """

    def __init__(self, path=SENT_BRIEFS_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, provider, route_id, text, outcome, at=None) -> None:
        moment = at or datetime.now(timezone.utc)
        line = json.dumps({
            "at": moment.isoformat(),
            "provider": provider,
            "route": route_id,
            "outcome": outcome,
            "text": text,
        }, ensure_ascii=False)

        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as output:
                output.write(line + "\n")
                output.flush()
                os.fsync(output.fileno())

    def entries(self) -> List[dict]:
        """
        The record, oldest first. A line that cannot be read (a write
        cut short by a power loss leaves a partial last line) is left
        out rather than hiding the whole record.
        """
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []

        found = []

        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                found.append(entry)

        return found


class Dispatcher:
    """
    The one place a brief can leave from.

    Sending takes two steps. `authorize` checks a composed brief
    again from its parts, under the owner's present settings, and
    reserves its usage. `send` accepts only an authorization made
    here, for the same text, once, soon, and only if the owner's
    settings are unchanged and the brief still passes screening. A
    route's own `ask` is never called from anywhere else, and a route
    enforces that by taking the send's one permit (`take_permit`).

    The value of a task and whether it is urgent come from the caller
    acting for the owner, never from the model's plan.
    """

    def __init__(
        self,
        settings: CloudSettings,
        ledger: UsageLedger,
        routes: Iterable[Route],
        memories: Callable[[], Iterable[Tuple[str, str]]],
        sent: SentBriefs,
        corrections: Optional[Corrections] = None,
        closeness=None,
        clock: Callable[[], float] = time.monotonic,
        on_reply: Optional[Callable[[Route, object], None]] = None,
        held: Optional[Callable[[str], Optional[str]]] = None,
    ):
        # Says why a provider cannot be asked just now (its own notice
        # that the plan's limit is reached), or None.
        self._held = held
        self._settings = settings
        self._ledger = ledger
        self._routes: Dict[str, Route] = {r.route_id: r for r in routes}
        self._memories = memories
        self._sent = sent
        self._corrections = corrections
        self._closeness = closeness
        self._clock = clock
        self._on_reply = on_reply
        self._lock = threading.Lock()
        self._serial = 0
        self._open: Dict[int, Authorization] = {}
        self._in_flight: Dict[str, int] = {}

    def screen(self, text: str) -> ScreeningResult:
        return screen_brief(
            text, self._memories(), self._corrections, self._closeness,
        )

    def in_flight(self, text: str) -> bool:
        """Whether a send of this exact text is waiting for its route."""
        with self._lock:
            return self._in_flight.get(brief_hash(text), 0) > 0

    def take_permit(self, text: str) -> bool:
        """
        Use up the one permit a send in progress holds for this exact
        text. A route's capability calls this before sending and
        refuses when it is False.

        Each authorized send carries exactly one permit, so each
        authorization leads to at most one call to the route, even if
        something else asks for the same text at the same moment.
        """
        digest = brief_hash(text)

        with self._lock:
            waiting = self._in_flight.get(digest, 0)

            if waiting <= 0:
                return False
            if waiting == 1:
                del self._in_flight[digest]
            else:
                self._in_flight[digest] = waiting - 1

            return True

    def _check_screening(self, text, hold_released):
        screening = self.screen(text)

        if not screening.held:
            return

        if any(match.kind == MATCH_UNAVAILABLE for match in screening.matches):
            raise DispatchRefused(
                "Screening corrections are unreadable; recover them before sending.",
                screening=screening,
            )

        fixed = any(
            match.kind == MATCH_ALWAYS_PRIVATE for match in screening.matches
        )

        if fixed or not hold_released:
            raise DispatchRefused(
                "The brief contains something marked always private."
                if fixed
                else "The brief matches something in your personal store.",
                screening=screening,
            )

    def _expire(self):
        """Give back the usage of authorizations nobody used in time."""
        now = self._clock()

        with self._lock:
            stale = [
                authorization
                for authorization in self._open.values()
                if now - authorization.issued > AUTHORIZATION_SECONDS
            ]
            for authorization in stale:
                del self._open[authorization.serial]

        for authorization in stale:
            self._release(authorization)

    def _release(self, authorization):
        try:
            self._ledger.release(authorization.event_id)
        except KeyError:
            pass

    def authorize(
        self,
        brief: ComposedBrief,
        route_id: str,
        task_id: str,
        *,
        value: str = VALUE_NORMAL,
        urgent: bool = False,
        owner_released_hold: bool = False,
        owner_agreed_to_exceed: bool = False,
        now=None,
    ) -> Authorization:
        """
        Check one composed brief for one route and reserve its usage.

        Nothing the brief says about itself is trusted: its parts are
        sorted again under the present settings, and its text must be
        exactly what those cleared parts assemble to.

        `value` and `urgent` are the owner's, passed by the caller.
        `owner_released_hold` is the owner answering "send it once
        anyway" to a held brief; it does not release anything marked
        always private.
        """
        self._expire()
        route = self._routes.get(route_id)

        if route is None:
            raise DispatchRefused("There is no such route.")
        if brief.provider != route.provider:
            raise DispatchRefused("The brief was composed for another provider.")
        if not self._settings.provider_enabled(route.provider):
            raise DispatchRefused(f"{route.provider} is turned off.")

        waiting = self._held(route.provider) if self._held is not None else None

        if waiting:
            raise DispatchRefused(waiting)

        revision = self._settings.revision
        review = review_brief(
            brief.parts, route.provider, self._settings, brief.approved_kinds,
        )

        if not review.ready or not review.send:
            raise DispatchRefused(
                "Some of the material still needs your permission.",
                review=review,
            )
        if brief.revision != revision or brief.text is None:
            raise DispatchRefused(
                "Your cloud settings changed; the brief must be composed again.",
                review=review,
            )
        if brief.text != assemble_brief(brief.instructions, review.send):
            raise DispatchRefused(
                "The brief is not what its cleared material makes.",
                review=review,
            )

        self._check_screening(brief.text, owner_released_hold)

        assessment = self._ledger.reserve(
            route.route_id,
            route.unit,
            route.estimate(brief.text),
            task_id,
            value=value,
            urgent=urgent,
            owner_agreed_to_exceed=owner_agreed_to_exceed,
            now=now,
        )

        if assessment.verdict != GO or assessment.event_id is None:
            raise DispatchRefused(assessment.reason, assessment=assessment)

        with self._lock:
            self._serial += 1
            authorization = Authorization(
                brief_hash=brief_hash(brief.text),
                route_id=route.route_id,
                provider=route.provider,
                settings_revision=revision,
                event_id=assessment.event_id,
                issued=self._clock(),
                serial=self._serial,
                hold_released=owner_released_hold,
            )
            self._open[authorization.serial] = authorization

        return authorization

    def send(self, authorization: Authorization, text: str):
        """
        Send the brief an authorization was made for, and nothing else.
        """
        with self._lock:
            known = self._open.get(getattr(authorization, "serial", None))

            # Compare before removing, so that a wrong copy cannot
            # cancel the real authorization.
            if known is None or known != authorization:
                raise DispatchRefused("That authorization is not valid.")

            del self._open[authorization.serial]

        route = self._routes[authorization.route_id]
        digest = brief_hash(text)

        def refuse(reason, **details):
            self._release(authorization)
            return DispatchRefused(reason, **details)

        if digest != authorization.brief_hash:
            raise refuse("The text is not the brief that was authorized.")
        if self._clock() - authorization.issued > AUTHORIZATION_SECONDS:
            raise refuse("The authorization is too old.")

        try:
            # Screened again: a memory or an always-private mark may
            # have been added since the brief was authorized.
            self._check_screening(text, authorization.hold_released)
        except DispatchRefused as held:
            raise refuse(held.reason, screening=held.screening)

        # A provider may have said its limit is reached since this
        # brief was authorized, when another brief was sent.
        waiting = self._held(route.provider) if self._held is not None else None

        if waiting:
            raise refuse(waiting)

        with self._lock:
            # The last look at the owner's settings and the mark that
            # this text is going are made together.
            if self._settings.revision != authorization.settings_revision:
                changed = "Your cloud settings changed; the brief must be checked again."
            elif not self._settings.provider_enabled(route.provider):
                changed = f"{route.provider} is turned off."
            else:
                changed = None
                self._in_flight[digest] = self._in_flight.get(digest, 0) + 1

        if changed:
            raise refuse(changed)

        try:
            reply = route.ask(text)
        except Exception as exc:
            if getattr(exc, "sent", True):
                # It may have left, and what it used is not known. The
                # owner's record comes first, but a record that cannot
                # be written (a full disk) must not leave the
                # reservation open, so the ledger is settled either way.
                try:
                    self._sent.record(
                        route.provider, route.route_id, text, "outcome unknown",
                    )
                finally:
                    self._ledger.mark_unknown(authorization.event_id)
            else:
                self._release(authorization)
            raise
        finally:
            # A route that never asked for its permit leaves it
            # behind; it must not stay for someone else to use.
            self.take_permit(text)

        # The owner's record comes first: it must exist even if the
        # accounting below goes wrong, and the accounting happens even
        # if the record cannot be written.
        try:
            self._sent.record(route.provider, route.route_id, text, "answered")
        finally:
            used = getattr(reply, "tokens", None)

            if isinstance(used, int) and not isinstance(used, bool) and used > 0:
                self._ledger.settle(authorization.event_id, used)
            else:
                # No usable figure came back, so the estimate stands.
                self._ledger.mark_unknown(authorization.event_id)

        if self._on_reply is not None:
            try:
                self._on_reply(route, reply)
            except Exception:
                # Display failure cannot turn an already completed send into
                # a retry or change its accounting. Never log reply content.
                logging.getLogger(__name__).warning("The worker response could not be displayed.")

        return reply
