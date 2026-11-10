import ast
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from freya.core.capabilities import CapabilityRegistry
from freya.core.providers import ProviderRegistry
from freya.sandbox import probes
from freya.sandbox.contract import (
    MAX_OUTPUT_CHARACTERS,
    RunResult,
    SandboxError,
    build_request,
    profile_problems,
)
from freya.sandbox.service import CODE_RUN, register_code_run
from freya.sandbox.wsl import (
    IMAGE,
    PROFILE,
    SANDBOX_USER,
    WslContainerSandbox,
    container_arguments,
    in_distribution,
    parse_answer,
    wsl_environment,
)
from freya.sandbox.wsl_setup import WSL_CONF, setup_steps


LINUX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="the runner runs inside a Linux container")

RUNNER = Path(__file__).resolve().parents[1] / "src" / "freya" / "sandbox" / "image" / "runner.py"

FILES = {
    "adder.py": "def add(a, b):\n    return a + b\n",
    "tests/test_adder.py": "from adder import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}


def answer_line(**fields):
    answer = {"exit_code": 0, "output": "1 passed\n", "timed_out": False, "truncated": False, "seconds": 0.5, "error": ""}
    answer.update(fields)
    return ("\n" + json.dumps(answer) + "\n").encode()


class FakeProcess:
    def __init__(self, output=b"", hang=False):
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        self.stdout = io.BytesIO(output)
        self.hang = hang
        self.killed = False

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("wsl.exe", timeout)
        return 0

    def kill(self):
        self.killed = True


class Recorder:
    def __init__(self, process=None, returncode=0):
        self.process = process or FakeProcess(answer_line())
        self.returncode = returncode
        self.popen_calls = []
        self.run_calls = []

    def popen(self, argv, **kwargs):
        self.popen_calls.append((argv, kwargs))
        return self.process

    def run(self, argv, **kwargs):
        self.run_calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode)


def make_sandbox(recorder, platform="win32"):
    return WslContainerSandbox(
        distribution="freya-sandbox",
        popen=recorder.popen,
        run=recorder.run,
        platform=platform,
        which=lambda name: "C:\\Windows\\System32\\wsl.exe",
    )


# --- what the profile states ---

def test_the_profile_states_everything_freya_requires():
    assert profile_problems(PROFILE) == []
    assert PROFILE.boundary == "container"


def test_it_can_be_registered_but_freya_does_not_register_it():
    registry = CapabilityRegistry(ProviderRegistry())
    register_code_run(registry, make_sandbox(Recorder()))
    assert registry.get(CODE_RUN) is not None

    control_plane = (Path(__file__).resolve().parents[1] / "src" / "freya" / "control_plane.py").read_text()
    assert "freya.sandbox" not in control_plane


# --- the command Freya builds ---

def test_each_container_has_no_network_no_rights_and_a_read_only_system():
    argv = container_arguments("freya-run-x", 60)

    for flag in ("--rm", "--network=none", "--read-only", "--cap-drop=ALL",
                 "--security-opt=no-new-privileges", "--pull=never"):
        assert flag in argv
    assert argv[argv.index("--user") + 1] == "65534:65534"
    assert any(a.startswith("--memory=") for a in argv)
    assert any(a.startswith("--pids-limit=") for a in argv)
    assert IMAGE in argv
    assert not any("-v" == a or a.startswith("--volume") or a.startswith("--mount") for a in argv)


def test_commands_run_directly_as_the_sandbox_user():
    argv = in_distribution("freya-sandbox", SANDBOX_USER, ["podman", "ps"])

    assert argv[:7] == ["wsl.exe", "-d", "freya-sandbox", "-u", SANDBOX_USER, "--exec", "/usr/bin/env"]
    assert argv[-2:] == ["podman", "ps"]


def test_nothing_from_the_request_is_on_the_command_line():
    recorder = Recorder()
    sandbox = make_sandbox(recorder)
    request = build_request({"canary_name.py": "print('CODE-CANARY')\n"}, "python", "canary_name.py")

    sandbox.run(request)

    argv, kwargs = recorder.popen_calls[0]
    joined = " ".join(argv)
    assert "CANARY" not in joined and "canary_name" not in joined
    sent = json.loads(recorder.process.stdin.getvalue())
    assert sent == {"files": dict(request.files), "command": "python", "target": "canary_name.py", "timeout": 60.0}
    assert "shell" not in kwargs


