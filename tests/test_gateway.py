import http.client
import json
import threading
import time

import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from freya.events import EventBus
from freya.gateway.server import MAX_UPLOAD_BYTES, Gateway, SESSION_COOKIE
from freya.image.gallery import ImageGallery
from freya.window import WindowActions


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class Recorder:
    """Stands in for Freya: records what the window asks for."""

    def __init__(self, bus):
        self.bus = bus
        self.said = []
        self.commands = []
        self.images = []
        self.muted = []
        self.volumes = []
        self.interrupts = 0
        self.called = threading.Event()

    def actions(self):
        return WindowActions(
            say=self.say,
            generate_image=self.generate_image,
            set_muted=self.muted.append,
            set_volume=self.volumes.append,
            interrupt=self.interrupt,
            status=lambda: {"microphone": "listening", "model": "healthy"},
            run_command=self.run_command,
            console_history=lambda: ["Freya online.", "You: "],
        )

    def run_command(self, text):
        self.commands.append(text)
        self.bus.publish("console", text=f"You (window): {text}")
        self.called.set()

    def say(self, text):
        self.said.append(text)
        self.bus.publish("user", text=text, source="window")
        self.bus.publish("assistant_start")
        self.bus.publish("assistant_chunk", text="Hello ")
        self.bus.publish("assistant_chunk", text="there.")
        self.bus.publish("assistant_end")
        self.called.set()

    def generate_image(self, prompt):
        self.images.append(prompt)
        self.called.set()

    def interrupt(self):
        self.interrupts += 1
        self.called.set()


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def gallery(tmp_path):
    return ImageGallery(tmp_path / "images")


@pytest.fixture
def freya(bus):
    return Recorder(bus)


@pytest.fixture
def gateway(bus, gallery, freya):
    gateway = Gateway(bus, gallery, freya.actions(), port=0)
    gateway.start()
    yield gateway
    gateway.stop()


