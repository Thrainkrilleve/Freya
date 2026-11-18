"""
The pipe between Freya and the Minecraft child.

The child is a Node program the owner installed. Freya does not
download it. Lines of JSON on the child's standard input and output
are the only channel. The child connects outward to a loopback
address; this process does not listen. `stop` ends the child without
waiting for the action in progress.

Sign-in is offline unless `FREYA_MINECRAFT_AUTH` is `microsoft`.
Account caches stay with that program, outside this repository.
Nothing here reads or writes them, and a token is not an argument.
"""

import ipaddress
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

import psutil

from freya.child_environment import child_environment
from freya.player.schema import ActionRefused, PermissionRefused, PlayError


_FIXED_ERRORS = frozenset({
    "That action is not one this adapter performs.",
    "The action failed.",
    "The bot is not in a world.",
    "Play has stopped.",
    "The plan is not JSON.",
    "The plan is not a schema action.",
})

_READ_SLICE = 0.1


def script_path() -> Path:
    """The Node program shipped beside the repository, not inside Core."""
    return Path(__file__).resolve().parents[4] / "adapters" / "minecraft" / "bot.js"


def parse_target(target):
    """
    `(host, port)` for a loopback Minecraft server.

    Anything else is refused before a process is started. The host
    returned is a canonical loopback form.
    """
    if not isinstance(target, str):
        raise PermissionRefused("That target is not on this machine.")
    text = target.strip()
    if not text or any(mark in text for mark in (" ", "/", "\\", "\n", "\t", "@")):
        raise PermissionRefused("That target is not on this machine.")

    host = text
    raw_port = "25565"
    if host.startswith("["):
        end = host.find("]")
        if end < 1:
            raise PermissionRefused("That target is not on this machine.")
        rest = text[end + 1:]
        host = host[1:end]
        if rest:
            if not rest.startswith(":"):
                raise PermissionRefused("That target is not on this machine.")
            raw_port = rest[1:]
    elif host.count(":") == 1 and host.rsplit(":", 1)[-1].isdigit():
        host, raw_port = host.rsplit(":", 1)
    elif host.count(":") > 1 and host != "::1":
        raise PermissionRefused("That target is not on this machine.")

    try:
        port = int(raw_port)
    except ValueError as exc:
        raise PermissionRefused("That target is not on this machine.") from exc
    if not 1 <= port <= 65535:
        raise PermissionRefused("That target is not on this machine.")

    if host.casefold() == "localhost":
        return "localhost", port
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise PermissionRefused("That target is not on this machine.") from exc
    if not address.is_loopback:
        raise PermissionRefused("That target is not on this machine.")
    if isinstance(address, ipaddress.IPv6Address):
        return address.compressed, port
    return str(address), port


def resolve_auth(environ) -> str:
    """`offline` or `microsoft`. Any other value is refused, and not echoed."""
    raw = "" if environ is None else environ.get("FREYA_MINECRAFT_AUTH", "")
    text = "" if raw is None else str(raw).strip().casefold()
    if text in {"", "offline"}:
        return "offline"
    if text == "microsoft":
        return "microsoft"
    raise PermissionRefused("That sign-in is not one Freya will use.")


def resolve_username(environ) -> str:
    raw = "" if environ is None else environ.get("FREYA_MINECRAFT_USERNAME", "")
    text = "" if raw is None else str(raw).strip()
    if not text:
        return "Freya"
    if not text.isascii() or not all(ch.isalnum() or ch == "_" for ch in text) or not 1 <= len(text) <= 16:
        raise PermissionRefused("That player name is not one Freya will use.")
    return text


def resolve_version(environ) -> str:
    raw = "" if environ is None else environ.get("FREYA_MINECRAFT_VERSION", "")
    text = "" if raw is None else str(raw).strip()
    if not text:
        return ""
    parts = text.split(".")
    if len(parts) not in {2, 3} or not all(part.isdigit() and part != "" for part in parts):
        raise PermissionRefused("That version is not one Freya will use.")
    return text


def node_command(environ) -> str:
    raw = "" if environ is None else environ.get("FREYA_MINECRAFT_NODE", "")
    text = "" if raw is None else str(raw).strip()
    if not text:
        return "node"
    if any(mark in text for mark in ("\n", "\r", "\x00")):
        raise PermissionRefused("That target is not on this machine.")
    return text


def bridge_environment(host, port, environ=None) -> dict:
    """
    The child's environment.

    Credential-named variables are dropped. The host and port are the
    ones already checked. Auth is only `offline` or `microsoft`.
    """
    source = os.environ if environ is None else environ
    extra = {
        "FREYA_MINECRAFT_HOST": host,
        "FREYA_MINECRAFT_PORT": str(port),
        "FREYA_MINECRAFT_USERNAME": resolve_username(source),
        "FREYA_MINECRAFT_AUTH": resolve_auth(source),
    }
    version = resolve_version(source)
    if version:
        extra["FREYA_MINECRAFT_VERSION"] = version
    return child_environment(extra, source)


