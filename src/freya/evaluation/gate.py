"""
The promotion gate (NEW-CB, docs/design/skill-learning.md, a proposal).

Anything that would change Freya's behaviour on later turns is run
against Freya without it, on the same cases, model, fixtures and seed,
before it may take effect. Only the candidate's held-out cases decide;
the owner's bars are the pass marks; fewer than 15 attempts is
unmeasured. The gate reports; it switches nothing on. Approval and
rollback are the owner's (NEW-CC), built later.

Kinds gated in this version: a change to the tool hint (a prompt
change) and a change of conversation model (NEW-CF). Skills are added
with the skill format (NEW-BW, build step 2).

Run with python -m freya.evaluation.gate; never launched by Freya.
"""

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from freya.delegation.record import DEFAULT_BARS, MINIMUM_ATTEMPTS, validate_bars

KIND_HINT = "hint"
KIND_MODEL = "model"
KIND_SKILL = "skill"
KINDS = (KIND_HINT, KIND_MODEL)

BASELINE = "baseline"
CANDIDATE = "candidate"

PASSED = "passed"
FAILED = "failed"
UNMEASURED = "unmeasured"

# Same bound as a skill's steps (NEW-BW).
MAX_HINT_BYTES = 2000
MIN_CASES = 3
MAX_CASES = 10
# Over this share of trials with no reply, an arm says nothing about
# the candidate: the runtime was failing, not the change.
MAX_NO_REPLY_SHARE = 0.5

# What may train anything (NEW-CG): chosen by where it came from,
# never by what it says. Text that arrived through a tool is never one.
TRAINING_PROVENANCE = ("owner-turn", "skill-case", "gate-report")

NAME = re.compile(r"\A[a-z0-9][a-z0-9-]{0,59}\Z")
FIELDS = {"name", "kind", "value", "change", "cases", "held_out", "trained_on"}

# A worker's statuses that are a reply, graded or not.
REPLIED = ("passed", "failed")


@dataclass(frozen=True)
class Candidate:
    name: str
    kind: str
    change: Dict[str, str]
    value: str = "normal"
    cases: Tuple[str, ...] = ()
    held_out: Tuple[str, ...] = ()
    trained_on: Tuple[Dict[str, str], ...] = field(default=())

    @property
    def drafting_cases(self) -> Tuple[str, ...]:
        return tuple(c for c in self.cases if c not in self.held_out)


def split_cases(name: str, cases: Sequence[str]) -> Tuple[str, ...]:
    """
    The held-out third of a candidate's cases, the same every time for
    the same name and cases: ordered by a hash, so neither the drafter
    nor the order cases were written in chooses which ones decide.
    """
    ordered = sorted(cases, key=lambda case: hashlib.sha256(f"{name}\0{case}".encode()).hexdigest())
    count = max(1, math.ceil(len(cases) / 3))
    return tuple(sorted(ordered[:count]))


