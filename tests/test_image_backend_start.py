import threading
import time

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.providers import ProviderHealth
from freya.providers.image.comfyui import (
    COMFYUI_PROVIDER,
    failure_reason,
)


class FakeServer:
    """Stands in for ComfyUI: how long it takes to start and whether it does."""

    def __init__(self):
        self.running = False
        self.starts = 0
        self.fail_with = None
        self.delay = 0.0

    def health(self):
        return ProviderHealth.HEALTHY if self.running else ProviderHealth.OFFLINE

    def start(self):
        self.starts += 1
        time.sleep(self.delay)
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        self.running = True
        return "started"


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    server = FakeServer()
    provider = plane.providers.get(COMFYUI_PROVIDER)
    provider.start_handler = server.start
    provider.health_handler = server.health
    provider.metadata.update(can_start=True, headless=True)
    plane.server = server
    yield plane
    plane.close()


def test_it_is_started_as_freya_comes_online(plane):
    said = []

    assert plane.start_image_backend_early(said.append) is True

    assert plane.server.starts == 1
    assert said == []
    assert plane.providers.get(COMFYUI_PROVIDER).health == ProviderHealth.HEALTHY


def test_one_already_running_is_left_alone(plane):
    plane.server.running = True

    assert plane.start_image_backend_early() is True
    assert plane.server.starts == 0


def test_a_failed_start_is_said_once_with_the_reason_and_is_not_an_error(plane):
    plane.server.fail_with = (
        "The ComfyUI server stopped while starting up: Windows blocked one "
        "of ComfyUI's files."
    )
    said = []

    assert plane.start_image_backend_early(said.append) is False

    assert len(said) == 1
    assert "could not start ComfyUI" in said[0]
    assert "Windows blocked" in said[0]


def test_a_later_request_for_a_picture_tries_again(plane):
    plane.server.fail_with = "It stopped."
    plane.start_image_backend_early(lambda text: None)
    plane.server.fail_with = None

    plane.ensure_image_backend(lambda text: None)

    assert plane.server.starts == 2
    assert plane.server.running


@pytest.mark.parametrize(
    "metadata",
    [{"can_start": False, "headless": False}, {"can_start": True, "headless": False}],
)
def test_a_server_that_needs_its_own_window_is_not_started_early(plane, metadata):
    plane.providers.get(COMFYUI_PROVIDER).metadata.update(metadata)

    assert plane.start_image_backend_early() is False
    assert plane.server.starts == 0


def test_two_starts_at_once_launch_one_server(plane):
    plane.server.delay = 0.3
    early = threading.Thread(target=plane.start_image_backend_early)

    early.start()
    time.sleep(0.05)
    # A picture is asked for while the early start is still under way.
    plane.ensure_image_backend(lambda text: None)
    early.join()

    assert plane.server.starts == 1


def test_the_reason_is_read_from_the_servers_own_log(tmp_path):
    log = tmp_path / "comfyui_server.log"
    log.write_text(
        "[INFO] starting\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 7, in <module>\n'
        "ImportError: DLL load failed while importing _quadpack: An "
        "Application Control policy has blocked this file.\n",
        encoding="utf-8",
    )

    reason = failure_reason(log)

    assert "Windows blocked one of ComfyUI's files" in reason
    assert "Smart App Control" in reason


def test_other_errors_are_quoted_briefly_and_silence_is_empty(tmp_path):
    log = tmp_path / "comfyui_server.log"
    log.write_text("[INFO] ok\nRuntimeError: " + "x" * 2000 + "\n", "utf-8")

    reason = failure_reason(log)
    assert reason.startswith("RuntimeError: xxx") and len(reason) <= 301

    log.write_text("[INFO] everything is fine\n[INFO] still fine\n", "utf-8")
    assert failure_reason(log) == ""
    assert failure_reason(tmp_path / "no-such.log") == ""
