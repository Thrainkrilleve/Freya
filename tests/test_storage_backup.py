import json
import sqlite3
from datetime import datetime

import pytest

from freya.storage import backup
from freya.storage.backup import (
    BackupRefused,
    back_up,
    check,
    default_target,
)
from freya.storage.placement import HDD, SSD


WHEN = datetime(2026, 10, 9, 17, 30, 0)


@pytest.fixture
def data(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    # A made-up database in the mode Freya's control database uses.
    connection = sqlite3.connect(folder / "freya_memory.db")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    connection.executemany(
        "INSERT INTO notes (body) VALUES (?)", [("made-up note",)] * 50,
    )
    connection.commit()
    connection.close()
    (folder / "freya_settings.json").write_text('{"voice_volume": 80}', "utf-8")
    (folder / "images").mkdir()
    (folder / "images" / "picture.png").write_bytes(b"not copied")
    return folder


def other_drive(monkeypatch, data, target):
    """Make the two folders count as being on different drives."""
    monkeypatch.setattr(
        backup, "drive_of", lambda path: "C" if str(path).startswith(str(data)) else "D",
    )
    return target


def test_databases_and_settings_are_copied_and_can_be_opened(tmp_path, data, monkeypatch):
    target = other_drive(monkeypatch, data, tmp_path / "elsewhere")

    folder = back_up(data, target, WHEN)

    assert folder == target / "2026-10-09_173000"
    assert sorted(p.name for p in folder.iterdir()) == [
        "freya_memory.db", "freya_settings.json", "manifest.json",
    ]
    copy = sqlite3.connect(folder / "freya_memory.db")
    try:
        assert copy.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 50
        # One plain file, with no sidecar files left to lose.
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        copy.close()
    assert check(folder) == []


def test_a_backup_on_the_same_drive_is_refused(tmp_path, data, monkeypatch):
    monkeypatch.setattr(backup, "drive_of", lambda path: "C")

    with pytest.raises(BackupRefused) as refused:
        back_up(data, tmp_path / "beside", WHEN)

    assert "same drive" in str(refused.value)
    assert not (tmp_path / "beside").exists()


def test_the_original_is_left_exactly_as_it_was(tmp_path, data, monkeypatch):
    target = other_drive(monkeypatch, data, tmp_path / "elsewhere")
    before = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}

    back_up(data, target, WHEN)

    after = {p.name: p.read_bytes() for p in data.iterdir() if p.is_file()}
    # SQLite may leave its own housekeeping files beside a database it
    # read; the database and the settings themselves are untouched.
    assert {name: after[name] for name in before} == before
    assert (data / "images" / "picture.png").read_bytes() == b"not copied"


def test_each_run_makes_a_new_folder_and_keeps_the_old(tmp_path, data, monkeypatch):
    target = other_drive(monkeypatch, data, tmp_path / "elsewhere")

    first = back_up(data, target, WHEN)
    second = back_up(data, target, WHEN.replace(minute=45))

    assert first != second and first.is_dir() and second.is_dir()
    with pytest.raises(BackupRefused):
        back_up(data, target, WHEN)


def test_a_changed_or_missing_copy_is_reported(tmp_path, data, monkeypatch):
    target = other_drive(monkeypatch, data, tmp_path / "elsewhere")
    folder = back_up(data, target, WHEN)

    (folder / "freya_settings.json").write_text("{}", "utf-8")
    assert check(folder) == ["freya_settings.json has changed since it was copied."]

    (folder / "freya_memory.db").unlink()
    assert "freya_memory.db is missing." in check(folder)

    (folder / "manifest.json").unlink()
    assert check(folder) == ["The manifest is missing or unreadable."]


def test_nothing_to_back_up_is_an_error_and_leaves_no_folder(tmp_path, monkeypatch):
    empty = tmp_path / "data"
    empty.mkdir()
    target = other_drive(monkeypatch, empty, tmp_path / "elsewhere")

    with pytest.raises(BackupRefused):
        back_up(empty, target, WHEN)

    assert list(target.iterdir()) == []


def test_the_target_is_another_drive_preferring_a_hard_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "drive_of", lambda path: "C")

    assert str(default_target(tmp_path, {"C": SSD, "D": HDD, "E": SSD})).startswith("D:")
    assert str(default_target(tmp_path, {"C": SSD, "E": SSD})).startswith("E:")
    assert default_target(tmp_path, {"C": SSD}) is None
    assert default_target(tmp_path, {}) is None

    with pytest.raises(BackupRefused):
        back_up(tmp_path, None)


def test_the_manifest_holds_sizes_and_checksums_not_contents(tmp_path, data, monkeypatch):
    target = other_drive(monkeypatch, data, tmp_path / "elsewhere")

    folder = back_up(data, target, WHEN)
    manifest = (folder / "manifest.json").read_text("utf-8")

    assert "made-up note" not in manifest
    entries = json.loads(manifest)["copied"]
    assert {entry["kind"] for entry in entries} == {"database", "file"}
    assert all(len(entry["sha256"]) == 64 and entry["bytes"] > 0 for entry in entries)
