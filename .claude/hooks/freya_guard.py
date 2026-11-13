"""
Claude Code PreToolUse hook that enforces the working rules in CLAUDE.md.

Claude Code runs this before every file edit and shell command and passes the
tool call as JSON on stdin. Exit code 2 blocks the call and shows the message
on stderr to the model; exit code 0 lets it through.

What it refuses:
- edits to Freya's private data (anything under data/, and *.db files);
- hand edits to uv.lock (change dependencies, then run `uv lock`);
- edits to the vendored browser libraries, which must stay byte-exact;
- any change to an existing entry of MIGRATIONS in the control store
  (appending a new migration is allowed);
- `git commit` or `git push` on main, and force pushes;
- shell commands that name the memory or control databases, or delete data/;
- a `git commit` while the test suite fails (FREYA_SKIP_COMMIT_TESTS=1 skips
  this check for one session, for example when only docs changed).

Standard library only, so it runs under any Python without Freya installed.
The checks are a guard rail, not a security boundary: a determined command
can get around a text match. They exist so honest mistakes are caught.
"""

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO = Path(
    os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2]
)

MIGRATIONS_FILE = "src/freya/core/persistence/control_store.py"
VENDOR_DIR = "src/freya/gateway/static/vendor/"
DATABASE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")
PRIVATE_DATABASES = ("freya_memory.db", "freya_control.db")


def relative_path(file_path, repo=REPO):
    """
    The path relative to the repository, with forward slashes, or None when
    it is outside the repository.
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = Path(repo) / path
    try:
        relative = os.path.relpath(path.resolve(), Path(repo).resolve())
    except ValueError:
        # Different drive on Windows.
        return None
    relative = relative.replace(os.sep, "/")
    if relative == ".." or relative.startswith("../"):
        return None
    return relative


def _folded(text):
    # Windows paths are case-insensitive.
    return text.lower() if os.name == "nt" else text


def path_refusal(relative):
    """Why an edit to this repository path is refused, or None."""
    if relative is None:
        return None
    folded = _folded(relative)
    if folded == "data" or folded.startswith("data/"):
        return (
            "data/ holds Freya's private memories, audit log and settings. "
            "Agents never edit it (CLAUDE.md, Working rules)."
        )
    if folded.endswith(DATABASE_SUFFIXES):
        return "Database files are never edited by hand (CLAUDE.md, Working rules)."
    if folded == "uv.lock":
        return (
            "uv.lock is generated. Change pyproject.toml, then run `uv lock` "
            "and commit both files."
        )
    if folded.startswith(_folded(VENDOR_DIR)):
        return (
            "Vendored libraries are copied unmodified and checked by checksum "
            "(gateway/static/vendor/README.txt). Replace them only as a "
            "deliberate, reviewed upgrade."
        )
    return None


def _apply_edit(text, old, new, replace_all=False):
    if old not in text:
        return None
    if replace_all:
        return text.replace(old, new)
    return text.replace(old, new, 1)


def content_after(tool_name, tool_input, current):
    """
    The file's text after this tool call, or None when it cannot be worked
    out (the edit would fail on its own anyway).
    """
    if tool_name == "Write":
        return tool_input.get("content", "")
    if tool_name == "Edit":
        return _apply_edit(
            current,
            tool_input.get("old_string", ""),
            tool_input.get("new_string", ""),
            tool_input.get("replace_all", False),
        )
    if tool_name == "MultiEdit":
        text = current
        for edit in tool_input.get("edits", []):
            text = _apply_edit(
                text,
                edit.get("old_string", ""),
                edit.get("new_string", ""),
                edit.get("replace_all", False),
            )
            if text is None:
                return None
        return text
    return None


def migration_entries(source):
    """
    The MIGRATIONS list from the module source, or None when the module
    does not parse or has no literal MIGRATIONS list.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "MIGRATIONS"
            for target in node.targets
        ):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            if isinstance(value, list):
                return value
    return None


