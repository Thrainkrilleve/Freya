"""
What isolation this machine can actually provide.

The command reads. It does not install software, change a system setting,
attach a drive, or register ``code.run``. A facility that is present is
not a validated tier: the verified tier stays T0 until containment has
been measured and the image is a pinned signed digest, which it is not.

Nothing written here is taken from a command's own text. A check answers
yes, no, or a fixed phrase, and the file is refused if that phrase is not
one of the phrases below.
"""

import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


YES = "yes"
NO = "no"
UNKNOWN = "unknown"
NOT_APPLICABLE = "not applicable"
NOT_RUN = "not run"
NOT_MEASURED = "not measured"
PASS = "pass"
FAIL = "fail"
ABSENT = "absent"
SKIPPED = "skipped"
TIER = "T0"
NONE = "none"

ANSWERS = {
    YES, NO, UNKNOWN, NOT_APPLICABLE, NOT_RUN, NOT_MEASURED,
    PASS, FAIL, ABSENT, SKIPPED, TIER, NONE,
}

REASON_NOT_RUN = "Containment checks were not run."
REASON_FAILED = "A containment check failed."
REASON_UNSIGNED = "The image is not a pinned signed digest."
REASONS = {REASON_NOT_RUN, REASON_FAILED, REASON_UNSIGNED}

# Strongest facility first. Plain runc is not in this list: it is not a boundary.
_CANDIDATES = (
    ("hyper_v", "hyper-v", False),
    ("windows_sandbox", "windows-sandbox", False),
    ("apple_vz", "apple-vz", False),
    ("firecracker", "firecracker", False),
    ("kata", "kata", False),
    ("gvisor", "gvisor", False),
    ("wsl", "wsl2", True),
    ("docker_rootless", "rootless-docker", True),
    ("docker_desktop", "docker-desktop", True),
)

_CHECKS = (
    "Process limits",
    "Filesystem confinement",
    "Network denial",
    "Timeout",
    "Fresh environment",
    "Credentials absent",
    "Read-only root",
    "Capabilities dropped",
    "Seccomp",
    "No new privileges",
    "Non-root",
    "CPU limit",
    "Memory limit",
    "PID limit",
    "Hardware virtualization",
    "Image digest",
    "Signature",
    "Host drive attach",
)

DEFAULT_OUTPUT = Path("docs/results/sandbox-probes.md")


@dataclass(frozen=True)
class MachineFacts:
    """Yes, no, unknown, or not applicable. No command text is kept."""

    system: str
    hyper_v: str = NOT_APPLICABLE
    windows_sandbox: str = NOT_APPLICABLE
    wsl: str = NOT_APPLICABLE
    kvm: str = NOT_APPLICABLE
    firecracker: str = NO
    apple_vz: str = NOT_APPLICABLE
    docker: str = NO
    docker_rootless: str = NOT_APPLICABLE
    docker_desktop: str = NOT_APPLICABLE
    gvisor: str = NO
    kata: str = NO
    runc: str = NO
    podman: str = NO
    hardware_virtualization: str = UNKNOWN


@dataclass(frozen=True)
class SurveyReport:
    verified_tier: str
    reason: str
    runtime: str
    fresh_per_task: str
    hardware_virtualization: str
    image_digest: str
    signature_ok: str
    weaker: str
    remote: str
    owner_opt_in: str
    candidate: str
    facilities: tuple
    checks: tuple
    recorded: str


def _answer(value) -> str:
    if value not in ANSWERS:
        return UNKNOWN
    return value


def _system_name(system) -> str:
    if system is None:
        system = sys.platform
    system = str(system).lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    if system.startswith("linux"):
        return "linux"
    return "other"