def get(gateway, path, cookie=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", gateway.port, timeout=5)
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if host:
        headers["Host"] = host
    connection.request("GET", path, headers=headers)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def sign_in(gateway):
    """Exchange a fresh window key for a session cookie."""
    path = gateway.new_window_url().split(f":{gateway.port}", 1)[1]
    response, _ = get(gateway, path)
    assert response.status == 302
    assert response.getheader("Location") == "/"
    cookie = response.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    return cookie.split(";", 1)[0]


def open_socket(gateway, cookie, origin=None):
    return connect(
        f"ws://127.0.0.1:{gateway.port}/ws",
        additional_headers={"Cookie": cookie},
        origin=origin or f"http://127.0.0.1:{gateway.port}",
        open_timeout=5,
    )


def receive(socket, wanted, limit=20):
    """Read messages until one of the wanted type arrives."""
    for _ in range(limit):
        message = json.loads(socket.recv(timeout=5))
        if message["type"] == wanted:
            return message
    raise AssertionError(f"no {wanted} message arrived")


def test_window_url_is_local_and_carries_a_key(gateway):
    url = gateway.new_window_url()

    assert url.startswith(f"http://127.0.0.1:{gateway.port}/open?key=")
    assert len(url.split("key=", 1)[1]) >= 32
    assert gateway.new_window_url() != url


def test_nothing_is_served_without_a_session(gateway, gallery):
    image = gallery.add("a fox", PNG)

    for path in ("/", "/app.js", "/app.css", f"/images/{image['id']}", "/ws"):
        response, body = get(gateway, path)
        assert response.status == 403, path
        assert b"<html" not in body

    response, _ = get(gateway, "/", cookie=f"{SESSION_COOKIE}=made-up")
    assert response.status == 403


def test_window_key_works_exactly_once(gateway):
    path = gateway.new_window_url().split(f":{gateway.port}", 1)[1]

    first, _ = get(gateway, path)
    second, body = get(gateway, path)

    assert first.status == 302
    assert second.status == 403
    assert b"already been used" in body

    wrong, _ = get(gateway, "/open?key=guess")
    missing, _ = get(gateway, "/open")
    assert (wrong.status, missing.status) == (403, 403)


def test_signed_in_window_gets_the_page_and_its_files(gateway):
    cookie = sign_in(gateway)

    page, body = get(gateway, "/", cookie=cookie)
    script, js = get(gateway, "/app.js", cookie=cookie)
    styles, css = get(gateway, "/app.css", cookie=cookie)
    missing, _ = get(gateway, "/secrets.txt", cookie=cookie)

    assert page.status == 200
    assert page.getheader("Content-Type").startswith("text/html")
    assert b"<title>Freya</title>" in body
    assert "script-src 'self'" in page.getheader("Content-Security-Policy")
    assert page.getheader("X-Content-Type-Options") == "nosniff"
    assert script.status == 200 and b"WebSocket" in js
    assert styles.status == 200 and b"--accent" in css
    assert missing.status == 404
    # The page never builds HTML from what Freya says.
    assert b"innerHTML" not in js


def test_requests_for_another_host_name_are_refused(gateway):
    cookie = sign_in(gateway)

    response, _ = get(gateway, "/", cookie=cookie, host="evil.example")
    rebinding, _ = get(
        gateway, "/", cookie=cookie, host=f"evil.example:{gateway.port}"
    )
    localhost, _ = get(
        gateway, "/", cookie=cookie, host=f"localhost:{gateway.port}"
    )

    assert response.status == 403
    assert rebinding.status == 403
    assert localhost.status == 200


def test_images_are_served_only_from_the_gallery(gateway, gallery):
    cookie = sign_in(gateway)
    image = gallery.add("a fox", PNG)

    found, body = get(gateway, f"/images/{image['id']}", cookie=cookie)

    assert found.status == 200
    assert found.getheader("Content-Type") == "image/png"
    assert body == PNG

    for path in (
        "/images/",
        "/images/index.json",
        "/images/..%2Findex.json",
        "/images/../../pyproject.toml",
        "/images/" + "0" * 32,
        f"/images/{image['id']}.png",
    ):
        response, _ = get(gateway, path, cookie=cookie)
        assert response.status == 404, path


def test_socket_needs_a_session_and_the_right_origin(gateway):
    cookie = sign_in(gateway)

    with pytest.raises(InvalidStatus):
        open_socket(gateway, f"{SESSION_COOKIE}=made-up")

    with pytest.raises(InvalidStatus):
        open_socket(gateway, cookie, origin="http://evil.example")

    with open_socket(gateway, cookie) as socket:
        assert receive(socket, "hello")["status"]["model"] == "healthy"


def test_hello_replays_the_conversation_and_the_gallery(gateway, bus, gallery):
    bus.publish("user", text="hi", source="typed")
    bus.publish("assistant_start")
    bus.publish("assistant_chunk", text="Hello.")
    bus.publish("assistant_end")
    bus.publish("status", status={})
    older = gallery.add("first", PNG)
    newer = gallery.add("second", PNG)

    with open_socket(gateway, sign_in(gateway)) as socket:
        hello = receive(socket, "hello")

    assert [e["type"] for e in hello["history"]] == [
        "user",
        "assistant_start",
        "assistant_chunk",
        "assistant_end",
    ]
    assert hello["history"][0]["source"] == "typed"
    assert [i["id"] for i in hello["images"]] == [newer["id"], older["id"]]


def test_saying_something_runs_a_turn_and_streams_the_reply(gateway, freya):
    with open_socket(gateway, sign_in(gateway)) as socket:
        receive(socket, "hello")
        socket.send(json.dumps({"type": "say", "text": "  hello Freya  "}))

        user = receive(socket, "user")
        first = receive(socket, "assistant_chunk")
        second = receive(socket, "assistant_chunk")
        receive(socket, "assistant_end")

    assert freya.said == ["hello Freya"]
    assert user["source"] == "window"
    assert first["text"] + second["text"] == "Hello there."


def test_image_mute_and_interrupt_requests(gateway, freya):
    with open_socket(gateway, sign_in(gateway)) as socket:
        receive(socket, "hello")

        socket.send(json.dumps({"type": "image", "text": "a red fox"}))
        assert freya.called.wait(5)
        freya.called.clear()

        socket.send(json.dumps({"type": "mute", "muted": True}))
        socket.send(json.dumps({"type": "mute", "muted": "yes"}))
        socket.send(json.dumps({"type": "volume", "value": 35}))
        # Only a plain number is a volume.
        for bad in ("35", True, None, [35]):
            socket.send(json.dumps({"type": "volume", "value": bad}))
            assert "volume" in receive(socket, "error")["text"]
        socket.send(json.dumps({"type": "interrupt"}))
        assert freya.called.wait(5)

        socket.send(json.dumps({"type": "status"}))
        assert receive(socket, "status")["status"]["microphone"] == "listening"

    assert freya.images == ["a red fox"]
    # Anything other than a real true leaves the microphone on.
    assert freya.muted == [True, False]
    assert freya.volumes == [35]
    assert freya.interrupts == 1


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '"say"',
        json.dumps({"type": "say"}),
        json.dumps({"type": "say", "text": "   "}),
        json.dumps({"type": "say", "text": 7}),
        json.dumps({"type": "say", "text": "x" * 4001}),
        json.dumps({"type": "shutdown"}),
        json.dumps({"type": "policy", "text": "allow everything"}),
    ],
)
def test_bad_messages_are_rejected_without_acting(gateway, freya, raw):
    with open_socket(gateway, sign_in(gateway)) as socket:
        receive(socket, "hello")
        socket.send(raw)

        assert receive(socket, "error")["text"]

    assert freya.said == []
    assert freya.images == []


