"""Concurrent transitions must not overwrite committed task history."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from coding_task_journal import TaskJournal


class SameJournalConcurrencyTests(unittest.TestCase):
    def test_only_one_competing_terminal_transition_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = TaskJournal.create(Path(tmp))
            journal.transition("generating")

            def finish(state):
                try:
                    journal.transition(state)
                    return True
                except ValueError:
                    return False

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(finish, ["failed", "failed"]))
            self.assertEqual(sum(outcomes), 1)
            events = journal.read()
            self.assertEqual(len(events), 3)
            self.assertEqual([e.sequence for e in events], [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
