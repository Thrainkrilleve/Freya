import re
from dataclasses import dataclass
from typing import List, Mapping, Optional, Protocol


# What one run may be given and may give back. These are checked by
# Freya before anything reaches a sandbox, whichever sandbox it is.
MAX_FILES = 200
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 8_000_000
MAX_PATH_CHARACTERS = 200
MAX_OUTPUT_CHARACTERS = 100_000

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 600

# A run names one of these, never a command line. The code inside the
# sandbox can do whatever it likes there, so this protects nothing in
# the sandbox: it keeps text a model wrote out of the command Freya
# builds on this machine.
COMMAND_PYTHON = "python"
COMMAND_PYTEST = "pytest"
COMMANDS = (COMMAND_PYTHON, COMMAND_PYTEST)

# Each part of a path starts with a letter, digit or underscore, so
# ".", "..", a hidden file and a name a program would read as an
# option are all refused.
_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\Z")

# Everything a terminal could act on except a new line and a tab.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class SandboxError(RuntimeError):
    """
    The code was not run, or the run could not be read. The message
    is fixed wording that is safe to show and to record. `detail`
    holds what the sandbox said, which may repeat part of the code, so
    it is shown only to the owner and never written to the audit log.
    """

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class SandboxProfile:
    """
    What a sandbox says about itself. These are its own statements:
    `profile_problems` checks that they are the right ones, and a
    sandbox's own tests are what show they are true.
    """

    name: str
    # Can the code reach any network, this machine's loopback included?
    network: bool
    # Can it see any file of this machine beyond what the run is given?
    host_files: bool
    # Is it given any sign-in, key or environment variable of Freya's?
    credentials: bool
    # Is everything a run wrote gone before the next run starts?
    fresh_each_run: bool
    # What holds the code in: "container" or "virtual-machine".
    boundary: str


def profile_problems(profile) -> List[str]:
    """
    Why this sandbox may not be used, as fixed sentences. An empty
    list means it states everything Freya requires.
    """
    if not isinstance(profile, SandboxProfile):
        return ["The sandbox does not describe itself."]

    problems = []

    if profile.network is not False:
        problems.append("The sandbox can reach a network.")
    if profile.host_files is not False:
        problems.append("The sandbox can see this machine's files.")
    if profile.credentials is not False:
        problems.append("The sandbox is given credentials.")
    if profile.fresh_each_run is not True:
        problems.append("The sandbox keeps files from one run to the next.")
    if profile.boundary not in ("container", "virtual-machine"):
        problems.append("The sandbox does not say what holds the code in.")

    return problems


@dataclass(frozen=True)
class RunRequest:
    """
    One run, already checked by `build_request`. `files` maps a
    relative path, with forward slashes, to that file's text.
    """

    files: Mapping[str, str]
    command: str
    target: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class RunResult:
    """
    What a run gave back. `output` is what the code printed, standard
    output and errors together. It is untrusted content: input for a
    model or the owner to read, never an instruction to Freya.
    `exit_code` is None when the run was stopped for taking too long.
    """

    exit_code: Optional[int]
    output: str
    timed_out: bool = False
    truncated: bool = False
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    """
    A place where code a model wrote may run. An implementation must
    give the code only the request's files, stop it at the request's
    timeout, and leave nothing behind for the next run.
    """

    def profile(self) -> SandboxProfile: ...

    def ready(self) -> bool: ...

    def run(self, request: RunRequest) -> RunResult: ...


def _path_problem(path) -> bool:
    return (
        not isinstance(path, str)
        or not path
        or len(path) > MAX_PATH_CHARACTERS
        or any(not _SEGMENT.match(part) for part in path.split("/"))
    )


def build_request(
    files, command, target="", timeout=DEFAULT_TIMEOUT_SECONDS,
) -> RunRequest:
    """
    Check what a caller asked to run and return the request, or raise
    SandboxError saying what was wrong with it.
    """
    if not isinstance(files, Mapping) or not files:
        raise SandboxError("There are no files to run.")
    if len(files) > MAX_FILES:
        raise SandboxError("There are too many files to run.")

    total = 0

    for path, text in files.items():
        if _path_problem(path):
            raise SandboxError("A file has a name the sandbox does not accept.")
        if not isinstance(text, str) or "\x00" in text:
            raise SandboxError("A file is not text.")

        size = len(text.encode("utf-8", errors="replace"))
        total += size

        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise SandboxError("The files are too large to run.")

    # "a" and "a/b" cannot both exist: one name would be a file and a folder.
    folders = {
        path.rsplit("/", index)[0]
        for path in files
        for index in range(1, path.count("/") + 1)
    }

    if folders & set(files):
        raise SandboxError("A file has a name the sandbox does not accept.")

    if command not in COMMANDS:
        raise SandboxError("That is not a command the sandbox runs.")
    if not isinstance(target, str):
        raise SandboxError("That is not something in the files to run.")

    if command == COMMAND_PYTHON:
        if target not in files or not target.endswith(".py"):
            raise SandboxError("That is not something in the files to run.")
    elif target and target not in files and target not in folders:
        raise SandboxError("That is not something in the files to run.")

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 1 <= timeout <= MAX_TIMEOUT_SECONDS
    ):
        raise SandboxError("That is not a time limit the sandbox accepts.")

    return RunRequest(dict(files), command, target, float(timeout))


def checked_result(result) -> RunResult:
    """
    Return what a sandbox gave back in the form callers rely on:
    bounded, with nothing in the output a terminal would act on.
    Raises SandboxError when it is not a result at all.
    """
    if (
        not isinstance(result, RunResult)
        or not isinstance(result.output, str)
        or isinstance(result.exit_code, bool)
        or not isinstance(result.exit_code, (int, type(None)))
    ):
        raise SandboxError("The sandbox gave an answer Freya could not read.")

    timed_out = bool(result.timed_out)
    output = _CONTROL.sub("?", result.output[:MAX_OUTPUT_CHARACTERS])

    return RunResult(
        exit_code=None if timed_out else result.exit_code,
        output=output,
        timed_out=timed_out,
        truncated=bool(result.truncated)
        or len(result.output) > MAX_OUTPUT_CHARACTERS,
        seconds=(
            float(result.seconds)
            if isinstance(result.seconds, (int, float))
            and not isinstance(result.seconds, bool)
            else 0.0
        ),
    )
