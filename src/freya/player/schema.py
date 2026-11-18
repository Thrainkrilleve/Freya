"""
The one action schema every game adapter speaks (NEW-EI, NEW-EJ).

A plan is a name from a closed list plus the arguments that name
declares. Text arguments are bounded and non-empty, the same rule
ModelToolHost uses. Counts and coordinates are bounded numbers.
An extra field or a string of code is refused. The refusal's wording
is fixed and does not contain the plan.

An observation is a fixed set of fields. A game that does not have one
omits it. The encoded observation stays under the tool-result clip so
a clipped copy cannot hide a field: an oversized observation is
refused, not shortened. Summarising, which drops far-away entries
before the check, is the adapter's job.
"""

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType


# Tool results are clipped at 2,400 bytes. Stay under that.
MAX_OBSERVATION_BYTES = 2_200

# The original six. The mock performs these and does not grow when the
# list does. A later adapter declares what it can perform.
M0_ACTIONS = frozenset({
    "follow", "stop", "look_at", "collect", "chat", "drop",
})

# Names a small planner can pick. The adapter turns one into actions.
# A goal is not a program and it has no prose field.
GOAL_NAMES = frozenset({
    "build_shelter", "get_iron", "get_wood", "get_food",
})


@dataclass(frozen=True)
class Arg:
    """One declared argument. `kind` is text, item, enum, int, or number."""

    kind: str
    limit: int = 80
    low: float = 0
    high: float = 0
    required: bool = True
    choices: tuple = ()


def _text(limit, required=True):
    return Arg("text", limit=limit, required=required)


def _item(required=True):
    return Arg("item", limit=80, required=required)


def _enum(choices, required=True):
    return Arg("enum", choices=tuple(choices), required=required)


def _int(low, high, required=True):
    return Arg("int", low=low, high=high, required=required)


def _num(low, high, required=True):
    return Arg("number", low=low, high=high, required=required)


# World height for a block action. Entity positions in an observation
# use the wider position range, because a reading is not a request.
_Y_LOW = -64
_Y_HIGH = 320
_XZ = 30_000_000

_X = _num(-_XZ, _XZ, required=False)
_Y = _num(_Y_LOW, _Y_HIGH, required=False)
_Z = _num(-_XZ, _XZ, required=False)

_SLOTS = ("hand", "offhand", "head", "torso", "legs", "feet")

# Closed list. A free-text command is not one of them. `mine` is the
# same request as `dig`. Goals have no arguments.
ACTIONS = {
    "goto": {
        "x": _X, "y": _Y, "z": _Z,
        "block": _item(required=False),
        "player": _text(16, required=False),
    },
    "follow": {"target": 80},
    "stop": {},
    "look_at": {"target": 80},
    "jump": {},
    "sprint": {"state": _enum(("on", "off"))},
    "sneak": {"state": _enum(("on", "off"))},
    "swim": {
        "direction": _enum(("up", "down", "forward")),
        "ticks": _int(1, 40, required=False),
    },
    "strafe": {
        "direction": _enum(("left", "right", "forward", "back")),
        "ticks": _int(1, 40, required=False),
    },
    "dig": {
        "block": _item(required=False),
        "x": _X, "y": _Y, "z": _Z,
    },
    "mine": {
        "block": _item(required=False),
        "x": _X, "y": _Y, "z": _Z,
    },
    "place": {
        "block": _item(),
        "x": _X, "y": _Y, "z": _Z,
    },
    "collect": {"item": _item()},
    "craft": {"item": _item(), "count": _int(1, 64, required=False)},
    "smelt": {
        "item": _item(),
        "fuel": _item(required=False),
        "count": _int(1, 64, required=False),
    },
    "equip": {"item": _item(), "slot": _enum(_SLOTS)},
    "unequip": {"slot": _enum(_SLOTS)},
    "use": {"item": _item(required=False)},
    "eat": {"item": _item(required=False)},
    "attack": {"target": 40},
    "defend": {"target": 40},
    "open_chest": {"x": _X, "y": _Y, "z": _Z},
    "deposit": {"item": _item(), "count": _int(1, 64, required=False)},
    "withdraw": {"item": _item(), "count": _int(1, 64, required=False)},
    "drop": {"item": _item(), "count": _int(1, 64, required=False)},
    "toss": {"item": _item(), "count": _int(1, 64, required=False)},
    "sleep": {},
    "chat": {"text": 200},
    "build_shelter": {},
    "get_iron": {},
    "get_wood": {},
    "get_food": {},
}

ACTION_NAMES = frozenset(ACTIONS)

