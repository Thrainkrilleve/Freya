import json
import threading
import time

import numpy as np
import pytest
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from freya.events import EventBus
from freya.gateway import speech_relay
from freya.gateway.server import PROTOCOL_VERSION, Gateway
from freya.gateway.speech_relay import (
    FINISHED,
    FRAME_HEADER,
    FRAME_MAGIC,
    INTERRUPTED,
    LOST,
    NO_CLIENT,
    TIMED_OUT,
    SpeechRelay,
)
from freya.image.gallery import ImageGallery
from freya.unreal_client import KEY_VARIABLE, PORT_VARIABLE, UnrealClient, configured_client
from freya.window import WindowActions


RATE = 24000


def tone(seconds=0.35, level=0.5):
    return np.full(int(RATE * seconds), level, dtype=np.float32)


class Client:
    """Stands in for a client that plays speech: records what it is sent."""

    def __init__(self, relay, answer=None):
        self.relay = relay
        self.sent = []
        self.answer = answer
        self.alive = True
        self.owner = relay.claim(self.send)

    def send(self, message):
        if not self.alive:
            return False

        self.sent.append(message)

        if isinstance(message, dict) and message["type"] == "speech.end" and self.answer:
            threading.Thread(
                target=self.relay.acknowledge,
                args=(self.owner, {"type": self.answer, "utterance": message["utterance"]}),
            ).start()

        return True

    def texts(self):
        return [m for m in self.sent if isinstance(m, dict)]

    def frames(self):
        return [m for m in self.sent if isinstance(m, bytes)]


def play(relay, audio=None, volume=0.4, interrupted=None):
    return relay.play(tone() if audio is None else audio, RATE, volume, interrupted or threading.Event())


# -- the relay ---------------------------------------------------------------


def test_with_no_client_nothing_is_sent_and_freya_plays_it_herself():
    relay = SpeechRelay()

    assert relay.active is False
    assert play(relay) == NO_CLIENT


def test_an_utterance_reaches_the_client_whole_and_at_full_level():
    relay = SpeechRelay()
    client = Client(relay, answer="speech.finished")

    assert play(relay, tone(0.35, 0.5), volume=0.4) == FINISHED

    start, end = client.texts()[0], client.texts()[-1]
    assert start["type"] == "speech.start" and end["type"] == "speech.end"
    assert (start["sample_rate"], start["channels"], start["format"]) == (RATE, 1, "pcm_s16le")
    # The volume is told, not applied.
    assert start["volume"] == 0.4
    assert start["samples"] == end["samples"] == int(RATE * 0.35)

    samples = []
    for index, frame in enumerate(client.frames()):
        magic, utterance, sequence = FRAME_HEADER.unpack_from(frame)
        assert (magic, utterance, sequence) == (FRAME_MAGIC, start["utterance"], index)
        body = np.frombuffer(frame[FRAME_HEADER.size:], dtype="<i2")
        # No frame is longer than a tenth of a second.
        assert 0 < len(body) <= RATE // 10
        samples.extend(body)

    assert len(samples) == start["samples"]
    assert set(samples) == {round(0.5 * 32767)}


def test_only_one_client_may_hold_playback():
    relay = SpeechRelay()
    first = Client(relay)

    assert first.owner is not None and relay.active
    assert relay.claim(lambda message: True) is None

    relay.release(first.owner)
    assert not relay.active
    assert relay.claim(lambda message: True) is not None


def test_reports_are_believed_only_from_the_client_that_is_playing():
    relay = SpeechRelay()
    client = Client(relay)
    done = []
    thread = threading.Thread(target=lambda: done.append(play(relay, tone(5.0))))
    thread.start()

    while not client.texts():
        time.sleep(0.01)

    utterance = client.texts()[0]["utterance"]
    # Someone else, the wrong utterance, and an unknown kind change nothing.
    relay.acknowledge(client.owner + 1, {"type": "speech.finished", "utterance": utterance})
    relay.acknowledge(client.owner, {"type": "speech.finished", "utterance": utterance + 7})
    relay.acknowledge(client.owner, {"type": "speech.started", "utterance": utterance})
    time.sleep(0.1)
    assert thread.is_alive()

    relay.acknowledge(client.owner, {"type": "speech.finished", "utterance": utterance})
    thread.join(timeout=5)
    assert done == [FINISHED]


