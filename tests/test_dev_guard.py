"""
Tests for the Claude Code guard hook in .claude/hooks/freya_guard.py.

The hook is a guard rail for agents working on the repository, not part of
Freya. These tests pin what it refuses so a later edit cannot quietly widen it.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / ".claude" / "hooks" / "freya_guard.py"

spec = importlib.util.spec_from_file_location("freya_guard", GUARD_PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


MIGRATIONS_SOURCE = 'MIGRATIONS = [\n    """\n    CREATE TABLE a (x);\n    """,\n]\n'


@pytest.fixture
def repo(tmp_path):
    target = tmp_path / guard.MIGRATIONS_FILE
    target.parent.mkdir(parents=True)
    target.write_text(MIGRATIONS_SOURCE, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "data/freya_memory.db",
        "data/freya_settings.json",
        "data/avatar_models/x.vrm",
        "somewhere/else.db",
        "notes.sqlite3",
        "uv.lock",
        "src/freya/gateway/static/vendor/three/three.module.js",
    ],
)
def test_protected_paths_are_refused(repo, path):
    refusal = guard.check_edit("Write", {"file_path": str(repo / path), "content": "x"}, repo)
    assert refusal


@pytest.mark.parametrize(
    "path",
    ["src/freya/main.py", "docs/tasks/notes.md", "tests/test_x.py", "database_notes.md"],
)
def test_ordinary_paths_are_allowed(repo, path):
    assert guard.check_edit("Write", {"file_path": str(repo / path), "content": "x"}, repo) is None


def test_paths_outside_the_repository_are_not_judged(repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "data" / "x.db"
    assert guard.check_edit("Write", {"file_path": str(elsewhere), "content": "x"}, repo) is None


def test_relative_paths_resolve_against_the_repository(repo):
    assert guard.check_edit("Write", {"file_path": "data/x.json", "content": "x"}, repo)


def test_appending_a_migration_is_allowed(repo):
    edit = {
        "file_path": str(repo / guard.MIGRATIONS_FILE),
        "old_string": '    """,\n]',
        "new_string": '    """,\n    """\n    CREATE TABLE b (y);\n    """,\n]',
    }
    assert guard.check_edit("Edit", edit, repo) is None


def test_changing_a_shipped_migration_is_refused(repo):
    edit = {
        "file_path": str(repo / guard.MIGRATIONS_FILE),
        "old_string": "CREATE TABLE a (x);",
        "new_string": "CREATE TABLE a (x, z);",
    }
    assert "never change" in guard.check_edit("Edit", edit, repo)


def test_removing_a_migration_through_multi_edit_is_refused(repo):
    edit = {
        "file_path": str(repo / guard.MIGRATIONS_FILE),
        "edits": [{"old_string": "CREATE TABLE a (x);", "new_string": ""}],
    }
    assert guard.check_edit("MultiEdit", edit, repo)


def test_rewriting_the_module_without_a_literal_list_is_refused(repo):
    write = {
        "file_path": str(repo / guard.MIGRATIONS_FILE),
        "content": "MIGRATIONS = load_migrations()\n",
    }
    assert "literal list" in guard.check_edit("Write", write, repo)


def test_an_edit_that_would_not_apply_is_left_to_the_tool(repo):
    edit = {
        "file_path": str(repo / guard.MIGRATIONS_FILE),
        "old_string": "not in the file",
        "new_string": "anything",
    }
    assert guard.check_edit("Edit", edit, repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "sqlite3 data/freya_memory.db .dump",
        "python -c \"open('data/FREYA_CONTROL.DB')\"",
        "rm -rf data",
        "rm -rf ./data/",
        "Remove-Item -Recurse data",
        "git push --force origin feature",
        "git push -f",
        "git push origin main",
        "git push origin HEAD:main",
    ],
)
def test_dangerous_commands_are_refused(command):
    assert guard.check_command(command, "feature")


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git commit -m 'work'",
        "git push -u origin feature",
        "python -m pytest",
        "rm -rf build",
        "ls metadata",
        "git log --oneline main",
    ],
)
def test_ordinary_commands_are_allowed(command):
    assert guard.check_command(command, "feature") is None


def test_commit_and_bare_push_on_main_are_refused():
    assert guard.check_command("git commit -m x", "main")
    assert guard.check_command("git -C . commit -m x", "main")
    assert guard.check_command("git push", "main")


def test_skipping_the_commit_test_run(monkeypatch):
    monkeypatch.setenv("FREYA_SKIP_COMMIT_TESTS", "1")
    assert guard.tests_refusal() is None


def run_hook(payload):
    return subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"CLAUDE_PROJECT_DIR": str(ROOT), "PATH": ""},
        timeout=30,
    )


def test_the_hook_blocks_with_exit_code_2_and_a_reason():
    result = run_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(ROOT / "data" / "x.json"), "content": ""}}
    )
    assert result.returncode == 2
    assert "data/" in result.stderr


def test_the_hook_lets_ordinary_calls_through():
    result = run_hook({"tool_name": "Read", "tool_input": {"file_path": str(ROOT / "README.md")}})
    assert result.returncode == 0
    assert result.stderr == ""