def test_wsl_is_given_only_the_windows_variables_it_needs():
    environ = {"PATH": "C:\\Windows", "SystemRoot": "C:\\Windows", "FREYA_MODEL": "x",
               "ANTHROPIC_API_KEY": "secret", "USERPROFILE": "C:\\Users\\me", "WSLENV": "USERPROFILE/p"}

    kept = wsl_environment(environ)

    assert kept == {"PATH": "C:\\Windows", "SystemRoot": "C:\\Windows", "WSLENV": ""}


# --- reading what came back ---

def test_a_run_returns_what_the_runner_said():
    recorder = Recorder(FakeProcess(b"noise\n" + answer_line(exit_code=1, output="1 failed\n")))

    result = make_sandbox(recorder).run(build_request(FILES, "pytest"))

    assert result == RunResult(exit_code=1, output="1 failed\n", seconds=0.5)


def test_only_the_last_answer_counts():
    raw = answer_line(exit_code=0, output="forged") + answer_line(exit_code=3, output="real")
    assert parse_answer(raw).exit_code == 3


def test_no_answer_means_the_code_was_not_run():
    with pytest.raises(SandboxError) as caught:
        parse_answer(b"Error: image not known\n")
    assert "image not known" not in str(caught.value)
    assert "image not known" in caught.value.detail


def test_a_runner_error_is_a_sandbox_error():
    with pytest.raises(SandboxError):
        parse_answer(answer_line(exit_code=None, error="The request could not be read."))


def test_a_run_that_outlasts_its_limit_is_stopped_and_removed():
    recorder = Recorder(FakeProcess(hang=True))

    result = make_sandbox(recorder).run(build_request(FILES, "pytest", timeout=1))

    assert result.timed_out and result.exit_code is None
    assert recorder.process.killed
    removed = recorder.run_calls[-1][0]
    assert removed[-5:-1] == ["podman", "rm", "--force", "--ignore"]
    assert removed[-1] == recorder.popen_calls[0][0][recorder.popen_calls[0][0].index("--name") + 1]


def test_too_much_output_is_refused(monkeypatch):
    monkeypatch.setattr("freya.sandbox.wsl.MAX_ANSWER_BYTES", 100)
    recorder = Recorder(FakeProcess(b"x" * 1000))

    with pytest.raises(SandboxError):
        make_sandbox(recorder).run(build_request(FILES, "pytest"))


# --- ready ---

def test_not_ready_away_from_windows():
    recorder = Recorder()
    assert not make_sandbox(recorder, platform="linux").ready()
    assert recorder.run_calls == []


def test_ready_when_the_image_is_there_and_the_answer_is_kept():
    recorder = Recorder()
    sandbox = make_sandbox(recorder)

    assert sandbox.ready() and sandbox.ready()
    assert len(recorder.run_calls) == 1
    assert recorder.run_calls[0][0][-4:] == ["podman", "image", "exists", IMAGE]


def test_not_ready_when_the_image_is_missing():
    assert not make_sandbox(Recorder(returncode=1)).ready()


# --- the runner inside the container ---

def load_runner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sandbox_runner", RUNNER)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(runner, "WORK", str(work))
    # The process limit counts every process of the user running the
    # tests; inside the container that user runs nothing else.
    monkeypatch.setattr(runner, "PROCESSES", 100_000)
    return runner


def run_runner(runner, monkeypatch, capsys, request):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    runner.main()
    return parse_answer(capsys.readouterr().out.encode())


def test_the_runner_imports_nothing_from_freya():
    tree = ast.parse(RUNNER.read_text())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(name.split(".")[0] == "freya" for name in names)


