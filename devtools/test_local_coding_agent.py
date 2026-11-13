import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from local_coding_agent import selected_source, validate_proposal, build_prompt


class LocalCodingAgentTests(unittest.TestCase):
    def test_selected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "safe.py").write_text("x = 1")
            self.assertEqual(selected_source(root, "safe.py"), "x = 1")
            with self.assertRaises(ValueError):
                selected_source(root, "../other.py")

    def test_reject_unselected_patch(self):
        patch = "--- a/secret.py\n+++ b/secret.py\n@@ -1 +1 @@\n-a\n+b\n"
        with self.assertRaises(ValueError):
            validate_proposal(patch, {"safe.py"})

    def test_valid_patch(self):
        patch = "--- a/safe.py\n+++ b/safe.py\n@@ -1 +1 @@\n-a\n+b\n"
        validate_proposal(patch, {"safe.py"})

    def test_prompt_contains_task(self):
        self.assertIn("fix bug", build_prompt("fix bug", [("a.py", "pass")]))

    def test_personal_storage_rejected_before_any_file_read(self):
        # Every path and payload here is synthetic; no owner storage is used.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("data/example.py", "nested/data/example.py",
                         "Data/example.py", "nested/DATA/example.py"):
                with self.subTest(name=name):
                    with patch.object(Path, "read_text", side_effect=AssertionError("read")) as read:
                        with self.assertRaises(ValueError):
                            selected_source(root, name)
                        read.assert_not_called()

    def test_nonstorage_source_with_data_in_filename_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data_model.py").write_text("pass", encoding="utf-8")
            self.assertEqual(selected_source(root, "data_model.py"), "pass")

    def test_crlf_source_is_refused_rather_than_silently_normalised(self):
        # A normalised read would let an LF patch pass the applicability
        # check against the copy and be marked verified, while failing
        # on the real CRLF file.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "windows.py").write_bytes(b"x = 1\r\ny = 2\r\n")
            with self.assertRaisesRegex(ValueError, "carriage returns"):
                selected_source(root, "windows.py")


if __name__ == "__main__":
    unittest.main()
