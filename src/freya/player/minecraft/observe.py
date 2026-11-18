"""
Eyes for one Minecraft reading.

The child reports structured state. This module copies the fields the
schema allows, keeps the nearest blocks and entities, and drops the
rest until the encoding fits the observation cap. Pixels are not a
field. The control words and the adapter id are supplied by the
caller, so the child cannot write the status strip.
"""

import json
import math

from freya.player.minecraft.goals import (
    COAL_BLOCKS,
    FOOD_BLOCKS,
    FOOD_MOBS,
    LOGS,
    ORE_BLOCKS,
    STRUCTURE_BLOCKS,
)
from freya.player.schema import (
    ENTITY_KINDS,
    MAX_OBSERVATION_BYTES,
    STATUSES,
    check_observation,
    item_name,
    plain_text,
)


_BLOCKS = 8
_ENTITIES = 6
_INVENTORY = 8
_CHAT = 3
_THREATS = 8
_TARGET_BLOCKS = LOGS | ORE_BLOCKS | COAL_BLOCKS | FOOD_BLOCKS
_LOCAL_FILL = 4


def summarize(raw, *, adapter, control, status=None) -> dict:
    """A schema observation under the byte cap. Unknown fields are omitted."""
    if not isinstance(raw, dict):
        raw = {}
    if status not in STATUSES:
        status = raw.get("status") if raw.get("status") in STATUSES else "idle"

    body = {"adapter": adapter, "status": status, "control": control}
    position = _position(raw.get("position"))
    if position is not None:
        body["position"] = position

    health = _unit(raw.get("health"), 0, 100)
    if health is not None:
        body["health"] = health
    food = _unit(raw.get("food"), 0, 20)
    if food is not None:
        body["food"] = food
    clock = _clock(raw.get("time_of_day"))
    if clock is not None:
        body["time_of_day"] = clock
    held = _bare(raw.get("held"))
    if held and plain_text(held, 64) and item_name(held):
        body["held"] = held

    inventory = []
    for row in raw.get("inventory") or []:
        stack = _stack(row)
        if stack is not None:
            inventory.append(stack)
    if inventory:
        body["inventory"] = inventory[:_INVENTORY]

    blocks = [block for block in (_block(entry) for entry in raw.get("blocks") or []) if block]
    chosen = _choose_blocks(blocks, position)
    if chosen:
        body["blocks"] = chosen

    entities = [entity for entity in (_entity(entry) for entry in raw.get("entities") or []) if entity]
    chosen_entities = _choose_entities(entities, position)
    if chosen_entities:
        body["entities"] = chosen_entities

    threats = []
    for name in raw.get("threats") or []:
        text = _threat_name(name)
        if text and text not in threats:
            threats.append(text)
    for entity in body.get("entities") or []:
        if entity["kind"] == "hostile" and entity["name"] not in threats:
            threats.append(entity["name"])
    if threats:
        body["threats"] = threats[:_THREATS]

    chat = []
    for line in raw.get("chat") or []:
        if not isinstance(line, str):
            continue
        shortened = line.strip()[:80]
        if plain_text(shortened, 80):
            chat.append(shortened)
    if chat:
        body["chat"] = chat[-_CHAT:]
        body["chat_trust"] = "untrusted"

    return _fit(body)


def _fit(body) -> dict:
    """Drop optional entries from the far end until the cap holds."""
    while _size(body) > MAX_OBSERVATION_BYTES:
        if body.get("chat"):
            body["chat"] = body["chat"][:-1]
            if not body["chat"]:
                body.pop("chat", None)
                body.pop("chat_trust", None)
            continue
        if body.get("entities"):
            body["entities"] = body["entities"][:-1]
            if not body["entities"]:
                body.pop("entities", None)
            continue
        if body.get("blocks"):
            body["blocks"] = body["blocks"][:-1]
            if not body["blocks"]:
                body.pop("blocks", None)
            continue
        if body.get("inventory"):
            body["inventory"] = body["inventory"][:-1]
            if not body["inventory"]:
                body.pop("inventory", None)
            continue
        if body.get("threats"):
            body["threats"] = body["threats"][:-1]
            if not body["threats"]:
                body.pop("threats", None)
            continue
        for key in ("held", "time_of_day", "food", "health", "position"):
            if key in body:
                body.pop(key)
                break
        else:
            break
    return check_observation(body)


