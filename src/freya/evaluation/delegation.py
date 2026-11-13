"""Opt-in measurement of how well a model does delegated work, by kind of task."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
import time

from freya.evaluation.delegation_tasks import (
    KINDS,
    TASKS,
    suite_version,
    tasks_of,
)


OLLAMA_HOST = "http://127.0.0.1:11434"

# The same standing instructions a cloud worker is given, so local and
# cloud models are measured on the same footing.
WORKER_INSTRUCTIONS = (
    "You are a worker model. Another AI system has given you one "
    "self-contained brief. Do exactly what the brief asks and reply "
    "with the result only. You have no tools and no files. If the "
    "brief cannot be done from what it contains, say what is missing."
)

CLOUD_APPROVAL_FLAG = "--owner-approved-cloud-run"


def local_route(model, request_timeout, context_tokens, output_tokens, think=None):
    """
    A route that asks an installed Ollama model. It contacts only the
    Ollama server on this machine and downloads nothing.
    """
    import ollama

    from freya.providers.model.ollama import runs_elsewhere

    # Pinned to this machine, with proxies ignored, like the provider.
    client = ollama.Client(
        host=OLLAMA_HOST, timeout=request_timeout, trust_env=False,
    )
    installed = {entry.model: entry.digest for entry in client.list().models}
    name = model if model in installed else f"{model}:latest"

    if name not in installed:
        raise RuntimeError(f"Required model is not installed: {model}")
    if runs_elsewhere(model):
        # A model Ollama runs on its own servers is a cloud model,
        # whatever address it is asked at.
        raise RuntimeError(
            f"{model} does not run on this machine, so it cannot be "
            "measured as a local model."
        )

    def ask(brief):
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": WORKER_INSTRUCTIONS},
                {"role": "user", "content": brief},
            ],
            options={"num_ctx": context_tokens, "num_predict": output_tokens},
            **({} if think is None else {"think": think}),
        )
        return {
            "text": response["message"]["content"] or "",
            "input_tokens": response.get("prompt_eval_count") or 0,
            "output_tokens": response.get("eval_count") or 0,
            "served_by": installed[name],
        }

    return ask


def claude_plan_route(model, request_timeout):
    """
    A route that asks Claude through the owner's subscription. Every
    brief leaves this machine and uses plan allowance.
    """
    from freya.providers.model.claude_plan import (
        ask_claude_plan,
        find_claude_program,
    )

    program = find_claude_program()

    if not program:
        raise RuntimeError("The Claude program was not found on this machine.")

    def ask(brief):
        reply = ask_claude_plan(
            brief, program, model=model, timeout=request_timeout,
        )
        return {
            "text": reply.text,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "served_by": reply.model,
            "truncated": reply.truncated,
        }

    return ask


def run_task(task, ask):
    """
    One attempt at one task. A route that fails is an error, which is
    kept apart from a reply that was wrong.
    """
    started = time.monotonic()

    try:
        answer = ask(task.brief)
    except Exception as exc:
        return {
            "task": task.name,
            "kind": task.kind,
            "hard": task.hard,
            "stretch": task.stretch,
            "status": "error",
            "problem": str(exc)[:500],
            "elapsed_seconds": time.monotonic() - started,
        }

    problem = task.grade(answer.get("text"))

    if problem is None and answer.get("truncated"):
        problem = "The reply was cut short."

    return {
        "task": task.name,
        "kind": task.kind,
        "hard": task.hard,
        "stretch": task.stretch,
        "status": "failed" if problem else "passed",
        "problem": problem,
        "reply": (answer.get("text") or "")[:4000],
        "input_tokens": answer.get("input_tokens", 0),
        "output_tokens": answer.get("output_tokens", 0),
        "served_by": answer.get("served_by"),
        "elapsed_seconds": time.monotonic() - started,
    }


def summarize(attempts):
    """
    Results by kind of task. Errors are counted but left out of the
    pass rate, which describes only the replies that were graded.
    """
    summary = {}

    for kind in KINDS:
        group = [a for a in attempts if a["kind"] == kind]
        graded = [a for a in group if a["status"] != "error"]

        if not group:
            continue

        passed = sum(a["status"] == "passed" for a in graded)
        summary[kind] = {
            "attempts": len(group),
            "graded": len(graded),
            "passed": passed,
            "errors": len(group) - len(graded),
            "pass_rate": passed / len(graded) if graded else None,
            "median_seconds": (
                statistics.median(a["elapsed_seconds"] for a in graded)
                if graded else None
            ),
            "mean_tokens": (
                statistics.mean(
                    a["input_tokens"] + a["output_tokens"] for a in graded
                )
                if graded else None
            ),
        }

    return summary


def positive_int(value):
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer.")
    return parsed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure a model on synthetic delegated tasks.",
    )
    parser.add_argument("--list", action="store_true", help="List the tasks and contact nothing.")
    parser.add_argument("--kind", action="append", choices=KINDS)
    parser.add_argument("--trials", type=positive_int, default=3)
    parser.add_argument("--model", help="An installed Ollama model, or with --claude-plan a Claude model name.")
    parser.add_argument("--claude-plan", action="store_true",
                        help="Ask Claude through the owner's subscription instead of a local model.")
    parser.add_argument(CLOUD_APPROVAL_FLAG, dest="cloud_approved", action="store_true",
                        help="The owner's go-ahead to send the synthetic tasks to a cloud model and use plan allowance.")
    parser.add_argument("--think", choices=["off", "on", "low", "medium", "high", "xhigh"],
                        help="A local model's thinking setting. Unset leaves the model's own default.")
    parser.add_argument("--request-timeout", type=float, default=180)
    parser.add_argument("--context-tokens", type=positive_int, default=8192)
    parser.add_argument("--output-tokens", type=positive_int, default=4096)
    parser.add_argument("--output", type=Path, help="New JSON report; existing files are never overwritten.")
    args = parser.parse_args(argv)

    selected = tasks_of(args.kind)

    if args.list:
        for task in selected:
            print(f"{task.kind}: {task.name}")
        return 0
    if args.output is None:
        parser.error("--output is required.")
    if args.output.exists():
        parser.error("The output file already exists; choose a new file.")
    if args.claude_plan and not args.cloud_approved:
        parser.error(
            "A cloud run sends the tasks to the vendor and uses plan "
            f"allowance. It needs the owner's approval: {CLOUD_APPROVAL_FLAG}."
        )
    if not args.claude_plan and not args.model:
        parser.error("--model is required for a local run.")

    route = "anthropic-plan" if args.claude_plan else "ollama-local"
    attempts = []
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "route": route,
        "model": args.model,
        # A result belongs to a model together with the settings that
        # were really applied. The cloud route takes none of these.
        "settings": {} if args.claude_plan else {
            "think": args.think or "default",
            "context_tokens": args.context_tokens,
            "output_tokens": args.output_tokens,
        },
        "suite": suite_version(),
        "trials": args.trials,
        "tasks": len(selected),
        "grading": "Deterministic checks on synthetic tasks; no reply is executed.",
        "attempts": attempts,
    }

    # Reserve the requested file before doing expensive model work.
    with args.output.open("x", encoding="utf-8") as output:
        try:
            ask = (
                claude_plan_route(args.model, args.request_timeout)
                if args.claude_plan
                else local_route(
                    args.model, args.request_timeout,
                    args.context_tokens, args.output_tokens,
                    {None: None, "off": False, "on": True}.get(args.think, args.think),
                )
            )
        except Exception as exc:
            report["preflight_error"] = str(exc)
            json.dump(report, output, indent=2)
            print(f"Evaluation could not start: {exc}", file=sys.stderr)
            return 2

        for task in selected:
            for trial in range(1, args.trials + 1):
                attempt = run_task(task, ask)
                attempt["trial"] = trial
                attempts.append(attempt)
                report["summary"] = summarize(attempts)
                # What actually answered: a local model's digest, or
                # the model a cloud route served. More than one means
                # the report measures no single model.
                served = sorted({
                    a["served_by"] for a in attempts if a.get("served_by")
                })
                report["identity"] = served[0] if len(served) == 1 else None
                report["mixed_models"] = len(served) > 1
                # Keep what is finished if a later task fails.
                output.seek(0)
                output.truncate()
                json.dump(report, output, indent=2)
                output.flush()
                print(f"{task.kind}/{task.name} {trial}/{args.trials}: {attempt['status']}")

    for kind, row in report.get("summary", {}).items():
        rate = "n/a" if row["pass_rate"] is None else f"{row['pass_rate']:.0%}"
        print(f"{kind}: {row['passed']}/{row['graded']} ({rate}), {row['errors']} errors")

    return 0 if all(a["status"] == "passed" for a in attempts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
