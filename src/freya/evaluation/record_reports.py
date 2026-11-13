"""Take finished delegation reports into the model record Freya reads."""

import argparse
import json
from pathlib import Path
import sys

from freya.evaluation.delegation import summarize
from freya.evaluation.delegation_tasks import TASKS, suite_version


CUT_SHORT = "The reply was cut short."

# A report keeps at most this much of each reply (see `run_task`).
STORED_REPLY_LIMIT = 4000


def regrade(report) -> dict:
    """
    The same report with every stored reply graded by the checks as
    they stand now, so that figures taken under older, looser checks
    never reach the record. Replies are read, never executed.

    Only the stored part of a reply can be graded again. One that
    filled the stored length is graded on that part, as the published
    figures were; `graded_on_stored_part` counts them.
    """
    attempts = report.get("attempts")

    if not isinstance(attempts, list) or not attempts:
        raise ValueError("The report has no attempts to grade.")

    suite = report.get("suite")

    if suite and suite != suite_version():
        raise ValueError(
            "The report was measured on a different set of tasks, so "
            "today's checks do not apply to it."
        )

    tasks = {task.name: task for task in TASKS}
    graded, kept = [], 0

    for attempt in attempts:
        task = tasks.get(attempt.get("task"))

        if task is None or task.kind != attempt.get("kind"):
            raise ValueError(
                f"The report has a task that is not in the set: "
                f"{attempt.get('task')!r}."
            )

        attempt = dict(attempt)

        if attempt.get("status") != "error":
            reply = attempt.get("reply") or ""

            if len(reply) >= STORED_REPLY_LIMIT:
                kept += 1

            problem = task.grade(reply)

            if problem is None and attempt.get("problem") == CUT_SHORT:
                problem = CUT_SHORT

            attempt["status"] = "failed" if problem else "passed"
            attempt["problem"] = problem

        graded.append(attempt)

    return {
        **report,
        "attempts": graded,
        "summary": summarize(graded),
        "graded_on_stored_part": kept,
    }


def main(argv=None):
    from freya.delegation.record import MODEL_RECORD_PATH, ModelRecord

    parser = argparse.ArgumentParser(
        description=(
            "Grade finished delegation reports again with today's checks "
            "and put their counts in the model record. Contacts nothing."
        ),
    )
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--record", type=Path, default=MODEL_RECORD_PATH)
    args = parser.parse_args(argv)

    record = ModelRecord(args.record)
    failed = False

    for path in args.reports:
        try:
            report = regrade(json.loads(path.read_text(encoding="utf-8")))
            record.add_report(report)
        except (OSError, ValueError) as exc:
            failed = True
            print(f"{path.name}: not recorded. {exc}", file=sys.stderr)
            continue

        passed = sum(row["passed"] for row in report["summary"].values())
        attempts = sum(row["attempts"] for row in report["summary"].values())
        settings = report.get("settings") or {}
        print(
            f"{path.name}: {report.get('model') or report.get('route')}"
            f" thinking {settings.get('think', 'n/a')},"
            f" {passed} of {attempts} passed."
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
