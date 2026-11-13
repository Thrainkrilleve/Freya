import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from freya.control_plane import build_local_control_plane
from freya.file_access import FileAccess
from freya.settings import Settings
from freya.tools import file_tools
from freya.tools.file_tools import (
    FILE_READER_CAPABILITIES,
    FILE_READER_PROVIDER,
    FILES_READ,
    NOTHING_OPEN,
    OUTSIDE,
    UNREADABLE,
    WITHHELD,
    FileAccessError,
    FileReader,
    check_readable_folder,
)


@pytest.fixture
def tree(tmp_path):
    """An opened folder, a folder beside it that is not open, and Freya's data."""
    opened = tmp_path / "opened"
    (opened / "src").mkdir(parents=True)
    (opened / "src" / "app.py").write_text(
        "def greet(name):\n    return 'Hello, ' + name\n\nprint(greet('Ada'))\n",
        encoding="utf-8",
    )
    (opened / "notes.txt").write_text("Buy oat milk.\nCall the Plumber.\n", encoding="utf-8")
    (opened / ".env").write_text("TOKEN=made-up-token\n", encoding="utf-8")
    (opened / "server.pem").write_text("made-up key\n", encoding="utf-8")
    (opened / ".ssh").mkdir()
    (opened / ".git").mkdir()
    (opened / ".git" / "config").write_text(
        "url = https://user:made-up-password@example.invalid/repo", encoding="utf-8",
    )
    (opened / ".ssh" / "config").write_text("Host made-up\n", encoding="utf-8")
    (opened / "picture.bin").write_bytes(b"\x89PNG\x00\x00binary")
    (opened / "data").mkdir()
    (opened / "data" / "memory.txt").write_text("a made-up memory plumber\n", encoding="utf-8")

    beside = tmp_path / "beside"
    beside.mkdir()
    (beside / "private.txt").write_text("not for reading\n", encoding="utf-8")

    return opened


@pytest.fixture
def reader(tree):
    return FileReader(lambda: [str(tree)], closed=[tree / "data"])


def names(listing):
    return [entry["name"] for entry in listing["entries"]]


def test_nothing_can_be_read_until_a_folder_is_opened(tree):
    reader = FileReader(lambda: [])

    assert not reader.is_open()
    for call in (
        lambda: reader.list("."),
        lambda: reader.read(str(tree / "notes.txt")),
        lambda: reader.search(str(tree), "milk"),
    ):
        with pytest.raises(FileAccessError, match=NOTHING_OPEN):
            call()


def test_a_dot_lists_the_opened_folders(reader, tree):
    assert reader.list(".") == {"opened_folders": [str(tree.resolve())]}


def test_listing_shows_files_and_folders_but_never_keys_or_freyas_data(reader, tree):
    listing = reader.list(str(tree))

    assert names(listing) == ["notes.txt", "picture.bin", "src"]
    # .env, server.pem, .ssh, .git and Freya's data: said to be there, not named.
    assert listing["withheld"].startswith("5 more")
    assert "withheld" not in reader.list("src")
    assert {"name": "src", "folder": True} in listing["entries"]
    assert {"name": "notes.txt", "bytes": (tree / "notes.txt").stat().st_size} in (
        listing["entries"]
    )


def test_a_file_is_read_by_full_path_or_by_a_path_inside_the_folder(reader, tree):
    by_full = reader.read(str(tree / "src" / "app.py"))
    by_inner = reader.read("src/app.py", "2")

    assert by_full["text"].startswith("def greet(name):")
    assert (by_full["first_line"], by_full["last_line"], by_full["total_lines"]) == (1, 4, 4)
    assert by_full["more"] is False
    assert by_inner["text"].splitlines()[0] == "    return 'Hello, ' + name"
    assert by_inner["first_line"] == 2