def migrations_refusal(before, after):
    """Why this change to the migrations module is refused, or None."""
    old = migration_entries(before)
    if old is None:
        # The file as it stands is already unusual; leave it to review.
        return None
    new = migration_entries(after)
    if new is None:
        return (
            "MIGRATIONS must stay a literal list of SQL strings in "
            f"{MIGRATIONS_FILE}."
        )
    if new[: len(old)] != old:
        return (
            "Shipped entries in MIGRATIONS must never change. Append a new "
            "migration at the end instead (CLAUDE.md, Working rules)."
        )
    return None


def check_edit(tool_name, tool_input, repo=REPO):
    """Why this file edit is refused, or None."""
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not file_path:
        return None
    relative = relative_path(file_path, repo)
    refusal = path_refusal(relative)
    if refusal:
        return refusal
    if relative is not None and _folded(relative) == _folded(MIGRATIONS_FILE):
        target = Path(repo) / MIGRATIONS_FILE
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        after = content_after(tool_name, tool_input, current)
        if after is not None:
            return migrations_refusal(current, after)
    return None


GIT_COMMIT = re.compile(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+commit\b")
GIT_PUSH = re.compile(r"\bgit\b(?:\s+-\S+(?:\s+\S+)?)*\s+push\b(?P<rest>[^;&|]*)")
FORCE = re.compile(r"(?:^|\s)(?:-f|--force(?:-with-lease)?)(?:\s|=|$)")
PUSH_TO_MAIN = re.compile(r"(?:^|[\s:])(?:refs/heads/)?main(?:\s|$)")
DELETE_DATA = re.compile(
    r"\b(?:rm|rmdir|del|erase|Remove-Item|rd)\b[^;&|]*?(?:^|[\s\"'/\\])data(?:[\s\"'/\\]|$)",
    re.IGNORECASE,
)


def check_command(command, branch):
    """Why this shell command is refused, or None. `branch` is the current one."""
    lowered = command.lower()
    for name in PRIVATE_DATABASES:
        if name in lowered:
            return (
                f"{name} holds Freya's private data. Agents do not open, "
                "print, copy or change it (CLAUDE.md, Working rules)."
            )
    if DELETE_DATA.search(command):
        return "Deleting data/ would destroy Freya's memories and audit log."
    if GIT_COMMIT.search(command) and branch == "main":
        return (
            "Never commit directly to main. Create a branch "
            "(`git switch -c <name>`) and open a pull request."
        )
    push = GIT_PUSH.search(command)
    if push:
        rest = push.group("rest")
        if FORCE.search(rest):
            return "Force pushes are not allowed; push a new commit instead."
        if PUSH_TO_MAIN.search(rest) or (branch == "main" and not rest.strip()):
            return "Never push to main. Push your branch and open a pull request."
    return None


def current_branch(repo=REPO):
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def project_python(repo=REPO):
    """The project's virtual-environment Python, falling back to this one."""
    for candidate in (
        Path(repo) / ".venv" / "Scripts" / "python.exe",
        Path(repo) / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def tests_refusal(repo=REPO):
    """Run the suite before a commit; why the commit is refused, or None."""
    if os.environ.get("FREYA_SKIP_COMMIT_TESTS") == "1":
        return None
    try:
        result = subprocess.run(
            [project_python(repo), "-m", "pytest", "-q", "-x", "--no-header"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=840,
        )
    except subprocess.TimeoutExpired:
        return "The test suite did not finish within 14 minutes; not committing."
    except OSError as error:
        return f"Could not run the test suite before committing: {error}"
    if result.returncode == 0:
        return None
    tail = "\n".join(result.stdout.strip().splitlines()[-25:])
    return f"Tests fail, so the commit was stopped. Fix them first.\n{tail}"


def main():
    try:
        call = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool_name = call.get("tool_name", "")
    tool_input = call.get("tool_input") or {}

    if tool_name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        refusal = check_edit(tool_name, tool_input)
    elif tool_name in ("Bash", "PowerShell"):
        command = tool_input.get("command", "")
        refusal = check_command(command, current_branch())
        if refusal is None and GIT_COMMIT.search(command):
            refusal = tests_refusal()
    else:
        refusal = None

    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
