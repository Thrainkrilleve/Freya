"""Cooperative in-flight authorization revocation tests."""
import tempfile
import unittest
from pathlib import Path

from coding_task_runner import propose


class RevokedDuringTask(RuntimeError):
    pass


class CodingRevocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "sample.py").write_text("answer = 1\n")

    def test_revoked_during_generation_prevents_patch_artifact(self):
        allowed = True
        def checkpoint():
            if not allowed:
                raise RevokedDuringTask("authorization revoked")
        def generate(endpoint, model, prompt):
            nonlocal allowed
            allowed = False
            return "--- a/sample.py\n+++ b/sample.py\n@@ -1 +1 @@\n-answer = 1\n+answer = 2\n"
        with self.assertRaises(RevokedDuringTask):
            propose(self.repo, "change answer", ["sample.py"], "mock",
                    self.root / "journals", self.root / "patches",
                    generate=generate, checkpoint=checkpoint)
        self.assertFalse(list((self.root / "patches").glob("*.patch")) if
                         (self.root / "patches").exists() else False)
        self.assertEqual((self.repo / "sample.py").read_text(), "answer = 1\n")

    def test_revoked_before_read_prevents_task_creation(self):
        def checkpoint():
            raise RevokedDuringTask("authorization revoked")
        with self.assertRaises(RevokedDuringTask):
            propose(self.repo, "change answer", ["sample.py"], "mock",
                    self.root / "journals", self.root / "patches",
                    checkpoint=checkpoint)
        self.assertFalse((self.root / "journals").exists())


if __name__ == "__main__":
    unittest.main()
