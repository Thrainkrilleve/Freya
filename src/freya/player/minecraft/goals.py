"""
Goal steps the adapter chooses without asking the model.

A goal is one name from the closed list (`build_shelter`, `get_iron`,
`get_wood`, `get_food`). Each call looks at the current observation
and returns the next schema action, or None when the goal is done.
Done means the success count is in the inventory (or, for a shelter,
standing). A missing tree, ore, or berry is a walk to the next
waypoint, not a finished goal. The sequences are fixed in this file.
Nothing here evaluates text.

Block names here are the same set `bot.js` searches for.
"""

from freya.player.schema import parse_plan


_SHELTER = ((1, 0, 0), (-1, 0, 0), (0, 0, 1), (0, 0, -1))
_WOOD_NEED = 4
_XZ = 30_000_000

# One of each group is kept in the observation even when stone is closer.
# Oak is first so a mixed stack crafts the same planks the tests name.
_LOG_ORDER = (
    "oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log",
    "dark_oak_log", "mangrove_log", "cherry_log", "pale_oak_log",
    "crimson_stem", "warped_stem",
)
LOGS = frozenset(_LOG_ORDER)
_PLANK_ORDER = (
    "oak_planks", "spruce_planks", "birch_planks", "jungle_planks",
    "acacia_planks", "dark_oak_planks", "mangrove_planks", "cherry_planks",
    "pale_oak_planks", "crimson_planks", "warped_planks",
)
PLANKS = frozenset(_PLANK_ORDER)
ORE_BLOCKS = frozenset({"iron_ore", "deepslate_iron_ore"})
COAL_BLOCKS = frozenset({"coal_ore", "deepslate_coal_ore"})
STONE_BLOCKS = frozenset({"stone", "cobblestone"})
FOOD_BLOCKS = frozenset({
    "sweet_berry_bush", "wheat", "carrots", "potatoes", "melon", "pumpkin",
})
STRUCTURE_BLOCKS = frozenset({"crafting_table", "furnace"}) | PLANKS
FOOD_MOBS = frozenset({"cow", "pig", "chicken", "sheep", "rabbit"})

_FOOD_ITEM = {
    "sweet_berry_bush": "sweet_berries",
    "wheat": "wheat",
    "carrots": "carrot",
    "potatoes": "potato",
    "melon": "melon_slice",
    "pumpkin": "pumpkin",
}
_EDIBLE = (
    "cooked_beef", "cooked_porkchop", "cooked_chicken", "bread",
    "baked_potato", "apple", "carrot", "sweet_berries",
    "beef", "porkchop", "chicken",
)
_DIRS = (
    (1, 0), (1, 1), (0, 1), (-1, 1),
    (-1, 0), (-1, -1), (0, -1), (1, -1),
)


def next_step(name, observation, *, attempt=0):
    """The next schema action for this goal, or None when it is done."""
    if not isinstance(observation, dict):
        observation = {}
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 0
    if attempt < 0:
        attempt = 0
    if name == "get_wood":
        return _wood(observation, attempt)
    if name == "get_iron":
        return _iron(observation, attempt)
    if name == "build_shelter":
        return _shelter(observation, attempt)
    if name == "get_food":
        return _food(observation, attempt)
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


def _count_any(observation, names) -> int:
    return sum(_count(observation, name) for name in names)


def _first_item(observation, order) -> str:
    for name in order:
        if _count(observation, name) >= 1:
            return name
    return ""


def _planks_for(log) -> str:
    if log.endswith("_log"):
        return log[:-4] + "_planks"
    if log.endswith("_stem"):
        return log[:-5] + "_planks"
    return "oak_planks"


def _within(observation, block, reach) -> bool:
    position = observation.get("position")
    if not isinstance(position, dict) or not isinstance(block, dict):
        return False
    try:
        return (
            abs(float(block["x"]) - float(position["x"])) <= reach
            and abs(float(block["y"]) - float(position["y"])) <= reach
            and abs(float(block["z"]) - float(position["z"])) <= reach
        )
    except (KeyError, TypeError, ValueError):
        return False


def _has_block(observation, name) -> bool:
    """A table or furnace close enough to use, not one from the search ring."""
    for block in observation.get("blocks") or []:
        if not isinstance(block, dict) or _bare(block.get("name")) != name:
            continue
        if _within(observation, block, 8):
            return True
    return False


def _nearest(observation, names):
    """The closest listed block, or None when the search found nothing."""
    position = observation.get("position")
    best = None
    best_distance = None
    for block in observation.get("blocks") or []:
        if not isinstance(block, dict) or _bare(block.get("name")) not in names:
            continue
        if not isinstance(position, dict):
            return block
        try:
            distance = (
                (float(block["x"]) - float(position["x"])) ** 2
                + (float(block["y"]) - float(position["y"])) ** 2
                + (float(block["z"]) - float(position["z"])) ** 2
            )
        except (KeyError, TypeError, ValueError):
            continue
        if best is None or distance < best_distance:
            best = block
            best_distance = distance
    return best


def _has_entity(observation, name) -> bool:
    for entity in observation.get("entities") or []:
        if isinstance(entity, dict) and _bare(entity.get("name")) == name:
            return True
    return False


