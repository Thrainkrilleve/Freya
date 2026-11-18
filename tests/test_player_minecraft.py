"""
Player 2, milestone M1.

The bridge is a fake, or a short Python child. No Minecraft server and
no Node process is required. The tests cover the closed schema, reflex
triggers, the observation cap, and the kill switch.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from freya.child_environment import CREDENTIAL_NAME
from freya.player.audit import ActionLog
from freya.player.indicator import IN_CONTROL, PAUSED
from freya.player.minecraft.adapter import MinecraftAdapter, register_minecraft
from freya.player.minecraft.bridge import (
    LineBridge,
    bridge_environment,
    parse_target,
    resolve_auth,
    resolve_username,
    resolve_version,
    script_path,
    start_bridge,
)
from freya.player.minecraft.goals import next_step
from freya.player.minecraft.observe import summarize
from freya.player.minecraft.reflexes import ReflexMemory, decide_reflex
from freya.player.minecraft.__main__ import prepare
from freya.player.permissions import parse_allow_list
from freya.player.planner import DEFAULT_PLAY_MODEL
from freya.player.registry import AdapterRegistry
from freya.player.schema import (
    MAX_OBSERVATION_BYTES,
    ActionRefused,
    ObservationRefused,
    PermissionRefused,
    PlanRefused,
    check_observation,
    parse_plan,
)
from freya.player.session import open_session


SECRET = "secret-token-value"
ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "adapters" / "minecraft" / "bot.js"
PACKAGE = ROOT / "adapters" / "minecraft" / "package.json"

STUB = r"""
import json, sys, time
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if method == "hold":
        time.sleep(30)
    if method == "bad":
        body = {"id": msg.get("id"), "ok": False, "error": "SECRETTRACE"}
    elif method == "noworld":
        body = {"id": msg.get("id"), "ok": False, "error": "The bot is not in a world."}
    else:
        result = {"status": "idle"}
        if method == "observe":
            result = {"status": "idle", "health": 20, "food": 20}
        body = {"id": msg.get("id"), "ok": True, "result": result}
    sys.stdout.write(json.dumps(body) + "\n")
    sys.stdout.flush()
    if method == "stop":
        break
