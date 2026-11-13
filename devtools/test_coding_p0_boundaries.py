"""P0 boundary and crash-recovery regression checks (no live model required)."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from coding_task_journal import TaskJournal
from local_coding_agent import selected_source
from patch_validator import validate_unified_diff


class CodingP0BoundaryTests(unittest.TestCase):
    def test_parallel_distinct_task_journals_remain_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def run(_):
                journal = TaskJournal.create(root)
                journal.transition("generating")
                journal.transition("failed", "simulated model failure")
                return journal.task_id, [event.state for event in journal.read()]
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(run, range(64)))
            self.assertEqual(len({task_id for task_id, _ in results}), 64)
            self.assertTrue(all(states == ["requested", "generating", "failed"]
                                for _, states in results))

    def test_stale_temporary_file_does_not_change_committed_history(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = TaskJournal.create(Path(directory))
            journal.transition("generating")
            journal.path.with_suffix(".tmp").write_text("incomplete")
            self.assertEqual([e.state for e in journal.read()],
                             ["requested", "generating"])
            journal.transition("failed")
            self.assertEqual([e.state for e in journal.read()],
                             ["requested", "generating", "failed"])
            self.assertEqual(journal.path.with_suffix(".tmp").read_text(), "incomplete")

    def test_nested_secret_and_key_files_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("nested/.env.production", "nested/token.pem",
                         ".aws/credentials", ".ssh/id_rsa", "nested/secrets.json"):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("example")
                with self.subTest(name=name), self.assertRaises(ValueError):
                    selected_source(root, name)

    def test_parent_traversal_rejected_even_if_inside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "safe.py").write_text("pass")
            (root / "sub").mkdir()
            with self.assertRaises(ValueError):
                selected_source(root, "sub/../safe.py")

    def test_symlinked_parent_directory_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "real").mkdir()
            (root / "real" / "source.py").write_text("pass")
            try:
                (root / "alias").symlink_to(root / "real", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks unavailable")
            with self.assertRaises(ValueError):
                selected_source(root, "alias/source.py")

    def test_patch_rejects_second_unselected_file(self):
        patch = ("--- a/safe.py\n+++ b/safe.py\n@@ -1 +1 @@\n-old\n+new\n"
                 "--- a/secret.py\n+++ b/secret.py\n@@ -1 +1 @@\n-old\n+new\n")
        with self.assertRaises(ValueError):
            validate_unified_diff(patch, {"safe.py"})


if __name__ == "__main__":
    unittest.main()
