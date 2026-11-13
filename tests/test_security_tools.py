import types

import pytest

from freya.core.audit import SqliteAuditLog
from freya.core.audit.log import AuditVerification
from freya.core.capabilities import (
    LOCAL_OWNER,
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
)
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine, seed_alpha_policy
from freya.tools import security_tools as sec
from freya.tools.security_tools import (
    ALL_CHECKS,
    FAIL,
    PASS,
    SECURITY_PROVIDER,
    SELF_ASSESSMENT,
    SELF_ASSESSMENT_POLICY_SEED,
    SelfAssessmentContext,
    WARN,
    create_security_provider,
    run_self_assessment,
    security_self_assessment_capability,
)


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    yield store
    store.close()


@pytest.fixture
def policy(store):
    return SqlitePolicyEngine(store)


@pytest.fixture
def audit(store):
    log = SqliteAuditLog(store, checkpoint_interval=0)
    log.start()
    return log


def context(**overrides):
    base = dict(
        audit=None,
        policy=None,
        capabilities=None,
        owner_actor=LOCAL_OWNER.actor,
        package_root=None,
        repo_root=None,
    )
    base.update(overrides)
    return SelfAssessmentContext(**base)


# --- audit chain -------------------------------------------------------


def test_audit_chain_healthy_is_not_a_failure(audit):
    finding = sec.check_audit_chain(context(audit=audit))
    assert finding["status"] in (PASS, WARN)
    assert finding["status"] != FAIL


def test_audit_chain_reports_tampering_as_fail():
    fake = types.SimpleNamespace(
        verify=lambda: AuditVerification(ok=False, events_checked=3, problems=["event 3 altered"])
    )
    finding = sec.check_audit_chain(context(audit=fake))
    assert finding["status"] == FAIL
    assert finding["evidence"] == ["event 3 altered"]


def test_audit_chain_warnings_are_a_warning():
    fake = types.SimpleNamespace(
        verify=lambda: AuditVerification(ok=True, events_checked=3, warnings=["no off-host sink"])
    )
    finding = sec.check_audit_chain(context(audit=fake))
    assert finding["status"] == WARN


# --- policy ------------------------------------------------------------


def test_policy_deny_by_default_passes_on_empty_policy(policy):
    finding = sec.check_policy_deny_by_default(context(policy=policy))
    assert finding["status"] == PASS


def test_policy_deny_by_default_fails_on_wildcard_allow(policy):
    policy.add_rule("*", "*", "allow", "far too broad")
    finding = sec.check_policy_deny_by_default(context(policy=policy))
    assert finding["status"] == FAIL


def _registry_with(name, **extra):
    registry = CapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            name=name,
            description="Test capability.",
            provider="test-provider",
            handler=lambda: None,
            **extra,
        )
    )
    return registry


def test_policy_grants_pass_on_exact_non_destructive(policy):
    registry = _registry_with("system.info")
    policy.add_rule(LOCAL_OWNER.actor, "system.info", "allow", "ok")
    finding = sec.check_policy_grants(
        context(policy=policy, capabilities=registry)
    )
    assert finding["status"] == PASS


def test_policy_grants_fails_on_destructive_grant(policy):
    registry = _registry_with("files.delete", destructive=True)
    policy.add_rule(LOCAL_OWNER.actor, "files.delete", "allow", "oops")
    finding = sec.check_policy_grants(
        context(policy=policy, capabilities=registry)
    )
    assert finding["status"] == FAIL
    assert "files.delete" in finding["evidence"]


def test_policy_grants_warns_on_wildcard(policy):
    registry = CapabilityRegistry()
    policy.add_rule(LOCAL_OWNER.actor, "*", "allow", "broad")
    finding = sec.check_policy_grants(
        context(policy=policy, capabilities=registry)
    )
    assert finding["status"] == WARN


# --- gitignore ---------------------------------------------------------


def test_gitignore_pass(tmp_path):
    (tmp_path / ".gitignore").write_text(
        "data/\n*.db\n*.key\n*.pem\n*.safetensors\n*.gguf\n", encoding="utf-8"
    )
    finding = sec.check_gitignore(context(repo_root=tmp_path))
    assert finding["status"] == PASS


def test_gitignore_fails_when_data_not_ignored(tmp_path):
    (tmp_path / ".gitignore").write_text("*.db\n", encoding="utf-8")
    finding = sec.check_gitignore(context(repo_root=tmp_path))
    assert finding["status"] == FAIL
    assert "data/" in finding["evidence"]


def test_gitignore_warns_without_working_tree():
    finding = sec.check_gitignore(context(repo_root=None))
    assert finding["status"] == WARN


# --- tracked sensitive files ------------------------------------------


