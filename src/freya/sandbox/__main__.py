"""
python -m freya.sandbox status   is the WSL sandbox ready?
python -m freya.sandbox setup    set it up (owner action; see wsl_setup.py)
python -m freya.sandbox probe    record which isolation this machine has

None of these registers the sandbox with Freya or adds a policy rule.
probe does not install software or change a system setting.
"""

import sys
from pathlib import Path

from freya.sandbox.probes import run_probes
from freya.sandbox.survey import (
    DEFAULT_OUTPUT,
    REASON_FAILED,
    build_report,
    containment_if_ready,
    live_facts,
    write_report,
)
from freya.sandbox.wsl import WslContainerSandbox
from freya.sandbox.wsl_setup import run_setup


def _output_path(argv):
    if "--output" not in argv:
        return DEFAULT_OUTPUT
    index = argv.index("--output")
    if index + 1 >= len(argv):
        return None
    return Path(argv[index + 1])


def main(argv=None, say=print, facts=None, probes=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    action = argv[0] if argv else "status"
    sandbox = WslContainerSandbox()

    if action == "setup":
        return 0 if run_setup(sandbox.distribution, say=say) else 1

    if action == "status":
        ready = sandbox.ready()
        say(f"WSL sandbox '{sandbox.distribution}': {'ready' if ready else 'not ready'}")
        return 0 if ready else 1

    if action == "probe":
        output = _output_path(argv)
        if output is None:
            say("probe needs a path after --output.")
            return 2

        if facts is None:
            facts = live_facts()
        if probes is None:
            probes = containment_if_ready(
                sandbox.ready,
                lambda: run_probes(
                    sandbox,
                    sandbox.distribution,
                    include_drive_attach=False,
                ),
            )

        report = build_report(facts, probes)
        write_report(report, output)
        say(f"Verified tier: {report.verified_tier}")
        say(report.reason)
        say("code.run stays unregistered.")
        say("Wrote the sandbox probe results.")
        return 1 if report.reason == REASON_FAILED else 0

    say(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
