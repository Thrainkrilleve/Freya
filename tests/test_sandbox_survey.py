"""The isolation survey records what is present and does not enable code.run."""

import re
from pathlib import Path

from freya.sandbox.contract import RunResult
from freya.sandbox.probes import ProbeResult, run_probes
from freya.sandbox.survey import (
    REASON_FAILED,
    REASON_NOT_RUN,
    REASON_UNSIGNED,
    MachineFacts,
    build_report,
    containment_if_ready,
    live_facts,
    render_report,
    write_report,
)


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(
    r"(\d{1,3}(?:\.\d{1,3}){3})"
    r"|[A-Za-z]:\\"
    r"|/(?:home|Users|root|mnt)/"
    r"|localhost"
    r"|@",
    re.IGNORECASE,
)


class ScriptedSandbox:
    def __init__(self, sound=True):
        self.sound = sound

    def run(self, request):
        source = request.files["main.py"]
        if "while True" in source:
            return RunResult(None, "", timed_out=True)
        if "'x' * 5_000_000" in source:
            return RunResult(0, "x", truncated=True)
        return RunResult(0 if self.sound else 1, "ok")


def facts(**overrides):
    values = dict(
        system="linux",
        hyper_v="not applicable",
        windows_sandbox="not applicable",
        wsl="not applicable",
        kvm="no",
        firecracker="no",
        apple_vz="not applicable",
        docker="no",
        docker_rootless="not applicable",
        docker_desktop="not applicable",
        gvisor="no",
        kata="no",
        runc="no",
        podman="no",
        hardware_virtualization="no",
    )
    values.update(overrides)
    return MachineFacts(**values)


def test_an_empty_machine_is_recorded_as_t0(tmp_path):
    report = build_report(facts(), recorded="2026-11-16")
    path = write_report(report, tmp_path / "sandbox-probes.md")
    text = path.read_text(encoding="utf-8")

    assert report.verified_tier == "T0"
    assert report.reason == REASON_NOT_RUN
    assert report.runtime == "none"
    assert "code.run stays unregistered" in text
    assert "| Process limits | not run |" in text
    assert "| Network denial | not run |" in text
    assert "| Filesystem confinement | not run |" in text
    assert "| Timeout | not run |" in text
    assert "| Host drive attach | skipped |" in text
    assert not FORBIDDEN.search(text)


def test_a_present_facility_does_not_raise_the_tier():
    report = build_report(
        facts(system="windows", hyper_v="yes", wsl="yes", hardware_virtualization="yes"),
        recorded="2026-11-16",
    )
    text = render_report(report)

    assert report.verified_tier == "T0"
    assert report.candidate == "hyper-v"
    assert report.runtime == "none"
    assert "T1" not in text and "T2" not in text
    assert not FORBIDDEN.search(text)


def test_passing_container_checks_stay_t0_without_a_signed_image():
    probes = run_probes(ScriptedSandbox(), include_drive_attach=False, run=_refuse)
    report = build_report(facts(system="windows", wsl="yes"), probes, recorded="2026-11-16")
    text = render_report(report)

    assert report.reason == REASON_UNSIGNED
    assert report.verified_tier == "T0"
    assert report.runtime == "wsl2"
    assert report.weaker == "yes"
    assert "| Network denial | pass |" in text
    assert "| Filesystem confinement | pass |" in text
    assert "| Process limits | pass |" in text
    assert "| Timeout | pass |" in text
    assert "T1" not in text and "T2" not in text


def test_a_failed_check_is_recorded_and_its_note_is_not(tmp_path):
    probes = [
        ProbeResult(number, "check", False, "reached 203.0.113.9 /home/owner")
        for number in range(1, 8)
    ]
    report = build_report(facts(), probes, recorded="2026-11-16")
    text = write_report(report, tmp_path / "sandbox-probes.md").read_text(encoding="utf-8")

    assert report.reason == REASON_FAILED
    assert "| Network denial | fail |" in text
    assert "203.0.113.9" not in text
    assert "/home/" not in text
    assert not FORBIDDEN.search(text)


