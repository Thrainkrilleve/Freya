"""Broker integration tests for the opt-in local coding proposal capability.

Run with: PYTHONPATH=src:devtools python -m unittest devtools/test_coding_broker_integration.py
On Windows PowerShell: $env:PYTHONPATH="src;devtools"
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from freya.core.capabilities import CapabilityBroker, CapabilityRegistry
from freya.core.policy.engine import PolicyDecision
from freya.core.providers import ProviderRegistry

from coding_broker_adapter import register_coding_proposal
from coding_capability_contract import CODING_PROPOSE


class CodingBrokerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "example.py").write_text("answer = 1\n", encoding="utf-8")
        self.providers = ProviderRegistry()
        self.capabilities = CapabilityRegistry(self.providers)
        self.policy = Mock()
        self.audit = Mock()
        self.plane = SimpleNamespace(providers=self.providers, capabilities=self.capabilities)
        register_coding_proposal(
            self.plane, repo=self.repo, journal_dir=root / "journal",
            output_dir=root / "patches", model="test-model"
        )
        self.broker = CapabilityBroker(self.capabilities, self.policy, self.audit)

    def test_direct_provider_execution_is_denied(self):
        with patch("coding_broker_adapter.propose") as generate:
            result = self.capabilities.execute(
                CODING_PROPOSE, "change answer", ["example.py"]
            )
        self.assertFalse(result.success)
        self.assertIn("broker dispatch", result.error)
        generate.assert_not_called()

    def test_broker_supplies_trusted_policy_checkpoint(self):
        self.policy.decide.return_value = PolicyDecision(True, "grant")
        with patch("coding_broker_adapter.propose", return_value={"state": "verified"}) as generate:
            result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])
        self.assertEqual(result.outcome, "succeeded")
        checkpoint = generate.call_args.kwargs["checkpoint"]
        self.policy.decide.return_value = PolicyDecision(False, "revoked")
        with self.assertRaises(PermissionError):
            checkpoint()
        self.assertEqual(self.policy.decide.call_args.args, ("local-owner", CODING_PROPOSE))

    def test_no_grant_denies_without_generating(self):
        self.policy.decide.return_value = PolicyDecision(False, "No grant")
        with patch("coding_broker_adapter.propose") as generate:
            result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])
        self.assertEqual(result.outcome, "denied")
        generate.assert_not_called()
        self.assertFalse(any(c.kwargs.get("kind") == "dispatch" for c in self.audit.record.call_args_list))

    def test_policy_failure_denies_without_generating(self):
        self.policy.decide.side_effect = RuntimeError("policy offline")
        with patch("coding_broker_adapter.propose") as generate:
            result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])
        self.assertEqual(result.outcome, "denied")
        generate.assert_not_called()

    def test_audit_dispatch_failure_denies_without_generating(self):
        self.policy.decide.return_value = PolicyDecision(True, "Explicit test grant")
        self.audit.record.side_effect = RuntimeError("audit offline")
        with patch("coding_broker_adapter.propose") as generate:
            result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])
        self.assertEqual(result.outcome, "denied")
        generate.assert_not_called()

    def test_granted_request_is_dispatched_and_audited(self):
        self.policy.decide.return_value = PolicyDecision(True, "Explicit test grant")
        expected = {"state": "verified", "task_id": "test", "patch": "unused"}
        with patch("coding_broker_adapter.propose", return_value=expected) as generate:
            result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])
        self.assertEqual(result.outcome, "succeeded")
        self.assertEqual(result.result, expected)
        generate.assert_called_once()
        kinds = [c.kwargs.get("kind") for c in self.audit.record.call_args_list]
        self.assertEqual(kinds, ["dispatch", "outcome"])
        self.policy.decide.assert_called_once()
        self.assertEqual(self.policy.decide.call_args.args[1], CODING_PROPOSE)


if __name__ == "__main__":
    unittest.main()