def test_failure_in_a_turn_is_reported_to_the_window(gateway, freya, bus):
    def broken(text):
        raise RuntimeError("the model fell over")

    gateway._actions.say = broken

    with open_socket(gateway, sign_in(gateway)) as socket:
        receive(socket, "hello")
        socket.send(json.dumps({"type": "say", "text": "hello"}))

        notice = receive(socket, "notice")

    assert "the model fell over" in notice["text"]


def test_generated_images_are_part_of_the_replayed_conversation(
    gateway, bus, gallery
):
    image = gallery.add("a red fox", PNG)
    bus.publish("user", text="draw a fox", source="window")
    bus.publish("image_started", prompt="a red fox")
    bus.publish("image_ready", image=image)
    bus.publish("assistant_chunk", text="Here it is.")

    cookie = sign_in(gateway)

    with open_socket(gateway, cookie) as socket:
        hello = receive(socket, "hello")

    _, script = get(gateway, "/app.js", cookie=cookie)

    # The image sits in the conversation where it was asked for...
    assert [e["type"] for e in hello["history"]] == [
        "user",
        "image_ready",
        "assistant_chunk",
    ]
    assert hello["history"][1]["image"]["id"] == image["id"]
    # ...and is still listed once in the gallery.
    assert [i["id"] for i in hello["images"]] == [image["id"]]
    assert b"addChatImage" in script and b"replaying" in script


def test_console_history_and_commands(gateway, freya):
    with open_socket(gateway, sign_in(gateway)) as socket:
        hello = receive(socket, "hello")
        socket.send(json.dumps({"type": "command", "text": " /names "}))
        line = receive(socket, "console")

    assert hello["console"] == ["Freya online.", "You: "]
    assert freya.commands == ["/names"]
    assert line["text"] == "You (window): /names"
    assert freya.said == []


def test_page_offers_a_console_tab_opened_by_a_chat_command(gateway):
    cookie = sign_in(gateway)

    _, page = get(gateway, "/", cookie=cookie)
    _, script = get(gateway, "/app.js", cookie=cookie)

    assert b'id="tab-console"' in page and b'id="console-output"' in page
    assert b'"terminal"' in script and b'"console"' in script
    assert b'type: "command"' in script


def test_gateway_knows_when_no_window_is_connected(gateway):
    assert gateway.ever_connected is False
    assert gateway.idle_seconds() >= 0

    cookie = sign_in(gateway)

    with open_socket(gateway, cookie) as first:
        receive(first, "hello")
        assert gateway.ever_connected is True
        assert gateway.idle_seconds() is None

        with open_socket(gateway, cookie) as second:
            receive(second, "hello")

        # One window is still open.
        deadline = time.monotonic() + 5
        while gateway._clients != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert gateway.idle_seconds() is None

    deadline = time.monotonic() + 5
    while gateway.idle_seconds() is None and time.monotonic() < deadline:
        time.sleep(0.02)

    assert 0 <= gateway.idle_seconds() < 5


