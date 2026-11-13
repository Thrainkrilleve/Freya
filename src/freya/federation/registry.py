"""
The machines that may lend Freya hardware, and the owner's decisions
about them (proposed NEW-DB to NEW-DM, `docs/design/worker-nodes.md`).

A machine reaches this registry only as an *offer*: a profile it
measured itself and a join code it showed on its own screen. Being
found, or sending an offer, enrolls nothing (DIST-003). Only the owner
enrolls a machine, by typing that code at the main PC; a spoken request
cannot, because voice is not authentication. An enrollment is for named
roles only, and it expires unless renewed. Revoking is immediate and
needs no approval, so a machine can always be removed.

`may_assign` is the one question the rest of Freya asks before sending
work to a node, and it fails closed: an unknown, expired, revoked,
unbound or unreadable entry gets nothing.

This version has no network side at all. No node can be reached, so
an enrollment records the owner's decision and nothing more until the
worker channel is built (see the design).
"""

import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from freya.federation.network import classify_address, worker_address_allowed
from freya.federation.roles import (
    ROLES,
    ROLES_ALLOWED_NOW,
    suggest_roles,
)


OWNER_PRINCIPAL = "local-owner"

# How the owner's approval reached Freya. Voice is ambient input and
# is never enough to enroll or renew a machine (NEW-I, proposed).
APPROVAL_CHANNELS = ("typed", "window")

STATE_OFFERED = "offered"
STATE_ENROLLED = "enrolled"
STATE_DECLINED = "declined"
STATE_REVOKED = "revoked"
STATES = (STATE_OFFERED, STATE_ENROLLED, STATE_DECLINED, STATE_REVOKED)

DEFAULT_VALID_DAYS = 30
MAX_VALID_DAYS = 90
DAY_SECONDS = 86_400

# A noisy or hostile network cannot fill the list with offers.
MAX_OPEN_OFFERS = 16
# An offer is withdrawn after this many wrong codes; the machine must
# make a new one, with a new code.
MAX_CODE_ATTEMPTS = 3
OFFER_LIFETIME_SECONDS = 7 * DAY_SECONDS

MAX_NAME_CHARACTERS = 64
MAX_OFFER_BYTES = 16_384
MAX_HISTORY = 500

CODE_PATTERN = re.compile(r"\d{8}\Z")
FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class RegistryError(RuntimeError):
    """
    The change was not made. The message is fixed wording that is safe
    to show and to record; it never repeats what an offer contained.
    """


@dataclass(frozen=True)
class OwnerApproval:
    principal: str
    channel: str


@dataclass(frozen=True)
class AssignDecision:
    allowed: bool
    reason: str


def _clean_name(text):
    text = _CONTROL.sub("", str(text or "")).strip()
    return text[:MAX_NAME_CHARACTERS] or "unnamed machine"


def _number(value, minimum=0, maximum=2 ** 62):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    if value < minimum or value > maximum:
        return None
    return value


def clean_profile(profile):
    """
    Keep only the fields Freya reads, as plain bounded values. A
    profile is the machine's own statement and is never trusted for
    more than a suggestion.
    """
    if not isinstance(profile, dict):
        raise RegistryError("The offer has no readable hardware profile.")

    os_info = profile.get("os") if isinstance(profile.get("os"), dict) else {}
    cpu = profile.get("cpu") if isinstance(profile.get("cpu"), dict) else {}

    gpus = []
    for gpu in (profile.get("gpus") or [])[:8]:
        if not isinstance(gpu, dict):
            continue
        memory = _number(gpu.get("memory_bytes"), 1)
        if memory is None:
            continue
        gpus.append({
            "vendor": _clean_name(gpu.get("vendor"))[:16],
            "name": _clean_name(gpu.get("name")),
            "memory_bytes": memory,
        })

    return {
        "os": {
            "system": _clean_name(os_info.get("system"))[:32],
            "release": _clean_name(os_info.get("release"))[:32],
            "architecture": _clean_name(os_info.get("architecture"))[:32],
        },
        "cpu": {
            "model": _clean_name(cpu.get("model")),
            "logical_cores": _number(cpu.get("logical_cores"), 0, 4096) or 0,
        },
        "ram_bytes": _number(profile.get("ram_bytes"), 1),
        "gpus": gpus,
        "disk_free_bytes": _number(profile.get("disk_free_bytes"), 0),
    }


