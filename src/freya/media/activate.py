"""
Enable Freya's media capabilities: an owner action.

    python -m freya.media.activate                 show what is enabled
    python -m freya.media.activate --grant all     enable every media capability
    python -m freya.media.activate --grant media.catalog.read media.playback

Each capability becomes an allow rule for the local owner in the
control database. Nothing is granted unless this is run. Existing
rules are never changed: a capability the owner denied stays denied.
A grant takes effect at once; offering `media.recall` to the model
takes effect at Freya's next start.
"""

import argparse
import sys

from freya.core.capabilities import LOCAL_OWNER
from freya.core.persistence import CONTROL_DATABASE_PATH, ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.media.service import MEDIA_CAPABILITIES


REASON = "Enabled by the owner with freya.media.activate."


def describe(policy, actor=LOCAL_OWNER.actor) -> dict:
    return {name: policy.decide(actor, name).allow for name in MEDIA_CAPABILITIES}


def grant(policy, names, actor=LOCAL_OWNER.actor) -> list:
    """
    Add an allow rule for each named media capability not already
    allowed. Returns the names that were granted.
    """
    if list(names) == ["all"]:
        names = MEDIA_CAPABILITIES

    unknown = [name for name in names if name not in MEDIA_CAPABILITIES]

    if unknown:
        raise ValueError("Not media capabilities: " + ", ".join(unknown))

    granted = []

    for name in names:
        if policy.decide(actor, name).allow:
            continue

        existing = {
            (rule["subject"], rule["capability"], rule["effect"]) for rule in policy.list_rules()
        }

        if (actor, name, "allow") not in existing:
            policy.add_rule(actor, name, "allow", REASON)
            granted.append(name)

    return granted


def main(argv=None, database=CONTROL_DATABASE_PATH, out=print) -> int:
    parser = argparse.ArgumentParser(prog="python -m freya.media.activate")
    parser.add_argument("--grant", nargs="+", metavar="CAPABILITY")
    arguments = parser.parse_args(argv)

    store = ControlStore(database)

    try:
        policy = SqlitePolicyEngine(store)

        if arguments.grant:
            try:
                granted = grant(policy, arguments.grant)
            except ValueError as exc:
                out(str(exc))
                return 2

            out("Enabled: " + (", ".join(granted) if granted else "nothing new"))

        for name, allowed in describe(policy).items():
            out(f"  {'enabled ' if allowed else 'disabled'}  {name}")

        if not all(describe(policy).values()):
            out("A capability still disabled may be denied by a rule, or not granted yet.")
    finally:
        store.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
