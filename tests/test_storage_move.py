import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from freya.storage import backup, move
from freya.storage.backup import back_up_on_close, latest
from freya.storage.locations import (
    BULK_STORES,
    bulk_root,
    set_bulk_root,
    store_path,
)
from freya.storage.move import STALE_NOTE, MoveRefused, move_bulk


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.delenv("FREYA_BULK_DIR", raising=False)
    folder = tmp_path / "data"
    (folder / "images" / "2026").mkdir(parents=True)
    (folder / "images" / "2026" / "fox.png").write_bytes(b"a made-up picture")
    (folder / "images" / "index.json").write_text("{}", "utf-8")
    (folder / "avatar_models").mkdir()
    (folder / "avatar_models" / "model.vrm").write_bytes(b"x" * 5000)
    (folder / "freya_memory.db").write_bytes(b"stays where it is")
    return folder


def other_drive(monkeypatch, data):
    monkeypatch.setattr(
        move, "drive_of", lambda path: "C" if str(path).startswith(str(data)) else "D",
    )


def test_stores_live_in_data_until_the_owner_says_otherwise(data):
    assert bulk_root(data) is None
    assert store_path("images", data) == data / "images"
    assert store_path("freya_memory.db", data) == data / "freya_memory.db"


def test_only_bulk_stores_follow_the_setting(data, tmp_path):
    set_bulk_root(tmp_path / "bulk", data)

    assert store_path("images", data) == tmp_path / "bulk" / "images"
    for kept in ("freya_memory.db", "freya_control.db", "media", "speech_models"):
        assert store_path(kept, data) == data / kept
    assert not {"freya_memory.db", "media"} & set(BULK_STORES)


@pytest.mark.parametrize("content", ["not json", "[]", '{"bulk": 5}', '{"bulk": "relative/path"}', '{"bulk": ""}'])
def test_a_damaged_setting_means_data(data, content):
    (data / "storage_locations.json").write_text(content, "utf-8")

    assert bulk_root(data) is None


def test_a_missing_drive_is_not_quietly_replaced(data, tmp_path):
    gone = tmp_path / "unplugged"
    set_bulk_root(gone, data)

    # Still the configured place, though nothing is there: no second,
    # empty set of images is started in data/.
    assert store_path("images", data) == gone / "images"


def test_a_move_copies_checks_and_switches_over(data, tmp_path, monkeypatch):
    other_drive(monkeypatch, data)
    target = tmp_path / "bulk"

    moved = move_bulk(target, data, running_check=lambda: True)

    assert moved == ["images", "avatar_models"]
    assert (target / "images" / "2026" / "fox.png").read_bytes() == b"a made-up picture"
    assert (target / "avatar_models" / "model.vrm").stat().st_size == 5000
    assert bulk_root(data) == target
    # Stores with nothing in them yet have a folder waiting.
    assert all((target / name).is_dir() for name in BULK_STORES)
    # The originals are still there, marked, and nothing else moved.
    assert (data / "images" / "2026" / "fox.png").is_file()
    assert (data / "images" / STALE_NOTE).is_file()
    assert (data / "freya_memory.db").read_bytes() == b"stays where it is"
    assert not (target / "images" / STALE_NOTE).exists()


def test_it_is_refused_while_freya_is_running(data, tmp_path, monkeypatch):
    other_drive(monkeypatch, data)

    with pytest.raises(MoveRefused) as refused:
        move_bulk(tmp_path / "bulk", data, running_check=lambda: False)

    assert "running" in str(refused.value)
    assert bulk_root(data) is None
    assert not (tmp_path / "bulk").exists()


def test_it_is_refused_on_the_same_drive_or_a_relative_path(data, tmp_path, monkeypatch):
    monkeypatch.setattr(move, "drive_of", lambda path: "C")

    with pytest.raises(MoveRefused):
        move_bulk(tmp_path / "bulk", data, running_check=lambda: True)
    with pytest.raises(MoveRefused):
        move_bulk("bulk", data, running_check=lambda: True)
    assert bulk_root(data) is None


def test_it_will_not_write_over_what_is_already_there(data, tmp_path, monkeypatch):
    other_drive(monkeypatch, data)
    target = tmp_path / "bulk"
    (target / "images").mkdir(parents=True)
    (target / "images" / "someone-elses.png").write_bytes(b"other")

    with pytest.raises(MoveRefused):
        move_bulk(target, data, running_check=lambda: True)

    assert bulk_root(data) is None
    assert (target / "images" / "someone-elses.png").read_bytes() == b"other"


