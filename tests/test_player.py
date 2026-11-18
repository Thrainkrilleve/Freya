"""
Player 2, milestone M0.

The mock speaks the schema. No model is contacted: the planner is a
fake. No capability is registered by the control plane. The tests cover
P2-1, P2-2, P2-8, P2-13 and P2-14, and the kill switch, the indicator,
the allow-list, and the action record that sit with them.
"""

import ipaddress
import json
import os
import subprocess
from pathlib import Path

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import LOCAL_OWNER, CapabilityBroker, CapabilityRegistry
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.core.providers import ProviderRegistry
from freya.gpu.status import VENDOR_RANK
from freya.player.audit import ActionLog
from freya.player.indicator import IN_CONTROL, PAUSED
from freya.player.input_backend import InputResult, MissingInputBackend
from freya.player.mock import OWNER_AT, WOOD_AT, MockGame, register_mock
from freya.player.permissions import (
    AllowEntry,
    check_attachment,
    parse_allow_list,
    read_allow_list,
)
from freya.player.planner import (
    CONVERSATION_MODEL,
    DEFAULT_PLAY_MODEL,
    GPU_VENDOR_ORDER,
    PLAN_INTERVAL_SECONDS,
    resolve_play_model,
)
from freya.player.registry import AdapterRegistry
from freya.player.schema import (
    MAX_OBSERVATION_BYTES,
    Action,
    ActionRefused,
    ModelRefused,
    ObservationRefused,
    PermissionRefused,
    PlanRefused,
    check_observation,
    parse_goal,
    parse_plan,
)
from freya.player.service import (
    PLAY_ACT,
    PLAY_OBSERVE,
    PLAY_PROJECT,
    PLAY_PROVIDER,
    PLAY_STOP,
    create_play_provider,
    register_play_stop,
    seed_play_stop,
)
from freya.player.session import open_session, spoken_effect
from freya.providers.image import comfyui
from freya.settings import Settings
from freya.spatial_avatar.service import FUTURE_PERMISSIONS
from freya.window import build_status
from freya.tools.security_tools import OWNER_ONLY_CAPABILITIES


FOLLOW = '{"action":"follow","arguments":{"target":"owner"}}'
COLLECT = '{"action":"collect","arguments":{"item":"wood"}}'
GOAL = {"goal": "follow", "target": "owner"}
ALLOW = '[{"adapter":"mock","target":"local"}]'
TOKEN = "payload-token-9f3c"


class Immediate:
    """A brain that answers on the spot and is not asked again by poll."""

    def __init__(self, text):
        self.text = text
        self.asks = 0
        self.polls = 0
        self.messages = None

    def ask(self, messages):
        self.asks += 1
        self.messages = messages
        return self.text

    def poll(self):
        self.polls += 1
        return None


class Delayed:
    """A brain that accepts one request and never answers it."""

    def __init__(self):
        self.asks = 0
        self.polls = 0

    def ask(self, messages):
        self.asks += 1
        return None

    def poll(self):
        self.polls += 1
        return None


class HoldingKeys:
    """Reports a key still down until release_all, and never sends one."""

    def __init__(self, release=True):
        self.keys = ("w",)
        self.release = release
        self.presses = []

    def available(self):
        return True

    def press(self, key):
        self.presses.append(key)
        return InputResult(True, False, "not sent")

    def release_all(self):
        if self.release:
            self.keys = ()
        return InputResult(True, False, "released")

    def held(self):
        return self.keys


def registry():
    found = AdapterRegistry()
    register_mock(found)
    return found


def started(brain, **kwargs):
    return open_session(
        registry(),
        "mock",
        "local",
        parse_allow_list(ALLOW),
        brain,
        kwargs.pop("goal", GOAL),
        **kwargs,
    )


def walk(action, limit=8):
    game = MockGame()
    game.accept(action)
    last = None
    for _ in range(limit):
        last = game.reflex(action, IN_CONTROL)
        if game.finished:
            return game, last
    raise AssertionError("the mock did not finish")


# --- P2-1: the mock ----------------------------------------------------


def test_follow_and_collect_complete_against_the_mock():
    follow = parse_plan(FOLLOW).with_issuer("play")
    game, seen = walk(follow)

    assert game.finished is True
    assert seen["status"] == "idle"
    assert (game.x, game.y, game.z) == OWNER_AT
    assert seen["position"] == {"x": OWNER_AT[0], "y": 0, "z": 0}
    assert seen["control"] == IN_CONTROL
    assert seen["chat_trust"] == "untrusted"

    collect = parse_plan(COLLECT).with_issuer("play")
    game, seen = walk(collect)
    assert seen["status"] == "idle"
    assert (game.x, game.y, game.z) == WOOD_AT
    assert seen["inventory"] == [{"item": "wood", "count": 1}]


