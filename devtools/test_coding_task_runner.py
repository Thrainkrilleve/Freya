"""Offline tests: no Ollama service required."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from coding_task_journal import TaskJournal
from coding_task_runner import propose


PATCH = "--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-print('old')\n+print('new')\n"


class CodingTaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "demo.py").write_text("print('old')\n")
        self.journals = self.root / "journals"
        self.patches = self.root / "patches"

    def run_proposal(self, generate):
        return propose(self.repo, "Change greeting", ["demo.py"], "test-model",
                       self.journals, self.patches, generate=generate)

    def test_proposal_is_saved_not_applied(self):
        result = self.run_proposal(lambda endpoint, model, prompt: PATCH)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(Path(result["patch"]).read_text(), PATCH)
        artifact_bytes = Path(result["patch"]).read_bytes()
        self.assertEqual(artifact_bytes, PATCH.encode("utf-8"))
        self.assertEqual(hashlib.sha256(artifact_bytes).hexdigest(), result["sha256"])
        self.assertEqual((self.repo / "demo.py").read_text(), "print('old')\n")
        self.assertEqual([event.state for event in TaskJournal(
            self.journals, result["task_id"]).read()],
            ["requested", "generating", "proposed", "verified"])

    def test_invalid_patch_marks_failed(self):
        with self.assertRaises(ValueError):
            self.run_proposal(lambda endpoint, model, prompt: "not a patch")
        journals = list(self.journals.glob("*.json"))
        self.assertEqual(len(journals), 1)
        self.assertEqual(TaskJournal(self.journals, journals[0].stem).read()[-1].state, "failed")
        self.assertFalse(list(self.patches.glob("*.patch")))

    def test_model_error_marks_failed(self):
        def fail(*args):
            raise RuntimeError("model unavailable")
        with self.assertRaises(RuntimeError):
            self.run_proposal(fail)
        journal_path = next(self.journals.glob("*.json"))
        self.assertEqual(TaskJournal(self.journals, journal_path.stem).read()[-1].state, "failed")

    def test_rejects_unselected_source_before_creating_task(self):
        with self.assertRaises(ValueError):
            propose(self.repo, "test", ["../secret"], "test-model",
                    self.journals, self.patches, generate=lambda *args: PATCH)
        self.assertFalse(self.journals.exists())


if __name__ == "__main__":
    unittest.main()
