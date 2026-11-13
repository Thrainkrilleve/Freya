import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from freya.core.capabilities import CapabilityDefinition
from freya.core.providers import ProviderDefinition


ANTHROPIC_PLAN_PROVIDER = "anthropic-plan"
ANTHROPIC_PLAN_VENDOR = "anthropic"
CLOUD_ANTHROPIC_PLAN_ASK = "cloud.anthropic_plan.ask"

# Names the program Freya runs, for a machine where it is not in the
# Claude desktop app's folder.
CLAUDE_PROGRAM_VARIABLE = "FREYA_CLAUDE"

DEFAULT_TIMEOUT_SECONDS = 180
SIGN_IN_TIMEOUT_SECONDS = 30
# The sign-in is asked before every brief: it is a separate program
# run and the owner could have signed in differently in between.
SIGN_IN_CHECK_SECONDS = 0
MAX_BRIEF_CHARACTERS = 200_000
MAX_REPLY_CHARACTERS = 200_000
# No more than this much of the program's output is ever read.
MAX_OUTPUT_BYTES = 4_000_000

# The sign-in this route accepts: the owner's own Claude subscription,
# served by Anthropic. An API key, a console account or another cloud
# would bill or route differently, so each is refused.
SUBSCRIPTION_SIGN_IN = "claude.ai"
FIRST_PARTY = "firstParty"
SUBSCRIPTIONS = frozenset({"pro", "max", "team", "enterprise"})

WORKER_INSTRUCTIONS = (
    "You are a worker model. Another AI system has given you one "
    "self-contained brief. Do exactly what the brief asks and reply "
    "with the result only. You have no tools and no files. If the "
    "brief cannot be done from what it contains, say what is missing."
)

# These ask the program for a worker with no tools, none of the
# owner's customisations, and no saved session. They are requests to
# the vendor's program, not a containment boundary: its sign-in and
# any administrator-managed settings still apply.
FIXED_ARGUMENTS = (
    "--print",
    "--output-format", "json",
    "--tools", "",
    "--safe-mode",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
    "--permission-prompts", "none",
    "--system-prompt", WORKER_INSTRUCTIONS,
)

# The only variables the program is given. Anything else in Freya's
# environment, such as a setting that points Claude at another server,
# a key that would bill elsewhere, or a telemetry address, is left out.
ENVIRONMENT_ALLOWLIST = frozenset({
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "HOME", "SYSTEMROOT",
    "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "OS", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "TZ",
})

GIT_BASH_VARIABLE = "CLAUDE_CODE_GIT_BASH_PATH"

# Set for the worker: no traffic beyond the request itself.
WORKER_SETTINGS = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
}


@dataclass(frozen=True)
class LimitNotice:
    """
    The vendor's program saying the plan's allowance is used up.
    `resets_at` is None when it gave no time Freya could read.
    """

    weekly: bool
    resets_at: Optional[datetime] = None


# The vendor does not document these words and has changed them
# between versions, so this reads the forms seen so far and nothing
# cleverer: "...limit reached|1760000000", "5-hour limit reached,
# resets 3pm", "Weekly limit reached, resets Oct 14, 9am". A notice
# it recognises but cannot find a time in is still a notice.
_LIMIT_WORDS = re.compile(
    r"(?is)\blimit\b.{0,40}\b(?:reached|hit|exceeded)\b"
    r"|\b(?:reached|hit|exceeded)\b.{0,40}\blimit\b"
)
_RESET_EPOCH = re.compile(r"\|\s*(\d{10})\b")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec")
_RESET_TIME = re.compile(
    r"(?i)\bresets?\s+(?:at\s+|on\s+)?"
    r"(?:(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:,|\s+at)?\s*)?"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b"
)
# A time zone the notice names after the time, as "(Europe/London)".
_RESET_ZONE = re.compile(r"^\s*\(\s*([A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)*)\s*\)")
# A reset further off than this is not a plan's, so it was misread.
_FURTHEST_RESET = timedelta(days=8)


