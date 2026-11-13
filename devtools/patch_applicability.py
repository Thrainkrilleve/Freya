"""Check patch applicability against a disposable copy of selected source files.

This invokes trusted Git's patch parser, never runs code from the patch, and
never writes to the source repository. Not a general-purpose execution sandbox.

Git runs with an absolute executable path found on PATH (never the current
directory, never a script wrapper), a minimal environment that ignores the
user's and system Git configuration, no access to any repository above the
disposable directory, no standard input, and its output discarded.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from local_coding_agent import selected_source
from patch_validator import validate_unified_diff

IS_WINDOWS = os.name == "nt"
# Windows batch wrappers (git.cmd) run through cmd.exe, which reparses
# arguments; only a real executable is accepted.
GIT_NAMES = ("git.exe",) if IS_WINDOWS else ("git",)
CLEANUP_ATTEMPTS = 5


def find_git(search_path: str | None = None) -> Path:
    """Return an absolute path to Git from PATH, ignoring relative entries.

    Unlike a bare "git" or shutil.which, this never consults the current
    directory, where a planted executable could otherwise be picked up.
    """
    if search_path is None:
        search_path = os.environ.get("PATH", "")
    for entry in search_path.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        for name in GIT_NAMES:
            candidate = Path(entry) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    raise RuntimeError("Git patch validation unavailable: git was not found on PATH")


def git_environment(home: Path) -> dict[str, str]:
    """A minimal environment: no user, system, or repository configuration."""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        # Do not discover a repository above the disposable directory.
        "GIT_CEILING_DIRECTORIES": str(home.parent),
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    if IS_WINDOWS:
        # Windows programs may fail to start without these.
        for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
            if name in os.environ:
                environment[name] = os.environ[name]
        environment["USERPROFILE"] = str(home)
    return environment


def check_patch(repo: Path, patch: str, selected: set[str], *, timeout: float = 15,
                git: Path | None = None) -> None:
    validate_unified_diff(patch, selected)
    if not selected:
        raise ValueError("No files selected")
    if len({name.casefold() for name in selected}) != len(selected):
        # Two names for one file on a case-insensitive file system.
        raise ValueError("Selected paths differ only by case")
    executable = git if git is not None else find_git()
    if not executable.is_absolute():
        raise ValueError("Git executable must be an absolute path")

    root = Path(tempfile.mkdtemp(prefix="freya-patch-check-"))
    try:
        work = root / "work"
        work.mkdir()
        for relative in sorted(selected):
            # selected_source enforces containment, portability, and size.
            contents = selected_source(repo, relative)
            target = work / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8", newline="")
        patch_file = root / "proposal.patch"
        patch_file.write_text(patch, encoding="utf-8", newline="")
        _run_git_check(executable, patch_file, work, root, timeout)
    finally:
        _remove_tree(root)


def _run_git_check(executable: Path, patch_file: Path, work: Path, home: Path,
                   timeout: float) -> None:
    command = [str(executable), "apply", "--check", "--whitespace=error",
               "--", str(patch_file)]
    options = {}
    if IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(  # nosec B603 # fixed argv, absolute git path, no shell
            command, cwd=work, env=git_environment(home), timeout=timeout,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, **options,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Git patch validation timed out") from None
    except OSError:
        raise RuntimeError("Git patch validation unavailable") from None
    if result.returncode:
        raise ValueError("Patch does not apply cleanly to selected source files")


def _remove_tree(root: Path) -> None:
    """Delete the disposable copy, which holds the owner's source text.

    On Windows a just-killed process can hold a file briefly and read-only
    files cannot be deleted, so both are retried. If the copy still cannot
    be removed, that is reported rather than left silently.
    """
    def make_writable_and_retry(function, path, _):
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)

    for attempt in range(CLEANUP_ATTEMPTS):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(root, onexc=make_writable_and_retry)
            else:  # pragma: no cover - the project requires 3.12
                shutil.rmtree(root, onerror=make_writable_and_retry)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == CLEANUP_ATTEMPTS - 1:
                # Chained, not suppressed: an earlier error stays visible.
                raise RuntimeError(
                    f"Disposable patch check copy could not be removed: {root}"
                )
            time.sleep(0.1 * (attempt + 1))