"""


class Immediate:
    def __init__(self, text):
        self.text = text

    def ask(self, messages):
        return self.text

    def poll(self):
        return None


class FakeBridge:
    def __init__(self, observation=None):
        self.observation = observation or _healthy()
        self.sent = []
        self.stopped = False
        self.terminated = False

    def act(self, name, arguments):
        if self.stopped:
            raise ActionRefused("Play has stopped.")
        self.sent.append((name, dict(arguments)))

    def observe(self):
        if self.stopped:
            raise ActionRefused("Play has stopped.")
        return json.loads(json.dumps(self.observation))

    def halt(self):
        if not self.stopped:
            self.sent.append(("halt", {}))

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.terminated = True
        self.sent.append(("stop", {}))


def _healthy():
    return {
        "status": "idle",
        "position": {"x": 0, "y": 64, "z": 0},
        "health": 20,
        "food": 20,
        "time_of_day": 1000,
    }


def _adapter(observation=None):
    bridge = FakeBridge(observation)
    adapter = MinecraftAdapter(bridge=bridge)
    return adapter, bridge


def _accept(adapter, raw):
    action = parse_plan(raw).with_issuer("play")
    adapter.accept(action)
    return action


def _session(brain, bridge, log=None, goal=None):
    found = AdapterRegistry()
    register_minecraft(found, bridge=bridge)
    return open_session(
        found,
        "minecraft",
        "127.0.0.1:25565",
        parse_allow_list('[{"adapter":"minecraft","target":"127.0.0.1:25565"}]'),
        brain,
        goal or {"goal": "follow", "target": "owner"},
        environ={},
        log=log,
    )


# --- schema ------------------------------------------------------------


def test_movement_and_block_actions_are_bounded():
    goto = parse_plan({
        "action": "goto",
        "arguments": {"x": 1, "y": 64, "z": -2},
    })
    assert goto.name == "goto"
    assert dict(goto.arguments) == {"x": 1, "y": 64, "z": -2}

    by_block = parse_plan({"action": "goto", "arguments": {"block": "oak_log"}})
    assert dict(by_block.arguments) == {"block": "oak_log"}
    by_player = parse_plan({"action": "goto", "arguments": {"player": "Alex"}})
    assert dict(by_player.arguments) == {"player": "Alex"}

    with pytest.raises(PlanRefused, match="not a schema action"):
        parse_plan({
            "action": "goto",
            "arguments": {"x": 1, "y": 64, "z": 0, "block": "stone"},
        })
    with pytest.raises(PlanRefused, match="not a schema action"):
        parse_plan({"action": "goto", "arguments": {"x": 1, "y": 64}})
    with pytest.raises(PlanRefused, match="out of range"):
        parse_plan({"action": "goto", "arguments": {"x": 0, "y": 321, "z": 0}})
    with pytest.raises(PlanRefused, match="not a schema action"):
        parse_plan({"action": "goto", "arguments": {"x": True, "y": 64, "z": 0}})

    dig = parse_plan({"action": "dig", "arguments": {"block": "iron_ore"}})
    mine = parse_plan({"action": "mine", "arguments": {"x": 2, "y": 60, "z": 2}})
    assert dig.name == "dig"
    assert mine.name == "mine"
    with pytest.raises(PlanRefused, match="missing"):
        parse_plan({"action": "dig", "arguments": {}})

    sprint = parse_plan({"action": "sprint", "arguments": {"state": "on"}})
    assert dict(sprint.arguments) == {"state": "on"}
    with pytest.raises(PlanRefused, match="not a schema action"):
        parse_plan({"action": "sprint", "arguments": {"state": "sometimes"}})

    swim = parse_plan({
        "action": "swim",
        "arguments": {"direction": "up", "ticks": 8},
    })
    assert dict(swim.arguments)["ticks"] == 8
    with pytest.raises(PlanRefused, match="out of range"):
        parse_plan({"action": "strafe", "arguments": {"direction": "left", "ticks": 400}})

    craft = parse_plan({"action": "craft", "arguments": {"item": "oak_planks", "count": 1}})
    assert craft.arguments["count"] == 1
    with pytest.raises(PlanRefused, match="out of range"):
        parse_plan({"action": "craft", "arguments": {"item": "stick", "count": 0}})
    with pytest.raises(PlanRefused, match="not a schema action"):
        parse_plan({"action": "smelt", "arguments": {"item": "raw_iron", "count": True}})

    equip = parse_plan({
        "action": "equip",
        "arguments": {"item": "shield", "slot": "offhand"},
    })
    assert equip.arguments["slot"] == "offhand"
    sleep = parse_plan({"action": "sleep", "arguments": {}})
    assert sleep.name == "sleep"
    shelter = parse_plan({"action": "build_shelter", "arguments": {}})
    assert shelter.name == "build_shelter"


def test_unknown_and_malformed_actions_are_refused_without_their_text():
    with pytest.raises(PlanRefused, match="no known action"):
        parse_plan({"action": "fly", "arguments": {}})
    with pytest.raises(PlanRefused, match="field that is not allowed"):
        parse_plan({
            "action": "chat",
            "arguments": {"text": "hello"},
            "code": SECRET,
        })
    with pytest.raises(PlanRefused, match="not a schema action"):
        parse_plan({"action": "chat", "arguments": {"text": "function sneak() {\n  eval(" + SECRET + ")\n}"}})
    with pytest.raises(PlanRefused, match="out of scope"):
        parse_plan({"action": "goto", "arguments": {"player": "eve"}})
    with pytest.raises(PlanRefused, match="field that is not allowed"):
        parse_plan({"action": "get_iron", "arguments": {"note": SECRET}})


def test_a_refused_action_is_logged_and_not_sent():
    bridge = FakeBridge()
    log = ActionLog()
    brain = Immediate(json.dumps({
        "action": "goto",
        "arguments": {"block": "stone", "note": SECRET},
    }))
    session = _session(brain, bridge, log=log)
    session.tick(0)

    assert any(row.outcome == "refused" for row in log)
    assert SECRET not in log.text()
    assert all(name == "halt" for name, _arguments in bridge.sent)
    session.stop()


# --- reflexes ----------------------------------------------------------


def _world(**overrides):
    body = _healthy()
    body.update(overrides)
    return body


def test_low_food_eats_and_low_health_flees_before_the_plan():
    memory = ReflexMemory()
    eat = decide_reflex(_world(
        food=6,
        inventory=[{"item": "bread", "count": 1}],
    ), memory)
    assert eat.name == "eat"
    assert eat.arguments["item"] == "bread"

    flee = decide_reflex(_world(
        health=6,
        food=1,
        inventory=[{"item": "bread", "count": 1}],
        entities=[{"name": "zombie", "x": 4, "y": 64, "z": 0, "kind": "hostile"}],
    ), ReflexMemory())
    assert flee.name == "goto"
    assert flee.arguments["x"] < 0

    attack = decide_reflex(_world(
        entities=[{"name": "zombie", "x": 1, "y": 64, "z": 0, "kind": "hostile"}],
    ), ReflexMemory())
    assert attack.name == "attack"
    assert attack.arguments["target"] == "zombie"

    dodge = decide_reflex(_world(
        entities=[{"name": "skeleton", "x": 5, "y": 64, "z": 0, "kind": "hostile"}],
    ), ReflexMemory())
    assert dodge.name == "strafe"
    assert dodge.arguments["direction"] in {"left", "right"}

    assert decide_reflex(_world(), ReflexMemory()) is None


def test_a_stuck_bot_jumps_then_steps_aside():
    memory = ReflexMemory()
    seen = None
    for _ in range(4):
        seen = decide_reflex(_world(status="acting"), memory)
    assert seen is not None and seen.name == "jump"
    seen = decide_reflex(_world(status="acting"), memory)
    assert seen is None
    seen = decide_reflex(_world(status="acting"), memory)
    assert seen is not None and seen.name == "strafe"


def test_the_adapter_sends_a_reflex_instead_of_the_plan():
    adapter, bridge = _adapter(_world(
        food=3,
        inventory=[{"item": "bread", "count": 2}],
    ))
    action = _accept(adapter, {"action": "goto", "arguments": {"block": "stone"}})
    adapter.reflex(action, IN_CONTROL)
    assert bridge.sent[0][0] == "eat"
    assert "stone" not in json.dumps(bridge.sent[0][1])


def test_closing_the_adapter_sends_no_further_actions():
    adapter, bridge = _adapter()
    action = _accept(adapter, {"action": "follow", "arguments": {"target": "owner"}})
    adapter.reflex(action, IN_CONTROL)
    assert bridge.sent[0][0] == "follow"

    adapter.close()
    assert bridge.terminated is True
    assert bridge.sent[-1][0] == "stop"
    marker = len(bridge.sent)
    seen = adapter.reflex(action, PAUSED)
    assert bridge.sent[marker:] == []
    assert seen["control"] == PAUSED
    assert seen["status"] == "paused"
    assert seen["adapter"] == "minecraft"


def test_stop_mid_session_clears_control_and_halts_the_child():
    bridge = FakeBridge()
    session = _session(
        Immediate('{"action":"follow","arguments":{"target":"owner"}}'),
        bridge,
    )
    session.tick(0)
    assert any(name == "follow" for name, _arguments in bridge.sent)
    assert session.label == IN_CONTROL

    session.stop()
    seen = session.tick(1)
    assert bridge.terminated is True
    assert session.label == PAUSED
    assert seen["control"] == PAUSED
    assert seen["status"] == "paused"
    assert bridge.sent[-1][0] == "stop"


# --- goals -------------------------------------------------------------


def test_goals_choose_a_schema_step_from_the_inventory():
    assert next_step("get_wood", _world()).name == "dig"
    assert next_step("get_wood", _world(
        inventory=[{"item": "oak_log", "count": 4}],
    )) is None

    assert next_step("get_iron", _world()).arguments["block"] == "oak_log"
    assert next_step("get_iron", _world(
        inventory=[{"item": "oak_log", "count": 1}],
    )).arguments["item"] == "oak_planks"
    assert next_step("get_iron", _world(
        inventory=[{"item": "oak_planks", "count": 8}],
    )).arguments["item"] == "crafting_table"
    assert next_step("get_iron", _world(
        inventory=[
            {"item": "oak_planks", "count": 8},
            {"item": "crafting_table", "count": 1},
        ],
    )).name == "place"
    assert next_step("get_iron", _world(
        inventory=[{"item": "oak_planks", "count": 8}],
        blocks=[{"name": "crafting_table", "x": 1, "y": 64, "z": 0}],
    )).arguments["item"] == "stick"
    assert next_step("get_iron", _world(
        inventory=[
            {"item": "oak_planks", "count": 8},
            {"item": "stick", "count": 4},
        ],
        blocks=[{"name": "crafting_table", "x": 1, "y": 64, "z": 0}],
    )).arguments["item"] == "wooden_pickaxe"
    assert next_step("get_iron", _world(
        inventory=[{"item": "wooden_pickaxe", "count": 1}],
    )).arguments["block"] == "stone"
    assert next_step("get_iron", _world(
        inventory=[
            {"item": "wooden_pickaxe", "count": 1},
            {"item": "cobblestone", "count": 3},
            {"item": "stick", "count": 2},
        ],
    )).arguments["item"] == "stone_pickaxe"
    assert next_step("get_iron", _world(
        inventory=[{"item": "stone_pickaxe", "count": 1}],
    )).arguments["block"] == "iron_ore"
    assert next_step("get_iron", _world(
        inventory=[
            {"item": "stone_pickaxe", "count": 1},
            {"item": "raw_iron", "count": 1},
        ],
    )).arguments["item"] == "furnace"
    assert next_step("get_iron", _world(
        inventory=[
            {"item": "raw_iron", "count": 1},
            {"item": "furnace", "count": 1},
        ],
    )).name == "place"
    assert next_step("get_iron", _world(
        inventory=[{"item": "raw_iron", "count": 1}],
        blocks=[{"name": "furnace", "x": 1, "y": 64, "z": 0}],
    )).arguments["block"] == "coal_ore"
    smelt = next_step("get_iron", _world(
        inventory=[
            {"item": "raw_iron", "count": 1},
            {"item": "coal", "count": 1},
        ],
        blocks=[{"name": "furnace", "x": 1, "y": 64, "z": 0}],
    ))
    assert smelt.name == "smelt"
    assert smelt.arguments["fuel"] == "coal"
    assert next_step("get_iron", _world(
        inventory=[{"item": "iron_ingot", "count": 1}],
    )) is None

    assert next_step("get_food", _world(
        inventory=[{"item": "bread", "count": 1}],
    )).name == "eat"
    assert next_step("get_food", _world(
        entities=[{"name": "cow", "x": 2, "y": 64, "z": 0, "kind": "passive"}],
    )).arguments["target"] == "cow"
    assert next_step("get_food", _world()).arguments["item"] == "sweet_berries"

    placed = next_step("build_shelter", _world(
        inventory=[{"item": "oak_planks", "count": 4}],
    ))
    assert placed.name == "place"
    assert placed.arguments["x"] == 1
    assert next_step("build_shelter", _world(
        blocks=[
            {"name": "oak_planks", "x": 1, "y": 64, "z": 0},
            {"name": "oak_planks", "x": -1, "y": 64, "z": 0},
            {"name": "oak_planks", "x": 0, "y": 64, "z": 1},
            {"name": "oak_planks", "x": 0, "y": 64, "z": -1},
        ],
    )) is None


# --- eyes --------------------------------------------------------------


def test_observation_keeps_structured_state_and_drops_pixels_and_forged_control():
    seen = summarize({
        "adapter": "eve",
        "control": "Freya owns the computer",
        "status": "acting",
        "pixels": "frame",
        "position": {"x": 3, "y": 64, "z": 4},
        "health": 18,
        "food": 14,
        "time_of_day": 13000,
        "held": "minecraft:stone_pickaxe",
        "inventory": [{"item": "oak_log", "count": 3}],
        "blocks": [{"name": "stone", "x": 4, "y": 64, "z": 4}],
        "entities": [{"name": "zombie", "x": 6, "y": 64, "z": 4, "kind": "hostile"}],
        "chat": ["hello there"],
        "chat_trust": "trusted",
    }, adapter="minecraft", control=IN_CONTROL)

    assert seen["adapter"] == "minecraft"
    assert seen["control"] == IN_CONTROL
    assert seen["food"] == 14
    assert seen["time_of_day"] == 13000
    assert seen["held"] == "stone_pickaxe"
    assert seen["blocks"][0]["name"] == "stone"
    assert seen["entities"][0]["kind"] == "hostile"
    assert seen["chat_trust"] == "untrusted"
    assert "pixels" not in seen
    assert "eve" not in json.dumps(seen)
    assert len(json.dumps(seen, sort_keys=True, separators=(",", ":")).encode("utf-8")) <= MAX_OBSERVATION_BYTES


def test_a_large_reading_is_summarised_under_the_cap_and_a_raw_one_is_refused():
    name = "a" * 64
    raw = _healthy()
    raw["inventory"] = [{"item": name, "count": 64} for _ in range(8)]
    raw["blocks"] = [
        {"name": name, "x": index, "y": 64, "z": index} for index in range(8)
    ]
    raw["entities"] = [
        {"name": "zombie", "x": index, "y": 64, "z": 0, "kind": "hostile"}
        for index in range(6)
    ]
    raw["threats"] = ["zombie"] * 8
    raw["chat"] = ["b" * 80] * 3
    seen = summarize(raw, adapter="minecraft", control=IN_CONTROL)
    encoded = json.dumps(seen, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= MAX_OBSERVATION_BYTES
    check_observation(seen)

    huge = {
        "adapter": "minecraft",
        "status": "idle",
        "control": IN_CONTROL,
        "chat": ["c" * 200] * 20,
        "chat_trust": "untrusted",
    }
    with pytest.raises(ObservationRefused, match="too large"):
        check_observation(huge)

    too_many = _healthy()
    too_many["adapter"] = "minecraft"
    too_many["control"] = IN_CONTROL
    too_many["blocks"] = [
        {"name": "stone", "x": index, "y": 64, "z": 0} for index in range(9)
    ]
    with pytest.raises(ObservationRefused, match="too many"):
        check_observation(too_many)


# --- bridge ------------------------------------------------------------


def test_only_a_loopback_target_is_accepted_and_secrets_stay_out_of_the_child():
    assert parse_target("127.0.0.1:25565") == ("127.0.0.1", 25565)
    assert parse_target("localhost") == ("localhost", 25565)
    assert parse_target("[::1]:25566") == ("::1", 25566)

    for target in ("203.0.113.10:25565", "example.com", "127.0.0.1:0", "local"):
        with pytest.raises(PermissionRefused, match="not on this machine"):
            parse_target(target)

    env = bridge_environment("127.0.0.1", 25565, {
        "PATH": os.environ.get("PATH", ""),
        "MINECRAFT_ACCESS_TOKEN": SECRET,
        "PASSWORD": "secret-pass",
        "FREYA_MINECRAFT_AUTH": "microsoft",
        "FREYA_MINECRAFT_USERNAME": "Freya",
    })
    blob = json.dumps(env)
    assert SECRET not in blob
    assert "secret-pass" not in blob
    assert "MINECRAFT_ACCESS_TOKEN" not in env
    assert env["FREYA_MINECRAFT_AUTH"] == "microsoft"
    assert env["FREYA_MINECRAFT_HOST"] == "127.0.0.1"
    assert env["FREYA_MINECRAFT_PORT"] == "25565"
    assert all(CREDENTIAL_NAME.search(key) is None for key in env)

    with pytest.raises(PermissionRefused, match="sign-in") as caught:
        resolve_auth({"FREYA_MINECRAFT_AUTH": SECRET})
    assert SECRET not in str(caught.value)
    assert resolve_auth({}) == "offline"
    assert resolve_username({}) == "Freya"
    assert resolve_version({"FREYA_MINECRAFT_VERSION": "1.20.4"}) == "1.20.4"
    with pytest.raises(PermissionRefused, match="version") as bad_version:
        resolve_version({"FREYA_MINECRAFT_VERSION": "../" + SECRET})
    assert SECRET not in str(bad_version.value)


def test_a_public_target_does_not_start_a_process(monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    with pytest.raises(PermissionRefused):
        start_bridge("203.0.113.10:25565")
    assert spawned == []


def test_the_child_speaks_json_and_stop_kills_it_during_an_action():
    env = bridge_environment("127.0.0.1", 25565, os.environ)
    bridge = LineBridge(
        [sys.executable, "-u", "-c", STUB],
        env,
        stderr=subprocess.DEVNULL,
    )
    try:
        seen = bridge.observe()
        assert seen["health"] == 20
        bridge.act("jump", {})
        with pytest.raises(ActionRefused, match="not in a world"):
            bridge.request("noworld")
        with pytest.raises(ActionRefused, match="The action failed.") as caught:
            bridge.request("bad")
        assert "SECRETTRACE" not in str(caught.value)

        box = {}

        def hold():
            try:
                bridge.request("hold")
                box["done"] = "returned"
            except ActionRefused as exc:
                box["done"] = str(exc)

        thread = threading.Thread(target=hold)
        thread.start()
        time.sleep(0.4)
        started = time.monotonic()
        bridge.stop()
        thread.join(5)
        assert thread.is_alive() is False
        assert time.monotonic() - started < 2
        assert bridge.stopped is True
        with pytest.raises(ActionRefused, match="stopped"):
            bridge.act("jump", {})
    finally:
        bridge.stop()


def test_prepare_refuses_a_bad_goal_or_target_without_a_process(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    allow = tmp_path / "allow.json"
    allow.write_text(
        '[{"adapter":"minecraft","target":"127.0.0.1:25565"}]',
        encoding="utf-8",
    )
    args, _entries, model = prepare(
        ["--allow", str(allow), "--target", "127.0.0.1:25565", "--goal", "get_wood"],
        {},
    )
    assert args.goal == "get_wood"
    assert model == DEFAULT_PLAY_MODEL

    with pytest.raises(PlanRefused, match="no known action"):
        prepare(
            ["--allow", str(allow), "--target", "127.0.0.1:25565", "--goal", "fly"],
            {},
        )
    with pytest.raises(PermissionRefused):
        prepare(
            ["--allow", str(allow), "--target", "203.0.113.10", "--goal", "get_iron"],
            {},
        )
    eve = tmp_path / "eve.json"
    eve.write_text('[{"adapter":"minecraft","target":"eve"}]', encoding="utf-8")
    with pytest.raises(PermissionRefused, match="out of scope"):
        prepare(
            ["--allow", str(eve), "--target", "eve", "--goal", "get_food"],
            {},
        )
    assert spawned == []


def test_the_node_program_stays_on_loopback_and_does_not_store_secrets():
    text = BOT.read_text(encoding="utf-8")
    folded = text.casefold()
    assert "function isloopback" in folded
    assert "localhost" in folded
    assert "::1" in text
    assert "127" in text
    assert "0.0.0.0" not in text
    assert "writefile" not in folded
    assert "child_process" not in folded
    assert "password" not in folded
    assert "eval(" not in folded
    assert "anti-cheat" not in folded
    assert "anticheat" not in folded
    assert "listen(" not in folded
    assert "offline" in folded
    assert "microsoft" in folded
    for line in text.splitlines():
        if "require(" in line:
            assert "freya" not in line.casefold()
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    names = set(package["dependencies"])
    assert {
        "mineflayer",
        "mineflayer-pathfinder",
        "mineflayer-pvp",
        "mineflayer-collectblock",
        "mineflayer-tool",
        "mineflayer-armor-manager",
    } <= names
    assert script_path() == BOT
    assert BOT.is_file()