def read_limit_notice(text, now: Optional[datetime] = None) -> Optional[LimitNotice]:
    """
    Whether the vendor's error text says a usage limit is reached,
    and when it says the limit resets. A time is read in the zone
    the notice names after it, or as this machine's local time when
    it names none. A zone it names that cannot be looked up makes the
    time unreadable: guessing would hold the provider to a wrong hour.
    """
    if not isinstance(text, str) or not _LIMIT_WORDS.search(text[:2000]):
        return None

    text = text[:2000]
    now = now or datetime.now(timezone.utc)
    weekly = "week" in text.lower()
    resets_at = None
    epoch = _RESET_EPOCH.search(text)
    stated = _RESET_TIME.search(text)

    try:
        if epoch:
            resets_at = datetime.fromtimestamp(int(epoch.group(1)), timezone.utc)
        elif stated:
            month, day, hour, minute, half = stated.groups()
            zone = _RESET_ZONE.search(text[stated.end():])

            if zone:
                # An unknown name raises, and the time is then unread.
                # A named zone knows its own summer time, so the wall
                # time can be built in it directly.
                local_now = now.astimezone(ZoneInfo(zone.group(1)))
            else:
                # This machine's zone. `astimezone()` with no argument
                # would attach today's fixed offset, which is an hour
                # out for a time on the other side of a clock change,
                # so the wall time is built naive and converted at the
                # end, which uses the system's own rules for that day.
                local_now = now.astimezone().replace(tzinfo=None)

            hour = int(hour) % 12 + (12 if half.lower() == "p" else 0)
            moment = local_now.replace(
                hour=hour, minute=int(minute or 0), second=0, microsecond=0,
            )

            if month:
                moment = moment.replace(
                    month=_MONTHS.index(month.lower()) + 1, day=int(day),
                )
                if moment <= local_now:
                    moment = moment.replace(year=moment.year + 1)
            elif moment <= local_now:
                moment += timedelta(days=1)

            resets_at = moment if zone else moment.astimezone()
    except (ValueError, OverflowError, OSError, KeyError):
        resets_at = None

    if resets_at is not None and not now < resets_at <= now + _FURTHEST_RESET:
        resets_at = None

    return LimitNotice(weekly, resets_at)


class PlanRouteError(RuntimeError):
    """
    The brief was not answered. The message is fixed wording that is
    safe to show and to record. `detail` holds what the vendor's
    program said, which may repeat part of the brief, so it must be
    shown only to the owner and never written to the audit log.
    `sent` is False only when it is certain nothing left this machine.
    `limit` is set when the vendor said the plan's allowance is used up.
    """

    def __init__(self, message, sent=True, detail="", limit=None):
        super().__init__(message)
        self.sent = sent
        self.detail = detail
        self.limit = limit


@dataclass(frozen=True)
class WorkerReply:
    """
    What a cloud worker sent back. The text is untrusted content: it
    is input for Freya's main model, never an instruction to Freya.
    """

    text: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False
    raw_usage: Dict[str, object] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def worker_environment(environ=None) -> Dict[str, str]:
    environ = os.environ if environ is None else environ
    kept = {
        name: value
        for name, value in environ.items()
        if name.upper() in ENVIRONMENT_ALLOWLIST
    }
    # Some Windows installs must be told where Git Bash is. The path
    # is passed on only if it names a real bash program.
    bash = environ.get(GIT_BASH_VARIABLE, "")
    if bash and Path(bash).is_file() and Path(bash).name.lower() == "bash.exe":
        kept[GIT_BASH_VARIABLE] = bash

    kept.update(WORKER_SETTINGS)
    return kept


