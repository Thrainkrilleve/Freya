import json

import pytest

from freya import hands_free
from freya.settings import Settings


class FakeListener:
    def __init__(self, running=True):
        self.running = running
        self.muted = False

    def mute(self):
        self.muted = True

    def unmute(self):
        self.muted = False


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "settings.json")


def test_settings_default_to_freya(settings):
    assert settings.wake_names == ["freya"]
    assert not settings.path.exists()


def test_names_are_saved_and_reloaded(settings):
    assert settings.add_wake_name("  Computer ") == "computer"
    assert settings.add_wake_name("Hey You") == "hey you"

    reloaded = Settings(settings.path)

    assert reloaded.wake_names == ["freya", "computer", "hey you"]
    assert json.loads(settings.path.read_text(encoding="utf-8")) == {
        "wake_names": ["freya", "computer", "hey you"],
        "voice_volume": 100,
        "readable_folders": [],
    }


def test_duplicate_and_unusable_names_are_rejected(settings):
    for name in ("Freya", "freya", "", "123", "x" * 40):
        with pytest.raises(ValueError):
            settings.add_wake_name(name)

    assert settings.wake_names == ["freya"]


def test_names_can_be_removed_but_not_the_last_one(settings):
    settings.add_wake_name("computer")

    assert settings.remove_wake_name("Freya") == "freya"
    assert settings.wake_names == ["computer"]

    with pytest.raises(ValueError) as error:
        settings.remove_wake_name("computer")

    assert "only name" in str(error.value)
    assert settings.wake_names == ["computer"]

    with pytest.raises(ValueError):
        settings.remove_wake_name("nobody")


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"wake_names": "freya"}',
        '{"wake_names": []}',
        '{"wake_names": [1, null, ""]}',
    ],
)
def test_damaged_settings_fall_back_to_defaults(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")

    assert Settings(path).wake_names == ["freya"]


def test_usable_names_survive_a_partly_damaged_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        '{"wake_names": ["Computer", 7, "computer", "  Frey  "]}',
        encoding="utf-8",
    )

    assert Settings(path).wake_names == ["computer", "frey"]


def test_mute_and_unmute(settings, capsys):
    listener = FakeListener()

    assert hands_free.handle_listening_command("/mute", listener, settings)
    assert listener.muted is True
    assert "muted" in capsys.readouterr().out

    assert hands_free.handle_listening_command("/UNMUTE", listener, settings)
    assert listener.muted is False
    assert "listening" in capsys.readouterr().out


@pytest.mark.parametrize("listener", [None, FakeListener(running=False)])
def test_mute_without_a_running_microphone(settings, capsys, listener):
    assert hands_free.handle_listening_command("/mute", listener, settings)
    assert "not running" in capsys.readouterr().out


def test_name_commands(settings, capsys):
    listener = FakeListener()

    assert hands_free.handle_listening_command("/names", listener, settings)
    assert "I answer to Freya." in capsys.readouterr().out

    assert hands_free.handle_listening_command(
        "/name add Hey You", listener, settings
    )
    assert "Hey You" in capsys.readouterr().out
    assert settings.wake_names == ["freya", "hey you"]

    hands_free.handle_listening_command("/name add computer", listener, settings)
    hands_free.handle_listening_command("/names", listener, settings)
    assert "Freya, Hey You or Computer" in capsys.readouterr().out

    assert hands_free.handle_listening_command(
        "/name remove hey you", listener, settings
    )
    assert settings.wake_names == ["freya", "computer"]


def test_name_command_problems_are_explained(settings, capsys):
    listener = FakeListener()

    hands_free.handle_listening_command("/name add freya", listener, settings)
    assert "already answer" in capsys.readouterr().out

    hands_free.handle_listening_command("/name remove freya", listener, settings)
    assert "only name" in capsys.readouterr().out

    for malformed in ("/name", "/name add", "/name rename a b", "/names extra"):
        assert hands_free.handle_listening_command(malformed, listener, settings)
        assert "Usage:" in capsys.readouterr().out

    assert settings.wake_names == ["freya"]


@pytest.mark.parametrize(
    "text", ["", "hello", "/listen", "/named", "mute", "what does /mute do"]
)
def test_other_input_is_not_a_listening_command(settings, text):
    assert hands_free.handle_listening_command(text, FakeListener(), settings) is False


def test_describe_listening(settings):
    assert "off" in hands_free.describe_listening(None, settings)
    assert "off" in hands_free.describe_listening(
        FakeListener(running=False), settings
    )

    listener = FakeListener()
    settings.add_wake_name("computer")

    assert "Start with Freya or Computer" in hands_free.describe_listening(
        listener, settings
    )

    listener.mute()

    assert "muted" in hands_free.describe_listening(listener, settings)


def test_speech_recognition_failure_is_reported_not_raised(monkeypatch, capsys):
    def broken():
        raise RuntimeError("CUDA is not available")

    monkeypatch.setattr(hands_free.stt_module, "FreyaSTT", broken)

    assert hands_free.load_speech_recognition() is None
    assert "CUDA is not available" in capsys.readouterr().out
    assert (
        hands_free.start_background_listening(None, None, None, None, None)
        is None
    )
