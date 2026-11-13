"""Adversarial offline checks for local coding proposal boundaries."""
import tempfile
import unittest
from pathlib import Path

from local_coding_agent import selected_source
from patch_validator import validate_unified_diff

BASE = "--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-old\n+new\n"


class CodingAdversarialTests(unittest.TestCase):
    def test_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actual.py").write_text("pass\n")
            link = root / "linked.py"
            try:
                link.symlink_to(root / "actual.py")
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks not available")
            with self.assertRaises(ValueError):
                selected_source(root, "linked.py")

    def test_rejects_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("API_KEY=example\n")
            with self.assertRaises(ValueError):
                selected_source(root, ".env")

    def test_rejects_real_carriage_return(self):
        with self.assertRaises(ValueError):
            validate_unified_diff(BASE.replace("\n", "\r\n"), {"demo.py"})

    def test_rejects_real_nul(self):
        with self.assertRaises(ValueError):
            validate_unified_diff(BASE.replace("old", "ol\x00d"), {"demo.py"})

    def test_rejects_hunk_count_mismatch(self):
        bad = BASE.replace("@@ -1 +1 @@", "@@ -1,2 +1,2 @@")
        with self.assertRaises(ValueError):
            validate_unified_diff(bad, {"demo.py"})


if __name__ == "__main__":
    unittest.main()