def find_claude_program(environ=None) -> Optional[List[str]]:
    """
    The Claude Code program as Anthropic published it, or None.

    Freya runs the program itself rather than a launcher script, so
    nothing she passes is ever read by a command interpreter.
    """
    environ = os.environ if environ is None else environ
    named = environ.get(CLAUDE_PROGRAM_VARIABLE)

    if named:
        return [named] if Path(named).is_file() else None

    root = Path(environ.get("APPDATA", "")) / "Claude" / "claude-code"

    try:
        versions = sorted(
            (entry for entry in root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for version in versions:
        for program in version.rglob("claude.exe"):
            return [str(program)]

    return None


def _run(command, folder, stdin_bytes, timeout):
    """
    Run the program with its output going to files in `folder`, so a
    runaway reply fills neither Freya's memory nor her pipes. Returns
    (exit code, at most MAX_OUTPUT_BYTES of standard output).
    """
    out_path = Path(folder) / "stdout"
    err_path = Path(folder) / "stderr"
    work = Path(folder) / "work"
    work.mkdir()

    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            cwd=work,
            env=worker_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            process.communicate(stdin_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise

    with open(out_path, "rb") as out:
        return process.returncode, out.read(MAX_OUTPUT_BYTES)


_sign_in_checked: Dict[tuple, float] = {}


def check_plan_sign_in(program: Sequence[str], *, force=False) -> None:
    """
    Refuse unless the program reports that it is signed in to the
    owner's Claude subscription and served by Anthropic.

    Freya asks the program for its status and reads only how it is
    signed in. She never opens its sign-in files. The answer is
    trusted for a few minutes so that each brief does not ask again.
    """
    key = tuple(program)

    if not force and time.monotonic() - _sign_in_checked.get(key, -1e9) < (
        SIGN_IN_CHECK_SECONDS
    ):
        return

    try:
        with tempfile.TemporaryDirectory(
        prefix="freya-worker-", ignore_cleanup_errors=True,
    ) as folder:
            code, output = _run(
                [*program, "auth", "status", "--json"],
                folder, b"", SIGN_IN_TIMEOUT_SECONDS,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlanRouteError(
            "The Claude program could not report how it is signed in.",
            sent=False,
        ) from exc

    try:
        status = json.loads(output.decode("utf-8", errors="replace"))
    except ValueError:
        status = None

    if not isinstance(status, dict) or code != 0 or status.get("loggedIn") is not True:
        raise PlanRouteError(
            "The Claude program is not signed in. Sign in to it yourself, "
            "then try again.",
            sent=False,
        )

    if (
        status.get("authMethod") != SUBSCRIPTION_SIGN_IN
        or status.get("apiProvider") != FIRST_PARTY
        or status.get("subscriptionType") not in SUBSCRIPTIONS
    ):
        raise PlanRouteError(
            "The Claude program is not signed in with a Claude "
            "subscription, so this route will not use it.",
            sent=False,
        )

    _sign_in_checked[key] = time.monotonic()


def ask_claude_plan(
    brief: str,
    program: Sequence[str],
    *,
    model: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> WorkerReply:
    """
    Send one brief to Claude through the owner's own signed-in Claude
    Code program and return the reply.

    This function sends whatever it is given. Deciding that a brief
    may go (the provider switch, the material settings, screening,
    the owner's approval and the usage ledger) is the caller's job and
    must be done before calling it.

    The brief goes in on standard input, so no part of it becomes a
    command-line argument. The program runs in an empty folder made
    for this call, with only the allowed environment variables.
    """
    if not isinstance(brief, str) or not brief.strip():
        raise PlanRouteError("There is no brief to send.", sent=False)
    if len(brief) > MAX_BRIEF_CHARACTERS:
        raise PlanRouteError("The brief is too long to send.", sent=False)

    check_plan_sign_in(program)

    command = [*program, *FIXED_ARGUMENTS]
    if model:
        command += ["--model", model]

    with tempfile.TemporaryDirectory(
        prefix="freya-worker-", ignore_cleanup_errors=True,
    ) as folder:
        try:
            code, output = _run(
                command, folder, brief.encode("utf-8"), timeout,
            )
        except OSError as exc:
            raise PlanRouteError(
                "The Claude program could not be started.", sent=False,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PlanRouteError(
                "Claude did not answer in time. The brief may have been "
                "received.",
            ) from exc

    return _parse_reply(code, output)


def _parse_reply(code, output) -> WorkerReply:
    try:
        result = json.loads(output.decode("utf-8", errors="replace").strip())
    except ValueError:
        result = None

    if not isinstance(result, dict):
        raise PlanRouteError(
            "Claude's program gave an answer Freya could not read "
            f"(exit code {code})."
        )

    text = result.get("result")

    if result.get("is_error") or code != 0:
        limit = read_limit_notice(text)

        raise PlanRouteError(
            "Claude's plan has reached its usage limit."
            if limit is not None
            else "Claude did not answer the brief.",
            detail=text[:2000] if isinstance(text, str) else "",
            limit=limit,
        )

    if not isinstance(text, str) or not text.strip():
        raise PlanRouteError("Claude returned an empty answer.")

    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    return WorkerReply(
        text=text[:MAX_REPLY_CHARACTERS],
        model=_model_name(result),
        input_tokens=_count(usage, "input_tokens")
        + _count(usage, "cache_creation_input_tokens")
        + _count(usage, "cache_read_input_tokens"),
        output_tokens=_count(usage, "output_tokens"),
        truncated=len(text) > MAX_REPLY_CHARACTERS,
        raw_usage=usage,
    )


def _count(usage, name) -> int:
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _model_name(result) -> str:
    models = result.get("modelUsage")
    if isinstance(models, dict) and models:
        return ", ".join(sorted(str(name) for name in models))
    return ""


def create_claude_plan_provider(program=None) -> ProviderDefinition:
    """
    Describe Claude, reached through the owner's subscription, as a
    provider. Healthy means only that the program is present; how it
    is signed in is checked when a brief is sent.
    """

    def health():
        return bool(program or find_claude_program())

    return ProviderDefinition(
        provider_id=ANTHROPIC_PLAN_PROVIDER,
        name="Claude through the owner's subscription",
        capabilities=[CLOUD_ANTHROPIC_PLAN_ASK],
        start_handler=lambda: None,
        health_handler=health,
        metadata={
            "vendor": ANTHROPIC_PLAN_VENDOR,
            "leaves_this_machine": True,
        },
    )


def register_claude_plan_capability(
    registry, take_permit, program=None, model=None, on_failure=None,
):
    """
    Register the one capability this route offers. It is not offered
    to the model as a tool and is in no default policy seed: only a
    rule the owner adds allows it.

    Policy is one gate; the dispatcher is the other. `take_permit`
    is the dispatcher's: it yields the single permit that one
    authorized send of this exact text holds. A call that did not
    come through the dispatcher finds no permit and is refused even
    when policy allows the capability.
    """

    def ask(brief, timeout=DEFAULT_TIMEOUT_SECONDS):
        if not take_permit(brief):
            raise PlanRouteError(
                "This brief did not come through the sending checks.",
                sent=False,
            )

        resolved = program or find_claude_program()

        if not resolved:
            raise PlanRouteError(
                "The Claude program was not found on this machine.",
                sent=False,
            )

        try:
            return ask_claude_plan(
                brief, resolved, model=model, timeout=timeout,
            )
        except PlanRouteError as error:
            # The broker turns a failure into text, so the caller is
            # told here whether the brief may have left.
            if on_failure is not None:
                on_failure(error)
            raise

    registry.register(
        CapabilityDefinition(
            name=CLOUD_ANTHROPIC_PLAN_ASK,
            description=(
                "Sends one brief to Claude through the owner's "
                "subscription and returns the reply. The brief leaves "
                "this machine."
            ),
            provider=ANTHROPIC_PLAN_PROVIDER,
            handler=ask,
            category="cloud",
        )
    )