def _token(run, argv, timeout) -> str:
    """A single yes or no. Any other output is unknown and is discarded."""
    try:
        completed = run(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN

    text = completed.stdout.decode("utf-8", errors="replace").strip().lower()
    if text in ("yes", "true", "1"):
        return YES
    if text in ("no", "false", "0"):
        return NO
    return UNKNOWN


def _present(which, name) -> str:
    try:
        return YES if which(name) else NO
    except OSError:
        return UNKNOWN


def _cpu_virtualization() -> str:
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return UNKNOWN
    for line in text.splitlines():
        if line.lower().startswith("flags"):
            flags = set(line.split())
            return YES if "vmx" in flags or "svm" in flags else NO
    return UNKNOWN


def _kvm_present() -> str:
    try:
        return YES if Path("/dev/kvm").exists() else NO
    except OSError:
        return UNKNOWN


def live_facts(run=subprocess.run, which=shutil.which, system=None, timeout=5) -> MachineFacts:
    """
    Read which facilities exist. Programs are named here, never taken
    from outside, and their text is not kept.
    """
    kind = _system_name(system)
    kata = _present(which, "kata-runtime")
    if kata != YES:
        kata = _present(which, "containerd-shim-kata-v2")
    facts = {
        "system": kind,
        "hyper_v": NOT_APPLICABLE,
        "windows_sandbox": NOT_APPLICABLE,
        "wsl": NOT_APPLICABLE,
        "kvm": NOT_APPLICABLE,
        "firecracker": _present(which, "firecracker"),
        "apple_vz": NOT_APPLICABLE,
        "docker": _present(which, "docker"),
        "docker_rootless": NOT_APPLICABLE,
        "docker_desktop": NOT_APPLICABLE,
        "gvisor": _present(which, "runsc"),
        "kata": kata,
        "runc": _present(which, "runc"),
        "podman": _present(which, "podman"),
        "hardware_virtualization": UNKNOWN,
    }

    if kind == "windows":
        facts["hyper_v"] = _token(
            run,
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "if (Get-Command Get-VM -ErrorAction SilentlyContinue) { 'yes' } else { 'no' }"],
            timeout,
        )
        facts["windows_sandbox"] = _present(which, "WindowsSandbox.exe")
        facts["wsl"] = _present(which, "wsl.exe")
        facts["hardware_virtualization"] = _token(
            run,
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1 -ExpandProperty VirtualizationFirmwareEnabled)"],
            timeout,
        )
        facts["docker_desktop"] = facts["docker"]
    elif kind == "linux":
        facts["kvm"] = _kvm_present()
        cpu = _cpu_virtualization()
        if facts["kvm"] == YES or cpu == YES:
            facts["hardware_virtualization"] = YES
        elif facts["kvm"] == NO and cpu == NO:
            facts["hardware_virtualization"] = NO
        facts["docker_desktop"] = NO
    elif kind == "macos":
        facts["apple_vz"] = _token(run, ["sysctl", "-n", "kern.hv_support"], timeout)
        facts["hardware_virtualization"] = facts["apple_vz"]
        facts["docker_desktop"] = facts["docker"]

    if facts["docker"] == YES:
        rootless = _token(
            run,
            ["docker", "info", "--format",
             "{{range .SecurityOptions}}{{if eq . \"name=rootless\"}}yes{{end}}{{end}}"],
            timeout,
        )
        # An empty answer is "no", not a failure. Any other text is unknown.
        facts["docker_rootless"] = rootless if rootless in (YES, NO) else UNKNOWN
        if rootless == UNKNOWN:
            empty = _token(
                run,
                ["docker", "info", "--format", "no"],
                timeout,
            )
            if empty == NO:
                facts["docker_rootless"] = NO

    return MachineFacts(**{key: _answer(value) if key != "system" else value for key, value in facts.items()})


def _candidate(facts: MachineFacts):
    for field, name, weaker in _CANDIDATES:
        if field in ("firecracker", "kata") and facts.kvm != YES:
            continue
        if getattr(facts, field) == YES:
            return name, weaker
    return NONE, False


def _check_map(probes) -> dict:
    """Containment answers from probes 1 to 7. Probe notes are not kept."""
    if not probes:
        return {}

    passed = {}
    for probe in probes:
        number = getattr(probe, "number", None)
        if number in passed:
            continue
        passed[number] = PASS if getattr(probe, "passed", False) else FAIL

    def one(number):
        return passed.get(number, NOT_RUN)

    def both(left, right):
        if left not in passed or right not in passed:
            return NOT_RUN
        return PASS if passed[left] == PASS and passed[right] == PASS else FAIL

    limits = one(5)
    return {
        "Process limits": limits,
        "Filesystem confinement": one(3),
        "Network denial": both(1, 2),
        "Timeout": limits,
        "Fresh environment": one(4),
        "Credentials absent": one(7),
        "Capabilities dropped": one(7),
        "Non-root": one(7),
        "Memory limit": limits,
        "PID limit": limits,
    }


