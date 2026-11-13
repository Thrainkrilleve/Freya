import unittest
from patch_validator import validate_unified_diff

VALID = "--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-old\n+new\n"


class PatchValidatorTests(unittest.TestCase):
    def test_accept(self):
        validate_unified_diff(VALID, {"demo.py"})

    def test_rejects_unselected(self):
        with self.assertRaises(ValueError):
            validate_unified_diff(VALID, {"other.py"})

    def test_rejects_truncated_hunk(self):
        with self.assertRaises(ValueError):
            validate_unified_diff(VALID.rsplit("+new\n", 1)[0], {"demo.py"})

    def test_rejects_extra_hunk_content(self):
        with self.assertRaises(ValueError):
            validate_unified_diff(VALID + "+extra\n", {"demo.py"})

    def test_rejects_path_traversal(self):
        with self.assertRaises(ValueError):
            validate_unified_diff(VALID.replace("demo.py", "../demo.py"), {"../demo.py"})

    def test_rejects_multiple_headers_without_hunks(self):
        with self.assertRaises(ValueError):
            validate_unified_diff(VALID + "--- a/demo.py\n+++ b/demo.py\n", {"demo.py"})

    def test_rejects_markdown(self):
        with self.assertRaises(ValueError):
            validate_unified_diff("code:\n" + VALID, {"demo.py"})


if __name__ == "__main__":
    unittest.main()
