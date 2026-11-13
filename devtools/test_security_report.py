import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from security_report import bandit_summary, sarif_summary


class SecurityReportTests(unittest.TestCase):
    def bandit(self):
        return {"errors": [], "metrics": {"_totals": {"loc": 12}}, "results": []}

    def sarif(self):
        return {"version": "2.1.0", "runs": [
            {"tool": {"driver": {"name": "CodeQL"}}, "results": []}]}

    def test_clean_bandit(self):
        self.assertIn("HIGH: 0", bandit_summary(self.bandit()))

    def test_bandit_findings_are_advisory_without_source_or_message(self):
        data = self.bandit()
        data["results"] = [{"issue_severity": "MEDIUM", "filename": "demo.py",
                            "test_id": "B108", "line_number": 3,
                            "code": "PRIVATE_SOURCE", "issue_text": "PRIVATE_MESSAGE"}]
        summary = bandit_summary(data)
        self.assertIn('B108', summary)
        self.assertIn("MEDIUM: 1", summary)
        self.assertNotIn("PRIVATE", summary)

    def test_bandit_rejects_scan_errors(self):
        data = self.bandit()
        data["errors"] = [{"filename": "demo.py", "reason": "parse error"}]
        with self.assertRaises(ValueError):
            bandit_summary(data)

    def test_bandit_rejects_empty_and_malformed_evidence(self):
        for data in ({}, [], {"errors": []}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                bandit_summary(data)
        for loc in (0, -1, True, "12"):
            data = self.bandit()
            data["metrics"]["_totals"]["loc"] = loc
            with self.subTest(loc=loc), self.assertRaises(ValueError):
                bandit_summary(data)

    def test_bandit_rejects_unknown_severity(self):
        data = self.bandit()
        data["results"] = [{"issue_severity": "unknown"}]
        with self.assertRaises(ValueError):
            bandit_summary(data)

    def test_clean_sarif(self):
        self.assertIn("Total findings: 0", sarif_summary(self.sarif()))

    def test_sarif_rejects_missing_runs_results_and_identity(self):
        clean = self.sarif()
        cases = [{}, {"version": "2.1.0", "runs": []}, copy.deepcopy(clean),
                 copy.deepcopy(clean), copy.deepcopy(clean)]
        del cases[2]["runs"][0]["results"]
        del cases[3]["runs"][0]["tool"]
        cases[4]["runs"][0]["results"] = {}
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                sarif_summary(data)

    def test_sarif_rejects_failed_invocation(self):
        data = self.sarif()
        data["runs"][0]["invocations"] = [{"executionSuccessful": False}]
        with self.assertRaises(ValueError):
            sarif_summary(data)

    def test_sarif_preserves_findings_without_source_or_message(self):
        data = self.sarif()
        data["runs"][0]["results"] = [{
            "ruleId": "py/example", "message": {"text": "PRIVATE_MESSAGE"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": "src/demo.py"},
                "region": {"startLine": 9, "snippet": {"text": "PRIVATE_SOURCE"}}}}]}]
        summary = sarif_summary(data)
        self.assertIn('"src/demo.py":9', summary)
        self.assertIn("Total findings: 1", summary)
        self.assertNotIn("PRIVATE", summary)

    def test_cli_missing_malformed_and_empty_reports_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            script = Path(__file__).with_name("security_report.py")
            for contents in (None, "{", "{}"):
                if contents is not None:
                    report.write_text(contents, encoding="utf-8")
                result = subprocess.run([sys.executable, str(script), "bandit", str(report)],
                                        capture_output=True, text=True, check=False,
                                        env={key: value for key, value in os.environ.items()
                                             if key != "GITHUB_STEP_SUMMARY"})
                self.assertNotEqual(result.returncode, 0)
            result = subprocess.run([sys.executable, str(script), "codeql", str(root)],
                                    capture_output=True, text=True, check=False,
                                        env={key: value for key, value in os.environ.items()
                                             if key != "GITHUB_STEP_SUMMARY"})
            self.assertNotEqual(result.returncode, 0)

    def test_cli_high_severity_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            script = Path(__file__).with_name("security_report.py")
            data = self.bandit()
            for severity, expected in (("LOW", 0), ("MEDIUM", 0), ("HIGH", 1)):
                data["results"] = [{"issue_severity": severity,
                                    "filename": "example.py", "test_id": "B999",
                                    "line_number": 1}]
                report.write_text(json.dumps(data), encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, str(script), "bandit", str(report),
                     "--fail-on-high"],
                    capture_output=True, text=True, check=False,
                    env={key: value for key, value in os.environ.items()
                         if key != "GITHUB_STEP_SUMMARY"})
                self.assertEqual(result.returncode != 0, bool(expected),
                                 result.stderr)

    def test_cli_valid_report_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_text(json.dumps(self.bandit()), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(Path(__file__).with_name("security_report.py")),
                 "bandit", str(report)], capture_output=True, text=True, check=False,
                                        env={key: value for key, value in os.environ.items()
                                             if key != "GITHUB_STEP_SUMMARY"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Scanned source lines: 12", result.stdout)


if __name__ == "__main__":
    unittest.main()
