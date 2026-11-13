"""Policy revocation and cancellation checks without live model calls."""
import unittest
from unittest.mock import Mock

from coding_authorization import CodingAuthorizationRevoked, policy_checkpoint
from coding_capability_contract import CODING_PROPOSE
from freya.core.policy.engine import PolicyDecision


class CodingAuthorizationTests(unittest.TestCase):
    def test_grant_revoked_between_checkpoints(self):
        policy = Mock()
        policy.decide.side_effect = [PolicyDecision(True, "allow"),
                                     PolicyDecision(False, "revoked")]
        checkpoint = policy_checkpoint(policy, "local-owner")
        checkpoint()
        with self.assertRaises(CodingAuthorizationRevoked):
            checkpoint()
        self.assertEqual(policy.decide.call_args.args, ("local-owner", CODING_PROPOSE))

    def test_policy_unavailable_fails_closed(self):
        policy = Mock()
        policy.decide.side_effect = OSError("policy unavailable")
        with self.assertRaises(CodingAuthorizationRevoked):
            policy_checkpoint(policy, "local-owner")()

    def test_explicit_cancellation_fails_closed(self):
        policy = Mock()
        checkpoint = policy_checkpoint(policy, "local-owner", cancelled=lambda: True)
        with self.assertRaises(CodingAuthorizationRevoked):
            checkpoint()
        policy.decide.assert_not_called()

    def test_cancellation_state_failure_fails_closed(self):
        policy = Mock()
        def broken():
            raise RuntimeError("cancel state unavailable")
        with self.assertRaises(CodingAuthorizationRevoked):
            policy_checkpoint(policy, "local-owner", cancelled=broken)()
        policy.decide.assert_not_called()

    def test_non_boolean_policy_allow_fails_closed(self):
        policy = Mock()
        policy.decide.return_value = PolicyDecision(1, "malformed")
        with self.assertRaises(CodingAuthorizationRevoked):
            policy_checkpoint(policy, "local-owner")()

    def test_missing_policy_decision_fails_closed(self):
        policy = Mock()
        policy.decide.return_value = None
        with self.assertRaises(CodingAuthorizationRevoked):
            policy_checkpoint(policy, "local-owner")()

    def test_missing_actor_rejected(self):
        with self.assertRaises(ValueError):
            policy_checkpoint(Mock(), "")


if __name__ == "__main__":
    unittest.main()
