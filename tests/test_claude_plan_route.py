import json
import sys

import pytest

from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import (
    LOCAL_OWNER,
    CapabilityBroker,
    CapabilityRegistry,
)
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.core.providers import ProviderRegistry
from freya.providers.model import claude_plan
from freya.providers.model.claude_plan import (
    ANTHROPIC_PLAN_PROVIDER,
    CLOUD_ANTHROPIC_PLAN_ASK,
    FIXED_ARGUMENTS,
    PlanRouteError,
    ask_claude_plan,
    create_claude_plan_provider,
    find_claude_program,
    register_claude_plan_capability,
)


# Stands in for the Claude program. It records what it was given and
# prints whatever the test asked for.
FAKE_PROGRAM = r"""
import json, os, sys, time
from pathlib import Path

script = Path(__file__)
behaviour = json.loads((script.parent / "behaviour.json").read_text("utf-8"))

if sys.argv[1:3] == ["auth", "status"]:
    (script.parent / "asked_sign_in").write_text("yes", encoding="utf-8")
    sys.stdout.write(json.dumps(behaviour.get("sign_in", {
        "loggedIn": True, "authMethod": "claude.ai",
        "apiProvider": "firstParty", "subscriptionType": "pro",
        "email": "someone@example.org",
    })))
    sys.exit(0)

brief = sys.stdin.buffer.read().decode("utf-8")
(script.parent / "seen.json").write_text(json.dumps({
    "arguments": sys.argv[1:],
    "brief": brief,
    "folder": os.getcwd(),
    "files": os.listdir("."),
    "environment": sorted(os.environ),
}), encoding="utf-8")

time.sleep(behaviour.get("sleep", 0))
sys.stdout.write(behaviour.get("output", ""))
sys.exit(behaviour.get("exit", 0))
"""


def reply(text="A worker's answer.", **extra):
    return json.dumps({
        "type": "result",
        "is_error": False,
        "result": text,
        "usage": {
            "input_tokens": 12,
            "cache_read_input_tokens": 30,
            "output_tokens": 7,
        },
        "modelUsage": {"claude-test-model": {}},
        **extra,
    })


@pytest.fixture
def fake(tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_PROGRAM, encoding="utf-8")

    class Fake:
        program = [sys.executable, str(script)]

        @staticmethod
        def behave(**behaviour):
            (tmp_path / "behaviour.json").write_text(
                json.dumps(behaviour), encoding="utf-8",
            )

        @staticmethod
        def seen():
            return json.loads((tmp_path / "seen.json").read_text("utf-8"))

        @staticmethod
        def ran():
            return (tmp_path / "seen.json").exists()

    Fake.behave(output=reply())
    # Each test starts without a remembered sign-in check.
    claude_plan._sign_in_checked.clear()
    return Fake


def test_a_brief_is_answered_and_usage_is_read(fake):
    result = ask_claude_plan("Add two and two.", fake.program)

    assert result.text == "A worker's answer."
    assert result.model == "claude-test-model"
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.tokens == 49
    assert not result.truncated


def test_the_brief_goes_in_on_standard_input_never_as_an_argument(fake):
    brief = 'Summarise this: "quoted" & piped | text ; --tools default'

    ask_claude_plan(brief, fake.program)

    seen = fake.seen()
    assert seen["brief"] == brief
    assert seen["arguments"] == list(FIXED_ARGUMENTS)
    assert all(brief not in argument for argument in seen["arguments"])


def test_the_worker_gets_no_tools_settings_or_session(fake):
    ask_claude_plan("A brief.", fake.program, model="some-model")

    arguments = fake.seen()["arguments"]

    assert arguments[arguments.index("--tools") + 1] == ""
    for flag in (
        "--print",
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ):
        assert flag in arguments
    assert arguments[arguments.index("--permission-prompts") + 1] == "none"
    assert arguments[-2:] == ["--model", "some-model"]
    for forbidden in (
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--add-dir",
        "--continue",
        "--resume",
    ):
        assert forbidden not in arguments


def test_the_worker_runs_in_an_empty_folder_that_is_then_removed(fake, tmp_path):
    ask_claude_plan("A brief.", fake.program)

    seen = fake.seen()

    assert seen["files"] == []
    assert seen["folder"] != str(tmp_path)
    assert "freya-worker-" in seen["folder"]

    import os
    assert not os.path.exists(seen["folder"])
    assert not os.path.exists(os.path.dirname(seen["folder"]))


