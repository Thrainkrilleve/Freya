"""
Survival reflexes for the Minecraft adapter.

These run in adapter code. The play model is not asked. Thresholds
here are the ones the Node program repeats: eat at food 6 or below,
flee at health 6 or below, attack inside 2.5 blocks, dodge inside 6,
and jump after four identical acting positions. Health and food are
the game's own points, 0 to 20.
"""

import math
from dataclasses import dataclass

from freya.player.schema import parse_plan


# Keep these numbers in step with adapters/minecraft/bot.js.
EAT_AT_OR_BELOW = 6
FLEE_AT_OR_BELOW = 6
ATTACK_WITHIN = 2.5
DODGE_WITHIN = 6.0
STUCK_REPEATS = 4
FLEE_BLOCKS = 8

FOOD_ITEMS = frozenset({
    "bread", "cooked_beef", "cooked_porkchop", "cooked_chicken",
    "cooked_mutton", "cooked_rabbit", "baked_potato", "apple",
    "carrot", "golden_carrot", "sweet_berries", "beetroot",
    "melon_slice", "cookie", "pumpkin_pie", "beef", "porkchop",
    "chicken", "mutton", "rabbit", "potato", "rotten_flesh",
})


@dataclass
class ReflexMemory:
    """Position repeats and which way the last dodge stepped."""

    last: object = None
    repeats: int = 0
    dodge: str = "left"


def decide_reflex(observation, memory=None):
    """
    One reflex action, or None when the plan may proceed.

    Flee comes before eating. Eating comes before combat. A stuck
    bot jumps, then steps aside. `memory` is updated in place.
    """
    if memory is None:
        memory = ReflexMemory()
    if not isinstance(observation, dict):
        return None

    health = _stat(observation, "health")
    food = _stat(observation, "food")
    _note(memory, observation)

    if health is not None and health <= FLEE_AT_OR_BELOW:
        return _flee(observation)

    held = _food_item(observation)
    if food is not None and food <= EAT_AT_OR_BELOW and held:
        return parse_plan({"action": "eat", "arguments": {"item": held}})

    hostile = _nearest(observation, "hostile")
    if hostile is not None and (health is None or health > FLEE_AT_OR_BELOW):
        distance = _distance(observation.get("position"), hostile)
        if distance is not None and distance <= ATTACK_WITHIN:
            return parse_plan({
                "action": "attack",
                "arguments": {"target": _bare(hostile.get("name"))},
            })
        if distance is not None and distance <= DODGE_WITHIN:
            side = memory.dodge if memory.dodge in {"left", "right"} else "left"
            memory.dodge = "right" if side == "left" else "left"
            return parse_plan({
                "action": "strafe",
                "arguments": {"direction": side, "ticks": 8},
            })

    if memory.repeats == STUCK_REPEATS:
        return parse_plan({"action": "jump", "arguments": {}})
    if memory.repeats >= STUCK_REPEATS + 2:
        memory.repeats = 0
        return parse_plan({
            "action": "strafe",
            "arguments": {"direction": "left", "ticks": 8},
        })
    return None


def _note(memory, observation) -> None:
    position = observation.get("position")
    if observation.get("status") != "acting" or not isinstance(position, dict):
        memory.repeats = 0
        memory.last = None
        return
    try:
        key = (
            round(float(position["x"]), 1),
            round(float(position["y"]), 1),
            round(float(position["z"]), 1),
        )
    except (TypeError, ValueError):
        memory.repeats = 0
        memory.last = None
        return
    if key == memory.last:
        memory.repeats += 1
    else:
        memory.last = key
        memory.repeats = 1


def _stat(observation, key):
    value = observation.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _bare(name) -> str:
    if not isinstance(name, str):
        return ""
    return name.split(":")[-1]


def _food_item(observation) -> str:
    held = _bare(observation.get("held"))
    if held in FOOD_ITEMS:
        return held
    for row in observation.get("inventory") or []:
        if not isinstance(row, dict):
            continue
        name = _bare(row.get("item"))
        if name in FOOD_ITEMS:
            return name
    return ""


def _distance(position, entry):
    if not isinstance(position, dict) or not isinstance(entry, dict):
        return None
    try:
        return math.sqrt(
            (float(position["x"]) - float(entry["x"])) ** 2
            + (float(position["y"]) - float(entry["y"])) ** 2
            + (float(position["z"]) - float(entry["z"])) ** 2
        )
    except (KeyError, TypeError, ValueError):
        return None


def _nearest(observation, kind):
    position = observation.get("position")
    best = None
    best_distance = None
    for entity in observation.get("entities") or []:
        if not isinstance(entity, dict) or entity.get("kind") != kind:
            continue
        if not _bare(entity.get("name")):
            continue
        distance = _distance(position, entity)
        if distance is None:
            continue
        if best_distance is None or distance < best_distance:
            best = entity
            best_distance = distance
    return best


def _flee(observation):
    back = parse_plan({
        "action": "strafe",
        "arguments": {"direction": "back", "ticks": 10},
    })
    position = observation.get("position")
    hostile = _nearest(observation, "hostile")
    if not isinstance(position, dict) or hostile is None:
        return back
    try:
        x = float(position["x"])
        y = float(position["y"])
        z = float(position["z"])
        dx = x - float(hostile["x"])
        dz = z - float(hostile["z"])
    except (KeyError, TypeError, ValueError):
        return back
    if y < -64 or y > 320:
        return back
    length = math.hypot(dx, dz)
    if length < 0.5:
        return back
    tx = max(-30_000_000, min(30_000_000, int(round(x + dx / length * FLEE_BLOCKS))))
    tz = max(-30_000_000, min(30_000_000, int(round(z + dz / length * FLEE_BLOCKS))))
    ty = max(-64, min(320, int(round(y))))
    return parse_plan({"action": "goto", "arguments": {"x": tx, "y": ty, "z": tz}})