def test_a_long_file_is_read_a_part_at_a_time(reader, tree):
    (tree / "long.txt").write_text(
        "\n".join(f"line {n}" for n in range(1, 451)), encoding="utf-8",
    )

    first = reader.read("long.txt")
    second = reader.read("long.txt", str(first["last_line"] + 1))
    last = reader.read("long.txt", "401")

    assert (first["first_line"], first["last_line"], first["more"]) == (
        1, file_tools.MAX_READ_LINES, True,
    )
    assert second["text"].splitlines()[0] == f"line {file_tools.MAX_READ_LINES + 1}"
    assert (last["last_line"], last["total_lines"], last["more"]) == (450, 450, False)


def through_the_context(answer):
    """What the model receives of a tool result, as Freya's context passes it."""
    from freya.kernel.context import ContextAuthority

    fitted = ContextAuthority().fit_tools([
        {"role": "user", "content": "What is in the file?"},
        {"role": "tool", "content": json.dumps(answer, ensure_ascii=False)},
    ])

    return json.loads(fitted[-1]["content"])


def test_each_tools_largest_result_reaches_the_model_whole(reader, tree):
    # The context clips a tool result at 2,400 bytes. A result cut
    # there would carry line numbers and counts for text the model
    # never sees, so each tool keeps its own result under the clip.
    deep = tree / "many"
    deep.mkdir()
    for n in range(300):
        (deep / f"a-rather-long-file-name-number-{n:03}.txt").write_text(
            "\n".join(f"needle {n} " + "y" * 300 for _ in range(5)), encoding="utf-8",
        )
    (tree / "wide.txt").write_text(
        "\n".join("x" * 1000 for _ in range(300)), encoding="utf-8",
    )
    (tree / "long.txt").write_text(
        "\n".join(f"line {n} " + "z" * 60 for n in range(1, 451)), encoding="utf-8",
    )

    listed = reader.list("many")
    wide = reader.read("wide.txt")
    long = reader.read("long.txt")
    found = reader.search("many", "needle")

    for result in (listed, wide, long, found):
        received = through_the_context(result)
        assert "excerpt" not in received
        assert received["_context"]["truncated"] is False

    assert listed["not_shown"] + len(listed["entries"]) == 300
    assert long["last_line"] == len(long["text"].splitlines())
    assert long["more"] is True
    assert wide["text"].endswith("[line cut]")
    assert found["stopped_early"] == "the limit on matches was reached"
    assert 0 < len(found["matches"]) < file_tools.MAX_MATCHES


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_a_file_that_is_not_ordinary_is_refused_rather_than_waited_on(reader, tree):
    os.mkfifo(tree / "pipe")

    with pytest.raises(FileAccessError, match="not text"):
        reader.read("pipe")


def test_one_call_returns_a_bounded_amount_of_text(reader, tree):
    (tree / "wide.txt").write_text(
        "\n".join("x" * 1000 for _ in range(300)), encoding="utf-8",
    )

    part = reader.read("wide.txt")

    assert len(part["text"]) <= file_tools.MAX_READ_CHARACTERS + 100
    assert part["text"].splitlines()[0].endswith("[line cut]")
    assert part["more"] is True


@pytest.mark.parametrize("start", ["0", "-3", "two", "1.5", ""])
def test_a_bad_starting_line_is_refused(reader, start):
    with pytest.raises(FileAccessError, match="whole number"):
        reader.read("notes.txt", start)


def test_what_is_outside_is_refused_in_the_same_words_whether_or_not_it_exists(reader, tree):
    beside = tree.parent / "beside"

    for path in (
        str(beside / "private.txt"),
        str(beside / "no-such-file.txt"),
        str(tree / ".." / "beside" / "private.txt"),
        "../beside/private.txt",
        str(tree / "no-such-file.txt"),
        "C:\\Windows\\win.ini",
        "notes.txt:hidden",
    ):
        with pytest.raises(FileAccessError) as refused:
            reader.read(path)
        assert str(refused.value) == OUTSIDE

    with pytest.raises(FileAccessError, match=OUTSIDE):
        reader.list(str(beside))
    with pytest.raises(FileAccessError, match=OUTSIDE):
        reader.search(str(beside), "reading")