def test_only_allowed_variables_reach_the_worker(fake, monkeypatch):
    # Each of these could bill elsewhere, point Claude at another
    # server, or send the brief to a collector.
    for name in (
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CONFIG_DIR", "HTTPS_PROXY",
        "OTEL_EXPORTER_OTLP_ENDPOINT", "FREYA_HARMLESS_SETTING",
    ):
        monkeypatch.setenv(name, "set-by-the-test")

    ask_claude_plan("A brief.", fake.program)

    environment = {name.upper() for name in fake.seen()["environment"]}
    # The fake program is Python, and on Linux the interpreter adds
    # LC_CTYPE to its own environment at start; Freya did not pass it.
    environment.discard("LC_CTYPE")
    assert environment <= claude_plan.ENVIRONMENT_ALLOWLIST | set(
        claude_plan.WORKER_SETTINGS
    ) | {claude_plan.GIT_BASH_VARIABLE}
    # The program finds its sign-in through the user's folder, which
    # Windows names USERPROFILE; elsewhere there is none to pass on.
    if sys.platform == "win32":
        assert "USERPROFILE" in environment
    assert set(claude_plan.WORKER_SETTINGS) <= environment


@pytest.mark.parametrize(
    "sign_in",
    [
        {"loggedIn": False},
        {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty", "subscriptionType": "pro"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock", "subscriptionType": "pro"},
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": None},
        {"loggedIn": "yes", "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"},
        [],
    ],
)
def test_nothing_is_sent_unless_signed_in_with_a_subscription(fake, sign_in):
    fake.behave(output=reply(), sign_in=sign_in)

    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan("A brief.", fake.program)

    assert error.value.sent is False
    assert not fake.ran()


def test_the_sign_in_is_asked_before_every_brief(fake, tmp_path):
    ask_claude_plan("A brief.", fake.program)
    (tmp_path / "asked_sign_in").unlink()

    ask_claude_plan("Another brief.", fake.program)

    assert (tmp_path / "asked_sign_in").exists()


def test_who_and_where_are_not_passed_to_the_worker(fake, monkeypatch, tmp_path):
    for name in ("USERNAME", "USERDOMAIN", "COMPUTERNAME"):
        monkeypatch.setenv(name, "set-by-the-test")
    bash = tmp_path / "bash.exe"
    bash.write_bytes(b"")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(bash))

    ask_claude_plan("A brief.", fake.program)
    environment = {name.upper() for name in fake.seen()["environment"]}

    assert not {"USERNAME", "USERDOMAIN", "COMPUTERNAME"} & environment
    assert "CLAUDE_CODE_GIT_BASH_PATH" in environment

    # A path that is not a real bash program is not passed on.
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", str(tmp_path / "other.exe"))
    assert "CLAUDE_CODE_GIT_BASH_PATH" not in claude_plan.worker_environment()


def test_nothing_about_the_sign_in_is_kept_but_that_it_passed(fake):
    ask_claude_plan("A brief.", fake.program)

    assert "someone@example.org" not in repr(claude_plan._sign_in_checked)


@pytest.mark.parametrize("brief", ["", "   \n", None, 5])
def test_an_empty_brief_is_refused_before_anything_runs(fake, brief):
    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan(brief, fake.program)

    assert error.value.sent is False
    assert not fake.ran()


def test_an_oversized_brief_is_refused_before_anything_runs(fake):
    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan("x" * (claude_plan.MAX_BRIEF_CHARACTERS + 1), fake.program)

    assert error.value.sent is False
    assert not fake.ran()


def test_a_missing_program_is_reported_as_not_sent(tmp_path):
    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan("A brief.", [str(tmp_path / "no-such-program.exe")])

    assert error.value.sent is False


def test_an_error_from_claude_is_reported_and_counts_as_sent(fake):
    fake.behave(
        output=json.dumps({
            "is_error": True,
            "result": "Usage limit reached. Try again later.",
        }),
        exit=1,
    )

    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan("A brief.", fake.program)

    # What the vendor said may repeat the brief, so it is kept apart
    # from the message that gets shown and recorded.
    assert "Usage limit reached" not in str(error.value)
    assert "Usage limit reached" in error.value.detail
    assert error.value.sent is True


def test_only_a_bounded_amount_of_output_is_read(fake, monkeypatch):
    monkeypatch.setattr(claude_plan, "MAX_OUTPUT_BYTES", 200)
    fake.behave(output=reply("x" * 5000))

    with pytest.raises(PlanRouteError):
        ask_claude_plan("A brief.", fake.program)


