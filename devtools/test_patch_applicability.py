import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patch_applicability import check_patch

GOOD = "--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-old\n+new\n"


class ApplicabilityTests(unittest.TestCase):
    def test_checks_disposable_copy_not_original(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "demo.py").write_text("old\n")
            check_patch(repo, GOOD, {"demo.py"})
            self.assertEqual((repo / "demo.py").read_text(), "old\n")

    def test_rejects_context_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "demo.py").write_text("different\n")
            with self.assertRaises(ValueError):
                check_patch(repo, GOOD, {"demo.py"})

    def test_rejects_unselected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                check_patch(Path(directory), GOOD, {"other.py"})


if __name__ == "__main__":
    unittest.main()
