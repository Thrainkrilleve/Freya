import ast
import os
import subprocess
from pathlib import Path

import pytest

import freya
from freya.child_environment import child_environment


# Made-up credentials. Nothing here is, or resembles, a real one.
FAKE_CREDENTIALS = {
    "FREYA_JELLYFIN_TOKEN": "fake-jellyfin-token",
    "FREYA_YOUTUBE_API_KEY": "fake-youtube-key",
    "OPENAI_API_KEY": "fake-model-key",
    "HF_TOKEN": "fake-hub-token",
    "AWS_SECRET_ACCESS_KEY": "fake-cloud-secret",
    "AWS_ACCESS_KEY_ID": "fake-cloud-key-id",
    "PGPASSWORD": "fake-database-password",
    "GOOGLE_APPLICATION_CREDENTIALS": "fake-credentials-path",
    "SSH_PRIVATE_KEY": "fake-private-key",
}
MARKER = "FREYA_TEST_RUNTIME_SETTING"


def leaked(environment) -> list:
    """
    The names of the made-up credentials found in an environment.
    Names only, so that a failure never prints the real environment.
    """
    values = set(environment.values())

    return sorted(name for name, value in FAKE_CREDENTIALS.items() if value in values)


# -- the helper ----------------------------------------------------------------


def test_credentials_are_left_out_and_everything_else_is_passed_on():
    needed = {
        "PATH": r"C:\Windows\System32;C:\Tools",
        "SystemRoot": r"C:\Windows",
        "TEMP": r"C:\Temp",
        "USERPROFILE": r"C:\Users\someone",
        "CUDA_VISIBLE_DEVICES": "0",
        "PYTHONIOENCODING": "utf-8",
        "TOKENIZERS_PARALLELISM": "false",
        "FREYA_JELLYFIN_URL": "http://media.invalid:8096",
        "FREYA_BLENDER": r"C:\Blender\blender.exe",
    }

    environment = child_environment(environ={**needed, **FAKE_CREDENTIALS})

    assert environment == needed
    assert leaked(environment) == []


def test_names_are_matched_in_any_case():
    environment = child_environment(
        environ={"Github_Token": "fake-a", "npm_config_authtoken": "fake-b", "Path": "kept"}
    )

    assert environment == {"Path": "kept"}


def test_what_the_caller_adds_is_passed_on_unfiltered():
    environment = child_environment(
        {"FREYA_PAIRING_KEY": "one-use", "HF_HOME": "models"},
        environ={"PATH": "kept", "HF_TOKEN": "fake-hub-token"},
    )

    assert environment == {"PATH": "kept", "FREYA_PAIRING_KEY": "one-use", "HF_HOME": "models"}


def test_freyas_own_environment_is_not_changed(monkeypatch):
    monkeypatch.setenv("FREYA_JELLYFIN_TOKEN", "fake-jellyfin-token")

    assert leaked(child_environment()) == []
    assert os.environ["FREYA_JELLYFIN_TOKEN"] == "fake-jellyfin-token"


# -- every place a program is started --------------------------------------------


STARTERS = {"Popen", "run", "call", "check_call", "check_output"}


def test_every_program_freya_starts_is_given_an_environment():
    """
    A program started without `env` inherits everything. Each start in
    the source names its environment, and none builds it from the whole
    of Freya's own.
    """
    root = Path(freya.__file__).parent
    inheriting = []

    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "**os.environ" not in text, path.name

        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in STARTERS
                and not any(keyword.arg == "env" for keyword in node.keywords)
            ):
                inheriting.append(f"{path.relative_to(root)}:{node.lineno}")

    assert inheriting == []


class Started(Exception):
    """Raised by the stand-in starter, so no program is ever run."""


@pytest.fixture
def started(monkeypatch):
    """
    Put made-up credentials in the environment and record the
    environment of every program that is started.
    """
    for name, value in FAKE_CREDENTIALS.items():
        monkeypatch.setenv(name, value)

    monkeypatch.setenv(MARKER, "kept")
    environments = []

    def start(command, **options):
        environments.append(options.get("env"))
        raise Started()

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(subprocess, "run", start)

    return environments


def start_unreal_client(tmp_path, monkeypatch):
    from freya.unreal_client import UnrealClient

    UnrealClient(tmp_path / "Freya.exe", start=subprocess.Popen).launch(8770, "one-use")


def start_speech_helper(tmp_path, monkeypatch):
    from freya.speech.helper import HelperProcess

    HelperProcess("python", tmp_path / "worker.py", environment={"HF_HOME": "models"})._start()


def start_avatar_overlay(tmp_path, monkeypatch):
    from freya.spatial_avatar.host import AvatarHost

    AvatarHost(log_path=tmp_path / "overlay.log").start()


def start_display_probe(tmp_path, monkeypatch):
    from freya.spatial_avatar.geometry import probe_layout

    probe_layout()


def start_blender(tmp_path, monkeypatch):
    from freya.spatial_avatar.converter import run_blender

    run_blender(Path("blender.exe"), "inspect_model.py", {}, 5, tmp_path / "blender.log")


def start_comfyui(tmp_path, monkeypatch):
    from freya.providers.image.comfyui import LaunchPlan, _launch

    _launch(LaunchPlan(command=["comfyui"]))


def start_window(tmp_path, monkeypatch):
    from freya import window

    monkeypatch.setattr(window, "_edge_path", lambda: Path("msedge.exe"))
    window.open_app_window("http://127.0.0.1:8770/", tmp_path / "profile")


def start_media_decoder(tmp_path, monkeypatch):
    from freya.media.whisper import WhisperTranscriber

    recording = tmp_path / "recording.wav"
    recording.write_bytes(b"")

    class Speech:
        def transcribe_media_chunk(self, audio):
            return []

    imported = recording.stat()
    WhisperTranscriber(Speech).transcribe(
        recording, size=imported.st_size, modified_ns=imported.st_mtime_ns
    )


def start_gpu_query(tmp_path, monkeypatch):
    from freya.tools.system_tools import get_gpu_info

    get_gpu_info()


def start_hardware_query(tmp_path, monkeypatch):
    from freya.tools import hardware_inventory

    hardware_inventory._query()


def start_free_memory_query(tmp_path, monkeypatch):
    from freya.speech.setup import free_gpu_megabytes

    free_gpu_megabytes()


def start_engine_install(tmp_path, monkeypatch):
    from freya.speech import install

    install.run(["uv", "--version"], {"HF_HOME": "models"})


@pytest.mark.parametrize(
    "start",
    [
        start_unreal_client,
        start_speech_helper,
        start_avatar_overlay,
        start_display_probe,
        start_blender,
        start_comfyui,
        start_window,
        start_media_decoder,
        start_gpu_query,
        start_hardware_query,
        start_free_memory_query,
        start_engine_install,
    ],
    ids=lambda start: start.__name__,
)
def test_no_started_program_inherits_a_credential(start, started, tmp_path, monkeypatch):
    with pytest.raises(Started):
        start(tmp_path, monkeypatch)

    assert len(started) == 1
    environment = started[0]

    assert environment is not None
    assert leaked(environment) == []
    # What a program needs to run is still there.
    assert environment.get(MARKER) == "kept"
    assert "PATH" in {name.upper() for name in environment}
