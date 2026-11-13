import urllib.error

import sys

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.providers import ProviderHealth, ProviderState
from freya.image.image_generator import FreyaImageGenerator
from freya.providers.image import comfyui
from freya.providers.image.comfyui import (
    COMFYUI_PROVIDER,
    ComfyUIServer,
    LaunchPlan,
)


URL = "http://127.0.0.1:8188"


class FakeResponse:
    def read(self):
        return b'{"system": {}}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.exit_code = None

    def poll(self):
        return self.exit_code


class FakeComfy:
    """A ComfyUI that can be down, up, or come up some time after launch."""

    def __init__(self, monkeypatch):
        self.up = False
        self.launched = []
        self.processes = []
        self.stopped = []
        self.ready_after_polls = 3
        self.polls_since_launch = 0
        self.launch_error = None
        self.dies_on_launch = False
        self.now = 0.0
        monkeypatch.setattr(comfyui.urllib.request, "urlopen", self.urlopen)
        monkeypatch.setattr(comfyui, "_launch", self.launch)
        monkeypatch.setattr(comfyui, "_stop_process_tree", self.stop_process)
        monkeypatch.setattr(comfyui.time, "sleep", self.sleep)
        monkeypatch.setattr(comfyui.time, "monotonic", lambda: self.now)

    def urlopen(self, url, timeout=None):
        assert url == f"{URL}/system_stats"
        if not self.up:
            raise urllib.error.URLError("connection refused")
        return FakeResponse()

    def launch(self, plan, log_path=None):
        if self.launch_error:
            raise self.launch_error
        self.launched.append(plan)
        self.polls_since_launch = 0
        process = FakeProcess(1000 + len(self.launched))
        if self.dies_on_launch:
            process.exit_code = 1
        self.processes.append(process)
        return process

    def stop_process(self, process):
        self.stopped.append(process.pid)
        self.up = False

    def sleep(self, seconds):
        self.now += seconds
        if (
            self.launched
            and self.ready_after_polls is not None
            and not self.dies_on_launch
        ):
            self.polls_since_launch += 1
            if self.polls_since_launch >= self.ready_after_polls:
                self.up = True


@pytest.fixture
def comfy(monkeypatch):
    return FakeComfy(monkeypatch)


HEADLESS = LaunchPlan(command=["python", "main.py"], cwd="C:\\comfy", headless=True)
APP = LaunchPlan(command=["Comfy Desktop.exe"])


def make_server(monkeypatch, plan, **kwargs):
    monkeypatch.setattr(comfyui, "find_launch_plan", lambda url, configured=None: plan)
    return ComfyUIServer(URL, **kwargs)


def test_health_reflects_whether_the_server_answers(comfy):
    assert comfyui.check_comfyui_health(URL) == ProviderHealth.OFFLINE

    comfy.up = True

    assert comfyui.check_comfyui_health(URL + "/") == ProviderHealth.HEALTHY


def test_nothing_is_launched_when_already_running(comfy, monkeypatch):
    comfy.up = True
    server = make_server(monkeypatch, HEADLESS)

    assert server.start() == "already running"
    assert comfy.launched == []
    assert server.owns_server is False
    # A ComfyUI the owner started is never stopped by Freya.
    assert server.stop() == "not ours"
    assert comfy.stopped == []


def test_starts_the_server_and_waits_until_it_answers(comfy, monkeypatch):
    server = make_server(monkeypatch, HEADLESS)

    assert server.start() == "started"

    assert comfy.launched == [HEADLESS]
    assert comfy.now == 3.0
    assert server.owns_server is True


def test_stops_only_a_server_it_started(comfy, monkeypatch):
    server = make_server(monkeypatch, HEADLESS)
    server.start()

    assert server.stop() == "stopped"
    assert comfy.stopped == [1001]
    assert server.owns_server is False
    assert server.stop() == "not ours"
    assert comfy.stopped == [1001]


def test_an_opened_application_is_never_stopped(comfy, monkeypatch):
    server = make_server(monkeypatch, APP)

    assert server.start() == "started"
    assert server.owns_server is False
    assert server.stop() == "not ours"
    assert comfy.stopped == []


