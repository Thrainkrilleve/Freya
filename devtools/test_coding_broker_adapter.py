"""Tests for registration; broker integration requires full Freya dependencies."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from freya.core.policy.engine import PolicyDecision
from freya.core.capabilities.broker import LOCAL_OWNER
from freya.core.capabilities.execution_context import enter_broker_context, exit_broker_context

from coding_broker_adapter import register_coding_proposal
from coding_capability_contract import CODING_PROPOSE


class CodingBrokerAdapterTests(unittest.TestCase):
    def test_registration_has_no_policy_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            plane = SimpleNamespace(providers=Mock(), capabilities=Mock(), policy=Mock())
            register_coding_proposal(
                plane, repo=repo, journal_dir=root / "journals",
                output_dir=root / "patches", model="test-model"
            )
            definition = plane.capabilities.register.call_args.args[0]
            self.assertEqual(definition.name, CODING_PROPOSE)
            self.assertFalse(definition.metadata["model_tool"])
            plane.policy.assert_not_called() if callable(plane.policy) else None
            self.assertFalse(plane.policy.method_calls)

    def test_policy_checkpoint_rechecks_trusted_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            plane = SimpleNamespace(providers=Mock(), capabilities=Mock())
            policy = Mock()
            policy.decide.return_value = PolicyDecision(False, "revoked")
            register_coding_proposal(
                plane, repo=repo, journal_dir=root / "journals",
                output_dir=root / "patches", model="test-model",
                policy=policy, actor="local-owner"
            )
            handler = plane.capabilities.register.call_args.args[0].handler
            token = enter_broker_context(LOCAL_OWNER, policy)
            try:
                with patch("coding_broker_adapter.propose") as proposal:
                    handler("test", ["example.py"])
            finally:
                exit_broker_context(token)
            callback = proposal.call_args.kwargs["checkpoint"]
            with self.assertRaises(PermissionError):
                callback()
            policy.decide.assert_called_once_with("local-owner", CODING_PROPOSE)

    def test_policy_requires_trusted_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            plane = SimpleNamespace(providers=Mock(), capabilities=Mock())
            with self.assertRaises(ValueError):
                register_coding_proposal(
                    plane, repo=repo, journal_dir=root / "journals",
                    output_dir=root / "patches", model="test-model",
                    policy=Mock()
                )

    def test_artifacts_must_be_outside_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            plane = SimpleNamespace(providers=Mock(), capabilities=Mock())
            with self.assertRaises(ValueError):
                register_coding_proposal(
                    plane, repo=repo, journal_dir=repo / "journals",
                    output_dir=repo / "patches", model="test-model"
                )


if __name__ == "__main__":
    unittest.main()
