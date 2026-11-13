import tempfile
import unittest
from pathlib import Path

from coding_task_journal import TaskJournal


class TaskJournalTests(unittest.TestCase):
    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal.create(Path(directory))
            for state in ("generating", "proposed", "verified"):
                journal.transition(state)
            self.assertEqual([event.state for event in journal.read()],
                             ["requested", "generating", "proposed", "verified"])

    def test_reject_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal.create(Path(directory))
            with self.assertRaises(ValueError):
                journal.transition("verified")

    def test_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal.create(Path(directory))
            journal.transition("rejected")
            with self.assertRaises(ValueError):
                journal.transition("generating")

    def test_corrupt_history(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal.create(Path(directory))
            journal.path.write_text("[]")
            with self.assertRaises(ValueError):
                journal.read()


if __name__ == "__main__":
    unittest.main()