def test_a_refusal_never_names_the_path(reader, tree):
    for path in (str(tree.parent / "beside" / "private.txt"), str(tree / ".env")):
        with pytest.raises(FileAccessError) as refused:
            reader.read(path)
        assert "private" not in str(refused.value)
        assert str(tree) not in str(refused.value)


@pytest.mark.parametrize("path", [
    ".env", "server.pem", ".ssh/config", ".ssh", "data/memory.txt", "data",
    ".git/config", ".git",
])
def test_keys_sign_ins_and_freyas_own_data_stay_shut_inside_an_opened_folder(reader, path):
    for call in (reader.read, reader.list):
        with pytest.raises(FileAccessError) as refused:
            call(path)
        assert str(refused.value) == WITHHELD


@pytest.mark.parametrize("name, secret", [
    (".env", True), (".env.local", True), ("ID_RSA", True), ("id_ed25519.pub", True),
    ("Server.KEY", True), ("credentials.json", True), ("secrets.yaml", True),
    ("environment.py", False), ("keyboard.py", False), ("notes.txt", False),
    ("secretary.txt", False),
])
def test_which_file_names_count_as_secrets(name, secret):
    assert file_tools.is_secret_name(name) is secret


def test_an_opened_folder_replaced_by_a_link_is_no_longer_open(tree, tmp_path):
    # The owner opened a folder. Later its path is made to lead to a
    # folder of keys. What it leads to now was never opened.
    keys = tmp_path / "home" / ".ssh"
    keys.mkdir(parents=True)
    (keys / "config").write_text("Host made-up\n", encoding="utf-8")
    (keys / "known_hosts").write_text("made-up host key\n", encoding="utf-8")
    opened = tmp_path / "project"
    opened.mkdir()
    (opened / "notes.txt").write_text("ordinary notes\n", encoding="utf-8")
    reader = FileReader(lambda: [str(opened)])
    assert reader.read("notes.txt")

    (opened / "notes.txt").unlink()
    opened.rmdir()

    try:
        os.symlink(keys, opened, target_is_directory=True)
    except OSError:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(opened), str(keys)],
            capture_output=True,
        )
        if made.returncode != 0:
            pytest.skip("links cannot be made here")

    assert reader.roots() == []
    assert not reader.is_open()
    for call in (
        lambda: reader.read("config"),
        lambda: reader.read(str(opened / "known_hosts")),
        lambda: reader.list("."),
    ):
        with pytest.raises(FileAccessError):
            call()


def test_a_folder_that_may_no_longer_be_opened_is_left_out(tree, tmp_path):
    inside_data = tree / "data" / "kept"
    inside_data.mkdir(parents=True, exist_ok=True)

    # As if it had been opened before that place was closed.
    reader = FileReader(lambda: [str(inside_data), str(tree)], closed=[tree / "data"])

    assert reader.roots() == [tree.resolve()]


def test_a_link_inside_an_opened_folder_cannot_lead_out_of_it(reader, tree):
    target = tree.parent / "beside"

    try:
        os.symlink(target, tree / "way-out", target_is_directory=True)
    except OSError:
        # Without the right to make links, Windows still allows a
        # junction, which leads out of a folder the same way.
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(tree / "way-out"), str(target)],
            capture_output=True,
        )
        if made.returncode != 0:
            pytest.skip("This account can make neither links nor junctions.")

    with pytest.raises(FileAccessError, match=OUTSIDE):
        reader.read("way-out/private.txt")
    with pytest.raises(FileAccessError, match=OUTSIDE):
        reader.list("way-out")
    assert "way-out" not in names(reader.list(str(tree)))

    try:
        os.symlink(target / "private.txt", tree / "shortcut.txt")
    except OSError:
        pass
    else:
        with pytest.raises(FileAccessError, match=OUTSIDE):
            reader.read("shortcut.txt")
        assert "shortcut.txt" not in names(reader.list(str(tree)))

    assert reader.search(str(tree), "not for reading")["matches"] == []