def start_bridge(target, *, command=None, environ=None, stderr=subprocess.DEVNULL):
    """
    A bridge for one loopback target. The process is not started yet.

    `command` replaces the Node program in tests. A public address
    refuses before that command is remembered as something to run.
    """
    host, port = parse_target(target)
    source = os.environ if environ is None else environ
    env = bridge_environment(host, port, source)
    if command is None:
        script = script_path()
        if not script.is_file():
            raise PlayError("The Minecraft adapter is not installed.")
        command = [node_command(source), str(script)]
    return LineBridge(list(command), env, stderr=stderr)


class LineBridge:
    """One child. `stop` kills it even while an action is being read."""

    def __init__(self, command, env, stderr=subprocess.DEVNULL):
        self.command = list(command)
        self.env = dict(env)
        self._stderr = stderr
        self._process = None
        self._stopped = False
        self._id = 0
        self._write_lock = threading.Lock()
        self._queue = queue.Queue()
        self._reader = None

    @property
    def stopped(self) -> bool:
        return self._stopped

    def start(self) -> None:
        if self._stopped:
            raise ActionRefused("Play has stopped.")
        if self._process is not None:
            return
        try:
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                env=self.env,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise PlayError("Node is not available.") from exc
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="freya-play-bridge",
            daemon=True,
        )
        self._reader.start()

    def request(self, method, payload=None, timeout=60):
        if self._stopped:
            raise ActionRefused("Play has stopped.")
        self.start()
        with self._write_lock:
            if self._stopped or self._process is None or self._process.stdin is None:
                raise ActionRefused("Play has stopped.")
            self._id += 1
            ident = self._id
            message = {"id": ident, "method": method}
            if payload:
                message.update(payload)
            try:
                self._process.stdin.write(
                    json.dumps(message, separators=(",", ":")) + "\n"
                )
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ActionRefused("Play has stopped.") from exc
        return self._wait(ident, timeout)

    def act(self, name, arguments) -> None:
        self.request("act", {"action": name, "arguments": dict(arguments)})

    def observe(self) -> dict:
        result = self.request("observe")
        return result if isinstance(result, dict) else {}

    def wait_until_spawned(self, timeout=20) -> None:
        """
        Block until the child reports it is in a world, or the timeout.

        `ready` returns at once. This loop is what waits, so `stop` can
        kill the child between checks instead of after a long action.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._stopped:
                raise ActionRefused("Play has stopped.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ActionRefused("The bot is not in a world.")
            result = self.request("ready", timeout=min(5, remaining))
            if isinstance(result, dict) and result.get("spawned") is True:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ActionRefused("The bot is not in a world.")
            time.sleep(min(0.05, remaining))

    def halt(self) -> None:
        if self._stopped:
            return
        self.request("halt", timeout=5)

    def stop(self) -> None:
        """Halt the child now. Do not wait for the action it was running."""
        if self._stopped and self._process is None:
            return
        self._stopped = True
        process = self._process
        if process is not None and process.stdin is not None:
            try:
                with self._write_lock:
                    process.stdin.write('{"id":0,"method":"stop"}\n')
                    process.stdin.flush()
                    process.stdin.close()
            except (OSError, ValueError):
                pass
        self._kill(process)
        self._process = None

    def _wait(self, ident, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if self._stopped:
                raise ActionRefused("Play has stopped.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ActionRefused("The action failed.")
            try:
                line = self._queue.get(timeout=min(remaining, _READ_SLICE))
            except queue.Empty:
                continue
            if line is None:
                if self._stopped:
                    raise ActionRefused("Play has stopped.")
                raise ActionRefused("The action failed.")
            try:
                body = json.loads(line)
            except ValueError:
                continue
            if not isinstance(body, dict) or body.get("id") != ident:
                continue
            if body.get("ok") is True:
                return body.get("result")
            error = body.get("error")
            if error in _FIXED_ERRORS:
                raise ActionRefused(error)
            raise ActionRefused("The action failed.")

    def _read_stdout(self) -> None:
        process = self._process
        try:
            if process is None or process.stdout is None:
                return
            for line in process.stdout:
                self._queue.put(line)
        except Exception:
            pass
        self._queue.put(None)

    def _kill(self, process) -> None:
        if process is None:
            return
        family = []
        try:
            parent = psutil.Process(process.pid)
            family = parent.children(recursive=True) + [parent]
        except psutil.Error:
            family = []
        for member in family:
            try:
                member.kill()
            except psutil.Error:
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
