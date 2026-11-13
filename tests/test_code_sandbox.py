import pytest

from freya.control_plane import build_local_control_plane
from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import (
    LOCAL_OWNER,
    CapabilityBroker,
    CapabilityRegistry,
)
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.core.providers import ProviderRegistry
from freya.sandbox.contract import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_OUTPUT_CHARACTERS,
    MAX_TIMEOUT_SECONDS,
    RunResult,
    SandboxError,
    SandboxProfile,
    build_request,
    checked_result,
    profile_problems,
)
from freya.sandbox.service import (
    CODE_RUN,
    SANDBOX_PROVIDER,
    create_sandbox_provider,
    register_code_run,
)
from freya.tools.model_tools import MODEL_TOOL


SOUND = SandboxProfile(
    name="fake",
    network=False,
    host_files=False,
    credentials=False,
    fresh_each_run=True,
    boundary="container",
)

FILES = {
    "adder.py": "def add(a, b):\n    return a + b\n",
    "tests/test_adder.py": (
        "from adder import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}


class FakeSandbox:
    """Stands in for a sandbox. It runs nothing and records what it was asked."""

    def __init__(self, profile=SOUND, result=None, error=None):
        self._profile = profile
        self.is_ready = True
        self.result = result or RunResult(exit_code=0, output="1 passed\n")
        self.error = error
        self.requests = []

    def profile(self):
        return self._profile

    def ready(self):
        return self.is_ready

    def run(self, request):
        self.requests.append(request)

        if self.error is not None:
            raise self.error

        return self.result


@pytest.fixture
def brokered(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    sandbox = FakeSandbox()
    providers = ProviderRegistry()
    providers.register(create_sandbox_provider(sandbox))
    providers.start(SANDBOX_PROVIDER)
    providers.check_health(SANDBOX_PROVIDER)
    registry = CapabilityRegistry(providers)
    register_code_run(registry, sandbox)
    policy = SqlitePolicyEngine(store)
    audit = SqliteAuditLog(store)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit, LOCAL_OWNER)

    yield broker, policy, store, registry, providers, sandbox

    store.close()


def allow(policy):
    policy.add_rule(LOCAL_OWNER.actor, CODE_RUN, "allow", "Test: owner allowed it.")


# --- what a sandbox must state about itself ---


def test_a_sound_profile_has_no_problems():
    assert profile_problems(SOUND) == []


@pytest.mark.parametrize("change", [
    {"network": True},
    {"host_files": True},
    {"credentials": True},
    {"fresh_each_run": False},
    {"boundary": "process"},
    {"network": None},
])
def test_a_profile_missing_a_required_property_is_refused(change):
    profile = SandboxProfile(**{**SOUND.__dict__, **change})

    assert profile_problems(profile)

    with pytest.raises(ValueError):
        register_code_run(CapabilityRegistry(ProviderRegistry()), FakeSandbox(profile))


def test_something_that_is_not_a_profile_is_refused():
    assert profile_problems({"network": False})


# --- what a run may be given ---


def test_a_good_request_is_accepted():
    request = build_request(FILES, "pytest", "tests", 30)

    assert request.files == FILES
    assert (request.command, request.target, request.timeout) == ("pytest", "tests", 30.0)


def test_the_request_keeps_its_own_copy_of_the_files():
    files = dict(FILES)
    request = build_request(files, "pytest")
    files["later.py"] = "print('added afterwards')\n"

    assert "later.py" not in request.files


@pytest.mark.parametrize("path", [
    "../adder.py",
    "tests/../../adder.py",
    "/etc/passwd",
    "C:/Users/someone/file.py",
    "tests\\test_adder.py",
    "tests//test_adder.py",
    "tests/",
    ".hidden.py",
    "-rf.py",
    "tests/-option.py",
    "name with space.py",
    "a" * 201 + ".py",
    "",
    7,
])
def test_a_path_that_could_leave_the_run_folder_is_refused(path):
    with pytest.raises(SandboxError):
        build_request({path: "x = 1\n"}, "pytest")


def test_one_name_cannot_be_both_a_file_and_a_folder():
    with pytest.raises(SandboxError):
        build_request({"a": "x = 1\n", "a/b.py": "y = 2\n"}, "pytest")


@pytest.mark.parametrize("files", [
    {},
    None,
    ["adder.py"],
    {"adder.py": b"bytes"},
    {"adder.py": "nul\x00inside"},
    {"big.py": "x" * (MAX_FILE_BYTES + 1)},
    {f"f{n}.py": "" for n in range(MAX_FILES + 1)},
    {f"f{n}.py": "x" * MAX_FILE_BYTES for n in range(9)},
])
def test_files_that_are_not_bounded_text_are_refused(files):
    with pytest.raises(SandboxError):
        build_request(files, "pytest")


@pytest.mark.parametrize("command", [
    "bash", "python -c 'import os'", "pytest; rm -rf /", "", None, ["pytest"],
])
def test_only_a_named_command_is_run(command):
    with pytest.raises(SandboxError):
        build_request(FILES, command)


@pytest.mark.parametrize("command, target", [
    ("python", ""),
    ("python", "missing.py"),
    ("python", "tests"),
    ("pytest", "missing"),
    ("pytest", "--help"),
    ("pytest", "tests; echo"),
    ("pytest", None),
])
def test_the_target_must_be_one_of_the_files(command, target):
    with pytest.raises(SandboxError):
        build_request({**FILES, "notes.txt": "hello\n"}, command, target)


def test_python_runs_only_a_python_file():
    with pytest.raises(SandboxError):
        build_request({"notes.txt": "hello\n"}, "python", "notes.txt")

    assert build_request(FILES, "python", "adder.py").target == "adder.py"


@pytest.mark.parametrize("timeout", [0, -1, MAX_TIMEOUT_SECONDS + 1, True, "60", None])
def test_a_time_limit_outside_the_bounds_is_refused(timeout):
    with pytest.raises(SandboxError):
        build_request(FILES, "pytest", "", timeout)


# --- what a run may give back ---


def test_long_output_is_cut_and_marked():
    result = checked_result(RunResult(1, "x" * (MAX_OUTPUT_CHARACTERS + 5)))

    assert len(result.output) == MAX_OUTPUT_CHARACTERS
    assert result.truncated
    assert not result.passed


def test_output_a_terminal_would_act_on_is_made_harmless():
    result = checked_result(RunResult(0, "ok\x1b[2J\x07\tdone\r\n"))

    assert result.output == "ok?[2J?\tdone?\n"


def test_a_run_that_timed_out_has_no_exit_code_and_did_not_pass():
    result = checked_result(RunResult(0, "", timed_out=True))

    assert result.exit_code is None
    assert not result.passed


@pytest.mark.parametrize("answer", [
    None,
    "1 passed",
    {"exit_code": 0, "output": "1 passed"},
    RunResult(0, b"bytes"),
    RunResult("0", "text"),
    RunResult(True, "text"),
])
def test_an_answer_that_is_not_a_result_is_refused(answer):
    with pytest.raises(SandboxError):
        checked_result(answer)


# --- through the broker ---


def test_running_code_is_denied_until_the_owner_allows_it(brokered):
    broker, policy, _, _, _, sandbox = brokered

    denied = broker.invoke(CODE_RUN, FILES, "pytest")

    assert denied.outcome == "denied"
    assert sandbox.requests == []

    allow(policy)
    allowed = broker.invoke(CODE_RUN, FILES, "pytest", "tests")

    assert allowed.success
    assert allowed.result.passed
    assert allowed.result.output == "1 passed\n"
    assert [r.target for r in sandbox.requests] == ["tests"]


def test_a_bad_request_never_reaches_the_sandbox(brokered):
    broker, policy, _, _, _, sandbox = brokered
    allow(policy)

    result = broker.invoke(CODE_RUN, {"../escape.py": "x = 1\n"}, "pytest")

    assert not result.success
    assert sandbox.requests == []


def test_a_sandbox_that_is_not_ready_is_unavailable(brokered):
    broker, policy, _, _, providers, sandbox = brokered
    allow(policy)
    sandbox.is_ready = False
    providers.check_health(SANDBOX_PROVIDER)

    result = broker.invoke(CODE_RUN, FILES, "pytest")

    assert result.outcome == "unavailable"
    assert sandbox.requests == []


def test_a_sandbox_that_stops_being_ready_between_checks_is_not_used(brokered):
    broker, policy, _, _, _, sandbox = brokered
    allow(policy)
    sandbox.is_ready = False

    result = broker.invoke(CODE_RUN, FILES, "pytest")

    assert not result.success
    assert sandbox.requests == []


def test_what_the_sandbox_said_went_wrong_stays_out_of_the_audit_log(brokered):
    broker, policy, store, _, _, sandbox = brokered
    allow(policy)
    sandbox.error = RuntimeError("could not start: SECRET-CANARY in adder.py")

    result = broker.invoke(CODE_RUN, FILES, "pytest")

    assert not result.success
    assert "SECRET-CANARY" not in result.error

    rows = list(store.execute("SELECT * FROM audit_event"))
    assert rows
    assert all("SECRET-CANARY" not in str(tuple(row)) for row in rows)


def test_the_files_and_output_are_not_written_to_the_audit_log(brokered):
    broker, policy, store, _, _, sandbox = brokered
    allow(policy)
    sandbox.result = RunResult(0, "OUTPUT-CANARY\n")

    broker.invoke(CODE_RUN, {"main.py": "print('CODE-CANARY')\n"}, "python", "main.py")

    rows = list(store.execute("SELECT * FROM audit_event"))
    text = " ".join(str(tuple(row)) for row in rows)
    assert "CODE-CANARY" not in text and "OUTPUT-CANARY" not in text


def test_running_code_is_not_offered_to_the_model(brokered):
    _, _, _, registry, _, _ = brokered
    capability = registry.get(CODE_RUN)

    assert not capability.metadata.get(MODEL_TOOL)


def test_freya_as_she_starts_has_no_way_to_run_code(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)

    try:
        assert plane.capabilities.get(CODE_RUN) is None
        assert plane.providers.get(SANDBOX_PROVIDER) is None
        assert not plane.policy.decide(LOCAL_OWNER.actor, CODE_RUN).allow
    finally:
        plane.close()