def load_candidate(data, known_cases: Sequence[str]) -> Candidate:
    """A candidate from its file's JSON. Raises ValueError for anything else."""
    if not isinstance(data, dict):
        raise ValueError("A candidate is a JSON object.")
    unknown = set(data) - FIELDS
    if unknown:
        raise ValueError("Unknown candidate fields: " + ", ".join(sorted(unknown)) + ".")

    name = data.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError("A name is 1 to 60 lowercase letters, digits and dashes.")

    kind = data.get("kind")
    if kind == KIND_SKILL:
        raise ValueError("Skills are gated from build step 2 (NEW-BW); not yet.")
    if kind not in KINDS:
        raise ValueError("Kind is one of: " + ", ".join(KINDS) + ".")

    value = data.get("value", "normal")
    if value not in DEFAULT_BARS:
        raise ValueError("Value is one of: " + ", ".join(DEFAULT_BARS) + ".")

    change = data.get("change")
    if not isinstance(change, dict) or set(change) != {kind}:
        raise ValueError(f'The change is {{"{kind}": ...}} and nothing else.')
    text = change[kind]
    if not isinstance(text, str) or not text.strip():
        raise ValueError("The change is non-empty text.")
    if kind == KIND_HINT and len(text.encode("utf-8")) > MAX_HINT_BYTES:
        raise ValueError(f"A hint change is at most {MAX_HINT_BYTES} bytes.")

    cases = data.get("cases", [])
    if not isinstance(cases, list) or not all(isinstance(c, str) for c in cases):
        raise ValueError("Cases are a list of scenario names.")
    if len(set(cases)) != len(cases):
        raise ValueError("A case is listed twice.")
    missing = [c for c in cases if c not in known_cases]
    if missing:
        raise ValueError("Unknown cases: " + ", ".join(missing) + ".")
    if kind == KIND_MODEL and cases:
        # NEW-CF: a model is judged on the regression suite and every
        # approved skill's held-out cases, not on cases of its own.
        raise ValueError("A model change has no cases of its own.")
    if kind == KIND_HINT and not MIN_CASES <= len(cases) <= MAX_CASES:
        raise ValueError(f"A hint change has {MIN_CASES} to {MAX_CASES} cases.")

    held_out = data.get("held_out")
    if held_out is None:
        held_out = split_cases(name, cases) if cases else ()
    else:
        if not isinstance(held_out, list) or not held_out:
            raise ValueError("Held-out cases are a non-empty list.")
        if not set(held_out) < set(cases):
            raise ValueError("Held-out cases are some, not all, of the cases.")
        held_out = tuple(sorted(set(held_out)))

    trained_on = data.get("trained_on", [])
    if not isinstance(trained_on, list):
        raise ValueError("Trained-on is a list of sources.")
    for source in trained_on:
        if (not isinstance(source, dict) or set(source) != {"source", "provenance"}
                or not all(isinstance(v, str) and v for v in source.values())):
            raise ValueError('Each training source is {"source": ..., "provenance": ...}.')
        if source["provenance"] not in TRAINING_PROVENANCE:
            raise ValueError(
                "Only these may train anything (NEW-CG): "
                + ", ".join(TRAINING_PROVENANCE) + "."
            )

    return Candidate(
        name=name, kind=kind, change={kind: text}, value=value,
        cases=tuple(cases), held_out=tuple(held_out),
        trained_on=tuple(dict(s) for s in trained_on),
    )


def arms(candidate: Candidate, model: str) -> Dict[str, Dict[str, str]]:
    """What differs between the two runs: the candidate's change, nothing else."""
    baseline = {"model": model, "hint": ""}
    changed = dict(baseline)
    if candidate.kind == KIND_MODEL:
        changed["model"] = candidate.change[KIND_MODEL]
    else:
        changed["hint"] = candidate.change[KIND_HINT]
    return {BASELINE: baseline, CANDIDATE: changed}


Runner = Callable[[str, Dict[str, str], int], dict]


def run_gate(candidate: Candidate, runner: Runner, *, model: str, regression: Sequence[str],
             trials: int, base_seed: int, on_trial=None) -> List[dict]:
    """
    Every scenario of the regression suite and the candidate's cases,
    `trials` times, without the candidate and then with it. Trial i of
    a scenario uses the same seed in both arms, so the pair differs
    only in the candidate. Arms run one after the other, not
    alternately, so a model change does not reload models every trial.
    """
    scenarios = list(dict.fromkeys([*regression, *candidate.cases]))
    settings = arms(candidate, model)
    results = []
    for arm in (BASELINE, CANDIDATE):
        for scenario in scenarios:
            for index in range(trials):
                seed = base_seed + index
                outcome = runner(scenario, settings[arm], seed)
                outcome = dict(outcome)
                outcome.update(scenario=scenario, arm=arm, trial=index + 1, seed=seed)
                results.append(outcome)
                if on_trial is not None:
                    on_trial(outcome, results)
    return results


def tally(results: Sequence[dict], arm: str, scenarios: Sequence[str]) -> dict:
    chosen = [r for r in results if r["arm"] == arm and r["scenario"] in set(scenarios)]
    attempts = len(chosen)
    passed = sum(r["status"] == "passed" for r in chosen)
    no_reply = sum(r["status"] not in REPLIED for r in chosen)
    return {
        "attempts": attempts, "passed": passed, "no_reply": no_reply,
        "pass_rate": passed / attempts if attempts else None,
    }