def _size(body) -> int:
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return len(encoded.encode("utf-8"))


def _clock(value):
    """A day tick in 0..24000. Floats and a running world age are wrapped."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    clock = math.floor(value)
    if 0 <= clock <= 24000:
        return int(clock)
    wrapped = int(clock % 24000)
    if wrapped < 0:
        wrapped += 24000
    return wrapped


def _threat_name(name) -> str:
    text = _bare(name)
    if text and plain_text(text, 80) and item_name(text):
        return text
    return ""


def _choose_blocks(blocks, origin):
    """
    Keep a log or ore that is farther than the stone underfoot.

    Structures (the shelter, a table, a furnace) take up to four slots.
    One of each searched group is kept before the nearest filler.
    """
    ordered = sorted(blocks, key=lambda block: _near(origin, block))
    chosen = []
    used = set()

    def add(block) -> bool:
        key = (block["name"], block["x"], block["y"], block["z"])
        if key in used or len(chosen) >= _BLOCKS:
            return False
        used.add(key)
        chosen.append(block)
        return True

    structures = [block for block in ordered if _bare(block["name"]) in STRUCTURE_BLOCKS]
    for block in structures[:4]:
        add(block)
    for group in (LOGS, ORE_BLOCKS, COAL_BLOCKS, FOOD_BLOCKS):
        for block in ordered:
            if _bare(block["name"]) in group and add(block):
                break
    targets = [block for block in ordered if _bare(block["name"]) in _TARGET_BLOCKS]
    for block in targets:
        if len(chosen) >= _BLOCKS:
            break
        if sum(1 for item in chosen if _bare(item["name"]) not in STRUCTURE_BLOCKS) >= _LOCAL_FILL:
            break
        add(block)
    for block in ordered:
        if len(chosen) >= _BLOCKS:
            break
        add(block)
    return chosen


def _choose_entities(entities, origin):
    """Hostiles and food mobs are kept even when a player stands closer."""
    ordered = sorted(entities, key=lambda entity: _near(origin, entity))
    chosen = []
    used = set()

    def add(entity) -> None:
        key = (entity["name"], entity["kind"], entity["x"], entity["y"], entity["z"])
        if key in used or len(chosen) >= _ENTITIES:
            return
        used.add(key)
        chosen.append(entity)

    for entity in ordered:
        if entity["kind"] == "hostile" or _bare(entity["name"]) in FOOD_MOBS:
            add(entity)
        if len(chosen) >= 4:
            break
    for entity in ordered:
        if len(chosen) >= _ENTITIES:
            break
        add(entity)
    return chosen


def _bare(name) -> str:
    if not isinstance(name, str):
        return ""
    text = name.strip()
    if ":" in text:
        text = text.split(":")[-1]
    return text


def _unit(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def _position(value):
    if not isinstance(value, dict) or set(value) != {"x", "y", "z"}:
        return None
    point = {}
    for axis in ("x", "y", "z"):
        number = _unit(value[axis], -1_000_000, 1_000_000)
        if number is None:
            return None
        point[axis] = number
    return point


def _near(origin, entry) -> float:
    if not origin:
        return 0
    return (
        (entry["x"] - origin["x"]) ** 2
        + (entry["y"] - origin["y"]) ** 2
        + (entry["z"] - origin["z"]) ** 2
    )


def _block(entry):
    if not isinstance(entry, dict):
        return None
    name = _bare(entry.get("name"))
    if not name or not plain_text(name, 64) or not item_name(name):
        return None
    point = _position({"x": entry.get("x"), "y": entry.get("y"), "z": entry.get("z")})
    if point is None:
        return None
    point["name"] = name
    return point


def _entity(entry):
    if not isinstance(entry, dict):
        return None
    name = _bare(entry.get("name"))
    kind = entry.get("kind")
    if kind not in ENTITY_KINDS or not name or not plain_text(name, 64) or not item_name(name):
        return None
    point = _position({"x": entry.get("x"), "y": entry.get("y"), "z": entry.get("z")})
    if point is None:
        return None
    point["name"] = name
    point["kind"] = kind
    return point


def _stack(entry):
    if not isinstance(entry, dict):
        return None
    name = _bare(entry.get("item"))
    count = entry.get("count")
    if not name or not plain_text(name, 80):
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return None
    return {"item": name, "count": min(count, 999)}
