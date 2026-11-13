"""Additional fail-closed broker policy and provider boundary checks."""
import unittest
from unittest.mock import Mock
from freya.core.capabilities import CapabilityBroker, CapabilityRegistry
from freya.core.capabilities.capability import CapabilityDefinition, CapabilityStatus
from freya.core.policy.engine import PolicyDecision


class BrokerP0AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.registry = CapabilityRegistry()
        self.calls = []
        self.registry.register(CapabilityDefinition(
            name="development.test.sensitive",
            description="Mock protected action",
            provider="mock",
            category="development",
            handler=lambda: self.calls.append("executed"),
            status=CapabilityStatus.AVAILABLE,
        ))
        self.policy = Mock()
        self.audit = Mock()
        self.broker = CapabilityBroker(self.registry, self.policy, self.audit)

    def test_revoked_grant_denies_next_invocation(self):
        self.policy.decide.side_effect = [
            PolicyDecision(True, "Granted"),
            PolicyDecision(False, "Revoked"),
        ]
        self.assertEqual(self.broker.invoke("development.test.sensitive").outcome, "succeeded")
        self.assertEqual(self.broker.invoke("development.test.sensitive").outcome, "denied")
        self.assertEqual(self.calls, ["executed"])

    def test_policy_returns_error_denies(self):
        self.policy.decide.side_effect = RuntimeError("database unavailable")
        self.assertEqual(self.broker.invoke("development.test.sensitive").outcome, "denied")
        self.assertEqual(self.calls, [])

    def test_pre_dispatch_audit_failure_denies(self):
        self.policy.decide.return_value = PolicyDecision(True, "Granted")
        self.audit.record.side_effect = OSError("disk full")
        self.assertEqual(self.broker.invoke("development.test.sensitive").outcome, "denied")
        self.assertEqual(self.calls, [])

    def test_unavailable_capability_does_not_execute(self):
        self.policy.decide.return_value = PolicyDecision(True, "Granted")
        self.registry.set_status("development.test.sensitive", CapabilityStatus.UNAVAILABLE)
        self.assertEqual(self.broker.invoke("development.test.sensitive").outcome, "unavailable")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