def test_a_link_with_a_secret_name_is_withheld_whatever_it_leads_to(reader, tree):
    try:
        os.symlink(tree / "notes.txt", tree / "id_rsa")
    except OSError:
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/H", str(tree / "id_rsa"), str(tree / "notes.txt")],
            capture_output=True,
        )
        if made.returncode != 0:
            pytest.skip("This account can make neither links nor hard links.")

    with pytest.raises(FileAccessError) as refused:
        reader.read("id_rsa")

    assert str(refused.value) == WITHHELD
    assert "id_rsa" not in names(reader.list(str(tree)))
    assert len(reader.search(str(tree), "oat milk")["matches"]) == 1


def test_a_failure_of_the_disk_is_reported_without_the_path(reader, tree, monkeypatch):
    def fail(path):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(FileReader, "_text_of", staticmethod(fail))

    with pytest.raises(FileAccessError) as refused:
        reader.read("notes.txt")

    assert str(refused.value) == UNREADABLE
    assert refused.value.__cause__ is None

    monkeypatch.setattr(file_tools.os, "scandir", fail)

    with pytest.raises(FileAccessError, match=UNREADABLE):
        reader.list(str(tree))


def test_search_stops_on_time_even_among_empty_folders(reader, tree, monkeypatch):
    for number in range(20):
        (tree / f"empty-{number}").mkdir()

    monkeypatch.setattr(file_tools, "SEARCH_SECONDS", -1.0)
    found = reader.search(str(tree), "milk")

    assert found["matches"] == []
    assert "time" in found["stopped_early"]


def test_a_file_that_is_not_text_is_refused(reader):
    with pytest.raises(FileAccessError, match="not text"):
        reader.read("picture.bin")


def test_a_very_large_file_is_refused(reader, tree, monkeypatch):
    monkeypatch.setattr(file_tools, "MAX_READ_BYTES", 10)

    with pytest.raises(FileAccessError, match="too large"):
        reader.read("notes.txt")


def test_search_finds_lines_without_regard_to_capitals(reader, tree):
    found = reader.search(str(tree), "PLUMBER")

    assert found["matches"] == [{
        "file": str(tree.resolve() / "notes.txt"), "line": 2,
        "text": "Call the Plumber.",
    }]
    assert "stopped_early" not in found


def test_search_never_looks_in_keys_sign_ins_or_freyas_data(reader, tree):
    assert reader.search(str(tree), "made-up")["matches"] == []
    # "plumber" is in Freya's data too, and only the note is found.
    assert len(reader.search(str(tree), "plumber")["matches"]) == 1


def test_search_stops_at_its_limits_and_says_so(reader, tree, monkeypatch):
    for number in range(8):
        (tree / f"many-{number}.txt").write_text("needle\n" * 3, encoding="utf-8")

    monkeypatch.setattr(file_tools, "MAX_MATCHES", 5)
    by_matches = reader.search(str(tree), "needle")
    monkeypatch.setattr(file_tools, "MAX_MATCHES", 30)
    monkeypatch.setattr(file_tools, "MAX_SEARCHED_FILES", 2)
    by_files = reader.search(str(tree), "needle")

    assert len(by_matches["matches"]) == 5
    assert "matches" in by_matches["stopped_early"]
    assert by_files["files_searched"] == 2
    assert "files" in by_files["stopped_early"]


def test_search_skips_other_programs_bulk(reader, tree):
    (tree / "node_modules" / "pkg").mkdir(parents=True)
    (tree / "node_modules" / "pkg" / "index.js").write_text("needle\n", encoding="utf-8")

    assert reader.search(str(tree), "needle")["matches"] == []
    # It can still be read when asked for by path.
    assert reader.read("node_modules/pkg/index.js")["text"] == "needle"


