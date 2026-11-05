"""
Opt-in measurement of a decider at each decision point
(docs/design/local-decider.md, section 5).

Today the only decider is the rules Freya already uses, so this runs
anywhere and contacts nothing. When a model decider exists it is added
to DECIDERS and measured on the same examples, so the two can be
compared directly. Reports are refused rather than overwritten.
"""
import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

from freya.decider.contract import DecisionError, checked_decision
from freya.decider.points import POINTS
from freya.decider.laya_decider import LayaDecider
from freya.decider.rules import RulesDecider
from freya.evaluation.decision_sets import SETS, Example


DECIDERS = {"rules": RulesDecider, "laya": LayaDecider}
CALIBRATION_BINS = 10
SPLITS = ("all", "train", "held-out")


def _in_split(example: Example, split: str) -> bool:
    if split == "held-out":
        return example.held_out
    if split == "train":
        return not example.held_out
    return True


def decide_all(decider, examples: Sequence[Example]) -> List[dict]:
    rows = []

    for example in examples:
        point = POINTS[example.point]
        started = time.perf_counter()
        try:
            distribution = decider.decide(point, example.text)
            elapsed = (time.perf_counter() - started) * 1000
            decision = checked_decision(point, distribution, decider.decider_id, elapsed)
        except DecisionError as exc:
            rows.append({"text": example.text, "label": example.label, "error": str(exc)})
            continue
        except Exception as exc:
            # A decider that failed is an error, kept apart from a wrong answer.
            rows.append({"text": example.text, "label": example.label, "error": type(exc).__name__})
            continue
        rows.append({
            "text": example.text,
            "label": example.label,
            "held_out": example.held_out,
            "answer": decision.answer,
            "abstained": decision.abstained,
            "probability": decision.probability,
            "distribution": dict(decision.distribution),
            "elapsed_ms": decision.elapsed_ms,
        })

    return rows


def _percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)]


def expected_calibration_error(answered: Sequence[dict]) -> float:
    """
    How far stated probabilities are from how often the answer was
    right, weighted by how many answers fall in each band.
    """
    if not answered:
        return None
    total = 0.0
    for band in range(CALIBRATION_BINS):
        low, high = band / CALIBRATION_BINS, (band + 1) / CALIBRATION_BINS
        rows = [
            r for r in answered
            if low < r["probability"] <= high or (band == 0 and r["probability"] == 0)
        ]
        if rows:
            confidence = statistics.fmean(r["probability"] for r in rows)
            accuracy = statistics.fmean(r["answer"] == r["label"] for r in rows)
            total += len(rows) / len(answered) * abs(confidence - accuracy)
    return total


def brier(rows: Sequence[dict]) -> float:
    """Mean squared distance between the distribution and the right answer."""
    scored = [r for r in rows if r.get("distribution")]
    if not scored:
        return None
    return statistics.fmean(
        sum((p - (1.0 if option == r["label"] else 0.0)) ** 2 for option, p in r["distribution"].items())
        for r in scored
    )


def summarize(rows: Sequence[dict]) -> dict:
    usable = [r for r in rows if "error" not in r]
    answered = [r for r in usable if not r["abstained"]]
    right = [r for r in answered if r["answer"] == r["label"]]
    times = [r["elapsed_ms"] for r in usable]
    return {
        "examples": len(rows),
        "errors": len(rows) - len(usable),
        "answered": len(answered),
        "abstained": len(usable) - len(answered),
        "right": len(right),
        # Right out of the answers it gave; abstentions go to the fallback.
        "accuracy_when_answering": len(right) / len(answered) if answered else None,
        "coverage": len(answered) / len(usable) if usable else None,
        "ece": expected_calibration_error(answered),
        "brier": brier(usable),
        "p50_ms": _percentile(times, 0.5),
        "p95_ms": _percentile(times, 0.95),
    }


def measure(decider, points: Sequence[str], split: str) -> Dict[str, dict]:
    results = {}
    for name in points:
        examples = [e for e in SETS[name] if _in_split(e, split)]
        rows = decide_all(decider, examples)
        results[name] = {"summary": summarize(rows), "rows": rows}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure a decider on synthetic decision sets.")
    parser.add_argument("--list", action="store_true", help="List the decision points and their sets.")
    parser.add_argument("--decider", choices=sorted(DECIDERS), default="rules")
    parser.add_argument("--point", action="append", choices=sorted(SETS))
    parser.add_argument("--split", choices=SPLITS, default="held-out")
    parser.add_argument("--output", type=Path, help="New JSON report; existing files are never overwritten.")
    args = parser.parse_args(argv)
    points = args.point or sorted(SETS)

    if args.list:
        for name in points:
            examples = SETS[name]
            held = sum(e.held_out for e in examples)
            print(f"{name}: {len(examples)} examples, {held} held out")
        return 0
    if args.output is not None and args.output.exists():
        parser.error("The output file already exists; choose a new file.")

    decider = DECIDERS[args.decider]()

    if not decider.ready():
        print(
            f"The {args.decider} decider is not installed. "
            f"Set it up with: python -m freya.decider.install {args.decider}",
            file=sys.stderr,
        )
        return 2

    results = measure(decider, points, args.split)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "decider": decider.decider_id,
        "split": args.split,
        "labels": "Synthetic, written by hand; not yet checked by the owner.",
        "points": results,
    }

    if args.output is not None:
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2)

    stop = getattr(decider, "stop", None)
    if stop is not None:
        stop()

    for name, result in results.items():
        s = result["summary"]
        accuracy = "n/a" if s["accuracy_when_answering"] is None else f"{s['accuracy_when_answering']:.0%}"
        print(
            f"{name}: right {s['right']}/{s['answered']} answered ({accuracy}), "
            f"abstained {s['abstained']}, errors {s['errors']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
