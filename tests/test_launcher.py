import subprocess
import sys

import pytest

from freya import launcher
from freya.gpu import status
from freya.tools import hardware_inventory, system_tools


pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="console windows are a Windows concern"
)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture
def restore_popen():
    original = subprocess.Popen.__init__
    yield
    subprocess.Popen.__init__ = original
    if hasattr(subprocess.Popen, "_freya_hidden"):
        del subprocess.Popen._freya_hidden


def test_helpers_are_hidden_unless_the_caller_chose(restore_popen):
    received = []

    # Stand in for the real constructor, so nothing is actually started
    # and the flags the launcher passes on can be seen.
    def recorder(self, *args, creationflags=0, **kwargs):
        received.append(creationflags)

    subprocess.Popen.__init__ = recorder

    def flags_used(**kwargs):
        subprocess.Popen.__new__(subprocess.Popen).__init__(["helper"], **kwargs)
        return received[-1]

    assert flags_used() == 0

    launcher.hide_helper_windows()
    launcher.hide_helper_windows()  # harmless to ask twice

    assert flags_used() == NO_WINDOW

    # A caller that asked for a detached helper or a console of its own
    # keeps exactly what it asked for.
    assert (
        flags_used(creationflags=subprocess.DETACHED_PROCESS)
        == subprocess.DETACHED_PROCESS
    )
    assert (
        flags_used(creationflags=subprocess.CREATE_NEW_CONSOLE)
        == subprocess.CREATE_NEW_CONSOLE
    )

    # Other flags are kept and the window is still hidden.
    assert (
        flags_used(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        == subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW
    )


def test_hidden_helpers_still_run(restore_popen):
    launcher.hide_helper_windows()

    result = subprocess.run(
        [sys.executable, "-c", "print('still works')"],
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "still works"


def test_freyas_own_tools_ask_for_no_window(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = ""

    def fake_run(command, **kwargs):
        calls.append((command[0], kwargs.get("creationflags")))
        return Done()

    monkeypatch.setattr(status.subprocess, "run", fake_run)
    system_tools.get_gpu_info()

    Done.stdout = "{}"
    hardware_inventory.clear_hardware_inventory_cache()
    monkeypatch.setattr(hardware_inventory.platform, "system", lambda: "Windows")
    monkeypatch.setattr(hardware_inventory.subprocess, "run", fake_run)
    hardware_inventory.get_hardware_inventory()
    hardware_inventory.clear_hardware_inventory_cache()

    assert calls == [
        *[(command[0], NO_WINDOW) for command in status.probe_commands("Windows")],
        ("powershell.exe", NO_WINDOW),
    ]


def test_second_launch_does_not_touch_the_running_log(tmp_path, monkeypatch):
    log = tmp_path / "freya.log"
    log.write_text("log of the Freya that is already running")
    shown = []

    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(launcher, "LOG_PATH", log)
    monkeypatch.setattr("freya.single_instance.claim", lambda: False)
    monkeypatch.setattr("freya.single_instance.show_message", shown.append)
    streams = (sys.stdout, sys.stderr)

    launcher.main_window()

    assert shown == ["Freya is already running."]
    assert log.read_text() == "log of the Freya that is already running"
    assert (sys.stdout, sys.stderr) == streams