def test_gives_up_and_cleans_up_when_the_server_never_becomes_ready(
    comfy, monkeypatch
):
    comfy.ready_after_polls = None
    server = make_server(monkeypatch, HEADLESS, timeout=10, log_path="C:\\logs\\comfy.log")

    with pytest.raises(RuntimeError) as error:
        server.start()

    assert "did not become ready within 10 seconds" in str(error.value)
    assert "C:\\logs\\comfy.log" in str(error.value)
    assert comfy.stopped == [1001]
    assert server.owns_server is False


def test_application_that_waits_for_a_click_is_explained(comfy, monkeypatch):
    comfy.ready_after_polls = None
    server = make_server(monkeypatch, APP, timeout=10)

    with pytest.raises(RuntimeError) as error:
        server.start()

    assert "waiting for you in its own window" in str(error.value)
    assert comfy.stopped == []


def test_server_that_exits_during_startup_fails_fast(comfy, monkeypatch):
    comfy.dies_on_launch = True
    server = make_server(monkeypatch, HEADLESS, timeout=100)

    with pytest.raises(RuntimeError) as error:
        server.start()

    assert "stopped while starting up" in str(error.value)
    assert comfy.now == 1.0
    assert server.owns_server is False


def test_explains_when_it_does_not_know_how_to_start(comfy, monkeypatch):
    server = make_server(monkeypatch, None)

    with pytest.raises(RuntimeError) as error:
        server.start()

    assert "FREYA_COMFYUI_COMMAND" in str(error.value)
    assert comfy.launched == []


def test_launch_failure_is_reported(comfy, monkeypatch):
    comfy.launch_error = FileNotFoundError("python.exe not found")
    server = make_server(monkeypatch, HEADLESS)

    with pytest.raises(RuntimeError) as error:
        server.start()

    assert "could not be started" in str(error.value)


def test_a_dead_server_is_replaced(comfy, monkeypatch):
    server = make_server(monkeypatch, HEADLESS)
    server.start()

    comfy.up = False

    assert server.start() == "started"
    assert comfy.stopped == [1001]
    assert len(comfy.launched) == 2
    assert server.owns_server is True


def make_install(tmp_path, monkeypatch, with_extras=True):
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    install = local / "Comfy-Desktop" / "ComfyUI-Installs" / "ComfyUI"
    (install / "ComfyUI" / ".venv" / "Scripts").mkdir(parents=True)
    (install / "ComfyUI" / ".venv" / "Scripts" / "python.exe").write_text("")
    (install / "ComfyUI" / "main.py").write_text("")

    if with_extras:
        shared = local / "Comfy-Desktop" / "ComfyUI-Shared"
        (shared / "input").mkdir(parents=True)
        (shared / "output").mkdir(parents=True)
        configs = roaming / "Comfy Desktop" / "instance-model-paths"
        configs.mkdir(parents=True)
        (configs / "inst-1.yaml").write_text("")

    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    return install, local, roaming


@pytest.mark.skipif(sys.platform != "win32", reason="the plan quotes a Windows path to ComfyUI's main.py")
def test_server_plan_uses_the_desktop_install(tmp_path, monkeypatch):
    install, local, roaming = make_install(tmp_path, monkeypatch)

    plan = comfyui.find_launch_plan("http://127.0.0.1:8190")

    assert plan.headless is True
    assert plan.cwd == str(install)
    assert plan.command == [
        str(install / "ComfyUI" / ".venv" / "Scripts" / "python.exe"),
        "-s",
        "ComfyUI\\main.py",
        "--port",
        "8190",
        "--disable-auto-launch",
        "--extra-model-paths-config",
        str(roaming / "Comfy Desktop" / "instance-model-paths" / "inst-1.yaml"),
        "--input-directory",
        str(local / "Comfy-Desktop" / "ComfyUI-Shared" / "input"),
        "--output-directory",
        str(local / "Comfy-Desktop" / "ComfyUI-Shared" / "output"),
    ]


def test_server_plan_without_shared_folders_or_model_config(tmp_path, monkeypatch):
    make_install(tmp_path, monkeypatch, with_extras=False)

    plan = comfyui.find_launch_plan(URL)

    assert plan.headless is True
    assert plan.command[3:] == ["--port", "8188", "--disable-auto-launch"]


