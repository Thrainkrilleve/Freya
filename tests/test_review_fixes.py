"""
Regression tests for the four findings of the window review
(docs/tasks/claude-review-fixes.md).
"""
import json
import queue
import re
import subprocess
import threading
from importlib import resources

import pytest

from freya.events import EventBus
from freya.gateway.server import Gateway
from freya.image.gallery import ImageGallery
from freya.main import normalize_command
from freya.settings import Settings
from freya.turn_gate import TurnGate, TurnGateClosed
from freya.window import WindowActions, _edge_path
from freya.window_commands import CommandContext, run_window_command


WAIT = 5


def actions(**overrides):
    defaults = dict(
        say=lambda text: None,
        generate_image=lambda prompt: None,
        set_muted=lambda muted: None,
        interrupt=lambda: None,
        status=lambda: {},
        run_command=lambda text: None,
        console_history=lambda: [],
    )

    return WindowActions(**{**defaults, **overrides})


class FakeSocket:
    """
    Stands in for a connected window: records what is sent to it and
    hands the gateway the messages a test supplies.
    """

    def __init__(self, on_send=None):
        self.sent = queue.Queue()
        self.incoming = queue.Queue()
        self._on_send = on_send

    def send(self, raw):
        message = json.loads(raw)
        self.sent.put(message)

        if self._on_send is not None:
            self._on_send(message)

    def __iter__(self):
        while True:
            raw = self.incoming.get()

            if raw is None:
                return

            yield raw

    def next(self):
        return self.sent.get(timeout=WAIT)

    def say(self, text, kind="say"):
        self.incoming.put(json.dumps({"type": kind, "text": text}))

    def close(self):
        self.incoming.put(None)


def serve(gateway, socket):
    thread = threading.Thread(
        target=gateway._handle_socket, args=(socket,), daemon=True
    )
    thread.start()

    return thread


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def gallery(tmp_path):
    return ImageGallery(tmp_path / "images")


# -- 1. shutdown leaves turns running ----------------------------------


def test_stop_cancels_waiting_turns_and_waits_for_the_running_one(
    bus, gallery
):
    started = threading.Event()
    release = threading.Event()
    ran = []

    def say(text):
        ran.append(text)
        started.set()
        assert release.wait(WAIT)
        ran.append(f"{text} finished")

    gateway = Gateway(bus, gallery, actions(say=say), port=0)
    gateway.start()
    socket = FakeSocket()
    connection = serve(gateway, socket)
    assert socket.next()["type"] == "hello"

    socket.say("first")
    assert started.wait(WAIT)
    socket.say("second")
    # A reply to this proves "second" has been read and queued.
    socket.say("x", kind="status")
    assert socket.next()["type"] == "status"

    # The first turn is still running, so stopping reports that it did
    # not finish in the time allowed, rather than waiting for ever.
    assert gateway.stop(timeout=0.05) is False
    assert gateway._worker.is_alive()

    # New work is refused from now on.
    socket.say("third")
    refused = socket.next()
    assert refused == {"type": "error", "text": "Freya is shutting down."}

    release.set()
    gateway._worker.join(WAIT)

    assert not gateway._worker.is_alive()
    # The running turn finished; the waiting ones never started.
    assert ran == ["first", "first finished"]
    assert gateway.stop() is True

    socket.close()
    connection.join(WAIT)


def test_stop_returns_true_once_the_running_turn_has_finished(bus, gallery):
    started = threading.Event()
    release = threading.Event()
    finished = []

    def say(text):
        started.set()
        release.wait(WAIT)
        finished.append(text)

    gateway = Gateway(bus, gallery, actions(say=say), port=0)
    gateway.start()
    socket = FakeSocket()
    connection = serve(gateway, socket)
    socket.next()
    socket.say("only")
    assert started.wait(WAIT)

    stopped = []
    stopper = threading.Thread(target=lambda: stopped.append(gateway.stop()))
    stopper.start()
    assert gateway._stopping.wait(WAIT)
    release.set()
    stopper.join(WAIT)

    # stop() returned only after the turn was done.
    assert stopped == [True]
    assert finished == ["only"]

    socket.close()
    connection.join(WAIT)