def test_a_folder_that_has_gone_is_no_longer_open(tmp_path):
    gone = tmp_path / "gone"
    reader = FileReader(lambda: [str(gone), 7, None])

    assert reader.roots() == []


# Which folders the owner may open.

@pytest.mark.skipif(sys.platform != "win32", reason="the whole-drive and AppData refusals are Windows rules")
def test_only_a_real_folder_given_in_full_can_be_opened(tree):
    assert check_readable_folder(str(tree)) == tree.resolve()
    assert check_readable_folder(f'"{tree}"') == tree.resolve()

    for path, why in (
        ("", "full path"),
        ("opened", "full path"),
        (str(tree / "missing"), "not found"),
        (str(tree / "notes.txt"), "is a file"),
        (tree.anchor, "whole drive"),
        (str(Path.home()), "whole user folder"),
        (str(tree / ".ssh"), "sign-ins or keys"),
        (str(Path.home() / "AppData"), "settings and sign-ins"),
        (str(Path.home() / "AppData" / "Roaming"), "settings and sign-ins"),
    ):
        with pytest.raises(ValueError, match=why):
            check_readable_folder(path)


def test_freyas_own_data_cannot_be_opened(tree):
    with pytest.raises(ValueError, match="her own data"):
        check_readable_folder(str(tree / "data"), closed=[tree / "data"])

    # A folder around it can be, and the data inside stays shut.
    assert check_readable_folder(str(tree), closed=[tree / "data"]) == tree.resolve()


# The owner's setting and commands.

def test_opened_folders_are_kept_with_the_other_settings(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)

    assert settings.readable_folders == []

    settings.add_readable_folder("C:\\Work\\Notes")
    settings.add_wake_name("juno")
    settings.set_voice_volume(40)

    again = Settings(path)

    assert again.readable_folders == ["C:\\Work\\Notes"]
    assert again.wake_names == ["freya", "juno"]
    assert again.voice_volume == 40

    with pytest.raises(ValueError, match="already open"):
        again.add_readable_folder("c:\\work\\notes")

    assert again.remove_readable_folder("c:\\work\\notes\\") == "C:\\Work\\Notes"
    assert Settings(path).readable_folders == []

    with pytest.raises(ValueError, match="not open"):
        again.remove_readable_folder("C:\\Work\\Notes")


def test_damaged_folder_settings_open_nothing(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"readable_folders": "C:\\", "wake_names": ["freya"]}), encoding="utf-8")

    assert Settings(path).readable_folders == []

    path.write_text(json.dumps({"readable_folders": [5, "", {"a": 1}, ["b"], "C:\\Work", "C:\\Work"]}), encoding="utf-8")

    assert Settings(path).readable_folders == ["C:\\Work"]


@pytest.fixture
def access(tree, tmp_path):
    refreshed = []
    settings = Settings(tmp_path / "settings.json")
    access = FileAccess(settings, lambda: refreshed.append(True), closed=[tree / "data"])
    access.refreshed = refreshed
    return access


def test_the_folder_commands_open_list_and_close(access, tree, capsys):
    assert access.command("/folders") is True
    assert "No folder is open" in capsys.readouterr().out

    assert access.command(f"/folder open {tree}") is True
    assert "only read there, never change" in capsys.readouterr().out
    assert access.settings.readable_folders == [str(tree.resolve())]
    assert access.reader.read("notes.txt")["total_lines"] == 2
    assert access.refreshed == [True]

    access.command("/folders")
    assert str(tree.resolve()) in capsys.readouterr().out

    assert access.command(f"/folder close {tree}") is True
    assert access.settings.readable_folders == []
    assert access.refreshed == [True, True]
    with pytest.raises(FileAccessError):
        access.reader.read("notes.txt")


