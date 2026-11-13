"""Cross-process journal transitions cannot both commit terminal updates."""
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from coding_task_journal import TaskJournal


def attempt_terminal(args):
    directory, task_id = args
    try:
        TaskJournal(Path(directory), task_id).transition("failed")
        return "committed"
    except ValueError:
        return "rejected"


class JournalProcessLockTests(unittest.TestCase):
    def test_competing_processes_preserve_one_terminal_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = TaskJournal.create(Path(tmp))
            journal.transition("generating")
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(processes=2) as pool:
                results = pool.map(attempt_terminal, [(tmp, journal.task_id)] * 2)
            self.assertEqual(sorted(results), ["committed", "rejected"])
            self.assertEqual([event.state for event in journal.read()],
                             ["requested", "generating", "failed"])


if __name__ == "__main__":
    unittest.main()
