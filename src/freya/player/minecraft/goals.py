"""
Goal steps the adapter chooses without asking the model.

A goal is one name from the closed list (`build_shelter`, `get_iron`,
`get_wood`, `get_food`). Each call looks at the current observation
and returns the next schema action, or None when the goal is done.
The sequences are fixed in this file. Nothing here evaluates text.
"""

from freya.player.schema import parse_plan


_SHELTER = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))


def next_step(name, observation):
    """The next schema action for this goal, or None when it is done."""
    if not isinstance(observation, dict):
        observation = {}
    if name == "get_wood":
        return _wood(observation)
    if name == "get_iron":
        return _iron(observation)
    if name == "build_shelter":
        return _shelter(observation)
    if name == "get_food":
        return _food(observation)
    return None


def _count(observation, item) -> int:
    total = 0
    for row in observation.get("inventory") or []:
        if not isinstance(row, dict):
            continue
        if _bare(row.get("item")) != item:
            continue
        count = row.get("count")
        if isinstance(count, bool) or not isinstance(count, int):
            continue
        total += count
    held = observation.get("held")
    if _bare(held) == item:
        total += 1
    return total


def _bare(name) -> str:
    if not isinstance(name, str):
        return ""
    return name.split(":")[-1]


def _has_block(observation, name) -> bool:
    for block in observation.get("blocks") or []:
        if isinstance(block, dict) and _bare(block.get("name")) == name:
            return True
    return False


def _has_entity(observation, name) -> bool:
    for entity in observation.get("entities") or []:
        if isinstance(entity, dict) and _bare(entity.get("name")) == name:
            return True
    return False


def _dig(block):
    return parse_plan({"action": "dig", "arguments": {"block": block}})


def _craft(item):
    return parse_plan({"action": "craft", "arguments": {"item": item, "count": 1}})


def _wood(observation):
    if _count(observation, "oak_log") >= 4:
        return None
    return _dig("oak_log")


def _iron(observation):
    """
    The ordinary path to one iron ingot: wood, a stone pick, ore,
    a furnace, fuel, then smelt. One craft at a time, so a tick never
    asks for ingredients the inventory does not show.
    """
    if _count(observation, "iron_ingot") >= 1:
        return None
    logs = _count(observation, "oak_log")
    planks = _count(observation, "oak_planks")
    sticks = _count(observation, "stick")
    tables = _count(observation, "crafting_table")
    wood_pick = _count(observation, "wooden_pickaxe")
    stone_pick = _count(observation, "stone_pickaxe")
    cobble = _count(observation, "cobblestone")
    raw = _count(observation, "raw_iron")
    ore_item = _count(observation, "iron_ore")
    furnace = _count(observation, "furnace")
    coal = _count(observation, "coal")
    charcoal = _count(observation, "charcoal")

    if stone_pick < 1 and wood_pick < 1 and raw < 1 and ore_item < 1:
        if planks < 8:
            if logs >= 1:
                return _craft("oak_planks")
            return _dig("oak_log")
        if tables < 1 and not _has_block(observation, "crafting_table"):
            return _craft("crafting_table")
        if tables >= 1 and not _has_block(observation, "crafting_table"):
            return parse_plan({
                "action": "place",
                "arguments": {"block": "crafting_table"},
            })
        if sticks < 2:
            return _craft("stick")
        return _craft("wooden_pickaxe")

    if stone_pick < 1 and raw < 1 and ore_item < 1:
        if cobble < 3:
            return _dig("stone")
        return _craft("stone_pickaxe")

    if raw < 1 and ore_item < 1:
        return _dig("iron_ore")

    if not _has_block(observation, "furnace"):
        if furnace < 1:
            return _craft("furnace")
        return parse_plan({"action": "place", "arguments": {"block": "furnace"}})

    if coal < 1 and charcoal < 1:
        return _dig("coal_ore")

    item = "raw_iron" if raw >= 1 else "iron_ore"
    fuel = "coal" if coal >= 1 else "charcoal"
    return parse_plan({
        "action": "smelt",
        "arguments": {"item": item, "fuel": fuel, "count": 1},
    })


def _shelter(observation):
    if _placed(observation) >= 4:
        return None
    if _count(observation, "oak_planks") < 4:
        if _count(observation, "oak_log") >= 1:
            return _craft("oak_planks")
        return _dig("oak_log")
    spot = _next_post(observation)
    if spot is None:
        return None
    x, y, z = spot
    return parse_plan({
        "action": "place",
        "arguments": {"block": "oak_planks", "x": x, "y": y, "z": z},
    })


def _placed(observation) -> int:
    return sum(
        1
        for block in observation.get("blocks") or []
        if isinstance(block, dict) and _bare(block.get("name")) == "oak_planks"
    )


def _next_post(observation):
    position = observation.get("position")
    if not isinstance(position, dict):
        return None
    try:
        origin = (
            int(round(float(position["x"]))),
            int(round(float(position["y"]))),
            int(round(float(position["z"]))),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not -64 <= origin[1] <= 320:
        return None
    occupied = set()
    for block in observation.get("blocks") or []:
        if not isinstance(block, dict) or _bare(block.get("name")) != "oak_planks":
            continue
        try:
            occupied.add((
                int(round(float(block["x"]))),
                int(round(float(block["y"]))),
                int(round(float(block["z"]))),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    for dx, dy, dz in _SHELTER:
        spot = (origin[0] + dx, origin[1] + dy, origin[2] + dz)
        if spot not in occupied:
            return spot
    return None


def _food(observation):
    cooked = (
        "cooked_beef", "cooked_porkchop", "cooked_chicken", "bread",
        "baked_potato", "apple", "carrot", "sweet_berries",
        "beef", "porkchop", "chicken",
    )
    for name in cooked:
        if _count(observation, name) >= 1:
            return parse_plan({"action": "eat", "arguments": {"item": name}})
    for name in ("cow", "pig", "chicken"):
        if _has_entity(observation, name):
            return parse_plan({"action": "attack", "arguments": {"target": name}})
    return parse_plan({"action": "collect", "arguments": {"item": "sweet_berries"}})
