"""
Measure every decider candidate on the same decision sets and compare
them (docs/design/local-decider.md, section 5).

    python -m freya.evaluation.decider_bench --output work/deciders-first.json

Today's rules are always measured, as the baseline every model has to
beat. Laya is measured when its helper is installed, and each open
decision model when a local Ollaya has it (see
`python -m freya.decider.install candidates`). A candidate that is not
available is listed as skipped, not counted as wrong.

Each Ollaya model is loaded before its first timed decision and
unloaded after its last, so only one is in graphics memory at a time.
Chat models Ollama holds still take their share of the card, as they
would in use; the report says how much memory each decider took.

Labels are synthetic and not yet checked by the owner, so the result
ranks candidates for a closer look; it does not make one trusted
(proposed NEW-CQ).
"""
import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from freya.decider.laya_decider import LayaDecider
from freya.decider.rules import RulesDecider
from freya.decider.systemone_decider import CANDIDATES, SystemOneDecider
from freya.evaluation.decision_sets import SETS
from freya.evaluation.decisions import SPLITS, measure


BASELINES = ("rules", "laya")


class _Timed:
    """Keeps the server's own timing of each decision a decider made."""

    def __init__(self, decider):
        self._decider = decider
        self.eval_ms: List[float] = []

    @property
    def decider_id(self) -> str:
        return self._decider.decider_id

    def ready(self) -> bool:
        return self._decider.ready()

    def decide(self, point, state):
        result = self._decider.decide(point, state)
        found = getattr(self._decider, "last_eval_ms", None)
        if found is not None:
            self.eval_ms.append(found)
        return result


def _default_factories() -> Dict[str, object]:
    factories = {"rules": RulesDecider, "laya": LayaDecider}
    for key, candidate in CANDIDATES.items():
        factories[key] = (lambda c=candidate: SystemOneDecider(c))
    return factories


def overall(points: Dict[str, dict]) -> dict:
    """Pooled over every point: right out of answered, and coverage."""
    summaries = [p["summary"] for p in points.values()]
    answered = sum(s["answered"] for s in summaries)
    right = sum(s["right"] for s in summaries)
    usable = sum(s["examples"] - s["errors"] for s in summaries)
    errors = sum(s["errors"] for s in summaries)
    eces = [s["ece"] for s in summaries if s["ece"] is not None]
    briers = [s["brier"] for s in summaries if s["brier"] is not None]
    p50s = [s["p50_ms"] for s in summaries if s["p50_ms"] is not None]
    p95s = [s["p95_ms"] for s in summaries if s["p95_ms"] is not None]
    return {
        "answered": answered,
        "right": right,
        "errors": errors,
        "accuracy_when_answering": right / answered if answered else None,
        "coverage": answered / usable if usable else None,
        "mean_ece": statistics.fmean(eces) if eces else None,
        "mean_brier": statistics.fmean(briers) if briers else None,
        "worst_p50_ms": max(p50s) if p50s else None,
        "worst_p95_ms": max(p95s) if p95s else None,
    }


def run_one(name: str, decider, points: Sequence[str], split: str) -> dict:
    if not decider.ready():
        return {"name": name, "skipped": "not available"}

    entry = {"name": name}
    load = getattr(decider, "load", None)

    try:
        if load is not None:
            load()
        memory = getattr(decider, "memory", None)
        entry["memory"] = memory() if memory is not None else None
        timed = _Timed(decider)
        results = measure(timed, points, split)
    finally:
        stop = getattr(decider, "stop", None)
        if stop is not None:
            stop()

    entry["decider"] = decider.decider_id
    entry["points"] = results
    entry["overall"] = overall(results)
    if timed.eval_ms:
        entry["server_eval_ms_p50"] = statistics.median(timed.eval_ms)
    candidate = CANDIDATES.get(name)
    if candidate is not None:
        entry["candidate"] = {
            "model": candidate.name,
            "author": candidate.author,
            "licence": candidate.licence,
            "parameters": candidate.parameters,
            "ollaya_bench_score": candidate.bench_score,
        }
    return entry


def _cell(value, kind: str) -> str:
    if value is None:
        return "-"
    if kind == "pct":
        return f"{value:.0%}"
    if kind == "ms":
        return f"{value:.0f}"
    if kind == "gb":
        return f"{value / 1e9:.1f}"
    return f"{value:.3f}"


def table(entries: Sequence[dict]) -> str:
    header = (
        f"{'decider':<14} {'right':>9} {'acc':>5} {'cover':>6} {'ECE':>6} "
        f"{'Brier':>6} {'p50ms':>6} {'p95ms':>6} {'VRAM GB':>8}"
    )
    lines = [header, "-" * len(header)]

    for entry in entries:
        if "skipped" in entry:
            lines.append(f"{entry['name']:<14} skipped: {entry['skipped']}")
            continue
        o = entry["overall"]
        vram = (entry.get("memory") or {}).get("vram_bytes")
        lines.append(
            f"{entry['name']:<14} {o['right']:>4}/{o['answered']:<4} "
            f"{_cell(o['accuracy_when_answering'], 'pct'):>5} {_cell(o['coverage'], 'pct'):>6} "
            f"{_cell(o['mean_ece'], 'num'):>6} {_cell(o['mean_brier'], 'num'):>6} "
            f"{_cell(o['worst_p50_ms'], 'ms'):>6} {_cell(o['worst_p95_ms'], 'ms'):>6} "
            f"{_cell(vram, 'gb'):>8}"
            + (f"  ({o['errors']} errors)" if o["errors"] else "")
        )
    return "\n".join(lines)


def main(argv=None, factories: Optional[Dict[str, object]] = None) -> int:
    factories = factories or _default_factories()
    parser = argparse.ArgumentParser(description="Compare every decider candidate on Freya's decision sets.")
    parser.add_argument(
        "--decider", action="append", choices=sorted(factories),
        help="Measure only these (repeatable). Default: all of them.",
    )
    parser.add_argument("--point", action="append", choices=sorted(SETS))
    parser.add_argument("--split", choices=SPLITS, default="all")
    parser.add_argument("--output", type=Path, help="New JSON report; existing files are never overwritten.")
    args = parser.parse_args(argv)

    if args.output is not None and args.output.exists():
        parser.error("The output file already exists; choose a new file.")

    names = args.decider or list(factories)
    points = args.point or sorted(SETS)
    entries = []

    for name in names:
        print(f"Measuring {name}...", file=sys.stderr, flush=True)
        try:
            entries.append(run_one(name, factories[name](), points, args.split))
        except Exception as exc:
            # The type only: a message could quote the text decided about.
            entries.append({"name": name, "skipped": f"failed ({type(exc).__name__})"})

    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "split": args.split,
        "points": points,
        "labels": "Synthetic, written by hand; not yet checked by the owner.",
        "deciders": entries,
    }

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2)

    print(table(entries))
    print(
        "\nacc = right out of the answers given; cover = share not abstained "
        "(abstentions go to today's path). Labels are synthetic."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