def test_closed_window_stops_receiving_events(gateway, bus):
    with open_socket(gateway, sign_in(gateway)) as socket:
        receive(socket, "hello")

    deadline = time.monotonic() + 5
    while bus._subscribers and time.monotonic() < deadline:
        time.sleep(0.02)

    assert bus._subscribers == []
    bus.publish("notice", text="nobody is listening")


# -- the Voice tab -----------------------------------------------------


CLIP_ID = "c" * 32
WAV = b"RIFF" + b"\x00" * 40


@pytest.fixture
def voice_gateway(bus, gallery, freya):
    requests = []

    def voice_request(message):
        requests.append(message)
        return {
            "state": {"engines": [], "profiles": ["after"]},
            "clip": {"id": CLIP_ID, "seconds": 1.5},
            "error": None,
        }

    actions = freya.actions()
    actions.voice_state = lambda: {"engines": [], "profiles": []}
    actions.voice_request = voice_request
    actions.voice_clip = lambda clip_id: WAV if clip_id == CLIP_ID else None

    gateway = Gateway(bus, gallery, actions, port=0)
    gateway.requests = requests
    gateway.start()
    yield gateway
    gateway.stop()


def test_voice_request_reaches_the_studio_and_its_answer_comes_back(
    voice_gateway,
):
    request = {
        "type": "voice",
        "action": "preview",
        "request": "v1",
        "engine": "kokoro",
        "kind": "preset",
        "settings": {"language": "a", "voice": "af_heart", "speed": 1},
    }

    with open_socket(voice_gateway, sign_in(voice_gateway)) as socket:
        hello = receive(socket, "hello")
        socket.send(json.dumps(request))
        answer = receive(socket, "voice")

    assert hello["voice"] == {"engines": [], "profiles": []}
    assert voice_gateway.requests == [{**request, "upload": b""}]
    assert answer["request"] == "v1"
    assert answer["clip"] == {"id": CLIP_ID, "seconds": 1.5}
    assert answer["state"]["profiles"] == ["after"]
    assert answer["error"] is None


def test_recording_arrives_as_binary_messages_with_the_request(
    voice_gateway,
):
    with open_socket(voice_gateway, sign_in(voice_gateway)) as socket:
        receive(socket, "hello")
        socket.send(b"\x01\x02" * 1000)
        socket.send(b"\x03\x04" * 1000)
        socket.send(json.dumps({
            "type": "voice", "action": "reference", "request": "v1",
            "upload": "what the page claims",
        }))
        receive(socket, "voice")
        socket.send(json.dumps({"type": "voice", "action": "state"}))
        receive(socket, "voice")

    first, second = voice_gateway.requests

    # The bytes are the ones the gateway received, whatever the
    # message itself says, and they are used once.
    assert first["upload"] == b"\x01\x02" * 1000 + b"\x03\x04" * 1000
    assert second["upload"] == b""