def test_interrupting_stops_at_once_and_tells_the_client_to_fall_silent():
    relay = SpeechRelay()
    client = Client(relay)
    interrupted = threading.Event()
    done = []
    thread = threading.Thread(target=lambda: done.append(play(relay, tone(20.0), interrupted=interrupted)))
    thread.start()
    time.sleep(0.2)

    started = time.monotonic()
    interrupted.set()
    thread.join(timeout=5)

    assert done == [INTERRUPTED]
    assert time.monotonic() - started < 1.0
    assert client.texts()[-1]["type"] == "speech.cancel"


def test_a_client_that_vanishes_loses_the_sentence_and_playback():
    relay = SpeechRelay()
    client = Client(relay)
    done = []
    thread = threading.Thread(target=lambda: done.append(play(relay, tone(10.0))))
    thread.start()
    time.sleep(0.2)

    relay.release(client.owner)
    thread.join(timeout=5)

    assert done == [LOST]
    assert relay.active is False
    # The next sentence is hers to play.
    assert play(relay) == NO_CLIENT


def test_a_client_that_cannot_be_sent_to_is_dropped():
    relay = SpeechRelay()
    client = Client(relay)
    client.alive = False

    assert play(relay) == LOST
    assert relay.active is False


def test_a_client_that_never_reports_does_not_hold_her_forever(monkeypatch):
    monkeypatch.setattr(speech_relay, "FINISH_MARGIN_SECONDS", 0.2)
    relay = SpeechRelay()
    client = Client(relay)

    started = time.monotonic()
    assert play(relay, tone(0.1)) == TIMED_OUT
    assert time.monotonic() - started < 2.0
    assert client.texts()[-1]["type"] == "speech.cancel"
    # It keeps playback: it is still connected, only silent about it.
    assert relay.active


def test_an_overlong_utterance_is_cut_to_the_limit(monkeypatch):
    monkeypatch.setattr(speech_relay, "MAX_UTTERANCE_SECONDS", 1)
    relay = SpeechRelay()
    client = Client(relay, answer="speech.finished")

    play(relay, tone(3.0))

    assert client.texts()[0]["samples"] == RATE


# -- Freya's voice using it ------------------------------------------------------


def make_voice(monkeypatch):
    from freya.voice import tts

    played = []
    monkeypatch.setattr(tts.sd, "play", lambda audio, rate, blocking=False: played.append(np.array(audio)))
    monkeypatch.setattr(tts.sd, "get_stream", lambda: type("Stream", (), {"active": False})())
    voice = tts.FreyaVoice.__new__(tts.FreyaVoice)
    voice.volume = 0.5
    voice.speech_listener = None
    voice.remote_playback = None
    voice._speaking_event = threading.Event()
    voice._interrupt_event = threading.Event()

    return voice, played


def test_her_voice_goes_to_the_client_and_not_the_speakers(monkeypatch):
    voice, played = make_voice(monkeypatch)
    relay = voice.remote_playback = SpeechRelay()
    client = Client(relay, answer="speech.finished")
    heard = []

    class Listener:
        def speech_started(self, text):
            heard.append(("started", voice.is_speaking()))

        def speech_completed(self):
            heard.append("completed")

    voice.speech_listener = Listener()
    voice._play_audio(tone(0.2, 0.8))

    assert played == []
    body = np.frombuffer(client.frames()[0][FRAME_HEADER.size:], dtype="<i2")
    # Full level reaches the client even though her volume is half.
    assert body[0] == round(0.8 * 32767)
    assert client.texts()[0]["volume"] == 0.5
    # She counted as speaking while the client played, and not after.
    assert heard == [("started", True), "completed"]
    assert voice.is_speaking() is False


def test_she_plays_it_herself_when_no_client_is_there(monkeypatch):
    voice, played = make_voice(monkeypatch)
    voice.remote_playback = SpeechRelay()

    voice._play_audio(tone(0.1, 0.8))

    assert len(played) == 1 and round(float(played[0][0]), 2) == 0.4


