"""Revocation after patch creation must remove the artifact."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_task_runner import propose
from coding_task_journal import TaskJournal


class PatchCleanupTests(unittest.TestCase):
    def test_revocation_after_patch_written_removes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "example.py").write_text("answer = 1\n")
            count = 0

            def checkpoint():
                nonlocal count
                count += 1
                if count == 4:
                    raise PermissionError("grant revoked")

            diff = "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-answer = 1\n+answer = 2\n"
            with self.assertRaises(PermissionError):
                propose(repo, "change answer", ["example.py"], "mock",
                        root / "journal", root / "patches",
                        generate=lambda *args: diff, checkpoint=checkpoint)
            self.assertEqual(list((root / "patches").glob("*.patch")), [])
            self.assertEqual((repo / "example.py").read_text(), "answer = 1\n")
            journal_files = list((root / "journal").glob("*.json"))
            self.assertEqual(len(journal_files), 1)

    def test_preexisting_artifact_survives_exclusive_creation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "example.py").write_text("answer = 1\n")
            journal = TaskJournal.create(root / "journal")
            outputs = root / "patches"
            outputs.mkdir()
            existing = outputs / f"{journal.task_id}.patch"
            original = b"Owner-created proposal\n"
            existing.write_bytes(original)
            diff = "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-answer = 1\n+answer = 2\n"
            with patch("coding_task_runner.TaskJournal.create", return_value=journal):
                with self.assertRaises(FileExistsError):
                    propose(repo, "change answer", ["example.py"], "mock",
                            root / "journal", outputs, generate=lambda *args: diff)
            self.assertEqual(existing.read_bytes(), original)
            self.assertEqual(journal.read()[-1].state, "failed")
            self.assertEqual((repo / "example.py").read_text(), "answer = 1\n")

    def test_preexisting_artifact_survives_generation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "example.py").write_text("answer = 1\n")
            journal = TaskJournal.create(root / "journal")
            outputs = root / "patches"
            outputs.mkdir()
            existing = outputs / f"{journal.task_id}.patch"
            existing.write_bytes(b"Owner-created proposal\n")

            def fail(*args):
                raise RuntimeError("Synthetic generation failure")

            with patch("coding_task_runner.TaskJournal.create", return_value=journal):
                with self.assertRaises(RuntimeError):
                    propose(repo, "change answer", ["example.py"], "mock",
                            root / "journal", outputs, generate=fail)
            self.assertEqual(existing.read_bytes(), b"Owner-created proposal\n")
            self.assertEqual(journal.read()[-1].state, "failed")


    def test_journal_failure_is_noted_on_the_original_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "example.py").write_text("answer = 1\n")

            def fail(*args):
                raise RuntimeError("Synthetic generation failure")

            real_transition = TaskJournal.transition

            def transition(journal, state, details=""):
                if state == "failed":
                    raise OSError("journal disk full")
                return real_transition(journal, state, details)

            with patch.object(TaskJournal, "transition", transition):
                with self.assertRaises(RuntimeError) as caught:
                    propose(repo, "change answer", ["example.py"], "mock",
                            root / "journal", root / "patches", generate=fail)
            self.assertIn("journal could not record", " ".join(caught.exception.__notes__))

if __name__ == "__main__":
    unittest.main()
