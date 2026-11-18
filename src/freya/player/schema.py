"""
The one action schema every game adapter speaks (NEW-EI, NEW-EJ).

A plan is a name from a closed list plus the arguments that name
declares. Each argument is a bounded non-empty piece of text, the same
rule ModelToolHost uses, and an extra field or a string of code is
refused. The refusal's wording is fixed and does not contain the plan.

An observation is a fixed set of fields. A game that does not have one
omits it. The encoded observation stays under the tool-result clip so
a clipped copy cannot hide a field: an oversized observation is
refused, not shortened.
"""

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from types import MappingProxyType


# Tool results are clipped at 2,400 bytes. Stay under that.
MAX_OBSERVATION_BYTES = 2_200

# M1's list is the list. A later milestone may add names. A free-text
# command is not one of them.
ACTIONS = {
    "follow": {"target": 80},
    "stop": {},
    "look_at": {"target": 80},
    "collect": {"item": 80},
    "chat": {"text": 200},
    "drop": {"item": 80},
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
    "inventory",
    "threats",
    "chat",
    "chat_trust",
})

_PLAN_FIELDS = frozenset({"action", "arguments"})
_GOAL_FIELDS = frozenset({"goal", "target", "item", "text"})

_PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.,'!?-]*\Z")
_ADAPTER_ID = re.compile(r"[a-z][a-z0-9-]{0,39}\Z")
_CODE_WORD = re.compile(
    r"(?i)(?:^|\s)(?:import|def|lambda|function|exec|eval|subprocess)(?:\s|\Z)"
)

_MAX_LIST = 20


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


def parse_plan(raw) -> Action:
    """
    Read one plan and refuse anything but the fixed form.

    A string of code, an unknown name, and an extra field are refusals.
    Nothing in here starts a process.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise PlanRefused("The plan is not JSON.") from exc

    if not isinstance(raw, dict):
        raise PlanRefused("The plan is not a JSON object.")
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

    cleaned = {}
    for argument, limit in declared.items():
        cleaned[argument] = _clean_argument(argument, supplied.get(argument), limit)

    return Action(name, cleaned)


def parse_goal(raw) -> Goal:
    """Read a goal. The same closed list as an action, and no extra field."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise PlanRefused("The plan is not JSON.") from exc

    if not isinstance(raw, dict):
        raise PlanRefused("The plan is not a JSON object.")
    if set(raw) - _GOAL_FIELDS:
        raise PlanRefused("The plan has a field that is not allowed.")

    name = raw.get("goal")
    if name not in ACTIONS:
        raise PlanRefused("The plan names no known action.")

    declared = ACTIONS[name]
    present = set(raw) - {"goal"}
    if present - set(declared):
        raise PlanRefused("The plan has a field that is not allowed.")

    cleaned = {}
    for argument, limit in declared.items():
        cleaned[argument] = _clean_argument(argument, raw.get(argument), limit)

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

    if "position" in raw:
        position = raw["position"]
        if (
            not isinstance(position, dict)
            or set(position) != {"x", "y", "z"}
            or any(not _number(position[axis], -1_000_000, 1_000_000) for axis in ("x", "y", "z"))
        ):
            raise ObservationRefused("The observation is not the schema.")

    if "health" in raw and not _number(raw["health"], 0, 100):
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