def test_a_lost_sentence_is_not_repeated_on_the_speakers(monkeypatch):
    voice, played = make_voice(monkeypatch)
    relay = voice.remote_playback = SpeechRelay()
    client = Client(relay)
    client.alive = False

    voice._play_audio(tone(0.1))
    assert played == []

    # The client is gone, so the next sentence is played here.
    voice._play_audio(tone(0.1))
    assert len(played) == 1


# -- a paired program connecting -----------------------------------------------------


class FakeFreya:
    def __init__(self):
        self.closed = []

    def actions(self):
        return WindowActions(
            say=lambda text: None,
            generate_image=lambda prompt: None,
            set_muted=lambda muted: None,
            interrupt=lambda: None,
            status=lambda: {"microphone": "listening"},
            client_closed=self.closed.append,
        )


@pytest.fixture
def freya():
    return FakeFreya()


@pytest.fixture
def gateway(tmp_path, freya):
    gateway = Gateway(EventBus(), ImageGallery(tmp_path / "images"), freya.actions(), port=0)
    gateway.start()
    yield gateway
    gateway.stop()


def native(gateway, authorization):
    return connect(
        f"ws://127.0.0.1:{gateway.port}/ws",
        additional_headers={"Authorization": authorization} if authorization else None,
    )


def receive(socket, wanted, limit=30):
    for _ in range(limit):
        message = socket.recv(timeout=5)

        if isinstance(message, str) and json.loads(message)["type"] == wanted:
            return json.loads(message)

    raise AssertionError(f"no {wanted} message arrived")


def hello(socket, features=("speech.playback",), protocol=PROTOCOL_VERSION):
    socket.send(json.dumps({"type": "client.hello", "protocol": protocol,
                            "client": {"kind": "unreal", "name": "test", "version": "0"},
                            "features": list(features)}))


def test_a_program_needs_the_key_freya_gave_it(gateway):
    for authorization in (None, "FreyaPair wrong", "FreyaPair ", "Bearer x", "FreyaSession nothing"):
        with pytest.raises(InvalidStatus) as refused:
            native(gateway, authorization)
        assert refused.value.response.status_code == 403

    key = gateway.new_pairing_key()

    with native(gateway, f"FreyaPair {key}") as socket:
        receive(socket, "hello")

    # The key worked once.
    with pytest.raises(InvalidStatus):
        native(gateway, f"FreyaPair {key}")


def test_a_paired_program_is_welcomed_and_may_reconnect_with_its_session(gateway, freya):
    with native(gateway, f"FreyaPair {gateway.new_pairing_key()}") as socket:
        receive(socket, "hello")
        hello(socket, features=("speech.playback", "teleport"))
        welcome = receive(socket, "welcome")

        assert welcome["protocol"] == PROTOCOL_VERSION
        assert welcome["features"] == ["speech.playback"]
        assert welcome["unknown"] == ["teleport"]
        assert welcome["limits"]["text_characters"] == 4000
        assert gateway.speech.active

    # It went away: playback is given up and Freya is told.
    deadline = time.monotonic() + 5
    while gateway.speech.active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not gateway.speech.active
    assert freya.closed == ["native"]

    with native(gateway, f"FreyaSession {welcome['session']}") as socket:
        receive(socket, "hello")
        hello(socket)
        assert receive(socket, "welcome")["features"] == ["speech.playback"]


def test_a_second_program_is_not_given_playback(gateway):
    with native(gateway, f"FreyaPair {gateway.new_pairing_key()}") as first, \
            native(gateway, f"FreyaPair {gateway.new_pairing_key()}") as second:
        hello(first)
        assert receive(first, "welcome")["features"] == ["speech.playback"]
        hello(second)
        welcome = receive(second, "welcome")

        assert welcome["features"] == []
        assert welcome["not_available"] == ["speech.playback"]


