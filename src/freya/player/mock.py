"""
A deterministic game that speaks the schema.

There is no network, no child process, and no clock of its own. One
call of `reflex` is one step. Follow and collect finish after a fixed
number of steps. An unknown action is refused. Reflexes live here: a
plan that is missing, late, or from an issuer this adapter did not
accept does not move it.
"""

from freya.player.registry import AdapterSpec
from freya.player.schema import (
    ACTION_NAMES,
    M0_ACTIONS,
    ActionRefused,
    check_observation,
)


# The mock's places. Tests walk these exact distances.
OWNER_AT = (3, 0, 0)
WOOD_AT = (2, 0, 0)


def register_mock(registry, actions=None) -> None:
    """Register the mock. `actions` narrows what this copy may perform."""
    permitted = frozenset(actions if actions is not None else M0_ACTIONS)
    registry.register(
        AdapterSpec(
            "mock",
            permitted,
            "A deterministic mock game. It speaks the schema and nothing else.",
        ),
        lambda: MockGame(actions=permitted),
    )


class MockGame:
    """A small grid. The same start always takes the same steps."""

    adapter_id = "mock"

    def __init__(self, actions=None):
        self.actions = frozenset(actions if actions is not None else M0_ACTIONS)
        self.x = 0
        self.y = 0
        self.z = 0
        self.health = 20
        self.inventory = []
        self.threats = []
        self.chat = []
        self.facing = ""
        self.status = "idle"
        self.finished = False
        self.issuer = ""
        self.closed = False
        self.places = {"owner": OWNER_AT}
        self.items = {"wood": WOOD_AT}

    def close(self) -> None:
        """The kill switch calls this. The mock has no process to end."""
        self.closed = True
        self.status = "paused"
        self.issuer = ""
        self.finished = False

    def accept(self, action) -> None:
        """Remember who issued a plan. This does not move."""
        self._known(action)
        self.issuer = action.issuer
        self.finished = False
        self.closed = False

    def reflex(self, action, control) -> dict:
        """
        One tick of the adapter's own reflexes.

        `action` is the plan Freya's code already parsed, or None when
        the plan is missing. This method does not call a model.
        """
        if self.closed:
            self.status = "paused"
            self.finished = False
            return self.view(control)

        if action is None:
            self.status = "paused"
            return self.view(control)

        if action.name not in ACTION_NAMES or action.name not in self.actions:
            self.status = "refused"
            raise ActionRefused("That action is not one this adapter performs.")

        if not action.issuer or action.issuer != self.issuer:
            self.status = "paused"
            self.finished = False
            return self.view(control)

        self._step(action)
        return self.view(control)

    def view(self, control) -> dict:
        observation = {
            "adapter": self.adapter_id,
            "status": self.status,
            "control": control,
            "position": {"x": self.x, "y": self.y, "z": self.z},
            "health": self.health,
            "inventory": [{"item": item, "count": 1} for item in self.inventory],
            "threats": list(self.threats),
            "chat": list(self.chat),
            "chat_trust": "untrusted",
        }
        return check_observation(observation)

    def _known(self, action) -> None:
        if action.name not in ACTION_NAMES or action.name not in self.actions:
            raise ActionRefused("That action is not one this adapter performs.")

    def _step(self, action) -> None:
        name = action.name
        if name == "stop":
            self.status = "paused"
            self.finished = True
            return
        if name == "follow":
            self._follow(action.arguments["target"])
            return
        if name == "collect":
            self._collect(action.arguments["item"])
            return
        if name == "look_at":
            target = action.arguments["target"]
            if target not in self.places:
                self.status = "stuck"
                self.finished = False
                return
            self.facing = target
            self.status = "idle"
            self.finished = True
            return
        if name == "chat":
            self.chat = [*self.chat, action.arguments["text"]][-20:]
            self.status = "idle"
            self.finished = True
            return
        if name == "drop":
            item = action.arguments["item"]
            if item not in self.inventory:
                self.status = "stuck"
                self.finished = False
                return
            self.inventory = [held for held in self.inventory if held != item]
            self.status = "idle"
            self.finished = True
            return
        self.status = "refused"
        raise ActionRefused("That action is not one this adapter performs.")

    def _follow(self, target) -> None:
        place = self.places.get(target)
        if place is None:
            self.status = "stuck"
            self.finished = False
            return
        if self._at(place):
            self.status = "idle"
            self.finished = True
            return
        self._move_toward(place)
        arrived = self._at(place)
        self.status = "idle" if arrived else "acting"
        self.finished = arrived

    def _collect(self, item) -> None:
        if item in self.inventory:
            self.status = "idle"
            self.finished = True
            return
        place = self.items.get(item)
        if place is None:
            self.status = "stuck"
            self.finished = False
            return
        if not self._at(place):
            self._move_toward(place)
        if self._at(place):
            self.inventory = [*self.inventory, item]
            self.status = "idle"
            self.finished = True
            return
        self.status = "acting"
        self.finished = False

    def _at(self, place) -> bool:
        return (self.x, self.y, self.z) == tuple(place)

    def _move_toward(self, place) -> None:
        x, y, z = place
        if self.x != x:
            self.x += 1 if x > self.x else -1
        elif self.y != y:
            self.y += 1 if y > self.y else -1
        elif self.z != z:
            self.z += 1 if z > self.z else -1