def _dig(block):
    return parse_plan({"action": "dig", "arguments": {"block": block}})


def _dig_at(block):
    """Dig that block. Coordinates are sent only when the schema allows them."""
    name = _bare(block.get("name")) if isinstance(block, dict) else ""
    if not name:
        return _dig("stone")
    try:
        x = int(round(float(block["x"])))
        y = int(round(float(block["y"])))
        z = int(round(float(block["z"])))
    except (KeyError, TypeError, ValueError):
        return _dig(name)
    if not (-_XZ <= x <= _XZ and -64 <= y <= 320 and -_XZ <= z <= _XZ):
        return _dig(name)
    return parse_plan({
        "action": "dig",
        "arguments": {"block": name, "x": x, "y": y, "z": z},
    })


def _need_block(observation, names, attempt):
    found = _nearest(observation, names)
    if found is None:
        return _explore(observation, attempt)
    return _dig_at(found)


def _clamp(value, low, high) -> int:
    return max(low, min(high, int(value)))


def _explore(observation, attempt):
    """Walk to the next ring. A missing resource is not a finished goal."""
    x = z = 0
    y = 64
    position = observation.get("position") if isinstance(observation, dict) else None
    if isinstance(position, dict):
        try:
            x = int(round(float(position["x"])))
            y = int(round(float(position["y"])))
            z = int(round(float(position["z"])))
        except (KeyError, TypeError, ValueError):
            x, y, z = 0, 64, 0
    y = _clamp(y, -64, 320)
    ring = 1 + int(attempt) // len(_DIRS)
    radius = min(64, 16 * ring)
    dx, dz = _DIRS[int(attempt) % len(_DIRS)]
    scale = radius if dx == 0 or dz == 0 else int(round(radius * 0.707))
    return parse_plan({
        "action": "goto",
        "arguments": {
            "x": _clamp(x + dx * scale, -_XZ, _XZ),
            "y": y,
            "z": _clamp(z + dz * scale, -_XZ, _XZ),
        },
    })


def _craft(item):
    return parse_plan({"action": "craft", "arguments": {"item": item, "count": 1}})


def _wood(observation, attempt):
    if _count_any(observation, LOGS) >= _WOOD_NEED:
        return None
    return _need_block(observation, LOGS, attempt)


def _iron(observation, attempt):
    """
    The ordinary path to one iron ingot: wood, a stone pick, ore,
    a furnace, fuel, then smelt. One craft at a time, so a tick never
    asks for ingredients the inventory does not show.
    """
    if _count(observation, "iron_ingot") >= 1:
        return None
    logs = _count_any(observation, LOGS)
    planks = _count_any(observation, PLANKS)
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
                return _craft(_planks_for(_first_item(observation, _LOG_ORDER)))
            return _need_block(observation, LOGS, attempt)
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
            return _need_block(observation, STONE_BLOCKS, attempt)
        return _craft("stone_pickaxe")

    if raw < 1 and ore_item < 1:
        return _need_block(observation, ORE_BLOCKS, attempt)

    if not _has_block(observation, "furnace"):
        if furnace < 1:
            return _craft("furnace")
        return parse_plan({"action": "place", "arguments": {"block": "furnace"}})

    if coal < 1 and charcoal < 1:
        return _need_block(observation, COAL_BLOCKS, attempt)

    item = "raw_iron" if raw >= 1 else "iron_ore"
    fuel = "coal" if coal >= 1 else "charcoal"
    return parse_plan({
        "action": "smelt",
        "arguments": {"item": item, "fuel": fuel, "count": 1},
    })


def _shelter(observation, attempt):
    if _placed(observation) >= 4:
        return None
    if _count_any(observation, PLANKS) < 4:
        if _count_any(observation, LOGS) >= 1:
            return _craft(_planks_for(_first_item(observation, _LOG_ORDER)))
        return _need_block(observation, LOGS, attempt)
    spot = _next_post(observation)
    if spot is None:
        return _explore(observation, attempt)
    plank = _first_item(observation, _PLANK_ORDER) or "oak_planks"
    x, y, z = spot
    return parse_plan({
        "action": "place",
        "arguments": {"block": plank, "x": x, "y": y, "z": z},
    })


def _placed(observation) -> int:
    return sum(
        1
        for block in observation.get("blocks") or []
        if (
            isinstance(block, dict)
            and _bare(block.get("name")) in PLANKS
            and _within(observation, block, 2)
        )
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
        if not isinstance(block, dict) or _bare(block.get("name")) not in PLANKS:
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


def _food(observation, attempt):
    for name in _EDIBLE:
        if _count(observation, name) >= 1:
            return parse_plan({"action": "eat", "arguments": {"item": name}})
    for name in ("cow", "pig", "chicken"):
        if _has_entity(observation, name):
            return parse_plan({"action": "attack", "arguments": {"target": name}})
    found = _nearest(observation, FOOD_BLOCKS)
    if found is not None:
        item = _FOOD_ITEM.get(_bare(found.get("name")), "sweet_berries")
        return parse_plan({"action": "collect", "arguments": {"item": item}})
    return _explore(observation, attempt)
