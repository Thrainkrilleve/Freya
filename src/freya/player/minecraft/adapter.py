"""
The Minecraft hands.

A schema action goes to the bridge. A goal becomes the next schema
action from `goals`. Reflexes are decided here, from the observation,
and they preempt the plan. Closing the adapter is the kill switch:
the child is terminated and later calls do not move it.

This module does not register a capability and it does not import Core.
"""

from freya.player.minecraft.bridge import start_bridge
from freya.player.minecraft.goals import next_step
from freya.player.minecraft.observe import summarize
from freya.player.minecraft.reflexes import ReflexMemory, decide_reflex
from freya.player.registry import AdapterSpec
from freya.player.schema import (
    ACTION_NAMES,
    GOAL_NAMES,
    STATUSES,
    ActionRefused,
    PlanRefused,
    parse_plan,
)


_SUMMARY = (
    "A private Minecraft world on this machine. "
    "The child connects to loopback only."
)


def register_minecraft(registry, *, bridge=None, target=None, environ=None, stderr=None) -> None:
    """Register the adapter. Passing `bridge` does not start a process."""

    def factory():
        return MinecraftAdapter(
            bridge=bridge, target=target, environ=environ, stderr=stderr,
        )

    registry.register(
        AdapterSpec("minecraft", frozenset(ACTION_NAMES), _SUMMARY),
        factory,
    )


class MinecraftAdapter:
    """One attachment. The bridge is started on the first reading."""

    adapter_id = "minecraft"

    def __init__(self, *, bridge=None, target=None, environ=None, stderr=None):
        self.actions = frozenset(ACTION_NAMES)
        self.bridge = bridge
        self._target = target
        self._environ = environ
        self._stderr = stderr
        self.status = "idle"
        self.finished = False
        self.closed = False
        self.issuer = ""
        self.memory = ReflexMemory()
        self._view = None
        self._spawned = False
        self._goal = ""
        self._attempt = 0

    def close(self) -> None:
        """End the child and refuse every later movement."""
        self.closed = True
        self.status = "paused"
        self.finished = False
        self.issuer = ""
        self._spawned = False
        self._goal = ""
        self._attempt = 0
        bridge = self.bridge
        if bridge is not None:
            bridge.stop()

    def accept(self, action) -> None:
        if self.closed:
            raise ActionRefused("Play has stopped.")
        try:
            parse_plan({"action": action.name, "arguments": dict(action.arguments)})
        except PlanRefused:
            raise
        if action.name not in self.actions:
            raise ActionRefused("That action is not one this adapter performs.")
        self.issuer = action.issuer
        self.finished = False

    def reflex(self, action, control) -> dict:
        if self.closed:
            self.status = "paused"
            self.finished = False
            return self.view(control)

        if action is None or not getattr(action, "issuer", "") or action.issuer != self.issuer:
            self.status = "paused"
            self.finished = False
            self._halt()
            return self._observe(control)

        if action.name not in self.actions:
            self.status = "refused"
            raise ActionRefused("That action is not one this adapter performs.")

        if not self._await_spawn(control):
            return self._view
        observation = self._observe(control)
        survival = decide_reflex(observation, self.memory)
        if survival is not None:
            self._send(survival)
            self.status = "acting"
            self.finished = False
            return self._observe(control)

        if action.name == "stop":
            self._halt()
            self.status = "paused"
            self.finished = True
            return self._observe(control)

        if action.name in GOAL_NAMES:
            if action.name != self._goal:
                self._goal = action.name
                self._attempt = 0
            step = next_step(action.name, observation, attempt=self._attempt)
            if step is None:
                self._attempt = 0
                self._halt()
                self.status = "idle"
                self.finished = True
                return self._observe(control)
            exploring = (
                step.name == "goto"
                and "block" not in step.arguments
                and "player" not in step.arguments
            )
            if exploring:
                self._attempt += 1
            else:
                self._attempt = 0
            self._send(step)
            self.status = "acting"
            self.finished = False
            return self._observe(control)

        self._send(action)
        self.status = "idle" if action.name != "follow" else "acting"
        self.finished = action.name != "follow"
        return self._observe(control)

    def view(self, control) -> dict:
        if self._view is None or self.closed:
            state = "paused" if self.closed else self.status
            return summarize(
                {}, adapter=self.adapter_id, control=control, status=state,
            )
        body = dict(self._view)
        body["control"] = control
        if self.status in STATUSES:
            body["status"] = self.status
        return summarize(body, adapter=self.adapter_id, control=control, status=body["status"])

    def _hands(self):
        if self.closed:
            raise ActionRefused("Play has stopped.")
        if self.bridge is None:
            self.bridge = start_bridge(
                self._target, environ=self._environ, stderr=self._stderr,
            )
            self.bridge.start()
        return self.bridge

    def _ensure_spawned(self) -> None:
        bridge = self._hands()
        if self._spawned:
            return
        waiter = getattr(bridge, "wait_until_spawned", None)
        if waiter is not None:
            waiter()
        self._spawned = True

    def _await_spawn(self, control) -> bool:
        """
        True once the child is in a world.

        The first action used to leave before spawn, and the bridge
        refused it. Waiting, including a timeout, is not a refusal:
        the next tick waits again. Stop still ends the wait.
        """
        try:
            self._ensure_spawned()
        except ActionRefused as exc:
            self._spawned = False
            if str(exc) == "The bot is not in a world." and not self.closed:
                self.status = "acting"
                self.finished = False
                self._view = summarize(
                    {}, adapter=self.adapter_id, control=control, status="acting",
                )
                return False
            self.status = "paused"
            self.finished = False
            self._view = summarize(
                {}, adapter=self.adapter_id, control=control, status="paused",
            )
            return False
        return True

    def _send(self, action) -> None:
        attempts = 0
        while True:
            try:
                self._ensure_spawned()
                self._hands().act(action.name, dict(action.arguments))
                return
            except ActionRefused as exc:
                attempts += 1
                if (
                    str(exc) == "The bot is not in a world."
                    and attempts < 2
                    and not self.closed
                ):
                    self._spawned = False
                    continue
                self.status = "refused"
                raise

    def _halt(self) -> None:
        if self.bridge is None or self.closed:
            return
        try:
            self.bridge.halt()
        except ActionRefused:
            pass

    def _observe(self, control) -> dict:
        if self.closed or self.bridge is None:
            try:
                self._hands()
            except ActionRefused:
                self.status = "paused"
                self._view = summarize(
                    {}, adapter=self.adapter_id, control=control, status="paused",
                )
                return self._view
        try:
            raw = self.bridge.observe()
        except ActionRefused:
            self.status = "paused"
            self._view = summarize(
                {}, adapter=self.adapter_id, control=control, status="paused",
            )
            return self._view
        if not isinstance(raw, dict):
            raw = {}
        reported = raw.get("status")
        if reported in STATUSES and self.status not in {"refused", "paused"}:
            self.status = reported
        self._view = summarize(
            raw, adapter=self.adapter_id, control=control, status=self.status,
        )
        return self._view
