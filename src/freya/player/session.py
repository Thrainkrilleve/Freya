"""
The planner and the adapter, one step at a time.

The model is asked when a plan finishes, when the adapter is stuck, or
a few seconds have passed. It is not asked on every tick. A late,
refused, or missing plan leaves the adapter on stop. Follow continues
only while the current plan still says follow and the planner that
issued it is still that planner.

`stop` ends control without a second approval. A spoken request can
run that stop. A spoken request cannot start a session and cannot
approve a target. The listener is not wired here: M0 does not change
how the microphone is read.
"""

import re

from freya.player.audit import ActionLog, arguments_digest
from freya.player.indicator import IN_CONTROL, PAUSED
from freya.player.input_backend import MissingInputBackend
from freya.player.permissions import check_attachment
from freya.player.planner import (
    LATE_PLAN_SECONDS,
    PLAN_INTERVAL_SECONDS,
    Planner,
    local_planner_allowed,
    plan_messages,
    resolve_play_model,
)
from freya.player.schema import (
    ActionRefused,
    ModelRefused,
    ObservationRefused,
    PermissionRefused,
    PlanRefused,
    parse_goal,
    parse_plan,
)


_WORDS = re.compile(r"[A-Za-z']+")
_STOP_WORDS = frozenset({"stop", "pause"})
_START_WORDS = frozenset({"start", "play", "approve", "allow", "yes"})


def spoken_effect(text) -> str:
    """
    What a spoken play phrase is.

    ``stop`` runs the kill switch. ``refused`` is a request to start or
    to approve, which voice cannot do. ``none`` is anything else.
    """
    if not isinstance(text, str):
        return "none"
    words = _WORDS.findall(text.casefold())
    if words and words[0] == "freya":
        words = words[1:]
    if not words:
        return "none"
    if words[0] in _STOP_WORDS:
        return "stop"
    if words[0] in _START_WORDS:
        return "refused"
    return "none"