def test_a_copy_that_does_not_match_is_not_switched_to(data, tmp_path, monkeypatch):
    other_drive(monkeypatch, data)
    real_copy = move.shutil.copytree

    def damaged(source, destination, *args, **kwargs):
        result = real_copy(source, destination, *args, **kwargs)
        for path in Path(destination).rglob("fox.png"):
            path.write_bytes(b"a damaged picture")
        return result

    monkeypatch.setattr(move.shutil, "copytree", damaged)

    with pytest.raises(MoveRefused) as refused:
        move_bulk(tmp_path / "bulk", data, running_check=lambda: True)

    assert "does not match" in str(refused.value)
    assert bulk_root(data) is None
    assert not (data / "images" / STALE_NOTE).exists()


def test_it_cannot_be_done_twice(data, tmp_path, monkeypatch):
    other_drive(monkeypatch, data)
    move_bulk(tmp_path / "bulk", data, running_check=lambda: True)

    with pytest.raises(MoveRefused):
        move_bulk(tmp_path / "again", data, running_check=lambda: True)


# The backup made on closing

NOW = datetime(2026, 10, 9, 18, 0, 0)


@pytest.fixture
def closing(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "freya_settings.json").write_text('{"voice_volume": 80}', "utf-8")
    target = tmp_path / "elsewhere"
    monkeypatch.setattr(
        backup, "drive_of", lambda path: "C" if str(path).startswith(str(folder)) else "D",
    )
    return folder, {"FREYA_BACKUP_DIR": str(target)}, target


def test_closing_makes_a_backup(closing):
    folder, environ, target = closing

    made = back_up_on_close(folder, NOW, environ)

    assert made == target / "2026-10-09_180000"
    assert latest(target) == NOW


def test_closing_again_soon_does_not_make_another(closing):
    folder, environ, target = closing
    back_up_on_close(folder, NOW, environ)

    assert back_up_on_close(folder, NOW + timedelta(minutes=20), environ) is None
    assert back_up_on_close(folder, NOW + timedelta(hours=2), environ) is not None
    assert len([p for p in target.iterdir() if p.is_dir()]) == 2


def test_it_can_be_switched_off_and_never_raises(closing, monkeypatch):
    folder, environ, target = closing

    assert back_up_on_close(folder, NOW, {**environ, "FREYA_BACKUP": "0"}) is None
    assert not target.exists()

    def broken(*args, **kwargs):
        raise OSError("the drive went away")

    monkeypatch.setattr(backup, "back_up", broken)
    assert back_up_on_close(folder, NOW, environ) is None

    monkeypatch.setattr(backup, "default_target", lambda data_dir: None)
    assert back_up_on_close(folder, NOW, {}) is None


def test_a_failed_backup_is_told_at_the_next_start(closing, monkeypatch):
    from freya.storage.backup import startup_warning

    folder, environ, target = closing
    assert startup_warning(folder, NOW, environ) == []      # nothing noted yet

    def broken(*args, **kwargs):
        raise OSError("the drive went away")

    monkeypatch.setattr(backup, "back_up", broken)
    assert back_up_on_close(folder, NOW, environ) is None

    warning = startup_warning(folder, NOW, environ)
    assert len(warning) == 1
    assert "did not work" in warning[0] and "the drive went away" in warning[0]


def test_a_good_backup_is_silent_until_it_is_old(closing):
    from freya.storage.backup import startup_warning

    folder, environ, target = closing
    back_up_on_close(folder, NOW, environ)

    assert startup_warning(folder, NOW + timedelta(days=1), environ) == []
    old = startup_warning(folder, NOW + timedelta(days=5), environ)
    assert len(old) == 1 and "5 days old" in old[0]


def test_no_second_drive_is_noted_and_switched_off_is_silent(closing, monkeypatch):
    from freya.storage.backup import startup_warning

    folder, environ, target = closing
    monkeypatch.setattr(backup, "default_target", lambda data_dir: None)

    back_up_on_close(folder, NOW, {})

    assert "no second drive" in startup_warning(folder, NOW, {})[0]
    assert startup_warning(folder, NOW, {"FREYA_BACKUP": "0"}) == []


def test_a_damaged_note_is_no_warning(closing):
    from freya.storage.backup import startup_warning

    folder, environ, target = closing
    (folder / "backup_status.json").write_text("not json", "utf-8")

    assert startup_warning(folder, NOW, environ) == []


def test_folders_that_are_not_backups_are_ignored(closing):
    folder, environ, target = closing
    (target / "notes").mkdir(parents=True)
    (target / "2026-13-45_999999").mkdir()

    assert latest(target) is None
    assert latest(target / "nowhere") is None
