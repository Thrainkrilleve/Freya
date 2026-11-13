"""
Where a worker may be reached, and which machines are on the owner's
tailnet.

Tailscale is an optional private network underneath the worker channel
(owner decision, 2026-11-10: build it in early). It is never enough on
its own: being on the tailnet says a device signed in to the owner's
Tailscale account, not that the owner enrolled it as a worker, so the
registry and, later, mutual TLS still decide (NEW-DC, NEW-DH).

This module only reads. `read_tailnet` asks the owner's installed
Tailscale program for its status; it never logs in, changes settings,
or contacts another machine. Installing Tailscale is an owner action.
"""

import ipaddress
import json
import os
import re
import shutil
import subprocess

LOOPBACK = "loopback"
TAILNET = "tailnet"
HOME = "home"
PUBLIC = "public"
INVALID = "invalid"

# Tailscale gives each device an address from the shared carrier-grade
# NAT range and from its own IPv6 prefix, and MagicDNS names end in
# ts.net.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
TAILNET_SUFFIX = ".ts.net"

# Where a worker may live: on the home network or on the tailnet. A
# public address is refused, because the split runtimes Freya may use
# (llama.cpp RPC among them) say they must never be exposed.
WORKER_KINDS = (HOME, TAILNET)

_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?\Z"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

STATUS_COMMAND = ["status", "--json"]
STATUS_TIMEOUT_SECONDS = 10
MAX_STATUS_BYTES = 2_000_000
MAX_PEERS = 64
WINDOWS_TAILSCALE = r"C:\Program Files\Tailscale\tailscale.exe"
QUERY_ENVIRONMENT = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "LANG")


def classify_address(host):
    """
    What kind of address `host` is. A name is accepted only if it is a
    MagicDNS name; any other name could resolve anywhere, so it is
    invalid here and the owner gives an address instead.
    """
    host = str(host or "")
    if _CONTROL.search(host):
        return INVALID
    host = host.strip().lower()
    if not host:
        return INVALID

    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.rstrip(".").endswith(TAILNET_SUFFIX) and _HOSTNAME.match(host):
            return TAILNET
        return INVALID

    if address.is_loopback:
        return LOOPBACK
    if address in TAILNET_V4 or address in TAILNET_V6:
        return TAILNET
    if address.is_private or address.is_link_local:
        return HOME
    return PUBLIC


def worker_address_allowed(host):
    return classify_address(host) in WORKER_KINDS


def _tailscale_program():
    found = shutil.which("tailscale")
    if found:
        return found
    if os.name == "nt" and os.path.exists(WINDOWS_TAILSCALE):
        return WINDOWS_TAILSCALE
    return None


def _query_environment():
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in QUERY_ENVIRONMENT
    }


def _run_status():
    program = _tailscale_program()
    if program is None:
        return None
    try:
        completed = subprocess.run(
            [program, *STATUS_COMMAND],
            env=_query_environment(),
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _clean(text, limit=64):
    return _CONTROL.sub("", str(text or "")).strip()[:limit]


def parse_status(text):
    """
    The other devices on the tailnet, from `tailscale status --json`.
    Returns None when the text is not a status this code understands.
    """
    if not text or len(text) > MAX_STATUS_BYTES:
        return None
    try:
        status = json.loads(text)
    except ValueError:
        return None
    if not isinstance(status, dict):
        return None

    peers = status.get("Peer") or {}
    if not isinstance(peers, dict):
        return None

    devices = []
    for peer in list(peers.values())[:MAX_PEERS]:
        if not isinstance(peer, dict):
            continue
        addresses = [
            address for address in (peer.get("TailscaleIPs") or [])
            if isinstance(address, str) and classify_address(address) == TAILNET
        ]
        if not addresses:
            continue
        dns_name = _clean(peer.get("DNSName"), 253).rstrip(".")
        devices.append({
            "name": _clean(peer.get("HostName")) or "unnamed device",
            "dns_name": dns_name if classify_address(dns_name) == TAILNET else "",
            "addresses": addresses[:4],
            "os": _clean(peer.get("OS"), 32),
            "online": peer.get("Online") is True,
        })
    return devices


def read_tailnet(run=_run_status):
    """
    (devices, problem). `problem` is a fixed sentence when the tailnet
    could not be read, and devices is then empty.
    """
    text = run()
    if text is None:
        return [], (
            "Tailscale is not installed or not running on this PC, so no "
            "tailnet machines could be listed."
        )
    devices = parse_status(text)
    if devices is None:
        return [], "Tailscale's status could not be read."
    return devices, ""
