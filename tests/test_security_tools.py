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
        verify=lambda: AuditVerification(ok=False, problems=["event 3 altered"])
    )
    finding = sec.check_audit_chain(context(audit=fake))
    assert finding["status"] == FAIL
    assert finding["evidence"] == ["event 3 altered"]


def test_audit_chain_warnings_are_a_warning():
    fake = types.SimpleNamespace(
        verify=lambda: AuditVerification(ok=True, warnings=["no off-host sink"])
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
