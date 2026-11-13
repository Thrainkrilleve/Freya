"""Journal crash residue must not block unrelated safe transitions."""
import tempfile
import unittest
from pathlib import Path

from coding_task_journal import TaskJournal


class JournalCrashRecoveryTests(unittest.TestCase):
    def test_stale_legacy_temp_does_not_block_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = TaskJournal.create(Path(tmp))
            stale = journal.path.with_suffix(".tmp")
            stale.write_text("incomplete")
            journal.transition("generating")
            self.assertEqual(journal.read()[-1].state, "generating")
            self.assertEqual(stale.read_text(), "incomplete")

    def test_unrelated_writer_temp_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = TaskJournal.create(Path(tmp))
            other = Path(tmp) / f"{journal.task_id}.other-writer.tmp"
            other.write_text("another writer")
            journal.transition("generating")
            self.assertEqual(other.read_text(), "another writer")


if __name__ == "__main__":
    unittest.main()