def test_stop_called_from_a_turn_does_not_wait_for_itself(bus, gallery):
    results = []
    done = threading.Event()
    gateway = Gateway(bus, gallery, actions(), port=0)

    def quit_command(text):
        results.append(gateway.stop())
        done.set()

    gateway._actions = actions(run_command=quit_command)
    gateway.start()
    socket = FakeSocket()
    connection = serve(gateway, socket)
    socket.next()
    socket.say("/exit", kind="command")

    assert done.wait(WAIT)
    assert results == [True]

    socket.close()
    connection.join(WAIT)


def test_a_turn_refused_during_shutdown_is_not_reported_as_a_failure(
    bus, gallery
):
    gate = TurnGate()
    entered = threading.Event()

    def say(text):
        entered.set()

        with gate.turn():
            raise AssertionError("no turn may begin after the gate closed")

    gateway = Gateway(bus, gallery, actions(say=say), port=0)
    gateway.start()
    socket = FakeSocket()
    connection = serve(gateway, socket)
    socket.next()

    # A spoken turn holds the gate while the window's turn waits.
    with gate.spoken_turn() as spoken:
        assert spoken is True
        socket.say("hello")
        assert entered.wait(WAIT)
        gate.close()
        gateway._stopping.set()

    assert gateway.stop() is True
    assert [e for e in bus.history() if e["type"] == "notice"] == []

    socket.close()
    connection.join(WAIT)


def test_closed_gate_lets_no_turn_begin_and_drain_waits_for_the_last():
    gate = TurnGate()
    inside = threading.Event()
    release = threading.Event()

    def spoken():
        with gate.spoken_turn() as acquired:
            assert acquired
            inside.set()
            release.wait(WAIT)

    thread = threading.Thread(target=spoken)
    thread.start()
    assert inside.wait(WAIT)

    gate.close()

    # The turn in progress is still running: drain says so and returns.
    assert gate.drain(0.05) is False

    release.set()
    thread.join(WAIT)

    assert gate.drain(WAIT) is True

    with pytest.raises(TurnGateClosed):
        with gate.turn():
            pass

    with gate.spoken_turn() as acquired:
        assert acquired is False

    assert gate.busy() is False


def test_closing_the_gate_releases_the_keyboards_hold(monkeypatch):
    gate = TurnGate()
    monkeypatch.setattr("builtins.input", lambda prompt: "hello")

    assert gate.read_line("You: ") == "hello"
    assert gate.busy() is True

    gate.close()

    assert gate.drain(WAIT) is True


def test_reply_in_progress_stops_when_freya_shuts_down(monkeypatch):
    from freya import main

    closed = []
    spoken = []
    monkeypatch.setattr(main, "EVENTS", EventBus())
    monkeypatch.setattr(main, "SHUTTING_DOWN", threading.Event())

    class Assistant:
        def respond_stream(self, text):
            try:
                yield "One. "
                # Shutdown begins while the reply is being written.
                main.SHUTTING_DOWN.set()
                yield "Two. "
                yield "Three. "
            finally:
                closed.append(True)

    class Voice:
        def speak(self, sentence):
            spoken.append(sentence)

    main.stream_freya_response(Assistant(), Voice(), "count")

    chunks = [
        e["text"] for e in main.EVENTS.history()
        if e["type"] == "assistant_chunk"
    ]
    # The reply stopped at its next piece, the model call was closed,
    # and the window was still told the reply had ended.
    assert chunks == ["One. "]
    assert spoken == ["One."]
    assert closed == [True]
    assert main.EVENTS.history()[-1]["type"] == "assistant_end"


def test_server_thread_ends_quietly_when_its_socket_closes_under_it(
    bus, gallery
):
    gateway = Gateway(bus, gallery, actions(), port=0)

    class ClosedUnderneath:
        def serve_forever(self):
            raise OSError(10038, "not a socket")

    gateway._server = ClosedUnderneath()

    # While running, that is a real failure and is not hidden.
    with pytest.raises(OSError):
        gateway._serve()

    # While stopping, it is just the server going away.
    gateway._stopping.set()
    gateway._serve()