def _fake_ls_files(monkeypatch, stdout, returncode=0):
    monkeypatch.setattr(
        sec.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=""
        ),
    )


def test_tracked_sensitive_fails_on_tracked_db(monkeypatch, tmp_path):
    _fake_ls_files(monkeypatch, "src/app.py\ndata/freya_memory.db\nREADME.md\n")
    finding = sec.check_no_tracked_sensitive_files(context(repo_root=tmp_path))
    assert finding["status"] == FAIL
    assert "data/freya_memory.db" in finding["evidence"]


def test_tracked_sensitive_pass_when_clean(monkeypatch, tmp_path):
    _fake_ls_files(monkeypatch, "src/app.py\nREADME.md\n.gitignore\n")
    finding = sec.check_no_tracked_sensitive_files(context(repo_root=tmp_path))
    assert finding["status"] == PASS


def test_tracked_sensitive_warns_without_working_tree():
    finding = sec.check_no_tracked_sensitive_files(context(repo_root=None))
    assert finding["status"] == WARN


# --- gateway -----------------------------------------------------------


def test_gateway_binds_loopback():
    finding = sec.check_gateway_loopback(context())
    assert finding["status"] == PASS


def test_gateway_csp_has_no_unsafe_directives():
    finding = sec.check_gateway_csp(context())
    assert finding["status"] == PASS


# --- avatar isolation --------------------------------------------------