@pytest.mark.parametrize(
    "output",
    ["", "not json", "[1, 2]", json.dumps({"is_error": False, "result": ""}),
     json.dumps({"is_error": False, "result": {"nested": "thing"}})],
)
def test_unreadable_or_empty_answers_are_errors(fake, output):
    fake.behave(output=output)

    with pytest.raises(PlanRouteError):
        ask_claude_plan("A brief.", fake.program)


def test_a_failed_exit_is_an_error_even_with_a_tidy_answer(fake):
    fake.behave(output=reply(), exit=3)

    with pytest.raises(PlanRouteError):
        ask_claude_plan("A brief.", fake.program)


def test_a_limit_notice_is_recognised_and_its_words_stay_out_of_the_message(fake):
    fake.behave(
        output=json.dumps({
            "is_error": True,
            "result": "Weekly limit reached - resets Oct 14, 9am",
        }),
        exit=1,
    )

    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan("A brief.", fake.program)

    assert str(error.value) == "Claude's plan has reached its usage limit."
    assert error.value.limit is not None and error.value.limit.weekly
    assert error.value.sent is True


def test_another_error_is_not_a_limit_notice(fake):
    fake.behave(
        output=json.dumps({"is_error": True, "result": "Something went wrong."}),
        exit=1,
    )

    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan("A brief.", fake.program)

    assert error.value.limit is None


# The vendor's wording is not documented. These are forms its program
# has used; none was captured from a real refusal on this machine.
@pytest.mark.parametrize("said, weekly, expected", [
    ("Claude AI usage limit reached|1791720000", False, "epoch"),
    ("5-hour limit reached - resets 3pm", False, (10, 15, 0)),
    ("You've hit your session limit, resets 3:45pm", False, (10, 15, 45)),
    ("You have reached your limit. Resets at 9am.", False, (11, 9, 0)),
    ("Weekly limit reached - resets Oct 14, 9am", True, (14, 9, 0)),
    ("Weekly limit reached - resets Oct 14 at 9:30 a.m.", True, (14, 9, 30)),
    # Recognised, but with no time that can be read.
    ("Usage limit reached. Try again later.", False, None),
    ("Weekly limit reached - resets soon", True, None),
    # A zone that cannot be looked up: the time is not guessed at.
    ("5-hour limit reached - resets 3pm (Mars/Olympus)", False, None),
    # A time too far off to be a plan's reset was misread.
    ("Limit reached - resets Mar 3, 9am", False, None),
])
def test_reset_times_are_read_from_the_forms_seen_so_far(said, weekly, expected):
    from datetime import datetime, timezone

    from freya.providers.model.claude_plan import read_limit_notice

    # Noon, local time, on 10 October 2026.
    now = datetime(2026, 10, 10, 12, 0).astimezone()
    notice = read_limit_notice(said, now)

    assert notice is not None and notice.weekly is weekly

    if expected is None:
        assert notice.resets_at is None
    elif expected == "epoch":
        assert notice.resets_at == datetime.fromtimestamp(1791720000, timezone.utc)
    else:
        local = notice.resets_at.astimezone()
        assert (local.day, local.hour, local.minute) == expected
        assert local > now


def test_a_reset_time_is_read_in_the_zone_the_notice_names():
    from datetime import datetime, timezone

    from freya.providers.model.claude_plan import read_limit_notice

    now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    tokyo = read_limit_notice("5-hour limit reached - resets 11pm (Asia/Tokyo)", now)
    utc = read_limit_notice("Weekly limit reached - resets Oct 14, 9am (UTC)", now)

    # 11pm in Tokyo is 14:00 UTC the same day.
    assert tokyo.resets_at == datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)
    assert utc.resets_at == datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)


def test_a_local_reset_time_across_a_clock_change_keeps_the_wall_clock_hour(monkeypatch):
    import time
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from freya.providers.model.claude_plan import read_limit_notice

    if not hasattr(time, "tzset"):
        pytest.skip("This platform cannot change the process's zone.")

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        # Half past midnight on the night the clocks go back: now is
        # summer time, 5am that day is standard time, an hour later
        # in UTC than a fixed offset would say.
        now = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo("America/New_York"))
        notice = read_limit_notice("5-hour limit reached - resets 5am", now)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    assert notice.resets_at == datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("said", [
    "Something went wrong.",
    "The reply was limited to 200 words.",
    "",
    None,
    42,
])
def test_other_words_are_not_a_limit_notice(said):
    from freya.providers.model.claude_plan import read_limit_notice

    assert read_limit_notice(said) is None


def test_a_slow_worker_is_stopped(fake):
    fake.behave(output=reply(), sleep=20)

    with pytest.raises(PlanRouteError) as error:
        ask_claude_plan("A brief.", fake.program, timeout=1)

    assert "in time" in str(error.value)
    assert error.value.sent is True


