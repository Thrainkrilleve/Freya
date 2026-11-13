"""
scripts/sync_agents.py gives every agent in agents.toml the shared rules and
skills. The first test keeps this repository's copies in step with the source;
the rest pin down what the script may and may not do to an agent's folder.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_agents", ROOT / "scripts" / "sync_agents.py")
sync_agents = importlib.util.module_from_spec(SPEC)
sys.modules["sync_agents"] = sync_agents  # dataclasses look the module up
SPEC.loader.exec_module(sync_agents)

REGISTRY = """
[source]
rules = "AGENTS.md"
skills = ".agents/skills"

[agents.claude]
enabled = true
rules_file = "CLAUDE.md"
skills_dir = ".claude/skills"

[agents.codex]
enabled = true

[agents.later]
enabled = false
rules_file = "LATER.md"
skills_dir = ".later/skills"
"""


def write_skill(root, name, body="Do the thing.\n", description="Use it."):
    folder = root / ".agents" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}", encoding="utf-8"
    )
    return folder


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "agents.toml").write_text(REGISTRY, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    write_skill(tmp_path, "plan")
    return tmp_path


def test_this_repository_is_in_step():
    report = sync_agents.run(ROOT, check=True)
    assert report.problems == []
    assert report.changes == [], "run `python scripts/sync_agents.py` and commit the result"


def test_copies_skills_and_creates_a_pointer(repo):
    report = sync_agents.run(repo, check=False)
    assert report.problems == []
    copied = repo / ".claude" / "skills" / "plan"
    assert (copied / "SKILL.md").read_bytes() == (
        repo / ".agents" / "skills" / "plan" / "SKILL.md"
    ).read_bytes()
    assert (copied / sync_agents.MARKER).exists()
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8").startswith("@AGENTS.md")
    assert sync_agents.run(repo, check=True).changes == []


def test_disabled_agents_get_nothing(repo):
    sync_agents.run(repo, check=False)
    assert not (repo / "LATER.md").exists()
    assert not (repo / ".later").exists()


def test_check_reports_drift_without_changing_anything(repo):
    sync_agents.run(repo, check=False)
    write_skill(repo, "plan", body="A newer method.\n")
    before = (repo / ".claude" / "skills" / "plan" / "SKILL.md").read_bytes()
    report = sync_agents.run(repo, check=True)
    assert ".claude/skills/plan/SKILL.md" in report.changes
    assert (repo / ".claude" / "skills" / "plan" / "SKILL.md").read_bytes() == before


def test_an_existing_rules_file_is_never_rewritten(repo):
    (repo / "CLAUDE.md").write_text("Our own guide.\n", encoding="utf-8")
    report = sync_agents.run(repo, check=False)
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "Our own guide.\n"
    assert any("never mentions AGENTS.md" in w for w in report.warnings)


def test_removed_skills_and_files_are_removed_from_copies(repo):
    folder = write_skill(repo, "extra")
    (folder / "notes.md").write_text("x", encoding="utf-8")
    sync_agents.run(repo, check=False)
    (folder / "notes.md").unlink()
    sync_agents.run(repo, check=False)
    assert not (repo / ".claude" / "skills" / "extra" / "notes.md").exists()
    for path in sorted(folder.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    folder.rmdir()
    sync_agents.run(repo, check=False)
    assert not (repo / ".claude" / "skills" / "extra").exists()


def test_a_hand_added_skill_is_left_alone(repo):
    own = repo / ".claude" / "skills" / "mine"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("mine", encoding="utf-8")
    sync_agents.run(repo, check=False)
    assert (own / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_a_name_clash_with_a_hand_added_skill_is_a_problem(repo):
    own = repo / ".claude" / "skills" / "plan"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("mine", encoding="utf-8")
    report = sync_agents.run(repo, check=False)
    assert any("not made by this script" in p for p in report.problems)
    assert (own / "SKILL.md").read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize(
    "front_matter, expected",
    [
        ("---\nname: other\ndescription: Use it.\n---\n", "name must be"),
        ("---\nname: plan\n---\n", "needs a description"),
        ("no front matter\n", "no front matter"),
    ],
)
def test_malformed_skills_are_problems(repo, front_matter, expected):
    (repo / ".agents" / "skills" / "plan" / "SKILL.md").write_text(front_matter, encoding="utf-8")
    report = sync_agents.run(repo, check=True)
    assert any(expected in p for p in report.problems)


def test_bytes_are_copied_exactly(repo):
    path = repo / ".agents" / "skills" / "plan" / "SKILL.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    sync_agents.run(repo, check=False)
    assert (repo / ".claude" / "skills" / "plan" / "SKILL.md").read_bytes() == path.read_bytes()