def _package_tree(tmp_path, core_files, avatar_files):
    core = tmp_path / "core"
    avatar = tmp_path / "spatial_avatar"
    core.mkdir()
    avatar.mkdir()
    for name, text in core_files.items():
        (core / name).write_text(text, encoding="utf-8")
    for name, text in avatar_files.items():
        (avatar / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_avatar_isolation_pass(tmp_path):
    root = _package_tree(
        tmp_path,
        core_files={"assistant.py": "import os\n# the avatar is elsewhere\n"},
        avatar_files={
            "model.py": "import math\n# dismiss the window on close\n",
        },
    )
    finding = sec.check_avatar_isolation(context(package_root=root))
    assert finding["status"] == PASS


def test_avatar_isolation_fails_on_cross_import(tmp_path):
    root = _package_tree(
        tmp_path,
        core_files={"assistant.py": "import os\n"},
        avatar_files={"model.py": "from freya.core.llm import something\n"},
    )
    finding = sec.check_avatar_isolation(context(package_root=root))
    assert finding["status"] == FAIL


def test_avatar_isolation_fails_on_screen_api(tmp_path):
    root = _package_tree(
        tmp_path,
        core_files={"assistant.py": "import os\n"},
        avatar_files={"grab.py": "import pyautogui\n"},
    )
    finding = sec.check_avatar_isolation(context(package_root=root))
    assert finding["status"] == FAIL


# --- whole report ------------------------------------------------------


def test_run_self_assessment_shape(audit, policy):
    ctx = context(
        audit=audit,
        policy=policy,
        capabilities=CapabilityRegistry(),
    )
    report = run_self_assessment(ctx)
    assert set(report) == {"generated_at", "overall", "summary", "findings"}
    assert report["overall"] in (PASS, WARN, FAIL)
    assert len(report["findings"]) == len(ALL_CHECKS)
    assert report["summary"]["total"] == len(ALL_CHECKS)
    for finding in report["findings"]:
        assert finding["status"] in (PASS, WARN, FAIL)


def test_a_broken_check_becomes_a_warning_not_a_crash(policy):
    # audit is None, so the audit check raises inside run and is caught.
    ctx = context(policy=policy, capabilities=CapabilityRegistry())
    report = run_self_assessment(ctx)
    assert len(report["findings"]) == len(ALL_CHECKS)
    assert report["summary"]["warn"] >= 1


# --- capability definition and broker wiring --------------------------


def test_capability_is_not_a_model_tool_and_not_destructive():
    capability = security_self_assessment_capability(lambda: {})
    assert capability.name == SELF_ASSESSMENT
    assert capability.provider == SECURITY_PROVIDER
    assert capability.destructive is False
    assert "model_tool" not in capability.metadata


def test_provider_is_always_healthy():
    provider = create_security_provider()
    assert provider.provider_id == SECURITY_PROVIDER
    assert provider.health_handler() is True


def _wire_broker(store, policy, audit, seed):
    registry = CapabilityRegistry()
    ctx = context(
        audit=audit, policy=policy, capabilities=CapabilityRegistry()
    )
    registry.register(
        security_self_assessment_capability(lambda: run_self_assessment(ctx))
    )
    if seed:
        seed_alpha_policy(
            policy, SELF_ASSESSMENT_POLICY_SEED, LOCAL_OWNER.actor, [SELF_ASSESSMENT]
        )
    return CapabilityBroker(registry, policy, audit)


def test_broker_runs_self_assessment_when_seeded(store, policy, audit):
    broker = _wire_broker(store, policy, audit, seed=True)
    result = broker.invoke(SELF_ASSESSMENT)
    assert result.success
    assert result.result["overall"] in (PASS, WARN, FAIL)


def test_broker_denies_self_assessment_without_seed(store, policy, audit):
    broker = _wire_broker(store, policy, audit, seed=False)
    result = broker.invoke(SELF_ASSESSMENT)
    assert not result.success
    assert result.outcome == "denied"


# --- release-line checks ----------------------------------------------


def _capability(name, *, destructive=False, model_tool=False):
    return CapabilityDefinition(
        name=name,
        description="test",
        provider="test-provider",
        handler=lambda: None,
        destructive=destructive,
        metadata={"model_tool": True} if model_tool else {},
    )


def test_unwritten_audit_without_a_broker_is_a_warning():
    assert sec.check_unwritten_audit(context())["status"] == WARN


def test_unwritten_audit_clean_broker_passes():
    broker = types.SimpleNamespace(pending_audit_records=0, lost_audit_records=0)
    assert sec.check_unwritten_audit(context(broker=broker))["status"] == PASS


def test_waiting_audit_records_are_a_warning():
    broker = types.SimpleNamespace(pending_audit_records=2, lost_audit_records=0)
    assert sec.check_unwritten_audit(context(broker=broker))["status"] == WARN


def test_lost_audit_records_are_a_failure():
    broker = types.SimpleNamespace(pending_audit_records=256, lost_audit_records=1)
    finding = sec.check_unwritten_audit(context(broker=broker))
    assert finding["status"] == FAIL


def test_real_broker_reports_no_unwritten_records(store, policy, audit):
    broker = CapabilityBroker(CapabilityRegistry(), policy, audit)
    assert sec.check_unwritten_audit(context(broker=broker))["status"] == PASS


def test_safe_model_tools_pass():
    registry = CapabilityRegistry()
    registry.register(_capability("system.cpu", model_tool=True))
    registry.register(_capability(SELF_ASSESSMENT))
    finding = sec.check_model_tool_exposure(context(capabilities=registry))
    assert finding["status"] == PASS


def test_destructive_model_tool_fails():
    registry = CapabilityRegistry()
    registry.register(_capability("files.delete", destructive=True, model_tool=True))
    finding = sec.check_model_tool_exposure(context(capabilities=registry))
    assert finding["status"] == FAIL


@pytest.mark.parametrize("name", sec.OWNER_ONLY_CAPABILITIES)
def test_owner_only_capability_offered_to_the_model_fails(name):
    registry = CapabilityRegistry()
    registry.register(_capability(name, model_tool=True))
    finding = sec.check_model_tool_exposure(context(capabilities=registry))
    assert finding["status"] == FAIL


def test_owner_locks_closed_by_default(policy):
    finding = sec.check_owner_locks(context(policy=policy))
    assert finding["status"] == PASS
    assert "closed" in finding["detail"]


def test_cloud_route_opened_by_the_owners_switch_passes(policy):
    policy.add_rule(
        LOCAL_OWNER.actor, sec.CLOUD_ROUTE_CAPABILITY, "allow",
        sec.CLOUD_SWITCH_REASON,
    )
    finding = sec.check_owner_locks(context(policy=policy))
    assert finding["status"] == PASS
    assert "open" in finding["detail"]


def test_cloud_route_opened_any_other_way_fails(policy):
    policy.add_rule(
        LOCAL_OWNER.actor, sec.CLOUD_ROUTE_CAPABILITY, "allow", "seeded",
    )
    assert sec.check_owner_locks(context(policy=policy))["status"] == FAIL


def test_code_run_allowed_fails(policy):
    policy.add_rule(LOCAL_OWNER.actor, sec.CODE_RUN_CAPABILITY, "allow", "test")
    assert sec.check_owner_locks(context(policy=policy))["status"] == FAIL


def test_switch_reason_matches_the_cloud_tab():
    from freya.delegation.service import SWITCH_REASON

    assert sec.CLOUD_SWITCH_REASON == SWITCH_REASON


def test_owner_only_names_match_the_real_capabilities():
    from freya.delegation.service import CLOUD_SETTINGS_CHANGE
    from freya.providers.model.claude_plan import CLOUD_ANTHROPIC_PLAN_ASK
    from freya.sandbox.service import CODE_RUN

    assert sec.CLOUD_ROUTE_CAPABILITY == CLOUD_ANTHROPIC_PLAN_ASK
    assert sec.CODE_RUN_CAPABILITY == CODE_RUN
    for name in (CLOUD_ANTHROPIC_PLAN_ASK, CLOUD_SETTINGS_CHANGE, CODE_RUN):
        assert name in sec.OWNER_ONLY_CAPABILITIES


def test_local_endpoints_pass_as_shipped():
    assert sec.check_local_endpoints(context())["status"] == PASS


def test_local_endpoints_fail_for_a_remote_ollama(monkeypatch):
    from freya.providers.model import ollama as ollama_provider

    monkeypatch.setattr(ollama_provider, "LOCAL_OLLAMA_HOST", "http://10.0.0.5:11434")
    assert sec.check_local_endpoints(context())["status"] == FAIL


def test_local_endpoints_fail_for_a_remote_world_view(monkeypatch):
    from freya import config

    monkeypatch.setattr(config, "WORLD_VIEW_ENABLED", True)
    monkeypatch.setattr(config, "WORLD_VIEW_URL", "http://example.com:4173")
    assert sec.check_local_endpoints(context())["status"] == FAIL


def test_child_environment_passes_as_shipped():
    assert sec.check_child_environment(context())["status"] == PASS


def test_child_environment_fails_when_the_filter_leaks(monkeypatch):
    from freya import child_environment as module

    monkeypatch.setattr(
        module, "child_environment",
        lambda extra=None, environ=None: dict(environ or {}),
    )
    assert sec.check_child_environment(context())["status"] == FAIL


def test_checkpoint_unset_is_a_warning(monkeypatch):
    from freya import config

    monkeypatch.setattr(config, "AUDIT_CHECKPOINT_PATH", None)
    assert sec.check_audit_checkpoint(context())["status"] == WARN


def test_checkpoint_set_passes(monkeypatch):
    from freya import config

    monkeypatch.setattr(config, "AUDIT_CHECKPOINT_PATH", "Z:/audit/checkpoints.log")
    assert sec.check_audit_checkpoint(context())["status"] == PASS


# --- /self-check -------------------------------------------------------


def test_self_check_command_runs_through_the_broker(store, policy, audit):
    broker = _wire_broker(store, policy, audit, seed=True)
    before = audit.head()
    text = sec.run_self_check_command(broker)
    assert text.startswith("Security self-assessment")
    # The run was written to the audit log.
    assert audit.head() != before


def test_self_check_command_reports_a_denial(store, policy, audit):
    broker = _wire_broker(store, policy, audit, seed=False)
    text = sec.run_self_check_command(broker)
    assert "did not run" in text
    assert "denied" in text


def test_self_check_in_the_window(tmp_path, capsys):
    from freya.events import EventBus
    from freya.main import normalize_command
    from freya.settings import Settings
    from freya.window_commands import CommandContext, run_window_command

    asked = []
    context_ = CommandContext(
        bus=EventBus(),
        voice=None,
        listener=None,
        settings=Settings(tmp_path / "settings.json"),
        window=None,
        normalize=normalize_command,
        run_image=lambda prompt: None,
        publish_status=lambda: None,
        self_check=lambda: asked.append(1) or "Security self-assessment: ok",
    )
    run_window_command("/self-check", context_)
    assert asked == [1]
    assert "Security self-assessment: ok" in capsys.readouterr().out


def test_self_check_is_in_the_window_help():
    from freya.window_commands import HELP_TEXT

    assert "/self-check" in HELP_TEXT


# --- startup line ------------------------------------------------------


def test_startup_line_names_only_titles(store, policy, audit):
    broker = _wire_broker(store, policy, audit, seed=True)
    line = sec.self_check_startup_line(broker)
    assert line.startswith("Security self-check")
    assert "\n" not in line


def test_startup_line_when_everything_passes():
    report = {
        "overall": PASS,
        "summary": {PASS: 3, WARN: 0, FAIL: 0, "total": 3},
        "findings": [],
    }
    broker = types.SimpleNamespace(
        invoke=lambda name: types.SimpleNamespace(success=True, result=report)
    )
    assert sec.self_check_startup_line(broker) == (
        "Security self-check: all 3 checks passed."
    )


def test_startup_line_hides_evidence():
    report = {
        "overall": FAIL,
        "summary": {PASS: 0, WARN: 0, FAIL: 1, "total": 1},
        "findings": [
            {
                "title": "No personal data or keys tracked by git",
                "status": FAIL,
                "detail": "git is tracking files that must never be committed.",
                "evidence": ["data/secret.key"],
            }
        ],
    }
    broker = types.SimpleNamespace(
        invoke=lambda name: types.SimpleNamespace(success=True, result=report)
    )
    line = sec.self_check_startup_line(broker)
    assert "No personal data or keys tracked by git" in line
    assert "secret.key" not in line
    assert "/self-check" in line


def test_startup_line_when_denied(store, policy, audit):
    broker = _wire_broker(store, policy, audit, seed=False)
    assert "did not run (denied)" in sec.self_check_startup_line(broker)