def test_a_long_reply_is_cut_and_marked(fake, monkeypatch):
    monkeypatch.setattr(claude_plan, "MAX_REPLY_CHARACTERS", 10)
    fake.behave(output=reply("0123456789ABCDEF"))

    result = ask_claude_plan("A brief.", fake.program)

    assert result.text == "0123456789"
    assert result.truncated


def test_odd_usage_figures_count_as_zero(fake):
    fake.behave(output=json.dumps({
        "is_error": False,
        "result": "ok",
        "usage": {"input_tokens": "many", "output_tokens": -4},
    }))

    result = ask_claude_plan("A brief.", fake.program)

    assert result.tokens == 0


def test_the_program_is_found_newest_first_or_by_name(tmp_path):
    root = tmp_path / "Claude" / "claude-code"
    older = root / "1.0" / "bin"
    newer = root / "2.0" / "bin"
    for folder in (older, newer):
        folder.mkdir(parents=True)
        (folder / "claude.exe").write_bytes(b"")

    import os
    os.utime(root / "1.0", (1_000_000, 1_000_000))
    os.utime(root / "2.0", (2_000_000, 2_000_000))

    found = find_claude_program({"APPDATA": str(tmp_path)})
    named = find_claude_program({
        "APPDATA": str(tmp_path),
        "FREYA_CLAUDE": str(older / "claude.exe"),
    })

    assert found == [str(newer / "claude.exe")]
    assert named == [str(older / "claude.exe")]
    assert find_claude_program({"APPDATA": str(tmp_path / "elsewhere")}) is None
    assert find_claude_program({"FREYA_CLAUDE": str(tmp_path / "gone.exe")}) is None


@pytest.fixture
def brokered(tmp_path, fake):
    store = ControlStore(tmp_path / "control.db")
    providers = ProviderRegistry()
    providers.register(create_claude_plan_provider(fake.program))
    providers.start(ANTHROPIC_PLAN_PROVIDER)
    providers.check_health(ANTHROPIC_PLAN_PROVIDER)
    registry = CapabilityRegistry(providers)
    going = set()

    def take(text):
        if text in going:
            going.discard(text)
            return True
        return False

    register_claude_plan_capability(registry, take, fake.program)
    policy = SqlitePolicyEngine(store)
    audit = SqliteAuditLog(store)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit, LOCAL_OWNER)

    yield broker, policy, store, registry, going

    store.close()


def test_the_route_is_denied_until_the_owner_allows_it(brokered, fake):
    broker, policy, store, _, going = brokered
    going.add("A brief.")

    denied = broker.invoke(CLOUD_ANTHROPIC_PLAN_ASK, "A brief.")

    assert not denied.success
    assert denied.outcome == "denied"
    assert not fake.ran()

    policy.add_rule("local-owner", CLOUD_ANTHROPIC_PLAN_ASK, "allow", "test")
    allowed = broker.invoke(CLOUD_ANTHROPIC_PLAN_ASK, "A brief.")

    assert allowed.success
    assert allowed.result.text == "A worker's answer."

    going.add("A brief.")
    fake.behave(
        output=json.dumps({"is_error": True, "result": "echo: A brief."}),
        exit=1,
    )
    failed = broker.invoke(CLOUD_ANTHROPIC_PLAN_ASK, "A brief.")
    assert not failed.success
    assert allowed.provider == ANTHROPIC_PLAN_PROVIDER

    rows = [
        dict(row)
        for row in store.execute("SELECT * FROM audit_event ORDER BY event_id")
    ]
    assert [row["kind"] for row in rows][-2:] == ["dispatch", "outcome"]
    # Briefs are never written to the audit log.
    assert "A brief." not in json.dumps(rows)


def test_policy_alone_does_not_let_a_brief_through(brokered, fake):
    broker, policy, _, _, going = brokered
    policy.add_rule("local-owner", CLOUD_ANTHROPIC_PLAN_ASK, "allow", "test")

    direct = broker.invoke(CLOUD_ANTHROPIC_PLAN_ASK, "A brief nobody checked.")

    assert not direct.success
    assert not fake.ran()

    going.add("A brief nobody checked.")
    assert broker.invoke(CLOUD_ANTHROPIC_PLAN_ASK, "A brief nobody checked.").success


def test_the_route_is_not_offered_to_the_model_as_a_tool(brokered):
    _, _, _, registry, _ = brokered

    capability = registry.get(CLOUD_ANTHROPIC_PLAN_ASK)

    assert not capability.metadata.get("model_tool")
    assert capability.category == "cloud"
