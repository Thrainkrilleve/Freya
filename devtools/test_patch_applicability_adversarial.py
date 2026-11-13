"""Adversarial tests for the disposable `git apply --check` step.

Everything runs in temporary directories. Tests that need a fake Git written
as a shell script are POSIX-only; the logic they cover is also tested
cross-platform through patched subprocess calls where possible.
"""
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import patch_applicability
from patch_applicability import _remove_tree, check_patch, find_git, git_environment

GOOD = "--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n-old\n+new\n"
POSIX = os.name != "nt"
HAVE_GIT = True
try:
    find_git()
except RuntimeError:
    HAVE_GIT = False


class Workspace(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "demo.py").write_bytes(b"old\n")
        # Disposable copies go here, so leftovers can be counted.
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        redirect = patch.object(tempfile, "tempdir", str(self.scratch))
        redirect.start()
        self.addCleanup(redirect.stop)

    def leftovers(self):
        return list(self.scratch.iterdir())

    def assert_git_not_run(self, selected, patch_text=GOOD, error=ValueError):
        with patch("patch_applicability.subprocess.run") as run:
            with self.assertRaises(error):
                check_patch(self.repo, patch_text, selected)
        run.assert_not_called()
        self.assertEqual(self.leftovers(), [])
        self.assertEqual((self.repo / "demo.py").read_bytes(), b"old\n")

    def fake_git(self, body: str) -> Path:
        directory = self.root / "fakebin"
        directory.mkdir(exist_ok=True)
        script = directory / "git"
        script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
        script.chmod(0o755)
        return script


class MalformedPatchTests(Workspace):
    def test_malformed_patches_never_reach_git(self):
        for patch_text in (
            "",
            "not a diff\n",
            GOOD.replace("\n", "\r\n"),
            GOOD[:-1],
            GOOD + "\0",
            "diff --git a/demo.py b/demo.py\n" + GOOD,
            GOOD.replace("@@ -1 +1 @@", "@@ -1 +1,5 @@"),
            GOOD.replace("@@ -1 +1 @@", "@@ -x +1 @@"),
            "--- a/demo.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n",
            "--- a/demo.py\n+++ b/demo.py\nGIT binary patch\n",
            GOOD + GOOD,
            GOOD + "trailing prose\n",
            "--- a/demo.py\n+++ b/demo.py\n@@ -1 +1 @@\n" + "\\ No newline at end of file\n-old\n+new\n",
            "x" * 1_000_001 + "\n",
        ):
            with self.subTest(patch=patch_text[:50]):
                self.assert_git_not_run({"demo.py"}, patch_text)

    @unittest.skipUnless(HAVE_GIT, "git is not installed")
    def test_file_without_trailing_newline_can_be_checked(self):
        # Regression: git's single-backslash marker was compared with a
        # two-backslash literal, so these patches were always rejected.
        (self.repo / "demo.py").write_bytes(b"keep\nold")
        check_patch(self.repo, "--- a/demo.py\n+++ b/demo.py\n@@ -1,2 +1,2 @@\n keep\n-old\n"
                    "\\ No newline at end of file\n+new\n\\ No newline at end of file\n",
                    {"demo.py"})
        self.assertEqual((self.repo / "demo.py").read_bytes(), b"keep\nold")
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(HAVE_GIT, "git is not installed")
    def test_context_mismatch_is_rejected_and_copy_removed(self):
        (self.repo / "demo.py").write_bytes(b"different\n")
        with self.assertRaisesRegex(ValueError, "does not apply"):
            check_patch(self.repo, GOOD, {"demo.py"})
        self.assertEqual(self.leftovers(), [])


class TraversalTests(Workspace):
    def test_escaping_and_platform_specific_paths_are_rejected(self):
        for name in ("../demo.py", "a/../../demo.py", "/etc/passwd", "C:/demo.py",
                     "C:demo.py", "dir\\demo.py", "demo.py:stream", "NUL", "con.py",
                     "lpt1/x.py", "demo.py.", "demo.py ", "./demo.py", "a//demo.py",
                     ".git/config", ".ssh/id_rsa", "data/freya_memory.db", ".env"):
            with self.subTest(name=name):
                patch_text = GOOD.replace("demo.py", name)
                self.assert_git_not_run({name}, patch_text)

    def test_patch_for_an_unselected_file_is_rejected(self):
        self.assert_git_not_run({"other.py"})

    def test_names_differing_only_by_case_are_rejected(self):
        (self.repo / "Demo.py").write_bytes(b"old\n") if POSIX else None
        patch_text = GOOD + GOOD.replace("demo.py", "Demo.py")
        self.assert_git_not_run({"demo.py", "Demo.py"}, patch_text)

    @unittest.skipUnless(HAVE_GIT, "git is not installed")
    def test_symlinked_source_is_rejected(self):
        outside = self.root / "outside.py"
        outside.write_bytes(b"old\n")
        try:
            (self.repo / "link.py").symlink_to(outside)
            (self.repo / "linkdir").symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("this account cannot create symbolic links")
        for name in ("link.py", "linkdir/outside.py"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "Symbolic-link"):
                    check_patch(self.repo, GOOD.replace("demo.py", name), {name})
        self.assertEqual(outside.read_bytes(), b"old\n")
        self.assertEqual(self.leftovers(), [])