def test_oversized_recording_is_refused_and_dropped(voice_gateway):
    with open_socket(voice_gateway, sign_in(voice_gateway)) as socket:
        receive(socket, "hello")

        for _ in range(MAX_UPLOAD_BYTES // 60000 + 1):
            socket.send(b"\x00" * 60000)

        error = receive(socket, "error")
        socket.send(json.dumps({"type": "voice", "action": "reference"}))
        receive(socket, "voice")

    assert "too large" in error["text"]
    assert voice_gateway.requests[0]["upload"] == b""


def test_voice_requests_are_refused_once_freya_is_shutting_down(voice_gateway):
    with open_socket(voice_gateway, sign_in(voice_gateway)) as socket:
        receive(socket, "hello")
        voice_gateway._stopping.set()
        socket.send(json.dumps({"type": "voice", "action": "state"}))
        refused = receive(socket, "error")

    assert refused["text"] == "Freya is shutting down."
    assert voice_gateway.requests == []


def test_generated_speech_is_served_only_to_a_signed_in_window(voice_gateway):
    cookie = sign_in(voice_gateway)

    found, body = get(voice_gateway, f"/audio/{CLIP_ID}", cookie=cookie)
    stranger, _ = get(voice_gateway, f"/audio/{CLIP_ID}")

    assert found.status == 200
    assert found.getheader("Content-Type") == "audio/wav"
    assert body == WAV
    assert stranger.status != 200

    for path in ("/audio/", "/audio/" + "d" * 32, f"/audio/{CLIP_ID}.wav"):
        response, _ = get(voice_gateway, path, cookie=cookie)
        assert response.status == 404, path


def test_page_can_play_its_own_audio_and_nothing_else(voice_gateway):
    cookie = sign_in(voice_gateway)

    page, body = get(voice_gateway, "/", cookie=cookie)
    _, script = get(voice_gateway, "/app.js", cookie=cookie)

    assert "media-src 'self'" in page.getheader("Content-Security-Policy")
    assert b'id="view-voice"' in body
    assert b'type: "voice"' in script


def test_cloner_can_import_trim_and_play_a_recording(voice_gateway):
    cookie = sign_in(voice_gateway)
    _, body = get(voice_gateway, "/", cookie=cookie)
    _, script = get(voice_gateway, "/app.js", cookie=cookie)

    for control in (
        b'id="clone-import"', b'id="clone-drop"', b'id="clone-start"',
        b'id="clone-listen"', b'id="clone-consent"',
    ):
        assert control in body

    # The file is decoded in the page and only samples are sent, so
    # playing sound needs no permission beyond the page's own files.
    assert b"decodeAudioData" in script and b'"drop"' in script
    assert "media-src 'self';" in get(voice_gateway, "/", cookie=cookie)[0].getheader(
        "Content-Security-Policy"
    )


def test_page_never_calls_a_blend_cloning_or_design(voice_gateway):
    _, body = get(voice_gateway, "/", cookie=sign_in(voice_gateway))
    page = body.decode("utf-8")
    blend = page.split('id="panel-blend"', 1)[1].split("</article>", 1)[0]

    assert "Blend Existing Voices" in blend
    assert "Experimental" in blend
    assert "not cloning" in blend
    assert "not designing" in blend


def test_cloud_answers_go_only_to_the_window_that_asked(gateway, freya):
    asked = []

    def cloud_request(message):
        asked.append(message)
        return {"state": {"sent_count": 1}, "sent": [{"text": "a private brief"}]}

    gateway._actions.cloud_request = cloud_request
    cookie = sign_in(gateway)

    with open_socket(gateway, cookie) as asking, open_socket(gateway, sign_in(gateway)) as other:
        receive(asking, "hello")
        receive(other, "hello")

        asking.send(json.dumps({"type": "cloud", "action": "sent", "request": "c1"}))
        answer = receive(asking, "cloud")

        assert answer["request"] == "c1"
        assert answer["sent"][0]["text"] == "a private brief"
        assert asked[0]["action"] == "sent"

        freya.bus.publish("notice", text="after")
        assert receive(other, "notice")["text"] == "after"

    assert all(event["type"] != "cloud" for event in freya.bus.history())


def test_memory_answers_go_only_to_the_window_that_asked(gateway, freya):
    asked = []

    def memory_request(message):
        asked.append(message)
        return {"query": message.get("query"), "memories": [{"id": 1, "content": "private"}],
                "error": None}

    gateway._actions.memory_request = memory_request
    cookie = sign_in(gateway)

    with open_socket(gateway, cookie) as asking, open_socket(gateway, sign_in(gateway)) as other:
        receive(asking, "hello")
        receive(other, "hello")

        asking.send(json.dumps({"type": "memory", "action": "search", "request": "m1",
                                "query": "private"}))
        answer = receive(asking, "memory")

        assert answer["request"] == "m1"
        assert answer["memories"][0]["content"] == "private"
        assert asked[0]["action"] == "search"

        # The other window hears ordinary events but not this answer.
        freya.bus.publish("notice", text="after")
        assert receive(other, "notice")["text"] == "after"

    # And it was never kept where a window opened later would be sent it.
    assert all(event["type"] != "memory" for event in freya.bus.history())