# -- 2. memory commands are rejected in the window ---------------------


class FakeAssistant:
    """Answers the way FreyaAssistant's memory-command handler does."""

    def __init__(self):
        self.asked = []

    def memory_command(self, line):
        self.asked.append(line)
        lowered = line.lower()

        if lowered == "/memory-help":
            return "Memory commands:\n/memories - Show saved memories"

        if lowered == "/memories":
            return "Saved memories:\n1. Likes tea\n2. Has a dog"

        if lowered.startswith("/forget "):
            return f"Forgot memory {line.split()[1]}."

        return None


class Stub:
    running = True

    def interrupt(self):
        pass

    def open(self):
        return "Freya's window is open."


@pytest.fixture
def assistant():
    return FakeAssistant()


@pytest.fixture
def context(bus, tmp_path, assistant):
    return CommandContext(
        bus=bus,
        voice=Stub(),
        listener=Stub(),
        settings=Settings(tmp_path / "settings.json"),
        window=Stub(),
        normalize=normalize_command,
        run_image=lambda prompt: None,
        publish_status=lambda: None,
        memory_command=assistant.memory_command,
    )


def notices(bus):
    return [e["text"] for e in bus.history() if e["type"] == "notice"]


def test_memory_commands_reach_the_assistant(context, bus, assistant, capsys):
    run_window_command("/memories", context)
    run_window_command("/forget 1", context)
    run_window_command("\\memory-help", context)

    assert assistant.asked == ["/memories", "/forget 1", "/memory-help"]
    assert notices(bus) == [
        "Saved memories:\n1. Likes tea\n2. Has a dog",
        "Forgot memory 1.",
        "Memory commands:\n/memories - Show saved memories",
    ]
    # Printed too, so the Console tab shows the same answer.
    assert "1. Likes tea" in capsys.readouterr().out


def test_unknown_and_keyboard_only_commands_are_unchanged(
    context, bus, assistant
):
    run_window_command("/frobnicate now", context)
    run_window_command("/talk", context)
    run_window_command("/help", context)

    unknown, keyboard, helped = notices(bus)
    assert "I don't know the command /frobnicate" in unknown
    assert "needs a keyboard in my terminal" in keyboard
    assert "/memory-help" in helped
    # Only the command nothing else recognised was offered to memory.
    assert assistant.asked == ["/frobnicate now"]


def test_real_assistant_handler_recognises_memory_commands():
    """
    The real handler, with no memory store behind it: /memory-help
    needs none, and text that is not a memory command gives None.
    """
    from freya.core.assistant import FreyaAssistant

    assistant = FreyaAssistant.__new__(FreyaAssistant)

    assert assistant.memory_command("/memory-help").startswith(
        "Memory commands:"
    )
    assert "/forget <ID>" in assistant.memory_command("/MEMORY-HELP")


def test_window_commands_work_without_a_memory_handler(bus, tmp_path):
    context = CommandContext(
        bus=bus, voice=Stub(), listener=Stub(),
        settings=Settings(tmp_path / "settings.json"), window=Stub(),
        normalize=normalize_command, run_image=lambda prompt: None,
        publish_status=lambda: None,
    )

    run_window_command("/memories", context)

    assert "I don't know the command /memories" in notices(bus)[0]


# -- 3. connecting can lose conversation events ------------------------


def test_subscribing_with_history_misses_and_repeats_nothing(bus):
    bus.publish("user", text="before")
    seen = []

    history = bus.subscribe_with_history(seen.append)
    bus.publish("assistant_chunk", text="after")

    assert [e["text"] for e in history] == ["before"]
    assert [e["text"] for e in seen] == ["after"]