class ExecutableResolutionTests(Workspace):
    def test_missing_git_is_reported_and_copy_never_made(self):
        with patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            with self.assertRaisesRegex(RuntimeError, "not found"):
                check_patch(self.repo, GOOD, {"demo.py"})
        self.assertEqual(self.leftovers(), [])

    def test_relative_path_entries_and_current_directory_are_ignored(self):
        planted = self.root / "cwd"
        planted.mkdir()
        for name in patch_applicability.GIT_NAMES:
            fake = planted / name
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
        previous = os.getcwd()
        os.chdir(planted)
        try:
            for search_path in ("", ".", os.curdir + os.pathsep + "relative/bin"):
                with self.subTest(search_path=search_path):
                    with self.assertRaises(RuntimeError):
                        find_git(search_path)
        finally:
            os.chdir(previous)

    def test_relative_git_argument_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            check_patch(self.repo, GOOD, {"demo.py"}, git=Path("git"))

    @unittest.skipIf(POSIX, "Windows-only: batch wrappers")
    def test_batch_wrapper_is_not_accepted_on_windows(self):
        directory = self.root / "cmdbin"
        directory.mkdir()
        (directory / "git.cmd").write_text("@exit /b 0\n")
        with self.assertRaises(RuntimeError):
            find_git(str(directory))


class IsolationTests(Workspace):
    def test_environment_contains_nothing_inherited_but_path(self):
        with patch.dict(os.environ, {"FREYA_TEST_SECRET": "s3cret", "GIT_DIR": "/elsewhere",
                                     "GIT_CONFIG_PARAMETERS": "'core.hooksPath'='/x'"}):
            environment = git_environment(self.root / "home")
        self.assertNotIn("FREYA_TEST_SECRET", environment)
        self.assertNotIn("GIT_DIR", environment)
        self.assertNotIn("GIT_CONFIG_PARAMETERS", environment)
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["HOME"], str(self.root / "home"))

    @unittest.skipUnless(POSIX, "uses a shell-script stand-in for git")
    def test_git_runs_isolated_on_a_copy(self):
        report = self.root / "report"
        git = self.fake_git(f"""\
            {{ pwd; echo "$@"; env | sort; echo STDIN; cat; }} > '{report}'
            ls -la .. >> '{report}'
            exit 0
        """)
        with patch.dict(os.environ, {"FREYA_TEST_SECRET": "s3cret"}):
            check_patch(self.repo, GOOD, {"demo.py"}, git=git)
        text = report.read_text()
        cwd, args = text.splitlines()[:2]
        self.assertNotEqual(Path(cwd).resolve(), self.repo.resolve())
        self.assertTrue(cwd.startswith(str(self.scratch.resolve())) or cwd.startswith(str(self.scratch)))
        self.assertIn("apply --check --whitespace=error --", args)
        self.assertNotIn("s3cret", text)
        self.assertIn("GIT_CONFIG_NOSYSTEM=1", text)
        self.assertEqual(text.split("STDIN\n", 1)[1].split("total")[0].strip(), "")
        self.assertEqual(self.leftovers(), [])

    def test_git_output_is_never_captured_or_decoded(self):
        with patch("patch_applicability.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            check_patch(self.repo, GOOD, {"demo.py"}, git=Path(sys.executable).resolve())
        options = run.call_args.kwargs
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.DEVNULL)
        self.assertIs(options["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", options)


class TimeoutAndCleanupTests(Workspace):
    def test_timeout_is_reported_and_copy_removed(self):
        with patch("patch_applicability.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                check_patch(self.repo, GOOD, {"demo.py"}, git=Path(sys.executable).resolve())
        self.assertEqual(self.leftovers(), [])

    @unittest.skipUnless(POSIX, "uses a shell-script stand-in for git")
    def test_hanging_git_is_killed_at_the_timeout(self):
        git = self.fake_git("exec sleep 30\n")
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            check_patch(self.repo, GOOD, {"demo.py"}, git=git, timeout=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.leftovers(), [])

    def test_read_only_files_are_removed(self):
        directory = self.root / "readonly"
        (directory / "nested").mkdir(parents=True)
        target = directory / "nested" / "file.py"
        target.write_text("source")
        target.chmod(stat.S_IREAD)
        _remove_tree(directory)
        self.assertFalse(directory.exists())

    def test_cleanup_failure_is_reported_with_the_original_error(self):
        with (patch("patch_applicability.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("git", 1)),
              patch("patch_applicability.shutil.rmtree", side_effect=PermissionError("in use")),
              patch("patch_applicability.time.sleep")):
            with self.assertRaisesRegex(RuntimeError, "could not be removed") as caught:
                check_patch(self.repo, GOOD, {"demo.py"}, git=Path(sys.executable).resolve())
        chain = []
        error = caught.exception
        while error is not None:
            chain.append(str(error))
            error = error.__context__
        self.assertTrue(any("timed out" in message for message in chain), chain)

    def test_copy_removed_when_a_source_cannot_be_read(self):
        (self.repo / "bad.py").write_bytes(b"\xff\xfe not utf-8\n")
        with self.assertRaises(UnicodeDecodeError):
            check_patch(self.repo, GOOD.replace("demo.py", "bad.py"), {"bad.py"},
                        git=Path(sys.executable).resolve())
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
