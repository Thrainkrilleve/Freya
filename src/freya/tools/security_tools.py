"""
Freya's read-only security self-assessment.

A single capability, ``security.self_assessment``, inspects Freya's own
configuration and state and reports how well her documented security
invariants are holding. It is defensive and strictly read-only: it runs
no exploits, executes no attack code, opens no socket, and changes
nothing. Fixing a finding is a separate, reviewed change.

It is reached only through the broker (deny-by-default policy, audited),
and it is deliberately NOT offered to the model as a tool, so a spoken
sentence, a model decision, or untrusted tool output cannot trigger it.
It is invoked by an explicit owner action.

Each check verifies an invariant Freya already documents rather than
inventing a new rule, and every check is wrapped so that a check that
cannot run becomes a warning instead of failing the whole report.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from freya.child_environment import child_environment
from freya.core.capabilities import CapabilityDefinition
from freya.core.providers import ProviderDefinition


SECURITY_PROVIDER = "security-local"
SELF_ASSESSMENT = "security.self_assessment"
SELF_ASSESSMENT_POLICY_SEED = "alpha-self-assessment"

PASS = "pass"
WARN = "warn"
FAIL = "fail"

# Patterns that must never be committed, mirrored from .gitignore and the
# working rules in CLAUDE.md.
REQUIRED_GITIGNORE = ("data/", "*.db")
RECOMMENDED_GITIGNORE = ("*.key", "*.pem", "*.safetensors", "*.gguf")

# Files git must never be tracking: the owner's personal data and keys.
SENSITIVE_TRACKED_PREFIXES = ("data/",)
SENSITIVE_TRACKED_SUFFIXES = (".db", ".key", ".pem")
SENSITIVE_TRACKED_NAMES = ("credentials.json", "secrets.json")

# Capabilities that only an explicit owner action may start. None of
# them may ever be offered to the model as a tool: a model decision,
# a spoken sentence or untrusted tool output must not reach them.
OWNER_ONLY_CAPABILITIES = (
    SELF_ASSESSMENT,
    "cloud.anthropic_plan.ask",
    "cloud.settings.change",
    "code.run",
)

# The cloud route may be allowed only by the rule the owner's switch in
# the Cloud tab writes, never by a seed or a hand-added rule. Kept as a
# literal (it must match delegation.service.SWITCH_REASON; a test
# checks) so this module does not import the delegation package.
CLOUD_ROUTE_CAPABILITY = "cloud.anthropic_plan.ask"
CLOUD_SWITCH_REASON = "Switched on by the owner in the Cloud tab."

# Running model-written code needs a validated sandbox first. Until
# the owner switches one on, no rule may allow it.
CODE_RUN_CAPABILITY = "code.run"

# Made-up variable names the child-environment check feeds through the
# filter. They hold no real value.
PROBE_CREDENTIAL_NAMES = (
    "FREYA_PROBE_API_KEY",
    "FREYA_PROBE_TOKEN",
    "FREYA_PROBE_SECRET",
    "FREYA_PROBE_PASSWORD",
    "FREYA_PROBE_ACCESS_KEY",
)

# Tokens that would mean the avatar had gained the ability to read the
# screen or send input. The avatar package must contain none of them.
AVATAR_FORBIDDEN_TOKENS = (
    "ImageGrab",
    "mss",
    "pyautogui",
    "SendInput",
    "keybd_event",
    "pynput",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finding(check_id, title, status, detail, evidence=None) -> dict:
    out = {
        "id": check_id,
        "title": title,
        "status": status,
        "detail": detail,
    }
    if evidence:
        out["evidence"] = evidence
    return out


@dataclass
class SelfAssessmentContext:
    """
    What the checks are allowed to read. Everything here is Freya's own
    state; there is no target argument, so nothing can be pointed at
    another host.
    """

    audit: object
    policy: object
    capabilities: object
    owner_actor: str
    package_root: Optional[Path] = None
    repo_root: Optional[Path] = None
    # The broker, to read how many audit records it still holds
    # unwritten. Optional so the checks that need only policy and the
    # registry can run without one.
    broker: object = None


def locate_source_roots() -> tuple[Optional[Path], Optional[Path]]:
    """
    Return (package_root, repo_root). The package root is the installed
    ``freya`` package; the repo root is its working tree when Freya runs
    from an editable install, else ``None``.
    """
    try:
        import freya

        package_root = Path(freya.__file__).resolve().parent
    except Exception:
        return None, None

    repo_root = None
    # src/freya -> src -> repo_root
    candidate = package_root.parents[1] if len(package_root.parents) >= 2 else None
    if candidate is not None and (candidate / ".gitignore").exists():
        repo_root = candidate
    return package_root, repo_root


# --- individual checks -------------------------------------------------


def check_audit_chain(ctx: SelfAssessmentContext) -> dict:
    title = "Audit log integrity"
    verification = ctx.audit.verify()
    if not verification.ok:
        return _finding(
            "audit-chain",
            title,
            FAIL,
            "The hash-chained audit log does not verify; the broker "
            "refuses every action until this is resolved.",
            evidence=verification.problems[:5],
        )
    if verification.warnings:
        return _finding(
            "audit-chain",
            title,
            WARN,
            "The audit chain verifies but reported warnings.",
            evidence=verification.warnings[:5],
        )
    return _finding(
        "audit-chain",
        title,
        PASS,
        "The hash-chained audit log verifies end to end.",
    )


def check_policy_deny_by_default(ctx: SelfAssessmentContext) -> dict:
    title = "Policy is deny-by-default"
    probe = "security.__self_assessment_probe__"
    decision = ctx.policy.decide(ctx.owner_actor, probe)
    if decision.allow:
        return _finding(
            "policy-deny-default",
            title,
            FAIL,
            "An unseeded capability was allowed, which means a "
            "wildcard allow rule is granting more than intended.",
            evidence=[decision.reason],
        )
    return _finding(
        "policy-deny-default",
        title,
        PASS,
        "An unknown capability is denied with no matching allow rule.",
    )


def check_policy_grants(ctx: SelfAssessmentContext) -> dict:
    title = "No over-broad or destructive grants"
    rules = ctx.policy.list_rules()
    allow_rules = [r for r in rules if r["effect"] == "allow"]

    wildcards = [
        r
        for r in allow_rules
        if r["subject"] == "*" or r["capability"] == "*"
    ]

    destructive = []
    for rule in allow_rules:
        name = rule["capability"]
        if name == "*":
            continue
        capability = ctx.capabilities.get(name)
        if capability is not None and getattr(capability, "destructive", False):
            destructive.append(name)

    granted = sorted({r["capability"] for r in allow_rules})

    if destructive:
        return _finding(
            "policy-grants",
            title,
            FAIL,
            "A capability marked destructive is granted to the owner.",
            evidence=destructive,
        )
    if wildcards:
        return _finding(
            "policy-grants",
            title,
            WARN,
            "A wildcard allow rule is present; grants should name an "
            "exact subject and capability.",
            evidence=[f"{r['subject']} -> {r['capability']}" for r in wildcards],
        )
    return _finding(
        "policy-grants",
        title,
        PASS,
        f"{len(granted)} capabilities are granted, each by exact name, "
        "none destructive.",
    )


def check_gitignore(ctx: SelfAssessmentContext) -> dict:
    title = "Personal data is git-ignored"
    if ctx.repo_root is None:
        return _finding(
            "gitignore",
            title,
            WARN,
            "No working tree found (installed, not editable); cannot "
            "inspect .gitignore.",
        )
    gitignore = ctx.repo_root / ".gitignore"
    if not gitignore.exists():
        return _finding("gitignore", title, WARN, "No .gitignore found.")

    lines = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
    }
    missing_required = [p for p in REQUIRED_GITIGNORE if p not in lines]
    missing_recommended = [p for p in RECOMMENDED_GITIGNORE if p not in lines]

    if missing_required:
        return _finding(
            "gitignore",
            title,
            FAIL,
            "Patterns protecting the owner's personal data and databases "
            "are missing from .gitignore.",
            evidence=missing_required,
        )
    if missing_recommended:
        return _finding(
            "gitignore",
            title,
            WARN,
            "Some recommended key/model patterns are not ignored.",
            evidence=missing_recommended,
        )
    return _finding(
        "gitignore",
        title,
        PASS,
        "data/ and *.db are git-ignored.",
    )


def check_no_tracked_sensitive_files(ctx: SelfAssessmentContext) -> dict:
    title = "No personal data or keys tracked by git"
    if ctx.repo_root is None:
        return _finding(
            "tracked-sensitive",
            title,
            WARN,
            "No working tree found; cannot list tracked files.",
        )
    try:
        completed = subprocess.run(
            ["git", "-C", str(ctx.repo_root), "ls-files"],
            capture_output=True,
            env=child_environment(),
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _finding(
            "tracked-sensitive",
            title,
            WARN,
            f"Could not run git to list tracked files: {exc}",
        )
    if completed.returncode != 0:
        return _finding(
            "tracked-sensitive",
            title,
            WARN,
            "git ls-files did not succeed (not a git checkout?).",
        )

    offenders = []
    for path in completed.stdout.splitlines():
        path = path.strip()
        if not path:
            continue
        name = path.rsplit("/", 1)[-1]
        if (
            path.startswith(SENSITIVE_TRACKED_PREFIXES)
            or path.endswith(SENSITIVE_TRACKED_SUFFIXES)
            or name in SENSITIVE_TRACKED_NAMES
        ):
            offenders.append(path)

    if offenders:
        return _finding(
            "tracked-sensitive",
            title,
            FAIL,
            "git is tracking files that must never be committed.",
            evidence=offenders[:10],
        )
    return _finding(
        "tracked-sensitive",
        title,
        PASS,
        "No personal-data files, databases, or keys are tracked.",
    )


def check_gateway_loopback(ctx: SelfAssessmentContext) -> dict:
    title = "Gateway binds loopback only"
    try:
        from freya.gateway import server
    except Exception as exc:
        return _finding(
            "gateway-loopback",
            title,
            WARN,
            f"Could not inspect the gateway module: {exc}",
        )
    loopback = getattr(server, "LOOPBACK", None)
    if loopback != "127.0.0.1":
        return _finding(
            "gateway-loopback",
            title,
            FAIL,
            f"The gateway bind address is {loopback!r}, not loopback.",
        )
    return _finding(
        "gateway-loopback",
        title,
        PASS,
        "The gateway binds 127.0.0.1 only.",
    )


def check_gateway_csp(ctx: SelfAssessmentContext) -> dict:
    title = "Content-Security-Policy is strict"
    try:
        from freya.gateway import server

        csp = server.content_security_policy()
    except Exception as exc:
        return _finding(
            "gateway-csp",
            title,
            WARN,
            f"Could not inspect the Content-Security-Policy: {exc}",
        )
    lowered = csp.lower()
    unsafe = [t for t in ("unsafe-inline", "unsafe-eval") if t in lowered]
    if unsafe:
        return _finding(
            "gateway-csp",
            title,
            FAIL,
            "The Content-Security-Policy allows unsafe inline or eval "
            "script.",
            evidence=unsafe,
        )
    if "default-src" not in lowered:
        return _finding(
            "gateway-csp",
            title,
            WARN,
            "The Content-Security-Policy has no default-src directive.",
        )
    return _finding(
        "gateway-csp",
        title,
        PASS,
        "The page's CSP has no unsafe-inline or unsafe-eval.",
    )


def check_avatar_isolation(ctx: SelfAssessmentContext) -> dict:
    title = "Avatar and Core stay isolated"
    if ctx.package_root is None:
        return _finding(
            "avatar-isolation",
            title,
            WARN,
            "Package root not found; cannot inspect imports.",
        )

    core_dir = ctx.package_root / "core"
    avatar_dir = ctx.package_root / "spatial_avatar"
    if not core_dir.is_dir() or not avatar_dir.is_dir():
        return _finding(
            "avatar-isolation",
            title,
            WARN,
            "Core or avatar package not found at the expected path.",
        )

    problems = []
    core_imports_avatar = re.compile(
        r"^\s*(?:from|import)\s+freya\.spatial_avatar", re.MULTILINE
    )
    avatar_imports_core = re.compile(
        r"^\s*(?:from|import)\s+freya\.core", re.MULTILINE
    )

    for path in core_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if core_imports_avatar.search(text):
            problems.append(f"Core imports avatar: {path.name}")

    for path in avatar_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if avatar_imports_core.search(text):
            problems.append(f"Avatar imports Core: {path.name}")
        for token in AVATAR_FORBIDDEN_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", text):
                problems.append(
                    f"Avatar references screen/input API {token!r}: {path.name}"
                )

    if problems:
        return _finding(
            "avatar-isolation",
            title,
            FAIL,
            "The avatar/Core isolation boundary is broken.",
            evidence=problems[:10],
        )
    return _finding(
        "avatar-isolation",
        title,
        PASS,
        "Core imports no avatar code; the avatar imports no Core and no "
        "screen/input APIs.",
    )


def check_unwritten_audit(ctx: SelfAssessmentContext) -> dict:
    title = "Every decision reached the audit log"
    broker = ctx.broker
    if broker is None:
        return _finding(
            "audit-unwritten",
            title,
            WARN,
            "No broker was given to this check; cannot read how many "
            "records are waiting to be written.",
        )
    lost = int(getattr(broker, "lost_audit_records", 0) or 0)
    pending = int(getattr(broker, "pending_audit_records", 0) or 0)
    if lost:
        return _finding(
            "audit-unwritten",
            title,
            FAIL,
            "Audit records were lost because the waiting list was "
            "full: the log has gaps for this run.",
            evidence=[f"{lost} lost", f"{pending} still waiting"],
        )
    if pending:
        return _finding(
            "audit-unwritten",
            title,
            WARN,
            "Some audit records have not been written yet; the broker "
            "refuses new actions until they are.",
            evidence=[f"{pending} waiting"],
        )
    return _finding(
        "audit-unwritten",
        title,
        PASS,
        "No audit record is waiting or was lost in this run.",
    )


def check_model_tool_exposure(ctx: SelfAssessmentContext) -> dict:
    title = "The model is offered only safe tools"
    problems = []
    offered = 0
    for capability in ctx.capabilities.list_capabilities():
        metadata = getattr(capability, "metadata", None) or {}
        if not metadata.get("model_tool"):
            continue
        offered += 1
        if getattr(capability, "destructive", False):
            problems.append(f"destructive capability offered: {capability.name}")
        if capability.name in OWNER_ONLY_CAPABILITIES:
            problems.append(f"owner-only capability offered: {capability.name}")
    if problems:
        return _finding(
            "model-tools",
            title,
            FAIL,
            "A capability the model must never choose is marked as a "
            "model tool.",
            evidence=problems[:10],
        )
    return _finding(
        "model-tools",
        title,
        PASS,
        f"{offered} capabilities are offered to the model; none is "
        "destructive or owner-only.",
    )


def check_owner_locks(ctx: SelfAssessmentContext) -> dict:
    title = "Cloud and code-running locks are closed unless the owner opened them"
    problems = []
    cloud_open = False
    for rule in ctx.policy.list_rules():
        if rule["effect"] != "allow":
            continue
        if rule["capability"] == CLOUD_ROUTE_CAPABILITY:
            if rule["reason"] == CLOUD_SWITCH_REASON:
                cloud_open = True
            else:
                problems.append(
                    "the cloud route is allowed by a rule the Cloud "
                    "tab's switch did not write"
                )
        if rule["capability"] == CODE_RUN_CAPABILITY:
            problems.append(
                "running model-written code is allowed before a "
                "sandbox has been validated"
            )
    if problems:
        return _finding(
            "owner-locks",
            title,
            FAIL,
            "A lock that only the owner opens is open by another route.",
            evidence=problems,
        )
    detail = (
        "Sending to Claude is open, by the owner's switch in the Cloud tab."
        if cloud_open
        else "Sending to a cloud model is closed."
    )
    return _finding(
        "owner-locks",
        title,
        PASS,
        detail + " Running model-written code is closed.",
    )


def check_local_endpoints(ctx: SelfAssessmentContext) -> dict:
    title = "Model and world-data endpoints stay on this machine"
    problems = []
    notes = []
    try:
        from freya.providers.model import ollama as ollama_provider
    except Exception as exc:
        return _finding(
            "local-endpoints",
            title,
            WARN,
            f"Could not inspect the Ollama provider: {exc}",
        )
    host = getattr(ollama_provider, "LOCAL_OLLAMA_HOST", "")
    if not host.startswith("http://127.0.0.1:"):
        problems.append(f"Ollama address is {host!r}, not loopback")
    client = getattr(ollama_provider, "ollama", None)
    inner = getattr(client, "_client", None)
    if inner is not None and getattr(inner, "_trust_env", False):
        problems.append("the Ollama client follows proxy settings")

    try:
        from freya import config
        from freya.tools.world_view import _is_local

        if getattr(config, "WORLD_VIEW_ENABLED", False):
            if not _is_local(config.WORLD_VIEW_URL):
                problems.append("the world-data address is not on this machine")
            else:
                notes.append("world data is on, from a program on this machine")
        else:
            notes.append("world data is off")
    except Exception as exc:
        notes.append(f"world data could not be inspected: {exc}")

    if problems:
        return _finding(
            "local-endpoints",
            title,
            FAIL,
            "A model or data endpoint could leave this machine.",
            evidence=problems,
        )
    return _finding(
        "local-endpoints",
        title,
        PASS,
        "Ollama is pinned to 127.0.0.1 and ignores proxies; "
        + "; ".join(notes) + ".",
    )


def check_child_environment(ctx: SelfAssessmentContext) -> dict:
    title = "Programs Freya starts get no credentials"
    try:
        from freya.child_environment import child_environment
    except Exception as exc:
        return _finding(
            "child-environment",
            title,
            WARN,
            f"Could not inspect the child-environment filter: {exc}",
        )
    probe = {name: "x" for name in PROBE_CREDENTIAL_NAMES}
    probe["PATH"] = "x"
    kept = child_environment(environ=probe)
    leaked = [name for name in PROBE_CREDENTIAL_NAMES if name in kept]
    if leaked:
        return _finding(
            "child-environment",
            title,
            FAIL,
            "Credential-named variables would be passed to programs "
            "Freya starts.",
            evidence=leaked,
        )
    if "PATH" not in kept:
        return _finding(
            "child-environment",
            title,
            WARN,
            "The filter also removes ordinary settings such as PATH.",
        )
    return _finding(
        "child-environment",
        title,
        PASS,
        "Credential-named variables are removed before a program starts.",
    )


def check_audit_checkpoint(ctx: SelfAssessmentContext) -> dict:
    title = "Audit checkpoints leave this machine"
    try:
        from freya.config import AUDIT_CHECKPOINT_PATH
    except Exception as exc:
        return _finding(
            "audit-checkpoint",
            title,
            WARN,
            f"Could not read the checkpoint setting: {exc}",
        )
    if not AUDIT_CHECKPOINT_PATH:
        return _finding(
            "audit-checkpoint",
            title,
            WARN,
            "No checkpoint file is set (FREYA_AUDIT_CHECKPOINT_PATH), so "
            "someone who can rewrite this PC's files could rewrite the "
            "audit log and its chain together without a trace.",
        )
    return _finding(
        "audit-checkpoint",
        title,
        PASS,
        "Checkpoints of the audit chain are written to the configured "
        "file. They add evidence only if this machine cannot rewrite it.",
    )


ALL_CHECKS: tuple[Callable[[SelfAssessmentContext], dict], ...] = (
    check_audit_chain,
    check_policy_deny_by_default,
    check_policy_grants,
    check_gitignore,
    check_no_tracked_sensitive_files,
    check_gateway_loopback,
    check_gateway_csp,
    check_avatar_isolation,
    check_unwritten_audit,
    check_model_tool_exposure,
    check_owner_locks,
    check_local_endpoints,
    check_child_environment,
    check_audit_checkpoint,
)


def run_self_assessment(context: SelfAssessmentContext) -> dict:
    """
    Run every read-only check and return a findings report. A check that
    raises becomes a warning rather than failing the whole run, so the
    report is always produced.
    """
    findings: List[dict] = []
    for check in ALL_CHECKS:
        try:
            findings.append(check(context))
        except Exception as exc:  # a check must never break the report
            findings.append(
                _finding(
                    getattr(check, "__name__", "check"),
                    getattr(check, "__name__", "check"),
                    WARN,
                    f"This check could not run: {exc}",
                )
            )

    summary = {
        PASS: sum(1 for f in findings if f["status"] == PASS),
        WARN: sum(1 for f in findings if f["status"] == WARN),
        FAIL: sum(1 for f in findings if f["status"] == FAIL),
        "total": len(findings),
    }
    if summary[FAIL]:
        overall = FAIL
    elif summary[WARN]:
        overall = WARN
    else:
        overall = PASS

    return {
        "generated_at": _now(),
        "overall": overall,
        "summary": summary,
        "findings": findings,
    }


def security_self_assessment_capability(handler) -> CapabilityDefinition:
    """
    The ``security.self_assessment`` capability definition.

    It takes no arguments, is not offered to the model as a tool, and is
    marked non-destructive because it only reads state.
    """
    return CapabilityDefinition(
        name=SELF_ASSESSMENT,
        description=(
            "Runs Freya's read-only security self-assessment: checks "
            "the audit chain and that no audit record was lost, that "
            "policy is deny-by-default with no destructive grants, that "
            "the cloud and code-running locks are closed unless the "
            "owner opened them, that the model is offered no owner-only "
            "or destructive tool, that personal data is git-ignored and "
            "untracked, that the gateway binds loopback with a strict "
            "CSP, that model and data endpoints stay local, that started "
            "programs get no credentials, and that the avatar and Core "
            "stay isolated. Returns a findings report and changes nothing."
        ),
        provider=SECURITY_PROVIDER,
        handler=handler,
        category="security",
        destructive=False,
        metadata={},
        aliases=["self-check", "security.self-check"],
    )


def create_security_provider() -> ProviderDefinition:
    """
    The in-process provider behind the self-assessment. It has nothing
    to start and no external dependency, so it is always healthy.
    """
    return ProviderDefinition(
        provider_id=SECURITY_PROVIDER,
        name="Local security self-assessment",
        capabilities=[SELF_ASSESSMENT],
        start_handler=lambda: None,
        stop_handler=lambda: None,
        health_handler=lambda: True,
    )


def format_report(report: dict) -> str:
    """
    Render a findings report as plain text for an owner-facing command.
    """
    lines = [
        f"Security self-assessment ({report['generated_at']}) — "
        f"overall: {report['overall'].upper()}",
        "",
    ]
    marks = {PASS: "[pass]", WARN: "[warn]", FAIL: "[FAIL]"}
    for finding in report["findings"]:
        lines.append(
            f"  {marks.get(finding['status'], finding['status'])} "
            f"{finding['title']}: {finding['detail']}"
        )
        for item in finding.get("evidence", []):
            lines.append(f"      - {item}")
    summary = report["summary"]
    lines.append("")
    lines.append(
        f"  {summary[PASS]} passed, {summary[WARN]} warnings, "
        f"{summary[FAIL]} failed, of {summary['total']} checks."
    )
    return "\n".join(lines)


SELF_CHECK_COMMANDS = ("/self-check", "/selfcheck")


def run_self_check_command(broker) -> str:
    """
    What ``/self-check`` answers: the report, run through the broker so
    the run is policy-checked and audited like any other action.
    """
    result = broker.invoke(SELF_ASSESSMENT)
    if not result.success:
        return (
            "The security self-check did not run "
            f"({result.outcome}: {result.error or 'no reason given'})."
        )
    return format_report(result.result)


def self_check_startup_line(broker) -> str:
    """
    The one line Freya prints as she starts: the overall result, and
    which checks did not pass by title only. The details stay behind
    ``/self-check``, so a weakness is not spelled out on every start.
    """
    result = broker.invoke(SELF_ASSESSMENT)
    if not result.success:
        return f"Security self-check did not run ({result.outcome})."
    report = result.result
    summary = report["summary"]
    if report["overall"] == PASS:
        return f"Security self-check: all {summary['total']} checks passed."
    titles = [
        finding["title"]
        for finding in report["findings"]
        if finding["status"] != PASS
    ]
    return (
        f"Security self-check: {summary[FAIL]} failed, {summary[WARN]} "
        f"warnings ({'; '.join(titles)}). Type /self-check for details."
    )
