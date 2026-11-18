"""
Run the Minecraft companion against a loopback server.

The owner writes the allow-list and starts the server. This command
carries out one named goal. Reflexes stay in the adapter. The play
model name is printed and is not loaded: a session that loads a local
planner passes a graphics reading into `open_session`, and an unknown
reading refuses.

Stop by typing stop, or with Ctrl+C. The child is terminated.
"""

import argparse
import json
import os
import sys
import time

from freya.player.indicator import PAUSED
from freya.player.minecraft.adapter import register_minecraft
from freya.player.minecraft.bridge import parse_target
from freya.player.permissions import check_attachment, read_allow_list
from freya.player.planner import resolve_play_model
from freya.player.registry import AdapterRegistry
from freya.player.schema import GOAL_NAMES, PlanRefused, PlayError
from freya.player.session import open_session, spoken_effect


class _GoalBrain:
    """Repeats one goal. It is not a model."""

    def __init__(self, text):
        self.text = text

    def ask(self, messages):
        return self.text

    def poll(self):
        return None


def parse_cli(argv):
    parser = argparse.ArgumentParser(
        description="Freya's Minecraft companion for a loopback world.",
    )
    parser.add_argument("--allow", required=True, help="Allow-list JSON the owner wrote.")
    parser.add_argument("--target", required=True, help="Loopback host and optional port.")
    parser.add_argument(
        "--goal",
        default="get_wood",
        help="One of build_shelter, get_iron, get_wood, get_food.",
    )
    return parser.parse_args(argv)


def prepare(argv, environ=None):
    """
    Check the goal, the allow-list, and the model name.

    Nothing is spawned. A bad target or a bad goal raises before the
    caller builds an adapter.
    """
    args = parse_cli(argv)
    if args.goal not in GOAL_NAMES:
        raise PlanRefused("The plan names no known action.")
    entries = read_allow_list(args.allow)
    check_attachment(entries, "minecraft", args.target)
    parse_target(args.target)
    model = resolve_play_model(environ)
    return args, entries, model


def main(argv=None, environ=None) -> int:
    source = os.environ if environ is None else environ
    try:
        args, entries, model = prepare(argv, source)
    except PlayError as exc:
        print(str(exc), flush=True)
        return 1

    print("play model " + model, flush=True)
    found = AdapterRegistry()
    register_minecraft(found, target=args.target, environ=source, stderr=None)
    goal = {"goal": args.goal}
    brain = _GoalBrain(json.dumps({"action": args.goal, "arguments": {}}))
    try:
        session = open_session(
            found,
            "minecraft",
            args.target,
            entries,
            brain,
            goal,
            environ=source,
            installed=None,
        )
    except PlayError as exc:
        print(str(exc), flush=True)
        return 1

    print(session.label, flush=True)
    watcher = _stdin(session)
    watcher.start()
    last = None
    code = 0
    try:
        while session.running:
            try:
                seen = session.tick(time.monotonic())
            except PlayError as exc:
                print(str(exc), flush=True)
                code = 1
                break
            line = seen.get("control", PAUSED) + " " + seen.get("status", "paused")
            if line != last:
                print(line, flush=True)
                last = line
            time.sleep(0.25)
    except KeyboardInterrupt:
        code = 0
    finally:
        session.stop()
        print(PAUSED, flush=True)
    return code


def _stdin(session):
    import threading

    def watch():
        try:
            for line in sys.stdin:
                if spoken_effect(line) == "stop" or line.strip().casefold() == "stop":
                    session.stop()
                    return
        except Exception:
            return

    thread = threading.Thread(target=watch, name="freya-play-stop", daemon=True)
    return thread


if __name__ == "__main__":
    raise SystemExit(main())
