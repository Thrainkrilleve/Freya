import unittest

from coding_capability_contract import (
    CODING_APPLY, CODING_MERGE, CODING_PROPOSE, CODING_VERIFY,
    assert_broker_authorized,
)


class CodingCapabilityContractTests(unittest.TestCase):
    def test_all_actions_are_distinct(self):
        self.assertEqual(len({CODING_PROPOSE, CODING_VERIFY, CODING_APPLY, CODING_MERGE}), 4)

    def test_denies_without_explicit_broker_decision(self):
        for value in (None, False, 1, "yes"):
            with self.subTest(value=value), self.assertRaises(PermissionError):
                assert_broker_authorized(broker_decision=value, capability=CODING_PROPOSE)

    def test_unknown_capability_denied(self):
        with self.assertRaises(ValueError):
            assert_broker_authorized(broker_decision=True, capability="development.root")

    def test_explicit_true_only(self):
        assert_broker_authorized(broker_decision=True, capability=CODING_VERIFY)


if __name__ == "__main__":
    unittest.main()
