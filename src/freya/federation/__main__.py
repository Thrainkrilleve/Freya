"""
The owner's commands for machines that could lend Freya hardware.

    python -m freya.federation profile [--save FILE] [--name NAME]
    python -m freya.federation add FILE [--address ADDRESS]
    python -m freya.federation tailnet
    python -m freya.federation nodes
    python -m freya.federation approve NODE [--role ROLE] [--days N]
    python -m freya.federation decline NODE
    python -m freya.federation renew NODE [--days N]
    python -m freya.federation revoke NODE

`approve` asks for the machine's join code at the keyboard, so it is a
typed approval by whoever sits at this PC. Nothing here contacts
another machine.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from freya.federation import profile as node_profile
from freya.federation.registry import (
    DEFAULT_VALID_DAYS,
    OWNER_PRINCIPAL,
    NodeRegistry,
    OwnerApproval,
    RegistryError,
)
from freya.federation.network import read_tailnet
from freya.federation.roles import ROLES, SANDBOX_HOST_CONDITIONS, suggest_roles

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
REGISTRY_FILE = "federation_nodes.json"
MAX_OFFER_FILE_BYTES = 16_384


def _date(seconds):
    return time.strftime("%Y-%m-%d", time.localtime(seconds))


def _show_node(node, out):
    out(f"{node['node_id']}  {node['name']}  [{node['state']}"
        f"{', expired' if node.get('expired') else ''}]")
    for line in node_profile.describe(node["profile"]):
        out(f"    {line}")
    if node.get("address"):
        out(f"    Reached at {node['address']} ({node['network']})")
    if node["state"] == "enrolled":
        out(f"    Enrolled for: {', '.join(node['roles'])}; "
            f"until {_date(node['expires_at'])}")
        if not node.get("fingerprint"):
            out("    Not yet bound to a key, so no work can be sent to it.")
    if node["state"] == "offered":
        out("    Could do:")
        for suggestion in suggest_roles(node["profile"]):
            marker = "" if suggestion.allowed_now else " (not in this version)"
            out(f"      - {suggestion.role}{marker}: {suggestion.reason}")


def _show_tailnet(registry, read, out):
    devices, problem = read()
    if problem:
        out(problem)
        return 1
    known = {node.get("address") for node in registry.nodes()}
    if not devices:
        out("No other machines are on the tailnet.")
    for device in devices:
        reached = device["dns_name"] or device["addresses"][0]
        enrolled = known & ({device["dns_name"]} | set(device["addresses"]))
        status = "already on Freya's list" if enrolled else "not offered yet"
        out(f"{device['name']} ({device['os'] or 'unknown system'}, "
            f"{'online' if device['online'] else 'offline'}): {reached}, {status}")
    if devices:
        out("Being on the tailnet does not make a machine a worker. To offer "
            "one, run profile.py on it and add the file here with "
            "--address set to its tailnet name.")
    return 0


def main(argv=None, registry=None, read_code=input, out=print,
         tailnet=read_tailnet):
    parser = argparse.ArgumentParser(prog="python -m freya.federation")
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("profile", help="what this machine could lend")
    show.add_argument("--save", metavar="FILE")
    show.add_argument("--name", default="")

    add = commands.add_parser("add", help="record a machine's offer file")
    add.add_argument("file")
    add.add_argument("--address", help="its home-network or tailnet address")

    commands.add_parser("tailnet", help="machines on the owner's tailnet")

    commands.add_parser("nodes", help="list machines and offers")

    approve = commands.add_parser("approve", help="enroll a machine")
    approve.add_argument("node")
    approve.add_argument("--role", action="append", choices=sorted(ROLES))
    approve.add_argument("--days", type=int, default=DEFAULT_VALID_DAYS)

    decline = commands.add_parser("decline", help="turn an offer down")
    decline.add_argument("node")

    renew = commands.add_parser("renew", help="extend an enrollment")
    renew.add_argument("node")
    renew.add_argument("--days", type=int, default=DEFAULT_VALID_DAYS)

    revoke = commands.add_parser("revoke", help="remove a machine now")
    revoke.add_argument("node")

    arguments = parser.parse_args(argv)

    if arguments.command == "profile":
        forwarded = []
        if arguments.save:
            forwarded += ["--save", arguments.save]
        if arguments.name:
            forwarded += ["--name", arguments.name]
        return node_profile.main(forwarded)

    if registry is None:
        registry = NodeRegistry(DATA_DIR / REGISTRY_FILE)

    if registry.unreadable:
        out("The list of machines could not be read. Nothing was changed; "
            f"repair or move {REGISTRY_FILE} in Freya's data folder.")
        return 1

    owner = OwnerApproval(OWNER_PRINCIPAL, "typed")

    try:
        if arguments.command == "add":
            path = Path(arguments.file)
            if path.stat().st_size > MAX_OFFER_FILE_BYTES:
                out("That file is too large to be a machine offer.")
                return 1
            try:
                offer = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                out("That file could not be read as a machine offer.")
                return 1
            node_id = registry.record_offer(offer, address=arguments.address)
            out(f"Recorded as {node_id}. Nothing was enrolled.")
            for node in registry.waiting():
                if node["node_id"] == node_id:
                    _show_node(node, out)
            out(f"To enroll it: python -m freya.federation approve {node_id}")

        elif arguments.command == "tailnet":
            return _show_tailnet(registry, tailnet, out)

        elif arguments.command == "nodes":
            listed = registry.nodes()
            if not listed:
                out("No machines yet. On a spare machine, run "
                    "'python profile.py --save offer.json' and bring the "
                    "file here.")
            for node in listed:
                _show_node(node, out)

        elif arguments.command == "approve":
            roles = arguments.role or ["model-worker"]
            if "sandbox-host" in roles:
                out("A sandbox host is allowed only while all of these hold:")
                for condition in SANDBOX_HOST_CONDITIONS:
                    out(f"  - {condition}")
            code = read_code("Join code shown on the machine: ")
            registry.approve(arguments.node, code, owner, roles, arguments.days)
            out(f"{arguments.node} is enrolled for {', '.join(roles)} for "
                f"{arguments.days} days. No work can reach it until the "
                "worker channel is built and binds its key.")

        elif arguments.command == "decline":
            registry.decline(arguments.node)
            out(f"{arguments.node}'s offer was turned down.")

        elif arguments.command == "renew":
            registry.renew(arguments.node, owner, arguments.days)
            out(f"{arguments.node} is renewed for {arguments.days} days.")

        elif arguments.command == "revoke":
            registry.revoke(arguments.node)
            out(f"{arguments.node} is removed.")

    except RegistryError as error:
        out(str(error))
        return 1
    except OSError:
        out("That file could not be read.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
