"""
python -m freya.sandbox status   is the WSL sandbox ready?
python -m freya.sandbox setup    set it up (owner action; see wsl_setup.py)
python -m freya.sandbox probe    run the section 6 tests against it

None of these registers the sandbox with Freya or adds a policy rule.
"""

import sys

from freya.sandbox.probes import run_probes
from freya.sandbox.wsl import WslContainerSandbox
from freya.sandbox.wsl_setup import run_setup


def main(argv=None, say=print) -> int:
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
        if not sandbox.ready():
            say("The sandbox is not ready. Run `python -m freya.sandbox setup` first.")
            return 1

        results = run_probes(sandbox, sandbox.distribution)
        for result in results:
            say(f"{result.number}. {'PASS' if result.passed else 'FAIL'}  {result.name}")
            if result.note:
                say(f"     {result.note}")

        failed = [r for r in results if not r.passed]
        say("")
        say("All checks passed." if not failed else f"{len(failed)} check(s) failed. Do not allow code.run.")
        return 0 if not failed else 1

    say(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