def test_the_survey_does_not_attach_a_drive():
    def boom(*_args, **_kwargs):
        raise AssertionError("a drive attach was attempted")

    results = run_probes(ScriptedSandbox(), run=boom, include_drive_attach=False)

    assert [item.number for item in results] == [1, 2, 3, 4, 5, 6, 7]
    assert boom


def test_containment_runs_only_when_a_sandbox_is_already_there():
    def explode():
        raise AssertionError("checks ran")

    assert containment_if_ready(lambda: False, explode) is None

    def unavailable():
        raise OSError("missing")

    assert containment_if_ready(unavailable, explode) is None


def test_live_facts_keep_only_fixed_answers_and_do_not_change_settings():
    seen = []

    def run(argv, **_kwargs):
        seen.append(tuple(argv))
        joined = " ".join(argv).lower()
        assert "install" not in joined
        assert "mount" not in joined
        assert "netsh" not in joined
        assert "reg " not in joined
        assert "set-item" not in joined

        class Completed:
            returncode = 0
            stdout = b"no\n"

        return Completed()

    linux = live_facts(run=run, which=lambda _name: None, system="linux")
    windows = live_facts(run=run, which=lambda _name: None, system="windows")
    macos = live_facts(
        run=run,
        which=lambda name: "docker" if name == "docker" else None,
        system="darwin",
    )

    for report_facts in (linux, windows, macos):
        for field in (
            "hyper_v", "windows_sandbox", "wsl", "kvm", "firecracker", "apple_vz",
            "docker", "docker_rootless", "docker_desktop", "gvisor", "kata",
            "runc", "podman", "hardware_virtualization",
        ):
            assert getattr(report_facts, field) in {
                "yes", "no", "unknown", "not applicable",
            }
    assert seen
    assert linux.hyper_v == "not applicable"
    assert windows.hyper_v == "no"
    assert macos.docker_desktop == "yes"


def test_the_probe_command_writes_results_and_leaves_code_run_off(tmp_path, monkeypatch):
    from freya.sandbox.__main__ import main

    def boom(*_args, **_kwargs):
        raise AssertionError("setup ran")

    monkeypatch.setattr("freya.sandbox.__main__.run_setup", boom)
    path = tmp_path / "nested" / "sandbox-probes.md"
    said = []
    code = main(
        ["probe", "--output", str(path)],
        say=said.append,
        facts=facts(system="windows", hyper_v="yes"),
        probes=[],
    )

    assert code == 0
    text = path.read_text(encoding="utf-8")
    assert "Verified tier: T0" in text
    assert "code.run stays unregistered" in text
    assert "Verified tier: T0" in said
    assert not FORBIDDEN.search("\n".join(said))
    assert not FORBIDDEN.search(text)

    control = (ROOT / "src" / "freya" / "control_plane.py").read_text(encoding="utf-8")
    command = (ROOT / "src" / "freya" / "sandbox" / "__main__.py").read_text(encoding="utf-8")
    survey = (ROOT / "src" / "freya" / "sandbox" / "survey.py").read_text(encoding="utf-8")
    assert "freya.sandbox" not in control
    assert "code.run" not in control
    assert "register_code_run" not in command
    assert "register_code_run" not in survey


def test_a_failed_probe_command_still_writes_the_file(tmp_path):
    from freya.sandbox.__main__ import main

    probes = [ProbeResult(number, "check", False, "") for number in range(1, 8)]
    path = tmp_path / "sandbox-probes.md"
    code = main(
        ["probe", "--output", str(path)],
        say=lambda *_args: None,
        facts=facts(),
        probes=probes,
    )

    assert code == 1
    assert "A containment check failed." in path.read_text(encoding="utf-8")


def test_the_results_template_is_safe_and_not_a_pass():
    template = (ROOT / "docs" / "results" / "sandbox-probes.template.md").read_text(encoding="utf-8")

    assert "not yet run" in template
    assert "code.run stays unregistered" in template
    assert "python -m freya.sandbox probe" in template
    assert "T1" not in template and "T2" not in template
    assert not FORBIDDEN.search(template)
    assert "docs/results/sandbox-probes.md" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def _refuse(*_args, **_kwargs):
    raise AssertionError("a drive attach was attempted")