STATUSES = frozenset({
    "idle", "acting", "stuck", "paused", "refused",
})

# What an observation may contain. Anything else is refused.
OBSERVATION_FIELDS = frozenset({
    "adapter",
    "status",
    "control",
    "position",
    "health",
    "food",
    "time_of_day",
    "held",
    "inventory",
    "blocks",
    "entities",
    "threats",
    "chat",
    "chat_trust",
})

ENTITY_KINDS = frozenset({"hostile", "passive", "player"})

_PLAN_FIELDS = frozenset({"action", "arguments"})

_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,'!?_-]*\Z")
_ITEM = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_:-]{0,79}\Z")
_ADAPTER_ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
_CODE_WORD = re.compile(
    r"(?i)(?:^|\s)(?:import|def|lambda|function|exec|eval|subprocess)(?:\s|\Z)"
)

_MAX_LIST = 20
_MAX_BLOCKS = 8
_MAX_ENTITIES = 6


class PlayError(Exception):
    """
    A play request was refused. The message is fixed wording, safe to
    show and to record. It never contains the plan or an observation.
    """


class PlanRefused(PlayError):
    """The plan is not a schema action."""


class ObservationRefused(PlayError):
    """The observation is not the schema, or it is too large."""


class ActionRefused(PlayError):
    """This adapter will not perform that action."""


class PermissionRefused(PlayError):
    """The adapter may not attach to that target."""


class ModelRefused(PlayError):
    """The play model is not one this session will use."""


def plain_text(value, limit) -> bool:
    """True when `value` is short plain text an observation may carry."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and _PLAIN.match(value) is not None
    )


def item_name(value) -> bool:
    """True for a block or item id, with or without a namespace."""
    return (
        isinstance(value, str)
        and _ITEM.match(value) is not None
        and _CODE_WORD.search(value) is None
        and not _names_eve(value)
        and not _is_public_address(value)
    )


def _is_program(value) -> bool:
    """True when a string would have to be evaluated to mean anything."""
    if not isinstance(value, str):
        return True
    if any(mark in value for mark in ("\n", "\r", "\x00", "(", ")", "{", "}", ";", "=", "`", "\\")):
        return True
    if _CODE_WORD.search(value):
        return True
    return not _PLAIN.match(value)


def _clean_argument(name, value, limit) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanRefused("An argument is missing.")
    if _is_program(value):
        raise PlanRefused("The plan is not a schema action.")
    value = value.strip()
    if len(value) > limit:
        raise PlanRefused("An argument is too long.")
    if _names_eve(value):
        raise PlanRefused("That game is out of scope.")
    if _is_public_address(value):
        raise PlanRefused("That target is not on this machine.")
    return value


def _names_eve(text) -> bool:
    """True for the game that is out of scope, not for ordinary words."""
    if not isinstance(text, str):
        return False
    folded = " ".join(text.casefold().replace("-", " ").replace("_", " ").split())
    padded = f" {folded} "
    return folded == "eve" or " eve " in padded


def _is_address(text) -> bool:
    """A host, an IP, or a URL. A short label, and a sentence, are not."""
    if not isinstance(text, str) or not text or any(mark in text for mark in (" ", "\n", "\t")):
        return False
    if "://" in text or "/" in text:
        return True
    if text.count(":") == 1 and text.rsplit(":", 1)[-1].isdigit():
        return True
    if text.startswith("[") and "]" in text:
        return True
    try:
        ipaddress_address(text)
        return True
    except ValueError:
        pass
    parts = text.split(".")
    if len(parts) < 2:
        return False
    return all(re.fullmatch(r"[A-Za-z0-9-]+", part) for part in parts)


def _is_loopback(text) -> bool:
    if text.casefold() == "localhost":
        return True
    host = text
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif host.count(":") == 1 and host.rsplit(":", 1)[-1].isdigit():
        host = host.rsplit(":", 1)[0]
    if host.casefold() == "localhost":
        return True
    try:
        return bool(ipaddress_address(host).is_loopback)
    except ValueError:
        return False


def _is_public_address(text) -> bool:
    return _is_address(text) and not _is_loopback(text)


def ipaddress_address(text):
    return ipaddress.ip_address(text)


def _as_arg(spec) -> Arg:
    if isinstance(spec, Arg):
        return spec
    if isinstance(spec, int):
        return Arg("text", limit=spec, required=True)
    raise PlanRefused("The plan is not a schema action.")


def _clean_value(value, arg: Arg):
    if arg.kind == "text":
        return _clean_argument("argument", value, arg.limit)
    if arg.kind == "item":
        if not isinstance(value, str) or not value.strip():
            raise PlanRefused("An argument is missing.")
        value = value.strip()
        if not item_name(value) or len(value) > arg.limit:
            raise PlanRefused("The plan is not a schema action.")
        return value
    if arg.kind == "enum":
        if not isinstance(value, str) or value.strip() not in arg.choices:
            raise PlanRefused("The plan is not a schema action.")
        return value.strip()
    if arg.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise PlanRefused("The plan is not a schema action.")
        if not arg.low <= value <= arg.high:
            raise PlanRefused("An argument is out of range.")
        return value
    if arg.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PlanRefused("The plan is not a schema action.")
        if not arg.low <= value <= arg.high:
            raise PlanRefused("An argument is out of range.")
        return value
    raise PlanRefused("The plan is not a schema action.")


def _take_arguments(declared, source) -> dict:
    if not isinstance(source, dict):
        raise PlanRefused("The plan is not a schema action.")
    cleaned = {}
    for name, spec in declared.items():
        arg = _as_arg(spec)
        if name not in source or source[name] is None or source[name] == "":
            if arg.required:
                raise PlanRefused("An argument is missing.")
            continue
        cleaned[name] = _clean_value(source[name], arg)
    return cleaned


def _coords_partial(cleaned) -> bool:
    present = [key for key in ("x", "y", "z") if key in cleaned]
    return bool(present) and len(present) != 3


def _check_shape(name, cleaned) -> None:
    """Refuse a mix of targets the action did not declare as one mode."""
    if name == "goto":
        if _coords_partial(cleaned):
            raise PlanRefused("The plan is not a schema action.")
        modes = 0
        if all(key in cleaned for key in ("x", "y", "z")):
            modes += 1
        if "block" in cleaned:
            modes += 1
        if "player" in cleaned:
            modes += 1
        if modes != 1:
            raise PlanRefused("The plan is not a schema action.")
        return
    if name in {"dig", "mine"}:
        if _coords_partial(cleaned):
            raise PlanRefused("The plan is not a schema action.")
        if not any(key in cleaned for key in ("x", "y", "z", "block")):
            raise PlanRefused("An argument is missing.")
        return
    if name in {"place", "open_chest"} and _coords_partial(cleaned):
        raise PlanRefused("The plan is not a schema action.")


@dataclass(frozen=True)
class Action:
    """One schema action. `issuer` is set by Freya's code, never by the plan."""

    name: str
    arguments: object
    issuer: str = ""

    def __post_init__(self):
        object.__setattr__(
            self, "arguments", MappingProxyType(dict(self.arguments)),
        )

    def with_issuer(self, issuer: str) -> "Action":
        if not isinstance(issuer, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,40}", issuer):
            raise PlanRefused("The plan has no issuer.")
        return Action(self.name, dict(self.arguments), issuer)