def test_the_mock_refuses_an_unknown_action():
    game = MockGame()
    with pytest.raises(ActionRefused, match="not one this adapter performs"):
        game.accept(Action("sprint", {}, "play"))

    with pytest.raises(ActionRefused, match="not one this adapter performs"):
        game.reflex(Action("sprint", {}, "play"), IN_CONTROL)
    assert game.status == "refused"
    assert (game.x, game.y, game.z) == (0, 0, 0)


def test_an_oversized_observation_is_refused_and_not_clipped():
    game = MockGame()
    small = game.view(IN_CONTROL)
    assert len(json.dumps(small).encode("utf-8")) <= MAX_OBSERVATION_BYTES

    huge = dict(small)
    huge["chat"] = ["a" * 200] * 20
    with pytest.raises(ObservationRefused, match="too large"):
        check_observation(huge)

    pixels = dict(small)
    pixels["pixels"] = "frame"
    with pytest.raises(ObservationRefused, match="not the schema"):
        check_observation(pixels)


def test_a_narrow_adapter_cannot_perform_what_it_did_not_declare():
    found = AdapterRegistry()
    register_mock(found, actions={"stop"})
    assert found.permits("mock", "stop") is True
    assert found.permits("mock", "follow") is False

    game = found.create("mock")
    with pytest.raises(ActionRefused):
        game.accept(parse_plan(FOLLOW).with_issuer("play"))


# --- P2-2: plans -------------------------------------------------------


