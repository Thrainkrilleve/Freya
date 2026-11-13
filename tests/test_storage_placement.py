import re
from pathlib import Path

from freya.storage import placement
from freya.storage.placement import (
    BULK,
    COPY,
    DATA_KINDS,
    FAST,
    HDD,
    SSD,
    UNKNOWN,
    describe,
    plan_placement,
)


BOTH = {"C": SSD, "D": HDD}


def data_on(tmp_path, monkeypatch, letter):
    """A made-up data folder that the plan believes is on a given drive."""
    folder = tmp_path / "data"
    folder.mkdir()
    for kind in DATA_KINDS:
        target = folder / kind.name
        if "." in kind.name:
            target.write_bytes(b"x" * 10)
        else:
            target.mkdir()
            (target / "file").write_bytes(b"x" * 100)
    monkeypatch.setattr(placement, "drive_of", lambda path: letter)
    return folder


def by_name(plan):
    return {entry.kind.name: entry for entry in plan}


def test_on_the_ssd_fast_data_stays_and_bulk_data_would_move(tmp_path, monkeypatch):
    plan = by_name(plan_placement(data_on(tmp_path, monkeypatch, "C"), BOTH))

    assert plan["freya_memory.db"].well_placed
    assert plan["speech_models"].well_placed
    assert plan["images"].best_drive == "D"
    assert plan["freya.log"].best_drive == "D"
    # A backup beside what it protects is the one that matters most.
    assert plan["backups"].best_drive == "D"
    assert "lost with it" in plan["backups"].reason


def test_on_the_hard_disk_fast_data_would_move_and_bulk_data_stays(tmp_path, monkeypatch):
    plan = by_name(plan_placement(data_on(tmp_path, monkeypatch, "D"), BOTH))

    assert plan["freya_memory.db"].best_drive == "C"
    assert plan["images"].well_placed
    assert plan["backups"].best_drive == "C"


def test_with_one_drive_nothing_is_asked_to_move(tmp_path, monkeypatch):
    plan = plan_placement(data_on(tmp_path, monkeypatch, "C"), {"C": SSD})

    assert all(entry.well_placed for entry in plan)
    assert "no second drive" in by_name(plan)["backups"].reason


def test_an_unknown_drive_means_no_advice(tmp_path, monkeypatch):
    folder = data_on(tmp_path, monkeypatch, "C")

    for drives in ({}, {"C": UNKNOWN, "D": HDD}):
        plan = plan_placement(folder, drives)
        assert all(entry.well_placed for entry in plan)


def test_sizes_are_read_and_missing_stores_are_noted(tmp_path, monkeypatch):
    folder = data_on(tmp_path, monkeypatch, "C")
    (folder / "freya_usage.db").unlink()

    plan = by_name(plan_placement(folder, BOTH))

    assert plan["images"].size == 100
    assert plan["freya_memory.db"].size == 10
    assert not plan["freya_usage.db"].present
    assert plan["freya_usage.db"].size == 0


def test_the_report_names_stores_and_says_nothing_moved(tmp_path, monkeypatch):
    folder = data_on(tmp_path, monkeypatch, "C")

    lines = describe(plan_placement(folder, BOTH), BOTH)
    text = "\n".join(lines)

    assert lines[0] == "Drives: C: ssd, D: hdd"
    assert "Would be better elsewhere:" in text and "Well placed:" in text
    assert "backups" in text and "C: -> D:" in text
    assert lines[-1] == "Nothing has been moved."


def test_planning_changes_nothing_on_disk(tmp_path, monkeypatch):
    folder = data_on(tmp_path, monkeypatch, "C")
    before = sorted(
        (str(path.relative_to(folder)), path.stat().st_size)
        for path in folder.rglob("*") if path.is_file()
    )

    describe(plan_placement(folder, BOTH), BOTH)

    after = sorted(
        (str(path.relative_to(folder)), path.stat().st_size)
        for path in folder.rglob("*") if path.is_file()
    )
    assert before == after


def test_every_need_is_a_known_one_and_names_are_unique():
    assert {kind.need for kind in DATA_KINDS} == {FAST, BULK, COPY}
    assert len({kind.name for kind in DATA_KINDS}) == len(DATA_KINDS)
    # What holds the owner's own words or recordings is marked personal.
    personal = {kind.name for kind in DATA_KINDS if kind.personal}
    assert {"freya_memory.db", "sent_briefs.jsonl", "backups", "voice_references"} <= personal


def test_stores_the_code_writes_under_data_are_all_listed():
    source = Path(placement.__file__).resolve().parents[1]
    listed = {kind.name for kind in DATA_KINDS}
    written = set()

    for path in source.rglob("*.py"):
        if "evaluation" in path.parts or path.name == "placement.py":
            continue
        text = path.read_text(encoding="utf-8")
        written.update(
            re.findall(r'(?:DATA_DIR|data_dir)\s*/\s*"([^"/]+)"', text)
        )

    assert written - listed == set()


def test_drive_kinds_reads_what_windows_reports(monkeypatch):
    class Completed:
        stdout = b'[{"letter":"C","kind":"SSD"},{"letter":"d","kind":"HDD"},{"letter":"E","kind":"Unspecified"},{"letter":"","kind":"SSD"}]'

    monkeypatch.setattr(placement.sys, "platform", "win32")
    monkeypatch.setattr(placement.subprocess, "run", lambda *a, **k: Completed())

    assert placement.drive_kinds() == {"C": SSD, "D": HDD, "E": UNKNOWN}

    Completed.stdout = b"not json"
    assert placement.drive_kinds() == {}