@dataclass(frozen=True)
class Goal:
    """What the owner asked for, as a closed record. It holds no prose field."""

    name: str
    arguments: object

    def __post_init__(self):
        object.__setattr__(
            self, "arguments", MappingProxyType(dict(self.arguments)),
        )

    def as_dict(self) -> dict:
        return {"goal": self.name, **dict(self.arguments)}


def _as_object(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise PlanRefused("The plan is not JSON.") from exc
    if not isinstance(raw, dict):
        raise PlanRefused("The plan is not a JSON object.")
    return raw


def parse_plan(raw) -> Action:
    """
    Read one plan and refuse anything but the fixed form.

    A string of code, an unknown name, and an extra field are refusals.
    Nothing in here starts a process.
    """
    raw = _as_object(raw)
    if set(raw) - _PLAN_FIELDS:
        raise PlanRefused("The plan has a field that is not allowed.")

    name = raw.get("action")
    if name not in ACTIONS:
        raise PlanRefused("The plan names no known action.")

    supplied = raw.get("arguments", {})
    if not isinstance(supplied, dict):
        raise PlanRefused("The plan is not a schema action.")
    declared = ACTIONS[name]
    if set(supplied) - set(declared):
        raise PlanRefused("The plan has a field that is not allowed.")

    cleaned = _take_arguments(declared, supplied)
    _check_shape(name, cleaned)
    return Action(name, cleaned)


def parse_goal(raw) -> Goal:
    """Read a goal. The same closed list as an action, and no extra field."""
    raw = _as_object(raw)
    name = raw.get("goal")
    if name not in ACTIONS:
        raise PlanRefused("The plan names no known action.")

    declared = ACTIONS[name]
    present = set(raw) - {"goal"}
    if present - set(declared):
        raise PlanRefused("The plan has a field that is not allowed.")

    cleaned = _take_arguments(declared, raw)
    _check_shape(name, cleaned)
    return Goal(name, cleaned)


def _number(value, low, high) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    return low <= value <= high


def _plain_list(value, limit) -> bool:
    if not isinstance(value, list) or len(value) > _MAX_LIST:
        return False
    return all(isinstance(item, str) and _PLAIN.match(item) and len(item) <= limit for item in value)


def _point(value, fields) -> bool:
    if not isinstance(value, dict) or set(value) != fields:
        return False
    return all(_number(value[axis], -1_000_000, 1_000_000) for axis in ("x", "y", "z"))


def check_observation(raw) -> dict:
    """
    Return a copy of one observation, or refuse it.

    Fields a game does not have may be omitted. `status`, `control`,
    and `adapter` may not. An unknown field is refused, and so is an
    encoded observation over the byte cap.
    """
    from freya.player.indicator import CONTROL_LABELS

    if not isinstance(raw, dict):
        raise ObservationRefused("The observation is not the schema.")
    if set(raw) - OBSERVATION_FIELDS:
        raise ObservationRefused("The observation is not the schema.")

    for required in ("adapter", "status", "control"):
        if required not in raw:
            raise ObservationRefused("The observation is not the schema.")

    if not isinstance(raw["adapter"], str) or not _ADAPTER_ID.match(raw["adapter"]):
        raise ObservationRefused("The observation is not the schema.")
    if raw["status"] not in STATUSES:
        raise ObservationRefused("The observation is not the schema.")
    if raw["control"] not in CONTROL_LABELS:
        raise ObservationRefused("The observation is not the schema.")

    if "position" in raw and not _point(raw["position"], {"x", "y", "z"}):
        raise ObservationRefused("The observation is not the schema.")

    if "health" in raw and not _number(raw["health"], 0, 100):
        raise ObservationRefused("The observation is not the schema.")

    if "food" in raw and not _number(raw["food"], 0, 20):
        raise ObservationRefused("The observation is not the schema.")

    if "time_of_day" in raw:
        clock = raw["time_of_day"]
        if isinstance(clock, bool) or not isinstance(clock, int) or not 0 <= clock <= 24000:
            raise ObservationRefused("The observation is not the schema.")

    if "held" in raw and not item_name(raw["held"]):
        raise ObservationRefused("The observation is not the schema.")

    if "inventory" in raw:
        items = raw["inventory"]
        if not isinstance(items, list) or len(items) > _MAX_LIST:
            raise ObservationRefused("The observation has too many entries.")
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item) != {"item", "count"}
                or not isinstance(item["item"], str)
                or not _PLAIN.match(item["item"])
                or isinstance(item["count"], bool)
                or not isinstance(item["count"], int)
                or not 1 <= item["count"] <= 999
            ):
                raise ObservationRefused("The observation is not the schema.")

    if "blocks" in raw:
        blocks = raw["blocks"]
        if not isinstance(blocks, list) or len(blocks) > _MAX_BLOCKS:
            raise ObservationRefused("The observation has too many entries.")
        for block in blocks:
            if (
                not _point(block, {"name", "x", "y", "z"})
                or not item_name(block["name"])
            ):
                raise ObservationRefused("The observation is not the schema.")

    if "entities" in raw:
        entities = raw["entities"]
        if not isinstance(entities, list) or len(entities) > _MAX_ENTITIES:
            raise ObservationRefused("The observation has too many entries.")
        for entity in entities:
            if (
                not _point(entity, {"name", "x", "y", "z", "kind"})
                or not item_name(entity["name"])
                or entity["kind"] not in ENTITY_KINDS
            ):
                raise ObservationRefused("The observation is not the schema.")

    if "threats" in raw and not _plain_list(raw["threats"], 80):
        raise ObservationRefused("The observation is not the schema.")

    if "chat" in raw:
        if not _plain_list(raw["chat"], 200):
            raise ObservationRefused("The observation is not the schema.")
        # Chat is untrusted content. The label is fixed, not taken from the game.
        if raw.get("chat_trust") != "untrusted":
            raise ObservationRefused("The observation is not the schema.")
    elif "chat_trust" in raw:
        raise ObservationRefused("The observation is not the schema.")

    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_OBSERVATION_BYTES:
        raise ObservationRefused("The observation is too large.")

    return json.loads(encoded)


def observation_json(observation) -> str:
    """The observation as the planner sees it, already checked."""
    checked = check_observation(observation)
    return json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
