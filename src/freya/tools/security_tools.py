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


ALL_CHECKS: tuple[Callable[[SelfAssessmentContext], dict], ...] = (
    check_audit_chain,
    check_policy_deny_by_default,
    check_policy_grants,
    check_gitignore,
    check_no_tracked_sensitive_files,
    check_gateway_loopback,
    check_gateway_csp,
    check_avatar_isolation,
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
            "the audit chain, that policy is deny-by-default with no "
            "destructive grants, that personal data is git-ignored and "
            "untracked, that the gateway binds loopback with a strict "
            "CSP, and that the avatar and Core stay isolated. Returns a "
            "findings report and changes nothing."
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