class PlaySession:
    """One attachment of one adapter. It starts only when the owner opens it."""

    def __init__(self, spec, adapter, planner, goal, inputs=None, log=None):
        self.spec = spec
        self.adapter = adapter
        self.planner = planner
        self.goal = goal
        self.inputs = inputs if inputs is not None else MissingInputBackend()
        self.log = log if log is not None else ActionLog()
        self.model = planner.model
        self._running = False
        self._plan = None
        self._waiting_since = None
        self._last_ask = None
        self._brain_gone = False
        self._asked_for_finish = False
        self._asked_for_stuck = False

    @property
    def label(self) -> str:
        """The fixed status-strip text, or paused when she cannot act."""
        if self.inputs.held():
            return IN_CONTROL
        if not self._running or self._brain_gone:
            return PAUSED
        return IN_CONTROL

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        self._running = True
        self._brain_gone = False

    def stop(self) -> None:
        """
        End control and release input. No second approval.

        Calling it again is the same result. A plan that arrives later
        is not applied.
        """
        self._running = False
        self._plan = None
        self._waiting_since = None
        self._brain_gone = False
        self.inputs.release_all()
        self.adapter.close()
        self.log.record(
            kind="stop",
            action="stop",
            outcome="stopped",
            reason="The owner stopped play.",
        )

    def handle_speech(self, text) -> str:
        """
        Apply a spoken play phrase.

        Stop runs `stop`. A request to start or approve is recorded and
        does not start a session and does not change a target.
        """
        effect = spoken_effect(text)
        if effect == "stop":
            self.stop()
            return "stop"
        if effect == "refused":
            self.log.record(
                kind="refusal",
                action="",
                outcome="refused",
                reason="A spoken request does not start play or approve a target.",
            )
            return "refused"
        return "none"

    def tick(self, now: float) -> dict:
        """
        One step. `now` is seconds on a clock the caller owns, so a
        test can move it without sleeping.
        """
        if not self._running:
            return self.adapter.reflex(None, self.label)
        self._resolve(now)
        plan = self._live_plan()
        try:
            return self.adapter.reflex(plan, self.label)
        except ActionRefused as exc:
            self._plan = None
            self.log.record(
                kind="refusal",
                action=getattr(plan, "name", ""),
                outcome="refused",
                reason=str(exc),
            )
            return self.adapter.view(self.label)

    def _live_plan(self):
        plan = self._plan
        if plan is None:
            return None
        if not self.planner.alive or plan.issuer != self.planner.issuer:
            self._plan = None
            self._brain_gone = True
            self.log.record(
                kind="refusal",
                action="stop",
                outcome="stopped",
                reason="The plan's issuer is gone.",
            )
            return None
        if plan.name not in self.spec.actions:
            self._plan = None
            return None
        return plan

    def _resolve(self, now) -> None:
        if self._brain_gone:
            return
        if self._waiting_since is not None:
            if now - self._waiting_since >= LATE_PLAN_SECONDS:
                self._waiting_since = None
                self._plan = None
                self._last_ask = now
                self.log.record(
                    kind="refusal",
                    action="stop",
                    outcome="stopped",
                    reason="The plan was late.",
                )
                return
            text = self.planner.poll()
            if text is None:
                return
            self._waiting_since = None
            self._take(text)
            return
        if not self._should_ask(now):
            return
        self._last_ask = now
        if self.adapter.finished:
            self._asked_for_finish = True
        if self.adapter.status == "stuck":
            self._asked_for_stuck = True
        text = self.planner.ask(self._prompt())
        if text is None:
            self._waiting_since = now
            return
        self._take(text)

    def _should_ask(self, now) -> bool:
        if self._last_ask is None:
            return True
        if now - self._last_ask < PLAN_INTERVAL_SECONDS:
            if (
                self.adapter.finished
                and self._plan is not None
                and not self._asked_for_finish
            ):
                return True
            if self.adapter.status == "stuck" and not self._asked_for_stuck:
                return True
            return False
        return True

    def _prompt(self):
        try:
            observation = self.adapter.view(self.label)
        except ObservationRefused:
            observation = {
                "adapter": self.adapter.adapter_id,
                "status": "paused",
                "control": self.label,
            }
        return plan_messages(self.goal, observation, self.spec.actions)

    def _take(self, text) -> None:
        try:
            action = parse_plan(text)
        except PlanRefused as exc:
            self._plan = None
            self.log.record(
                kind="refusal",
                action="",
                outcome="refused",
                reason=str(exc),
            )
            return
        if action.name not in self.spec.actions:
            self._plan = None
            self.log.record(
                kind="refusal",
                action=action.name,
                outcome="refused",
                reason="That action is not one this adapter performs.",
            )
            return
        try:
            issued = action.with_issuer(self.planner.issuer)
            self.adapter.accept(issued)
        except (PlanRefused, ActionRefused) as exc:
            self._plan = None
            self.log.record(
                kind="refusal",
                action=action.name,
                outcome="refused",
                reason=str(exc),
            )
            return
        self._plan = issued
        self._asked_for_finish = False
        self._brain_gone = False
        self.log.record(
            kind="action",
            action=issued.name,
            outcome="accepted",
            reason="The plan names a schema action.",
            arguments_digest=arguments_digest(issued.arguments),
        )


def open_session(
    registry,
    adapter_id,
    target,
    allow,
    brain,
    goal,
    *,
    issuer="play",
    environ=None,
    installed=None,
    inputs=None,
    log=None,
    graphics=None,
):
    """
    Attach one adapter, or refuse before anything is built.

    The model name is resolved first, then free graphics memory when
    the caller supplies a reading, then the allow-list, and only then
    is the adapter constructed. A refusal builds nothing. Omitting
    `graphics` does not load a model and does not probe a card: M1
    still does not load one. A session that does load a local planner
    passes `(free_bytes, estimate_bytes)` and an unknown figure refuses.
    """
    model = resolve_play_model(environ, installed)
    if graphics is not None:
        if not isinstance(graphics, tuple) or len(graphics) != 2:
            raise ModelRefused("Free graphics memory is unknown.")
        local_planner_allowed(graphics[0], graphics[1])
    check_attachment(allow, adapter_id, target)
    spec = registry.get(adapter_id)
    if spec is None:
        raise PermissionRefused("That adapter is not registered.")
    parsed = parse_goal(goal) if not hasattr(goal, "as_dict") else goal
    adapter = registry.create(adapter_id)
    planner = Planner(brain, model, issuer)
    session = PlaySession(
        spec, adapter, planner, parsed, inputs=inputs, log=log,
    )
    session.start()
    return session