def test_another_protocol_version_is_refused(gateway):
    with native(gateway, f"FreyaPair {gateway.new_pairing_key()}") as socket:
        hello(socket, protocol=99)
        refused = receive(socket, "refused")

        assert refused["protocol"] == PROTOCOL_VERSION
        assert not gateway.speech.active


def test_speech_travels_to_the_paired_program_and_back(gateway):
    with native(gateway, f"FreyaPair {gateway.new_pairing_key()}") as socket:
        hello(socket)
        receive(socket, "welcome")
        done = []
        thread = threading.Thread(
            target=lambda: done.append(gateway.speech.play(tone(0.25), RATE, 0.7, threading.Event()))
        )
        thread.start()

        start = receive(socket, "speech.start")
        frames = []
        while True:
            message = socket.recv(timeout=5)
            if isinstance(message, bytes):
                frames.append(message)
            elif json.loads(message)["type"] == "speech.end":
                break

        assert start["volume"] == 0.7
        assert sum(len(f) - FRAME_HEADER.size for f in frames) // 2 == start["samples"]
        assert thread.is_alive()

        socket.send(json.dumps({"type": "speech.finished", "utterance": start["utterance"]}))
        thread.join(timeout=5)
        assert done == [FINISHED]


def test_a_program_that_cannot_keep_up_is_disconnected_so_it_can_come_back(gateway, monkeypatch):
    import queue
    import socket as sockets

    from freya.gateway import server

    class Socket:
        def __init__(self):
            self.shut = []

        def shutdown(self, how):
            self.shut.append(how)

    class Connection:
        socket = Socket()

    monkeypatch.setattr(server, "PLAYBACK_SEND_SECONDS", 0.01)
    full = queue.Queue(maxsize=1)
    full.put("waiting")
    client = {"outbox": full, "open": True}
    send = gateway._playback_sender(Connection, client)

    assert send({"type": "speech.start"}) is False
    assert Connection.socket.shut == [sockets.SHUT_RDWR]
    # Nothing more is queued for it.
    assert send({"type": "speech.end"}) is False
    assert full.qsize() == 1


def test_a_program_cut_off_takes_playback_again_with_its_session(gateway):
    with native(gateway, f"FreyaPair {gateway.new_pairing_key()}") as first:
        hello(first)
        session = receive(first, "welcome")["session"]
        # What the gateway does to a program that cannot keep up.
        first.socket.shutdown(2)

    for _ in range(100):
        if not gateway.speech.active:
            break
        time.sleep(0.02)

    with native(gateway, f"FreyaSession {session}") as again:
        hello(again)
        assert receive(again, "welcome")["features"] == ["speech.playback"]


# -- starting the Unreal window ----------------------------------------------------------


def test_the_unreal_window_is_only_started_when_the_owner_named_it(tmp_path, monkeypatch):
    monkeypatch.delenv("FREYA_UNREAL_CLIENT", raising=False)
    assert configured_client() is None

    monkeypatch.setenv("FREYA_UNREAL_CLIENT", str(tmp_path / "missing.exe"))
    assert configured_client() is None

    program = tmp_path / "Freya.exe"
    program.write_bytes(b"")
    monkeypatch.setenv("FREYA_UNREAL_CLIENT", str(program))
    assert configured_client() == program


def test_the_key_is_handed_over_in_the_environment_not_the_command_line(tmp_path):
    started = []

    class Process:
        pid = 0

        def poll(self):
            return None

    def start(command, **options):
        started.append((command, options))
        return Process()

    client = UnrealClient(tmp_path / "Freya.exe", start=start)
    assert client.launch(8770, "secret-key") is True

    command, options = started[0]
    assert "secret-key" not in " ".join(command)
    assert options["env"][KEY_VARIABLE] == "secret-key"
    assert options["env"][PORT_VARIABLE] == "8770"
    # Asking again while it runs does not start a second one.
    assert client.launch(8770, "another") is True and len(started) == 1


def test_failing_to_start_it_leaves_freya_running(tmp_path, capsys):
    def start(command, **options):
        raise OSError("not found")

    client = UnrealClient(tmp_path / "Freya.exe", start=start)

    assert client.launch(8770, "key") is False
    assert client.running is False
    client.stop()
