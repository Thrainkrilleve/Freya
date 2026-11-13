"""Opt-in local chat smoke using temporary synthetic storage only."""

import argparse
from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit


class ScratchSession:
    def __init__(self, folder, client=None, voice_enabled=False):
        self.folder = Path(folder).resolve()
        self.client = client
        self.voice_enabled = voice_enabled
        self.stack = ExitStack()
        self.plane = self.assistant = self.window = self.voice = None

    def __enter__(self):
        from freya.control_plane import build_local_control_plane
        from freya.core.assistant import FreyaAssistant
        from freya.events import EventBus
        from freya.image.gallery import ImageGallery
        from freya.memory import memory_manager
        from freya.providers.model import ollama as model_provider
        from freya.settings import Settings
        from freya.turn_gate import TurnGate
        from freya.voice.tts import FreyaVoice
        from freya.window import FreyaWindow, WindowActions, build_status

        repo = Path(__file__).resolve().parents[1]
        if self.folder == repo or repo in self.folder.parents:
            raise ValueError("Scratch storage must be outside the checkout")
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            self.stack.enter_context(patch.object(memory_manager, "DATA_DIR", self.folder))
            self.stack.enter_context(patch.object(memory_manager, "DATABASE_PATH", self.folder / "memory.db"))
            if self.client is not None:
                self.stack.enter_context(patch.object(model_provider, "ollama", self.client))
            self.plane = build_local_control_plane(self.folder / "control.db", None)
            # No model tool host: smoke chat cannot dispatch hardware/image tools.
            self.assistant = FreyaAssistant(broker=self.plane.broker)
            self.plane.register_conversation_recall(self.assistant.recall)
            self.voice = FreyaVoice(enabled=self.voice_enabled)
            self.settings = Settings(self.folder / "settings.json")
            self.bus = EventBus()
            self.gate = TurnGate()

            def say(text):
                with self.gate.turn():
                    from freya.main import stream_freya_response
                    # The production stream publishes to its module bus.
                    with patch("freya.main.EVENTS", self.bus):
                        self.bus.publish("user", text=text, source="window")
                        self.assistant.source = "window"
                        stream_freya_response(self.assistant, self.voice, text)

            actions = WindowActions(
                say=say, generate_image=lambda prompt: None,
                set_muted=lambda muted: None, interrupt=self.voice.interrupt,
                status=lambda: build_status(None, self.settings, self.plane, self.voice),
            )
            self.window = FreyaWindow(self.bus, ImageGallery(self.folder / "images"), actions, port=0)
            if not self.window.start():
                raise RuntimeError("Scratch gateway could not start")
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if hasattr(self, "gate"):
            self.gate.close()
        finished = self.window.stop() if self.window is not None else True
        if hasattr(self, "gate"):
            finished = self.gate.drain(10) and finished
        if not finished:
            raise RuntimeError("Scratch turn did not stop; storage left open")
        try:
            if self.voice is not None:
                self.voice.shutdown()
            if self.assistant is not None:
                self.assistant.memory.connection.close()
            if self.plane is not None:
                self.plane.close()
        finally:
            self.stack.close()

    def __exit__(self, *args):
        self.close()


def protocol_exchange(session, prompt):
    from websockets.sync.client import connect

    gateway = session.window._gateway
    address = urlsplit(gateway.new_window_url())
    connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=10)
    try:
        connection.request("GET", address.path + "?" + address.query)
        response = connection.getresponse()
        response.read()
        if response.status != 302:
            raise RuntimeError("Scratch session exchange failed")
        cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        connection.request("GET", "/", headers={"Cookie": cookie})
        response = connection.getresponse()
        page = response.read()
        if response.status != 200 or b"view-chat" not in page:
            raise RuntimeError("Chat page could not be served")
    finally:
        connection.close()
    answer = []
    deadline = time.monotonic() + 90
    with connect(f"ws://127.0.0.1:{gateway.port}/ws", origin=f"http://127.0.0.1:{gateway.port}",
                 additional_headers={"Cookie": cookie}, open_timeout=10) as socket:
        socket.send(json.dumps({"type": "say", "text": prompt}))
        while time.monotonic() < deadline:
            event = json.loads(socket.recv(timeout=max(0.1, deadline - time.monotonic())))
            if event["type"] == "assistant_chunk":
                answer.append(event["text"])
            if event["type"] == "notice" and event.get("text", "").startswith("That did not work:"):
                raise RuntimeError("Scratch chat failed: " + event["text"])
            if event["type"] == "assistant_end":
                break
        else:
            raise TimeoutError("Scratch chat timed out")
    result = "".join(answer).strip()
    if not result:
        raise RuntimeError("Scratch chat returned no answer")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-seconds", type=int, default=0)
    parser.add_argument("--url-file", type=Path)
    args = parser.parse_args()
    if not 0 <= args.serve_seconds <= 300:
        parser.error("serve-seconds must be between 0 and 300")
    if args.url_file and args.url_file.exists():
        parser.error("url-file must not already exist")
    import ollama
    from freya.core.model_runtime import get_model_runtime
    from freya.memory import memory_manager

    client = ollama.Client(host="http://127.0.0.1:11434", timeout=20)
    report = {"storage": "temporary synthetic", "real_voice": "not run", "browser_visual": "not run"}
    with tempfile.TemporaryDirectory(prefix="freya-local-smoke-") as folder:
        with ScratchSession(folder, client) as session:
            vector = get_model_runtime().embed("Synthetic smoke test.")
            if not vector:
                raise RuntimeError("Embedding was empty")
            report["embedding"] = "passed"
            prompt = "For this synthetic smoke test, reply briefly with: Freya local chat ready."
            if args.serve_seconds:
                url = session.window._gateway.new_window_url()
                if args.url_file:
                    with args.url_file.open("x", encoding="utf-8") as handle:
                        handle.write(url)
                print("Scratch browser gateway ready.", flush=True)
                deadline = time.monotonic() + args.serve_seconds
                while time.monotonic() < deadline:
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
            answer = protocol_exchange(session, prompt)
            report["gateway_chat"] = "passed"
            report["answer"] = answer
            session.settings.set_voice_volume(37)
        # Reopen the same scratch stores; no model inference is needed.
        from freya.memory.memory_manager import MemoryManager
        from freya.settings import Settings
        with patch.object(memory_manager, "DATA_DIR", Path(folder)), patch.object(memory_manager, "DATABASE_PATH", Path(folder) / "memory.db"):
            memory = MemoryManager()
            try:
                count = memory.connection.execute("SELECT count(*) FROM conversation_turn WHERE content = ?", (prompt,)).fetchone()[0]
                if count != 1 or Settings(Path(folder) / "settings.json").voice_volume != 37:
                    raise RuntimeError("Scratch restart persistence failed")
                report["restart_persistence"] = "passed"
            finally:
                memory.connection.close()
    report["shutdown"] = "passed"
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
