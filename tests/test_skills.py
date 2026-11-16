"""Skill files. Keys and methods in here are made up for the test."""

from pathlib import Path

import pytest

from freya.learning.skill import (
    SkillError,
    learning_enabled,
    load_file,
    load_text,
    lookup,
    match,
    measurement_request,
    template_dir,
)


FILLED = """---
format: 1
version: 1
name: made-up-skill
status: {status}
applies_to: hello there
tools: files.read
steps: a made-up method for this test
check: tool-result
cases: one, two, three
---

# Made up
"""


def _write(root: Path, name: str, version: int, body: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"v{version}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_learning_off_reads_nothing(tmp_path, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("lookup opened a skill file while learning was off")

    monkeypatch.setattr(Path, "glob", refuse)
    assert learning_enabled({}) is False
    assert lookup("hello there", tmp_path, environ={}) == []
    assert lookup("hello there", environ={"FREYA_LEARNING": "0"}) == []


def test_learning_on_matches_an_approved_example(tmp_path):
    body = FILLED.format(status="approved")
    _write(tmp_path, "made-up-skill", 1, body)
    second = body.replace("version: 1", "version: 2").replace(
        "steps: a made-up method for this test",
        "steps: a later made-up method",
    )
    _write(tmp_path, "made-up-skill", 2, second)
    found = lookup("  Hello   there ", tmp_path, environ={"FREYA_LEARNING": "1"})
    assert len(found) == 1
    assert found[0].version == 2
    assert found[0].steps == "a later made-up method"
    assert match("something else", found) == []


def test_a_template_is_not_matched_and_is_not_measured():
    skills = [load_file(path) for path in sorted(template_dir().glob("*/*.md"))]
    assert [skill.name for skill in skills] == [
        "read-opened-file",
        "recurring-task",
        "report-temperatures",
    ]
    for skill in skills:
        assert skill.status == "template"
        assert skill.steps == ""
        assert skill.format == 1
        with pytest.raises(SkillError, match="Nothing was promoted"):
            measurement_request(skill)
    assert match("read a file", skills) == []


def test_an_invalid_file_is_refused(tmp_path):
    path = _write(tmp_path, "made-up-skill", 1, "not a skill\n")
    with pytest.raises(SkillError, match="front matter"):
        load_file(path)
    text = FILLED.format(status="approved").replace(
        "check: tool-result\n", "check: tool-result\nextra: 1\n"
    )
    with pytest.raises(SkillError, match="does not know"):
        load_text(text)
    with pytest.raises(SkillError, match="missing"):
        load_text(FILLED.format(status="approved").replace("check: tool-result\n", ""))


def test_a_version_mismatch_is_refused(tmp_path):
    body = FILLED.format(status="template")
    path = _write(tmp_path, "made-up-skill", 1, body.replace("version: 1", "version: 2"))
    with pytest.raises(SkillError, match="file name"):
        load_file(path)
    with pytest.raises(SkillError, match="accepts format 1"):
        load_text(body.replace("format: 1", "format: 2"))


def test_a_candidate_can_be_measured_and_is_not_promoted(tmp_path):
    path = _write(tmp_path, "made-up-skill", 1, FILLED.format(status="candidate"))
    skill = load_file(path)
    request = measurement_request(skill)
    assert request["promoted"] is False
    assert request["kind"] == "skill"
    assert request["version"] == 1
