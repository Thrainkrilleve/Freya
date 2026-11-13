"""Offline regression tests for the conservative developer permission profile."""
import unittest

from developer_profile import DEFAULT_PROFILE, DeveloperAction, DeveloperProfile


class DeveloperProfileTests(unittest.TestCase):
    def test_proposal_only(self):
        self.assertTrue(DEFAULT_PROFILE.allows(DeveloperAction.READ_SOURCE))
        self.assertTrue(DEFAULT_PROFILE.allows(DeveloperAction.PROPOSE_PATCH))

    def test_effects_disabled(self):
        for action in DeveloperAction:
            if action not in (DeveloperAction.READ_SOURCE, DeveloperAction.PROPOSE_PATCH):
                with self.subTest(action=action):
                    self.assertFalse(DEFAULT_PROFILE.allows(action))

    def test_unknown_action_denied(self):
        self.assertFalse(DEFAULT_PROFILE.allows("shell"))
        self.assertFalse(DEFAULT_PROFILE.allows(""))
        self.assertFalse(DEFAULT_PROFILE.allows("MERGE"))

    def test_immutable(self):
        with self.assertRaises(Exception):
            DEFAULT_PROFILE.name = "unsafe"

    def test_explicit_profile(self):
        profile = DeveloperProfile(permitted_actions=frozenset({DeveloperAction.READ_SOURCE}))
        self.assertFalse(profile.allows(DeveloperAction.PROPOSE_PATCH))

    def test_caller_owned_set_cannot_change_permissions(self):
        actions = {DeveloperAction.READ_SOURCE}
        profile = DeveloperProfile(permitted_actions=actions)
        actions.add(DeveloperAction.MERGE)
        self.assertFalse(profile.allows(DeveloperAction.MERGE))
        self.assertIsInstance(profile.permitted_actions, frozenset)

    def test_known_string_actions_are_normalized(self):
        profile = DeveloperProfile(permitted_actions=["read_source"])
        self.assertEqual(profile.permitted_actions, frozenset({DeveloperAction.READ_SOURCE}))
        self.assertFalse(profile.allows(DeveloperAction.PROPOSE_PATCH))

    def test_invalid_permissions_are_rejected(self):
        for actions in (None, 7, ["shell"], [object()], "read_source"):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                DeveloperProfile(permitted_actions=actions)


if __name__ == "__main__":
    unittest.main()