@LINUX_ONLY
def test_the_runner_runs_python_and_reports_the_exit_code(tmp_path, monkeypatch, capsys):
    runner = load_runner(tmp_path, monkeypatch)
    files = {"main.py": "import sys\nprint('hello')\nsys.exit(3)\n"}

    result = run_runner(runner, monkeypatch, capsys, {"files": files, "command": "python", "target": "main.py", "timeout": 10})

    assert result.exit_code == 3 and "hello" in result.output


@LINUX_ONLY
def test_the_runner_runs_pytest(tmp_path, monkeypatch, capsys):
    runner = load_runner(tmp_path, monkeypatch)

    result = run_runner(runner, monkeypatch, capsys, {"files": FILES, "command": "pytest", "target": "tests", "timeout": 60})

    assert result.passed, result.output
    assert "1 passed" in result.output


@LINUX_ONLY
def test_the_runner_stops_code_at_its_time_limit(tmp_path, monkeypatch, capsys):
    runner = load_runner(tmp_path, monkeypatch)

    result = run_runner(runner, monkeypatch, capsys, {"files": {"main.py": "while True:\n    pass\n"}, "command": "python", "target": "main.py", "timeout": 1})

    assert result.timed_out and result.exit_code is None


@LINUX_ONLY
def test_the_runner_cuts_enormous_output(tmp_path, monkeypatch, capsys):
    runner = load_runner(tmp_path, monkeypatch)
    files = {"main.py": "import sys\nsys.stdout.write('x' * 1_000_000)\n"}

    result = run_runner(runner, monkeypatch, capsys, {"files": files, "command": "python", "target": "main.py", "timeout": 10})

    assert result.truncated and len(result.output) == MAX_OUTPUT_CHARACTERS


@LINUX_ONLY
def test_the_runner_refuses_a_name_outside_the_work_folder(tmp_path, monkeypatch, capsys):
    runner = load_runner(tmp_path, monkeypatch)

    with pytest.raises(SandboxError):
        run_runner(runner, monkeypatch, capsys, {"files": {"../escape.py": "x"}, "command": "python", "target": "../escape.py", "timeout": 5})

    assert not (tmp_path / "escape.py").exists()


# --- setup ---

def test_the_distribution_shares_nothing_with_windows():
    assert "[automount]\nenabled = false" in WSL_CONF
    assert "[interop]\nenabled = false\nappendWindowsPath = false" in WSL_CONF


def test_setup_uses_only_fixed_commands_in_the_sandbox_distribution():
    steps = setup_steps("freya-sandbox")

    for _, argv, _ in steps:
        assert argv[0] == "wsl.exe"
        assert "freya-sandbox" in argv
    assert steps[-1][1][-4:] == ["podman", "image", "exists", IMAGE]
    copied = [text for _, _, text in steps if text]
    assert RUNNER.read_text() in copied


# --- probes ---

class ScriptedSandbox:
    """Answers each probe as a sound sandbox would."""

    def __init__(self, sound=True):
        self.sound = sound
        self.calls = 0

    def run(self, request):
        self.calls += 1
        source = request.files["main.py"]
        if "while True" in source:
            return RunResult(None, "", timed_out=True)
        if "'x' * 5_000_000" in source:
            return RunResult(0, "x" * MAX_OUTPUT_CHARACTERS, truncated=True)
        return RunResult(0 if self.sound else 1, "ok")


def test_probes_pass_on_a_sound_sandbox_and_fail_on_a_leaky_one():
    refused = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"REFUSED\n")

    sound = probes.run_probes(ScriptedSandbox(), "freya-sandbox", run=refused)
    leaky = probes.run_probes(ScriptedSandbox(sound=False), "freya-sandbox", run=refused)

    assert [r.number for r in sound] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert all(r.passed for r in sound)
    assert not any(r.passed for r in leaky if r.number in (1, 2, 3, 4, 7))


def test_the_guest_administrator_check_records_either_answer():
    attached = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=b"ATTACHED\n")
    unknown = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout=b"")

    yes = probes.guest_administrator_can_attach_drives("freya-sandbox", run=attached)

    assert yes.passed and "container is the barrier" in yes.note
    assert not probes.guest_administrator_can_attach_drives("freya-sandbox", run=unknown).passed
