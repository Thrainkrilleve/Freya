"""Validate advisory scanner evidence before publishing counts.

A successful scan with findings stays advisory. Missing, malformed, empty or
incomplete scan evidence fails CI. Summaries omit source excerpts and messages.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {label}")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise ValueError(f"Invalid {label}")
    return value


def bandit_summary(data):
    data = _object(data, "Bandit report")
    errors = _list(data.get("errors"), "Bandit errors")
    if errors:
        raise ValueError("Bandit skipped files or encountered scan errors")
    totals = _object(_object(data.get("metrics"), "Bandit metrics").get("_totals"),
                     "Bandit totals")
    loc = totals.get("loc")
    if not isinstance(loc, int) or isinstance(loc, bool) or loc <= 0:
        raise ValueError("Bandit did not scan any source lines")
    counts = {level: 0 for level in ("HIGH", "MEDIUM", "LOW")}
    lines = ["## Bandit findings (advisory)", ""]
    for item in _list(data.get("results"), "Bandit results"):
        item = _object(item, "Bandit finding")
        severity = item.get("issue_severity")
        if severity not in counts:
            raise ValueError("Unknown Bandit severity")
        counts[severity] += 1
        # JSON quoting prevents embedded newlines from creating log commands.
        location = json.dumps(item.get("filename", ""), ensure_ascii=True)
        rule = json.dumps(item.get("test_id", ""), ensure_ascii=True)
        lines.append(f"- {severity}: {rule} at {location}:{item.get('line_number', 0)}")
    lines.extend(f"- {level}: {count}" for level, count in counts.items())
    lines.append(f"- Scanned source lines: {loc}")
    return "\n".join(lines) + "\n"


def sarif_summary(data):
    data = _object(data, "SARIF report")
    if data.get("version") != "2.1.0":
        raise ValueError("Unsupported SARIF version")
    runs = _list(data.get("runs"), "SARIF runs")
    if not runs:
        raise ValueError("SARIF contains no analysis runs")
    lines = ["## CodeQL findings (advisory)", ""]
    count = 0
    for run in runs:
        run = _object(run, "SARIF run")
        driver = _object(_object(run.get("tool"), "SARIF tool").get("driver"),
                         "SARIF driver")
        if not driver.get("name"):
            raise ValueError("SARIF has no tool identity")
        for invocation in _list(run.get("invocations", []), "SARIF invocations"):
            invocation = _object(invocation, "SARIF invocation")
            if invocation.get("executionSuccessful") is False:
                raise ValueError("SARIF analysis invocation failed")
        for result in _list(run.get("results"), "SARIF results"):
            result = _object(result, "SARIF result")
            count += 1
            rule = json.dumps(result.get("ruleId", ""), ensure_ascii=True)
            locations = _list(result.get("locations", []), "SARIF locations")
            if locations:
                physical = _object(_object(locations[0], "SARIF location").get(
                    "physicalLocation", {}), "SARIF physical location")
                artifact = _object(physical.get("artifactLocation", {}), "SARIF artifact")
                region = _object(physical.get("region", {}), "SARIF region")
                uri = json.dumps(artifact.get("uri", ""), ensure_ascii=True)
                lines.append(f"- {rule} at {uri}:{region.get('startLine', 0)}")
            else:
                lines.append(f"- {rule} (no source location)")
    lines.append(f"- Total findings: {count}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scanner", choices=("bandit", "codeql"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--fail-on-high", action="store_true",
                        help="Fail if a validated Bandit report contains HIGH findings")
    args = parser.parse_args()
    try:
        files = ([args.path] if args.scanner == "bandit"
                 else sorted(args.path.rglob("*.sarif")))
        if not files:
            raise ValueError("CodeQL did not produce SARIF results")
        summarizer = bandit_summary if args.scanner == "bandit" else sarif_summary
        reports = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        summary = "".join(summarizer(report) for report in reports)
        report_data = reports[0] if args.scanner == "bandit" else None
    except (OSError, ValueError, TypeError) as exc:
        # Do not expose report content or source snippets on parse failure.
        raise SystemExit("Invalid or incomplete scanner evidence") from exc
    print(summary)
    if args.fail_on_high:
        if args.scanner != "bandit":
            raise SystemExit("--fail-on-high requires a Bandit report")
        # Reuse the validated report and avoid reading mutable evidence twice.
        if any(item["issue_severity"] == "HIGH" for item in report_data["results"]):
            raise SystemExit("High-severity Bandit findings require remediation")
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with open(destination, "a", encoding="utf-8") as output:
            output.write(summary)


if __name__ == "__main__":
    main()