def decide(results: Sequence[dict], candidate: Candidate, regression: Sequence[str],
           bars: Dict[str, float], partial_regression: bool = False) -> Tuple[str, List[str], dict]:
    """
    The verdict, its reasons and the counts it was made from.

    Passed means all of: enough attempts in both arms; no more than
    half of either arm's trials without a reply; at least as many
    regression passes with the candidate as without; and, when the
    candidate has cases, its held-out cases passed more often with it
    than without and at least as often as the owner's bar for its
    value. A partial regression suite can never pass.
    """
    bar = validate_bars(bars)[candidate.value]
    summary = {
        "regression": {arm: tally(results, arm, regression) for arm in (BASELINE, CANDIDATE)},
        "held_out": {arm: tally(results, arm, candidate.held_out) for arm in (BASELINE, CANDIDATE)},
        "bar": bar,
        "per_scenario": {},
    }
    for scenario in dict.fromkeys([*regression, *candidate.cases]):
        summary["per_scenario"][scenario] = {
            arm: "{passed}/{attempts}".format(**tally(results, arm, [scenario]))
            for arm in (BASELINE, CANDIDATE)
        }

    reasons = []
    unmeasured = []
    groups = [("regression suite", summary["regression"])]
    if candidate.held_out:
        groups.append(("held-out cases", summary["held_out"]))
    for label, counts in groups:
        for arm in (BASELINE, CANDIDATE):
            got = counts[arm]
            if got["attempts"] < MINIMUM_ATTEMPTS:
                unmeasured.append(
                    f"The {label} had {got['attempts']} attempts {'without' if arm == BASELINE else 'with'} "
                    f"the candidate; {MINIMUM_ATTEMPTS} are needed. Run more trials."
                )
            elif got["no_reply"] / got["attempts"] > MAX_NO_REPLY_SHARE:
                unmeasured.append(
                    f"Most {label} trials {'without' if arm == BASELINE else 'with'} the candidate "
                    f"got no reply ({got['no_reply']} of {got['attempts']}); the runtime was failing."
                )
    if unmeasured:
        return UNMEASURED, unmeasured, summary

    failed = []
    before, after = summary["regression"][BASELINE], summary["regression"][CANDIDATE]
    if after["pass_rate"] < before["pass_rate"]:
        failed.append(
            f"Regression suite: {after['passed']}/{after['attempts']} with the candidate, "
            f"against {before['passed']}/{before['attempts']} without."
        )
    else:
        reasons.append(
            f"Regression suite: {after['passed']}/{after['attempts']} with the candidate, "
            f"{before['passed']}/{before['attempts']} without."
        )
    if candidate.held_out:
        before, after = summary["held_out"][BASELINE], summary["held_out"][CANDIDATE]
        line = (f"Held-out cases: {after['passed']}/{after['attempts']} with the candidate, "
                f"{before['passed']}/{before['attempts']} without; the bar for {candidate.value} work is {bar:.0%}.")
        if after["pass_rate"] <= before["pass_rate"]:
            failed.append(line + " The candidate did not do better.")
        elif after["pass_rate"] < bar:
            failed.append(line + " The candidate is below the bar.")
        else:
            reasons.append(line)

    if failed:
        return FAILED, failed + reasons, summary
    if partial_regression:
        return UNMEASURED, ["Only part of the regression suite was run, so this cannot pass."] + reasons, summary
    return PASSED, reasons, summary


def describe(report: dict) -> str:
    """The report as a few lines the owner can read."""
    candidate = report["candidate"]
    lines = [
        f"Gate: {candidate['name']} ({candidate['kind']} change, {candidate['value']} value): "
        f"{report['verdict'].upper()}",
    ]
    lines += ["  " + reason for reason in report["reasons"]]
    changed = [
        f"  {name}: {counts[BASELINE]} without, {counts[CANDIDATE]} with"
        for name, counts in report["summary"]["per_scenario"].items()
        if counts[BASELINE] != counts[CANDIDATE]
    ]
    if changed:
        lines.append("Scenarios that changed:")
        lines += changed
    lines.append("This is a measurement. Nothing was switched on; promotion is the owner's.")
    return "\n".join(lines)


def worker_runner(*, embedding_model, background_model, request_timeout, timeout, scratch: Path) -> Runner:
    """A runner that runs each trial in a fresh workflow worker, as the workflow runner does."""
    from freya.child_environment import child_environment

    count = [0]

    def run(scenario, arm, seed):
        count[0] += 1
        result_path = scratch / f"trial-{count[0]}.json"
        command = [sys.executable, "-m", "freya.evaluation.workflow", "--worker",
                   "--scenario", scenario, "--model", arm["model"],
                   "--embedding-model", embedding_model,
                   "--request-timeout", str(request_timeout), "--seed", str(seed),
                   "--output", str(result_path)]
        if background_model:
            command += ["--background-model", background_model]
        if arm["hint"]:
            hint_path = scratch / f"hint-{count[0]}.txt"
            hint_path.write_text(arm["hint"], encoding="utf-8")
            command += ["--hint-file", str(hint_path)]
        started = time.monotonic()
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                       env=child_environment())
            if completed.returncode or not result_path.is_file():
                result = {"status": "error", "error": completed.stderr[-2000:]}
            else:
                result = json.loads(result_path.read_text(encoding="utf-8"))
        except subprocess.TimeoutExpired:
            result = {"status": "timeout", "error": "Trial exceeded its hard deadline; worker stopped."}
        result["elapsed_seconds"] = time.monotonic() - started
        return result

    return run