def test_event_published_while_the_hello_is_sent_is_not_lost(bus, gallery):
    bus.publish("user", text="hello")

    def while_sending(message):
        # This is the gap the review found: something is published
        # while the hello is on its way to the window.
        if message["type"] == "hello":
            bus.publish("assistant_chunk", text="chunk")

    gateway = Gateway(bus, gallery, actions(), port=0)
    socket = FakeSocket(on_send=while_sending)
    connection = serve(gateway, socket)

    hello = socket.next()
    bus.publish("assistant_end")
    bus.publish("status", status={})

    received = [socket.next(), socket.next(), socket.next()]

    # The hello came first and holds what was said before it; each
    # later event arrived once, in the order it was published.
    assert hello["type"] == "hello"
    assert [e["type"] for e in hello["history"]] == ["user"]
    assert [e["type"] for e in received] == [
        "assistant_chunk", "assistant_end", "status",
    ]
    assert socket.sent.empty()

    socket.close()
    connection.join(WAIT)
    assert bus._subscribers == []


def test_events_reach_a_window_in_the_order_they_were_published(gallery):
    bus = EventBus(history_limit=2000)
    gateway = Gateway(bus, gallery, actions(), port=0)
    socket = FakeSocket()
    connection = serve(gateway, socket)
    socket.next()

    ready = threading.Barrier(4)

    def publisher(name):
        ready.wait(WAIT)

        for number in range(200):
            bus.publish("assistant_chunk", text=f"{name}{number}")

    threads = [
        threading.Thread(target=publisher, args=(name,)) for name in "abcd"
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)

    delivered = [socket.next()["text"] for _ in range(800)]

    # Whatever order the threads interleaved in, the window saw the
    # same order as the bus's own record of the conversation (which
    # keeps the pieces of a reply as one entry).
    assert "".join(delivered) == "".join(e["text"] for e in bus.history())
    assert socket.sent.empty()

    socket.close()
    connection.join(WAIT)


def test_reconnecting_window_gets_everything_exactly_once(bus, gallery):
    gateway = Gateway(bus, gallery, actions(), port=0)

    first = FakeSocket()
    first_connection = serve(gateway, first)
    first.next()
    bus.publish("user", text="one")
    assert first.next()["text"] == "one"
    first.close()
    first_connection.join(WAIT)

    bus.publish("assistant_chunk", text="two")

    second = FakeSocket()
    second_connection = serve(gateway, second)
    hello = second.next()
    bus.publish("assistant_end")

    assert [e.get("text") for e in hello["history"]] == ["one", "two"]
    assert second.next()["type"] == "assistant_end"
    assert second.sent.empty()
    assert first.sent.empty()

    second.close()
    second_connection.join(WAIT)


def test_slow_subscriber_does_not_hold_the_bus(bus, gallery):
    """
    A window whose socket has stopped accepting data must not stall
    whoever is publishing.
    """
    stuck = threading.Event()
    release = threading.Event()

    def block(message):
        if message["type"] != "hello":
            stuck.set()
            release.wait(WAIT)

    gateway = Gateway(bus, gallery, actions(), port=0)
    socket = FakeSocket(on_send=block)
    connection = serve(gateway, socket)
    socket.next()

    bus.publish("assistant_chunk", text="first")
    assert stuck.wait(WAIT)

    # The send of "first" is blocked; publishing still returns at once.
    published = threading.Thread(
        target=lambda: bus.publish("assistant_chunk", text="second")
    )
    published.start()
    published.join(WAIT)
    assert not published.is_alive()

    release.set()
    socket.close()
    connection.join(WAIT)


# -- 4. bare exit and quit are sent as chat ----------------------------


FRONTEND_HARNESS = """
<script>
  // Stand-ins for the connection, so the real page script can run
  // from a file with nothing to connect to.
  window.sentMessages = [];
  class WebSocket {
    static OPEN = 1;
    constructor() { this.readyState = 1; }
    addEventListener() {}
    send(raw) { window.sentMessages.push(JSON.parse(raw)); }
  }
  window.WebSocket = WebSocket;
</script>
<script>__APP__</script>
<script>
  function typeInto(form, input, text) {
    document.getElementById(input).value = text;
    document.getElementById(form).dispatchEvent(
      new Event("submit", { cancelable: true })
    );
  }
  const results = {};
  for (const [name, form, input] of [
    ["chat", "chat-form", "chat-input"],
    ["console", "console-form", "console-input"],
  ]) {
    results[name] = __CASES__.map((text) => {
      window.sentMessages.length = 0;
      typeInto(form, input, text);
      return window.sentMessages.slice();
    });
  }
  const out = document.createElement("pre");
  out.id = "frontend-result";
  out.textContent = JSON.stringify(results);
  document.body.appendChild(out);
</script>
"""