def build_report(facts: MachineFacts, probes=None, recorded=None) -> SurveyReport:
    """
    The verified tier is T0. Seeing a facility, or even passing the
    container checks, does not raise it: the image has no pinned digest
    and no signature check.
    """
    measured = _check_map(probes)
    checks = []
    for name in _CHECKS:
        if name == "Hardware virtualization":
            result = _answer(facts.hardware_virtualization)
        elif name == "Image digest":
            result = ABSENT
        elif name == "Signature":
            result = NO
        elif name == "Host drive attach":
            result = SKIPPED
        elif name in ("Read-only root", "Seccomp", "No new privileges", "CPU limit"):
            result = NOT_MEASURED
        else:
            result = measured.get(name, NOT_RUN)
        checks.append((name, _answer(result)))

    containment = [result for name, result in checks if name in (
        "Process limits", "Filesystem confinement", "Network denial",
        "Timeout", "Fresh environment", "Credentials absent",
    )]
    if any(result == FAIL for result in containment):
        reason = REASON_FAILED
    elif any(result == NOT_RUN for result in containment):
        reason = REASON_NOT_RUN
    else:
        reason = REASON_UNSIGNED

    started = bool(probes) and reason != REASON_NOT_RUN
    candidate, _weaker_candidate = _candidate(facts)
    runtime = "wsl2" if started else NONE
    fresh = measured.get("Fresh environment", NOT_RUN)
    if fresh == PASS:
        fresh_per_task = YES
    elif fresh == FAIL:
        fresh_per_task = NO
    else:
        fresh_per_task = NOT_MEASURED

    return SurveyReport(
        verified_tier=TIER,
        reason=reason,
        runtime=runtime,
        fresh_per_task=fresh_per_task,
        hardware_virtualization=_answer(facts.hardware_virtualization),
        image_digest=ABSENT,
        signature_ok=NO,
        weaker=YES if runtime == "wsl2" else NOT_APPLICABLE,
        remote=NO,
        owner_opt_in=NO,
        candidate=candidate,
        facilities=(
            ("Hyper-V", _answer(facts.hyper_v)),
            ("Windows Sandbox", _answer(facts.windows_sandbox)),
            ("WSL", _answer(facts.wsl)),
            ("KVM", _answer(facts.kvm)),
            ("Firecracker", _answer(facts.firecracker)),
            ("Apple virtualization", _answer(facts.apple_vz)),
            ("Docker client", _answer(facts.docker)),
            ("Rootless Docker", _answer(facts.docker_rootless)),
            ("Docker Desktop", _answer(facts.docker_desktop)),
            ("gVisor", _answer(facts.gvisor)),
            ("Kata", _answer(facts.kata)),
            ("runc", _answer(facts.runc)),
            ("Podman", _answer(facts.podman)),
        ),
        checks=tuple(checks),
        recorded=recorded or datetime.now(timezone.utc).date().isoformat(),
    )


def render_report(report: SurveyReport) -> str:
    """Markdown made only of fixed phrases. Raises if one is not allowed."""
    if report.reason not in REASONS or report.verified_tier != TIER:
        raise ValueError("The survey result is not one of the fixed phrases.")
    if not report.recorded or not report.recorded[:4].isdigit() or len(report.recorded) != 10:
        raise ValueError("The survey result is not one of the fixed phrases.")

    lines = [
        "# Sandbox probe results",
        "",
        "This file records which isolation facilities were visible.",
        "It is not a validation. code.run stays unregistered.",
        "Nothing was installed and no system setting was changed.",
        "",
        f"- Recorded: {report.recorded}",
        f"- Verified tier: {report.verified_tier}",
        f"- Reason: {report.reason}",
        f"- Runtime: {report.runtime}",
        f"- Candidate facility: {report.candidate}",
        f"- Fresh per task: {report.fresh_per_task}",
        f"- Hardware virtualization: {report.hardware_virtualization}",
        f"- Image digest: {report.image_digest}",
        f"- Signature ok: {report.signature_ok}",
        f"- Weaker: {report.weaker}",
        f"- Remote: {report.remote}",
        f"- Owner opt-in: {report.owner_opt_in}",
        "",
        "## Facilities",
        "",
        "| Facility | Result |",
        "| --- | --- |",
    ]
    for name, result in report.facilities:
        lines.append(f"| {name} | {result} |")

    lines.extend([
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ])
    for name, result in report.checks:
        lines.append(f"| {name} | {result} |")

    lines.extend([
        "",
        "A skipped or failed check leaves the verified tier at T0.",
        "Host drive attach is skipped because attaching a drive changes the guest.",
        "",
    ])
    text = "\n".join(lines)
    for line in text.splitlines():
        if line.startswith("| ") and line.endswith(" |") and "---" not in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) == 2 and cells[1] not in ANSWERS and cells[1] != "Result":
                raise ValueError("The survey result is not one of the fixed phrases.")
        if line.startswith("- ") and ": " in line:
            value = line.split(": ", 1)[1]
            allowed = ANSWERS | REASONS | {"wsl2"} | {item[1] for item in _CANDIDATES}
            if value not in allowed and not (line.startswith("- Recorded:") and value == report.recorded):
                raise ValueError("The survey result is not one of the fixed phrases.")
    return text


def write_report(report: SurveyReport, path) -> Path:
    path = Path(path)
    text = render_report(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def containment_if_ready(ready, run_checks):
    """
    Run the container checks only when an already-built sandbox answers.
    ``run_checks`` must not attach a drive.
    """
    try:
        if not ready():
            return None
    except Exception:
        return None
    return run_checks()