def owner_bars() -> Dict[str, float]:
    """The owner's bars (CloudSettings.bars); the starting bars if they cannot be read."""
    try:
        from freya.delegation.material import CloudSettings

        return validate_bars(CloudSettings().bars)
    except Exception:
        return dict(DEFAULT_BARS)


def default_trials(candidate: Candidate, regression: Sequence[str]) -> int:
    """Enough trials for 15 attempts on the smallest group that decides, and at least three."""
    smallest = min(len(candidate.held_out) or len(regression), len(regression))
    return max(3, math.ceil(MINIMUM_ATTEMPTS / max(1, smallest)))


def main(argv=None, runner_factory=None):
    from freya.config import MODEL, EMBEDDING_MODEL
    from freya.evaluation.scenarios import SCENARIOS

    known = [s.name for s in SCENARIOS]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidate", type=Path, required=True, help="The candidate's JSON file.")
    parser.add_argument("--output", type=Path, required=True, help="New JSON report; existing files are never overwritten.")
    parser.add_argument("--trials", type=int, help="Trials per scenario and arm; default enough for 15 attempts.")
    parser.add_argument("--seed", type=int, default=2026, help="Seed of trial 1; trial i uses seed + i - 1 in both arms.")
    parser.add_argument("--regression-scenario", action="append", choices=known,
                        help="Run only these regression scenarios; the verdict can then not be passed.")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    parser.add_argument("--background-model")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--request-timeout", type=float, default=60)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("The output file already exists; choose a new file.")
    if args.trials is not None and args.trials < 1:
        parser.error("--trials must be a positive integer.")
    try:
        candidate = load_candidate(json.loads(args.candidate.read_text(encoding="utf-8")), known)
    except (OSError, ValueError) as exc:
        print(f"The candidate was refused: {exc}", file=sys.stderr)
        return 2

    regression = args.regression_scenario or known
    partial = set(regression) != set(known)
    trials = args.trials or default_trials(candidate, regression)
    bars = owner_bars()
    settings = arms(candidate, args.model)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gate": "NEW-CB, docs/design/skill-learning.md (a proposal, not adopted)",
        "candidate": {**asdict(candidate), "drafting_cases": list(candidate.drafting_cases)},
        "arms": settings,
        # What a promotion would replace, so that it can be undone (NEW-CB rollback).
        "replaces": {candidate.kind: settings[BASELINE]["model" if candidate.kind == KIND_MODEL else "hint"]},
        "embedding_model": args.embedding_model,
        "background_model": args.background_model or args.model,
        "regression": list(regression), "partial_regression": partial,
        "trials": trials, "base_seed": args.seed, "bars": bars,
        "grading": "The workflow runner's narrow deterministic checks; human rubrics are not scored.",
        "results": [],
    }
    with args.output.open("x", encoding="utf-8") as output:
        def save():
            output.seek(0)
            json.dump(report, output, indent=2)
            output.truncate()
            output.flush()

        if runner_factory is None:
            from freya.evaluation.workflow import model_inventory

            try:
                report["model_inventory"] = model_inventory(
                    args.model, args.embedding_model, args.request_timeout, args.background_model,
                )
                if candidate.kind == KIND_MODEL:
                    model_inventory(settings[CANDIDATE]["model"], None, args.request_timeout)
            except Exception as exc:
                report["preflight_error"] = str(exc)
                save()
                print(f"The gate could not start: {exc}", file=sys.stderr)
                return 2

        def on_trial(outcome, results):
            report["results"] = results
            print(f"{outcome['arm']} {outcome['scenario']} {outcome['trial']}/{trials}: {outcome['status']}", flush=True)
            save()

        with tempfile.TemporaryDirectory(prefix="freya-gate-", dir=args.output.parent) as scratch:
            factory = runner_factory or (lambda folder: worker_runner(
                embedding_model=args.embedding_model, background_model=args.background_model,
                request_timeout=args.request_timeout, timeout=args.timeout, scratch=folder,
            ))
            results = run_gate(candidate, factory(Path(scratch)), model=args.model,
                               regression=regression, trials=trials, base_seed=args.seed,
                               on_trial=on_trial)
        verdict, reasons, summary = decide(results, candidate, regression, bars, partial)
        report.update(results=results, verdict=verdict, reasons=reasons, summary=summary)
        save()
    print(describe(report))
    return 0 if verdict == PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