def test_a_folder_with_spaces_in_its_name_can_be_opened(access, tree, capsys):
    spaced = tree / "my papers"
    spaced.mkdir()

    access.command(f"/folder open {spaced}")

    assert access.settings.readable_folders == [str(spaced.resolve())]


def test_a_folder_that_may_not_be_opened_is_refused_with_the_reason(access, tree, capsys):
    for line, said in (
        (f"/folder open {tree / 'data'}", "her own data"),
        (f"/folder open {tree.anchor}", "whole drive"),
        (f"/folder close {tree}", "not open"),
        ("/folder", "Usage"),
        ("/folder open", "Usage"),
        ("/folder widen C:\\", "Usage"),
    ):
        assert access.command(line) is True
        assert said in capsys.readouterr().out

    assert access.settings.readable_folders == []
    assert access.refreshed == []


def test_other_input_is_not_a_folder_command(access):
    assert access.command("/foldersize") is False
    assert access.command("open the folder") is False
    assert access.command("") is False


# The tools as the model is offered them.

@pytest.fixture
def freya(tree, tmp_path):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    settings = Settings(tmp_path / "settings.json")
    access = FileAccess(settings, plane.refresh_file_reader, closed=[tree / "data"])
    plane.register_file_reader(access.reader)
    yield plane, access
    plane.store.close()


def offered(plane):
    return {tool["function"]["name"] for tool in plane.model_tools.definitions()}


FILE_TOOLS = {"files_list", "files_read", "files_search"}


def test_the_tools_are_not_offered_until_the_owner_opens_a_folder(freya, tree, capsys):
    plane, access = freya

    assert not FILE_TOOLS & offered(plane)
    assert json.loads(plane.model_tools.run("files_read", {"path": "x", "start_line": "1"})) == {
        "error": "That tool is not available.",
    }

    access.command(f"/folder open {tree}")

    assert FILE_TOOLS <= offered(plane)

    access.command(f"/folder close {tree}")

    assert not FILE_TOOLS & offered(plane)


def test_the_model_reads_through_the_broker_and_only_what_is_open(freya, tree, capsys):
    plane, access = freya
    access.command(f"/folder open {tree}")

    read = json.loads(plane.model_tools.run(
        "files_read", {"path": "src/app.py", "start_line": "1", "mode": "write"},
    ))
    found = json.loads(plane.model_tools.run(
        "files_search", {"folder": str(tree), "text": "greet"},
    ))
    refused = json.loads(plane.model_tools.run(
        "files_read", {"path": str(tree.parent / "beside" / "private.txt"), "start_line": "1"},
    ))

    assert read["total_lines"] == 4
    assert [match["line"] for match in found["matches"]] == [1, 4]
    assert refused == {"error": OUTSIDE}
    assert "start_line" in json.loads(
        plane.model_tools.run("files_read", {"path": "notes.txt"})
    )["error"]

    events = plane.store.execute(
        "SELECT * FROM audit_event WHERE capability = ?", (FILES_READ,),
    ).fetchall()

    assert events
    # The audit log records that a file was read, never which or what.
    assert "app.py" not in json.dumps([tuple(event) for event in events], default=str)
    assert "Hello" not in json.dumps([tuple(event) for event in events], default=str)


def test_the_tools_are_read_only_and_can_be_shut_by_policy(freya, tree, capsys):
    plane, access = freya
    access.command(f"/folder open {tree}")

    for name in FILE_READER_CAPABILITIES:
        capability = plane.capabilities.get(name)
        assert capability.provider == FILE_READER_PROVIDER
        assert not capability.destructive

    assert not [
        name for name in dir(FileReader)
        if any(word in name for word in ("write", "delete", "remove", "move", "create"))
    ]

    for rule in plane.policy.list_rules():
        if rule["capability"] in FILE_READER_CAPABILITIES:
            plane.policy.remove_rule(rule["rule_id"])

    assert "error" in json.loads(plane.model_tools.run("files_list", {"path": "."}))