def test_launch_plan_order_of_preference(tmp_path, monkeypatch):
    make_install(tmp_path, monkeypatch)

    # A configured program wins over everything.
    configured = comfyui.find_launch_plan(URL, "D:\\tools\\comfy.bat")
    assert configured == LaunchPlan(command=["D:\\tools\\comfy.bat"])

    # With no server install, fall back to opening the application.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nowhere"))
    app = tmp_path / "Program Files" / "Comfy Desktop" / "Comfy Desktop.exe"
    app.parent.mkdir(parents=True)
    app.write_text("")

    assert comfyui.find_launch_plan(URL) == LaunchPlan(command=[str(app)])

    # With neither, Freya cannot start it.
    app.unlink()

    assert comfyui.find_launch_plan(URL) is None


@pytest.fixture
def plane(tmp_path, fake_ollama, comfy, monkeypatch):
    monkeypatch.setattr(
        comfyui, "find_launch_plan", lambda url, configured=None: HEADLESS
    )
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


def test_comfyui_is_not_started_at_startup(plane, comfy):
    provider = plane.providers.get(COMFYUI_PROVIDER)

    assert comfy.launched == []
    assert provider.state == ProviderState.STOPPED
    assert provider.metadata["can_start"] is True
    assert provider.metadata["headless"] is True
    assert plane.startup_messages() == []


def test_image_request_starts_comfyui_when_it_is_down(plane, comfy):
    said = []

    plane.ensure_image_backend(announce=said.append)

    provider = plane.providers.get(COMFYUI_PROVIDER)

    assert comfy.launched == [HEADLESS]
    assert provider.state == ProviderState.RUNNING
    assert provider.health == ProviderHealth.HEALTHY
    assert "starting it" in said[0]
    assert said[-1] == "Freya: ComfyUI is ready."
    assert not any("start button" in line for line in said)


def test_image_request_does_nothing_when_comfyui_is_already_up(plane, comfy):
    comfy.up = True
    said = []

    plane.ensure_image_backend(announce=said.append)
    plane.ensure_image_backend(announce=said.append)

    assert comfy.launched == []
    assert said == []
    assert plane.providers.get(COMFYUI_PROVIDER).state == ProviderState.RUNNING


def test_comfyui_is_restarted_if_it_stopped_answering(plane, comfy):
    plane.ensure_image_backend(announce=lambda message: None)

    comfy.up = False

    plane.ensure_image_backend(announce=lambda message: None)

    assert len(comfy.launched) == 2
    assert plane.providers.get(COMFYUI_PROVIDER).health == ProviderHealth.HEALTHY


def test_failure_to_start_is_raised_and_can_be_retried(plane, comfy):
    comfy.launch_error = FileNotFoundError("python.exe not found")

    with pytest.raises(RuntimeError) as error:
        plane.ensure_image_backend(announce=lambda message: None)

    provider = plane.providers.get(COMFYUI_PROVIDER)

    assert "could not be started" in str(error.value)
    assert provider.state == ProviderState.FAILED

    comfy.launch_error = None
    plane.ensure_image_backend(announce=lambda message: None)

    assert provider.state == ProviderState.RUNNING


def test_closing_freya_stops_the_server_she_started(
    tmp_path, fake_ollama, comfy, monkeypatch
):
    monkeypatch.setattr(
        comfyui, "find_launch_plan", lambda url, configured=None: HEADLESS
    )
    plane = build_local_control_plane(tmp_path / "control.db", None)
    plane.ensure_image_backend(announce=lambda message: None)

    plane.close()

    assert comfy.stopped == [1001]


def test_closing_freya_leaves_the_owners_comfyui_running(
    tmp_path, fake_ollama, comfy, monkeypatch
):
    monkeypatch.setattr(
        comfyui, "find_launch_plan", lambda url, configured=None: HEADLESS
    )
    comfy.up = True
    plane = build_local_control_plane(tmp_path / "control.db", None)
    plane.ensure_image_backend(announce=lambda message: None)

    plane.close()

    assert comfy.stopped == []
    assert comfy.up is True


def test_user_is_warned_when_only_the_application_can_be_opened(
    tmp_path, fake_ollama, comfy, monkeypatch
):
    monkeypatch.setattr(
        comfyui, "find_launch_plan", lambda url, configured=None: APP
    )
    plane = build_local_control_plane(tmp_path / "control.db", None)
    said = []

    plane.ensure_image_backend(announce=said.append)
    plane.close()

    assert any("start button" in line for line in said)


def test_image_generator_calls_its_ensure_hook():
    calls = []

    FreyaImageGenerator(ensure_running=lambda: calls.append(1)).ensure_running()
    FreyaImageGenerator().ensure_running()

    assert calls == [1]