class NodeRegistry:
    """
    Kept in one JSON file. A file that cannot be read makes the
    registry refuse every assignment and every change until the owner
    deals with it; it is never silently replaced.
    """

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._nodes: Dict[str, dict] = {}
        self._history: List[dict] = []
        self.unreadable = False
        self._load()

    # Reading and writing the file

    def _load(self):
        if not self.path.exists():
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            nodes = data["nodes"]
            history = data.get("history", [])
            if not isinstance(nodes, dict) or not isinstance(history, list):
                raise ValueError
            for node in nodes.values():
                if node.get("state") not in STATES:
                    raise ValueError
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.unreadable = True
            return

        self._nodes = nodes
        self._history = history[-MAX_HISTORY:]

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "nodes": self._nodes, "history": self._history},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _writable(self):
        if self.unreadable:
            raise RegistryError(
                "The list of machines could not be read, so it cannot be "
                "changed until the owner repairs or removes the file."
            )

    def _record(self, node_id, event, **details):
        self._history.append({
            "at": int(self._clock()),
            "node_id": node_id,
            "event": event,
            **details,
        })
        del self._history[:-MAX_HISTORY]

    # Offers

    def record_offer(self, offer, fingerprint=None, address=None):
        """
        Record a machine's offer. It is a suggestion only: nothing about
        the machine changes, and nothing can be sent to it.

        `offer` is what `profile.py --save` writes. `fingerprint` is the
        SHA-256 of the machine's public key, when the offer came over a
        channel that proves one; a file carried by hand has none.
        `address` is where the machine will be reached: on the home
        network or the tailnet, never a public address.
        """
        if len(json.dumps(offer, default=str)) > MAX_OFFER_BYTES:
            raise RegistryError("The offer is too large to read.")
        if not isinstance(offer, dict) or offer.get("kind") != "freya-node-offer":
            raise RegistryError("This is not a Freya machine offer.")

        code = str(offer.get("join_code", ""))
        if not CODE_PATTERN.match(code):
            raise RegistryError("The offer has no valid join code.")

        if fingerprint is not None and not FINGERPRINT_PATTERN.match(
            str(fingerprint)
        ):
            raise RegistryError("The machine's key could not be read.")

        if address is not None and not worker_address_allowed(address):
            raise RegistryError(
                "A machine must be on the home network or the tailnet."
            )

        profile = clean_profile(offer.get("profile"))

        with self._lock:
            self._writable()
            now = self._clock()
            self._expire_offers(now)

            open_offers = [
                node for node in self._nodes.values()
                if node["state"] == STATE_OFFERED
            ]
            if len(open_offers) >= MAX_OPEN_OFFERS:
                raise RegistryError(
                    "There are too many machines waiting for a decision."
                )

            node_id = "node-" + secrets.token_hex(4)
            self._nodes[node_id] = {
                "node_id": node_id,
                "name": _clean_name(offer.get("name")),
                "state": STATE_OFFERED,
                "profile": profile,
                "suggested_roles": [s.role for s in suggest_roles(profile)],
                "join_code": code,
                "code_attempts": 0,
                "fingerprint": fingerprint,
                "address": str(address).strip().lower() if address else None,
                "network": classify_address(address) if address else None,
                "offered_at": int(now),
                "roles": [],
                "enrolled_at": None,
                "expires_at": None,
                "approved_via": None,
            }
            self._record(node_id, "offered")
            self._save()
            return node_id

    def _expire_offers(self, now):
        for node in self._nodes.values():
            if (
                node["state"] == STATE_OFFERED
                and now - node["offered_at"] > OFFER_LIFETIME_SECONDS
            ):
                node["state"] = STATE_DECLINED
                node["join_code"] = None
                self._record(node["node_id"], "offer-lapsed")

    # The owner's decisions

    def _check_approval(self, approval):
        if not isinstance(approval, OwnerApproval):
            raise RegistryError("Only the owner can approve a machine.")
        if approval.principal != OWNER_PRINCIPAL:
            raise RegistryError("Only the owner can approve a machine.")
        if approval.channel not in APPROVAL_CHANNELS:
            raise RegistryError(
                "A machine can only be approved by typing at this PC, "
                "not by voice."
            )

    def _check_roles(self, roles):
        roles = list(dict.fromkeys(roles or []))
        if not roles:
            raise RegistryError("Name at least one role for the machine.")
        for role in roles:
            if role not in ROLES:
                raise RegistryError("That role does not exist.")
            if role not in ROLES_ALLOWED_NOW:
                raise RegistryError(ROLES[role].why_not_yet)
        return roles

    def _valid_seconds(self, valid_days):
        days = _number(valid_days, 1, MAX_VALID_DAYS)
        if days is None:
            raise RegistryError(
                f"An enrollment lasts from 1 to {MAX_VALID_DAYS} days."
            )
        return days * DAY_SECONDS

    def approve(
        self,
        node_id,
        typed_code,
        approval,
        roles,
        valid_days=DEFAULT_VALID_DAYS,
    ):
        """
        Enroll a machine for `roles`, after the owner typed the code
        the machine showed. Three wrong codes withdraw the offer.
        """
        self._check_approval(approval)
        roles = self._check_roles(roles)
        seconds = self._valid_seconds(valid_days)
        typed = re.sub(r"[\s-]", "", str(typed_code or ""))

        with self._lock:
            self._writable()
            now = self._clock()
            self._expire_offers(now)
            node = self._nodes.get(node_id)

            if node is None or node["state"] != STATE_OFFERED:
                raise RegistryError("No machine is waiting under that name.")

            if not hmac.compare_digest(
                typed.encode("utf-8"), node["join_code"].encode("utf-8")
            ):
                node["code_attempts"] += 1
                if node["code_attempts"] >= MAX_CODE_ATTEMPTS:
                    node["state"] = STATE_DECLINED
                    node["join_code"] = None
                    self._record(node_id, "offer-withdrawn-wrong-code")
                    self._save()
                    raise RegistryError(
                        "The code was wrong three times, so the offer was "
                        "withdrawn. The machine must make a new one."
                    )
                self._record(node_id, "wrong-code")
                self._save()
                raise RegistryError(
                    "That code does not match the one the machine showed."
                )

            node.update({
                "state": STATE_ENROLLED,
                "roles": roles,
                "join_code": None,
                "enrolled_at": int(now),
                "expires_at": int(now + seconds),
                "approved_via": approval.channel,
            })
            self._record(node_id, "enrolled", roles=roles)
            self._save()

    def decline(self, node_id):
        with self._lock:
            self._writable()
            node = self._nodes.get(node_id)
            if node is None or node["state"] != STATE_OFFERED:
                raise RegistryError("No machine is waiting under that name.")
            node["state"] = STATE_DECLINED
            node["join_code"] = None
            self._record(node_id, "declined")
            self._save()

    def renew(self, node_id, approval, valid_days=DEFAULT_VALID_DAYS):
        """
        Extend an enrollment that has not been revoked. An expired one
        may be renewed, since the owner is deciding again now.
        """
        self._check_approval(approval)
        seconds = self._valid_seconds(valid_days)

        with self._lock:
            self._writable()
            node = self._nodes.get(node_id)
            if node is None or node["state"] != STATE_ENROLLED:
                raise RegistryError("That machine is not enrolled.")
            node["expires_at"] = int(self._clock() + seconds)
            node["approved_via"] = approval.channel
            self._record(node_id, "renewed")
            self._save()

    def revoke(self, node_id):
        """
        Remove a machine at once. Any way of asking is enough: taking
        authority away must never be harder than giving it.
        """
        with self._lock:
            self._writable()
            node = self._nodes.get(node_id)
            if node is None or node["state"] not in (
                STATE_OFFERED, STATE_ENROLLED
            ):
                raise RegistryError("That machine is not on the list.")
            node["state"] = STATE_REVOKED
            node["roles"] = []
            node["join_code"] = None
            self._record(node_id, "revoked")
            self._save()

    # Questions

    def may_assign(self, node_id, role) -> AssignDecision:
        """
        May work for `role` go to this node now? Fails closed.
        """
        with self._lock:
            if self.unreadable:
                return AssignDecision(False, "The list of machines is unreadable.")

            node = self._nodes.get(node_id)
            if node is None:
                return AssignDecision(False, "The machine is not on the list.")
            if node["state"] != STATE_ENROLLED:
                return AssignDecision(False, "The machine is not enrolled.")
            if node["expires_at"] is None or self._clock() >= node["expires_at"]:
                return AssignDecision(False, "The enrollment has expired.")
            if role not in node["roles"]:
                return AssignDecision(
                    False, "The machine is not enrolled for that role."
                )
            if not node.get("fingerprint"):
                # An enrollment from a file carried by hand proves nothing
                # about which machine will later connect. The worker
                # channel must bind a key, with the owner approving again.
                return AssignDecision(
                    False, "The machine is not yet bound to a key."
                )

            return AssignDecision(True, "Enrolled.")

    def nodes(self) -> List[dict]:
        """
        Every entry as the owner would see it, without join codes.
        """
        with self._lock:
            now = self._clock()
            listed = []
            for node in self._nodes.values():
                entry = {
                    key: value for key, value in node.items()
                    if key != "join_code"
                }
                entry["expired"] = bool(
                    node["state"] == STATE_ENROLLED
                    and node["expires_at"] is not None
                    and now >= node["expires_at"]
                )
                listed.append(entry)
            return sorted(listed, key=lambda entry: entry["offered_at"])

    def waiting(self) -> List[dict]:
        """
        Offers waiting for the owner, with what each machine could do.
        This is the "Freya found hardware and suggests it" list.
        """
        return [node for node in self.nodes() if node["state"] == STATE_OFFERED]

    def history(self) -> List[dict]:
        with self._lock:
            return list(self._history)