def test_an_extra_field_or_a_program_string_is_refused_and_spawns_nothing(monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append("popen"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append("run"))
    monkeypatch.setattr(os, "system", lambda *a, **k: spawned.append("system"))

    extra = {"action": "follow", "arguments": {"target": "owner"}, "code": TOKEN}
    with pytest.raises(PlanRefused, match="field that is not allowed"):
        parse_plan(extra)

    program = {"action": "chat", "arguments": {"text": "function sneak() {\n  eval(" + TOKEN + ")\n}"}}
    with pytest.raises(PlanRefused, match="not a schema action"):
        parse_plan(program)

    with pytest.raises(PlanRefused, match="not JSON"):
        parse_plan("function sneak(){ " + TOKEN + " }")

    brain = Immediate(json.dumps(extra))
    session = started(brain)
    seen = session.tick(0)
    assert seen["status"] == "paused"
    assert session.adapter.x == 0
    assert TOKEN not in session.log.text()
    assert spawned == []

    # A second tick still does not build a process from the refused plan.
    session.tick(0.5)
    assert spawned == []


def test_a_refused_plan_is_audited_without_its_text():
    log = ActionLog()
    brain = Immediate('{"action":"follow","arguments":{"target":"owner"},"note":"' + TOKEN + '"}')
    session = started(brain, log=log)
    session.tick(0)

    assert TOKEN not in log.text()
    assert any(row.outcome == "refused" and row.reason == "The plan has a field that is not allowed." for row in log)
    assert all(TOKEN not in row.reason for row in log)


def test_an_accepted_action_is_recorded_as_a_name_and_a_digest():
    log = ActionLog()
    session = started(Immediate(FOLLOW), log=log)
    session.tick(0)

    actions = [row for row in log if row.kind == "action"]
    assert len(actions) == 1
    assert actions[0].action == "follow"
    assert actions[0].outcome == "accepted"
    assert actions[0].arguments_digest
    assert "owner" not in log.text()


# --- P2-8: the loop ----------------------------------------------------


def test_a_delayed_brain_stops_the_adapter_without_a_call_per_tick():
    brain = Delayed()
    session = started(brain)

    for step in range(20):
        seen = session.tick(step * 0.1)

    assert brain.asks == 1
    assert session.planner.calls == 1
    assert session.adapter.x == 0
    assert seen["status"] == "paused"
    assert any(row.reason == "The plan was late." for row in session.log) is False

    seen = session.tick(PLAN_INTERVAL_SECONDS)
    assert brain.asks == 1
    assert session.planner.calls == 1
    assert session.adapter.x == 0
    assert seen["status"] == "paused"
    assert any(row.reason == "The plan was late." for row in session.log)


def test_follow_does_not_continue_after_its_issuer_is_gone():
    brain = Immediate(FOLLOW)
    session = started(brain)
    session.tick(0)
    assert session.adapter.x == 1
    assert session.label == IN_CONTROL

    session.planner.issuer = "gone"
    seen = session.tick(0.4)

    assert session.adapter.x == 1
    assert session.planner.calls == 1
    assert seen["status"] == "paused"
    assert seen["control"] == PAUSED
    assert session.label == PAUSED
    assert any(row.reason == "The plan's issuer is gone." for row in session.log)

    session.tick(0.8)
    assert session.adapter.x == 1
    assert session.planner.calls == 1


def test_the_same_issuer_keeps_following_without_a_new_model_call():
    session = started(Immediate(FOLLOW))
    session.tick(0)
    session.tick(0.4)
    session.tick(0.8)

    assert session.adapter.x == 3
    assert session.planner.calls == 1
    assert session.label == IN_CONTROL


# --- kill switch and the indicator -------------------------------------


def test_stop_sends_no_further_actions_and_clears_control():
    brain = Immediate(FOLLOW)
    session = started(brain)
    session.tick(0)
    assert session.adapter.x == 1

    session.stop()
    seen = session.tick(1)
    session.tick(5)

    assert session.adapter.x == 1
    assert session.adapter.closed is True
    assert brain.asks == 1
    assert session.label == PAUSED
    assert seen["control"] == PAUSED
    assert seen["status"] == "paused"
    assert any(row.kind == "stop" and row.reason == "The owner stopped play." for row in session.log)


def test_a_spoken_stop_runs_stop_and_a_spoken_start_does_not_start():
    assert spoken_effect("freya stop") == "stop"
    assert spoken_effect("Stop.") == "stop"
    assert spoken_effect("freya, pause") == "stop"
    assert spoken_effect("freya start") == "refused"
    assert spoken_effect("yes") == "refused"
    assert spoken_effect("approve the target") == "refused"
    assert spoken_effect("the bridge is north") == "none"

    session = started(Immediate(FOLLOW))
    session.tick(0)
    assert session.handle_speech("freya play") == "refused"
    assert session.running is True
    assert session.label == IN_CONTROL
    assert any(
        row.reason == "A spoken request does not start play or approve a target."
        for row in session.log
    )

    assert session.handle_speech("freya stop") == "stop"
    assert session.label == PAUSED
    assert session.running is False
    moved = session.adapter.x
    session.tick(2)
    assert session.adapter.x == moved


def test_a_key_that_is_still_down_keeps_the_control_label():
    keys = HoldingKeys(release=False)
    session = started(Immediate(FOLLOW), inputs=keys)
    session.tick(0)
    session.stop()

    assert keys.presses == []
    assert session.label == IN_CONTROL
    assert session.tick(1)["control"] == IN_CONTROL

    keys.release = True
    keys.release_all()
    # stop already ran; a second stop releases and the label can clear.
    session.stop()
    assert keys.held() == ()
    assert session.label == PAUSED


def test_the_window_shows_only_the_fixed_control_text(tmp_path, fake_ollama, monkeypatch):
    monkeypatch.setattr(
        comfyui, "check_comfyui_health", lambda url: comfyui.ProviderHealth.OFFLINE
    )
    monkeypatch.setattr(
        comfyui,
        "find_launch_plan",
        lambda url, configured=None: comfyui.LaunchPlan(["x"], headless=True),
    )
    plane = build_local_control_plane(tmp_path / "control.db", None)
    try:
        settings = Settings(tmp_path / "settings.json")
        plain = build_status(None, settings, plane)
        assert "play" not in plain

        held = build_status(None, settings, plane, play_state=IN_CONTROL)
        paused = build_status(None, settings, plane, play_state=PAUSED)
        forged = build_status(None, settings, plane, play_state="Freya owns the computer")

        assert held["play"] == "Freya in control"
        assert paused["play"] == "Freya paused"
        assert "play" not in forged
    finally:
        plane.close()


# --- permissions -------------------------------------------------------


def test_a_missing_or_damaged_allow_list_allows_nothing(tmp_path):
    assert read_allow_list(tmp_path / "missing.json") == ()
    assert parse_allow_list("{") == ()
    assert parse_allow_list('[{"adapter":"mock","target":"local","extra":"no"}]') == ()
    (tmp_path / "empty").mkdir()
    assert read_allow_list(tmp_path / "empty") == ()

    with pytest.raises(PermissionRefused, match="no allow-list"):
        check_attachment(None, "mock", "local")
    with pytest.raises(PermissionRefused, match="not on the allow-list"):
        check_attachment((), "mock", "local")


def test_an_unlisted_target_a_public_address_and_eve_are_refused():
    listed = parse_allow_list(ALLOW)
    with pytest.raises(PermissionRefused, match="not on the allow-list"):
        check_attachment(listed, "mock", "other")

    public = str(ipaddress.IPv4Address(1))
    allow_public = (AllowEntry("mock", public),)
    with pytest.raises(PermissionRefused, match="not on this machine"):
        check_attachment(allow_public, "mock", public)

    dotted = (AllowEntry("mock", "far.away"),)
    with pytest.raises(PermissionRefused, match="not on this machine"):
        check_attachment(dotted, "mock", "far.away")

    with pytest.raises(PermissionRefused, match="out of scope"):
        check_attachment((AllowEntry("mock", "eve"),), "mock", "eve")
    with pytest.raises(PermissionRefused, match="out of scope"):
        check_attachment((AllowEntry("mock", "EVE Online"),), "mock", "EVE Online")
    with pytest.raises(PlanRefused, match="out of scope"):
        parse_plan({"action": "follow", "arguments": {"target": "eve online"}})

    check_attachment(listed, "mock", "local")
    loopback = (AllowEntry("mock", "127.0.0.1"),)
    check_attachment(loopback, "mock", "127.0.0.1")


def test_opening_a_session_refuses_before_the_adapter_is_built():
    built = []

    def factory():
        built.append("built")
        return MockGame()

    found = AdapterRegistry()
    from freya.player.registry import AdapterSpec
    from freya.player.schema import ACTION_NAMES

    found.register(
        AdapterSpec("mock", frozenset(ACTION_NAMES), "mock"),
        factory,
    )
    with pytest.raises(ModelRefused):
        open_session(
            found, "mock", "local", parse_allow_list(ALLOW), Immediate(FOLLOW), GOAL,
            environ={}, installed=set(),
        )
    with pytest.raises(PermissionRefused):
        open_session(
            found, "mock", "elsewhere", parse_allow_list(ALLOW), Immediate(FOLLOW), GOAL,
            environ={}, installed={DEFAULT_PLAY_MODEL},
        )
    assert built == []


# --- the model name ----------------------------------------------------


def test_the_play_model_defaults_to_the_small_one_and_an_unknown_name_refuses():
    assert resolve_play_model({}) == DEFAULT_PLAY_MODEL
    assert DEFAULT_PLAY_MODEL == "qwen3.5-4b-mtp"
    assert resolve_play_model({"FREYA_MODEL": CONVERSATION_MODEL}) == DEFAULT_PLAY_MODEL
    assert resolve_play_model({"FREYA_MODEL": CONVERSATION_MODEL}) != CONVERSATION_MODEL

    chosen = resolve_play_model(
        {"FREYA_PLAY_MODEL": "other-local"},
        installed={"other-local", DEFAULT_PLAY_MODEL},
    )
    assert chosen == "other-local"

    with pytest.raises(ModelRefused, match="not installed"):
        resolve_play_model(
            {"FREYA_PLAY_MODEL": "missing-model"},
            installed={DEFAULT_PLAY_MODEL},
        )
    with pytest.raises(ModelRefused, match="not installed"):
        resolve_play_model({}, installed=set())
    with pytest.raises(ModelRefused, match="not one Freya will use"):
        resolve_play_model({"FREYA_PLAY_MODEL": "../other"})


def test_graphics_for_a_local_planner_are_read_amd_first():
    assert GPU_VENDOR_ORDER == tuple(sorted(VENDOR_RANK, key=VENDOR_RANK.get))
    assert GPU_VENDOR_ORDER == ("amd", "intel", "nvidia")


# --- P2-13: no windows-only input --------------------------------------


def test_a_missing_input_backend_does_not_send_keys():
    backend = MissingInputBackend()
    pressed = backend.press("w")
    released = backend.release_all()

    assert backend.available() is False
    assert pressed.available is False and pressed.sent is False
    assert released.sent is False
    assert backend.held() == ()

    session = started(Immediate(FOLLOW), inputs=backend)
    session.tick(0)
    session.stop()
    assert backend.held() == ()
    assert session.label == PAUSED


def test_the_package_has_no_windows_only_or_input_simulation_calls():
    root = Path(__file__).resolve().parents[1] / "src" / "freya" / "player"
    forbidden = (
        "sendinput", "keybd_event", "mouse_event", "pyautogui", "pynput",
        "ctypes", "win32api", "quartz", "cgevent", "uinput",
        "mineflayer", "minecraft", "elite dangerous",
        "import subprocess", "os.system", "anti-cheat", "anticheat",
        "socket.bind", "httpserver",
    )
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").casefold()
        for name in forbidden:
            assert name not in text, (path.name, name)


# --- P2-14: core and the avatar ----------------------------------------


def test_core_does_not_import_the_game_package():
    import freya

    root = Path(freya.__file__).parent
    for folder in ("core", "kernel"):
        for path in (root / folder).rglob("*.py"):
            assert "freya.player" not in path.read_text(encoding="utf-8"), path

    control = (root / "control_plane.py").read_text(encoding="utf-8")
    assert "freya.player" not in control
    for name in (PLAY_STOP, PLAY_ACT, PLAY_OBSERVE, PLAY_PROJECT):
        assert name not in control


def test_capture_and_input_stay_unregistered_on_the_avatar(tmp_path, fake_ollama, monkeypatch):
    monkeypatch.setattr(
        comfyui, "check_comfyui_health", lambda url: comfyui.ProviderHealth.OFFLINE
    )
    monkeypatch.setattr(
        comfyui,
        "find_launch_plan",
        lambda url, configured=None: comfyui.LaunchPlan(["x"], headless=True),
    )
    wanted = {"screen.capture", "screen.analyze", "input.keyboard", "input.mouse"}
    assert wanted <= set(FUTURE_PERMISSIONS)

    plane = build_local_control_plane(tmp_path / "control.db", None)
    try:
        for name in wanted:
            assert plane.broker.exists(name) is False
        for name in (PLAY_STOP, PLAY_ACT, PLAY_OBSERVE, PLAY_PROJECT):
            assert plane.broker.exists(name) is False
    finally:
        plane.close()


def test_play_names_are_owner_only_and_not_model_tools():
    for name in (PLAY_STOP, PLAY_ACT, PLAY_OBSERVE, PLAY_PROJECT):
        assert name in OWNER_ONLY_CAPABILITIES


# --- the capability, deny by default -----------------------------------


@pytest.fixture
def brokered(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    called = []
    providers = ProviderRegistry()
    providers.register(create_play_provider(lambda: True))
    providers.start(PLAY_PROVIDER)
    providers.check_health(PLAY_PROVIDER)
    found = CapabilityRegistry(providers)
    register_play_stop(found, lambda: called.append("stopped"))
    policy = SqlitePolicyEngine(store)
    audit = SqliteAuditLog(store)
    audit.start()
    broker = CapabilityBroker(found, policy, audit, LOCAL_OWNER)
    yield broker, policy, store, called
    store.close()


def test_play_stop_is_denied_until_the_owner_seeds_it_and_then_audited(brokered):
    broker, policy, store, called = brokered
    denied = broker.invoke(PLAY_STOP)

    assert denied.success is False
    assert denied.outcome == "denied"
    assert called == []

    assert seed_play_stop(policy, LOCAL_OWNER.actor) is True
    allowed = broker.invoke(PLAY_STOP)

    assert allowed.success is True
    assert allowed.result == {"control": PAUSED}
    assert called == ["stopped"]

    rows = store.execute(
        "SELECT capability, outcome, decision_reason FROM audit_event WHERE capability = ?",
        (PLAY_STOP,),
    ).fetchall()
    assert ("play.stop", "succeeded") in [(row["capability"], row["outcome"]) for row in rows]
    assert all(TOKEN not in (row["decision_reason"] or "") for row in rows)


def test_the_session_kill_switch_does_not_need_the_policy_seed(tmp_path):
    """Removing control is not a second approval, and it can be chained onto the audit log."""
    store = ControlStore(tmp_path / "control.db")
    audit = SqliteAuditLog(store)
    audit.start()

    def sink(row):
        audit.record(
            operation_id="play-" + row.kind,
            kind="outcome",
            actor=LOCAL_OWNER.actor,
            client=LOCAL_OWNER.client,
            domain=LOCAL_OWNER.domain,
            capability=PLAY_STOP if row.kind == "stop" else "play.act",
            decision="allow",
            decision_reason=row.reason,
            outcome=row.outcome,
            arguments_digest=row.arguments_digest or None,
        )

    log = ActionLog(sink)
    session = started(Immediate(FOLLOW), log=log)
    session.tick(0)
    session.stop()

    text = log.text()
    assert TOKEN not in text
    rows = store.execute(
        "SELECT capability, decision_reason, outcome FROM audit_event"
    ).fetchall()
    blob = json.dumps([tuple(row) for row in rows])
    assert "play.stop" in blob
    assert "The owner stopped play." in blob
    assert TOKEN not in blob
    store.close()


def test_a_goal_is_the_same_closed_schema():
    goal = parse_goal(GOAL)
    assert goal.name == "follow"
    assert dict(goal.arguments) == {"target": "owner"}
    with pytest.raises(PlanRefused, match="field that is not allowed"):
        parse_goal({"goal": "follow", "target": "owner", "code": TOKEN})