FRONTEND_CASES = [
    "exit",
    "quit",
    "  EXIT  ",
    "Quit",
    "/exit",
    "exit the building quietly",
    "should I quit my job?",
    "quit.",
    "/memories",
    "hello",
]


@pytest.fixture(scope="module")
def frontend(tmp_path_factory):
    """
    Runs the window's real page script in headless Edge and returns
    what each composer sent for each line typed into it.
    """
    edge = _edge_path()

    if edge is None:
        pytest.skip("Microsoft Edge is needed to run the page's script")

    static = resources.files("freya.gateway") / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert "</script>" not in script

    harness = FRONTEND_HARNESS.replace("__APP__", script).replace(
        "__CASES__", json.dumps(FRONTEND_CASES)
    )
    page = page.replace('<link rel="stylesheet" href="/app.css">', "")
    page = page.replace('<script src="/app.js"></script>', harness)

    folder = tmp_path_factory.mktemp("frontend")
    path = folder / "page.html"
    path.write_text(page, encoding="utf-8")

    dumped = subprocess.run(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={folder / 'profile'}",
            "--dump-dom",
            path.as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    ).stdout

    found = re.search(r'<pre id="frontend-result">(.*?)</pre>', dumped, re.S)

    if found is None:
        pytest.fail(f"the page's script did not finish:\n{dumped[-800:]}")

    import html

    results = json.loads(html.unescape(found.group(1)))

    return {
        composer: dict(zip(FRONTEND_CASES, sent))
        for composer, sent in results.items()
    }


@pytest.mark.parametrize("composer", ["chat", "console"])
def test_bare_exit_and_quit_are_sent_as_commands(frontend, composer):
    sent = frontend[composer]

    assert sent["exit"] == [{"type": "command", "text": "exit"}]
    assert sent["quit"] == [{"type": "command", "text": "quit"}]
    assert sent["  EXIT  "] == [{"type": "command", "text": "EXIT"}]
    assert sent["Quit"] == [{"type": "command", "text": "Quit"}]
    assert sent["/exit"] == [{"type": "command", "text": "/exit"}]


@pytest.mark.parametrize("composer", ["chat", "console"])
def test_sentences_containing_exit_or_quit_stay_chat(frontend, composer):
    sent = frontend[composer]

    for text in (
        "exit the building quietly",
        "should I quit my job?",
        "quit.",
        "hello",
    ):
        assert sent[text] == [{"type": "say", "text": text}], text

    assert sent["/memories"] == [{"type": "command", "text": "/memories"}]


@pytest.mark.parametrize("typed", ["exit", "QUIT", "Exit"])
def test_bare_quit_word_quits_only_a_window_only_freya(context, bus, typed):
    quits = []

    run_window_command(typed, context)

    assert "Type exit there" in notices(bus)[-1]
    assert quits == []

    context.request_quit = lambda: quits.append(1)
    run_window_command(typed, context)

    assert quits == [1]
    assert notices(bus)[-1] == "Closing down."


def test_a_long_reply_does_not_push_its_own_question_out_of_the_history():
    # A reply arrives one token at a time. The history is bounded by
    # entries, so a reply longer than the bound would otherwise push
    # the owner's question, and the start of the reply, out of what a
    # window opening later is shown.
    bus = EventBus(history_limit=50)
    live = []
    bus.subscribe(live.append)

    bus.publish("user", text="Tell me a long story.")
    bus.publish("assistant_start")
    for number in range(600):
        bus.publish("assistant_chunk", text=f"{number} ")
    bus.publish("assistant_end")

    kept = bus.history()

    assert [e["type"] for e in kept] == [
        "user", "assistant_start", "assistant_chunk", "assistant_end",
    ]
    assert kept[2]["text"] == "".join(f"{n} " for n in range(600))
    # The live events were delivered one by one, as they came.
    assert sum(1 for e in live if e["type"] == "assistant_chunk") == 600
    assert live[2]["text"] == "0 "
