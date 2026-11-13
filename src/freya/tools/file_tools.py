import fnmatch
import functools
import json
import os
import stat
import time
from pathlib import Path
from typing import Callable, Iterable, List, Sequence

from freya.core.capabilities import CapabilityDefinition
from freya.core.providers import ProviderDefinition
from freya.tools.model_tools import MODEL_TOOL, TOOL_PARAMETERS


FILE_READER_PROVIDER = "file-reader-local"

FILES_LIST = "files.list"
FILES_READ = "files.read"
FILES_SEARCH = "files.search"

FILE_READER_CAPABILITIES = [FILES_LIST, FILES_READ, FILES_SEARCH]

# What one call may return. The model's whole context is small, so a
# file is read a part at a time. The context clips a tool result at
# 2,400 bytes (`ContextAuthority.fit_tools`) and the model then sees
# an excerpt whose counts and line numbers no longer describe it, so
# every result is kept under MAX_RESULT_BYTES as encoded, and what
# did not fit is said in the result rather than cut off outside it.
MAX_RESULT_BYTES = 2_200
MAX_READ_LINES = 60
MAX_READ_CHARACTERS = 1_800
MAX_LINE_CHARACTERS = 400
MAX_LISTED = 200
MAX_MATCHES = 30

# What one search may cost.
MAX_SEARCHED_FILES = 3_000
MAX_SEARCHED_BYTES = 1_000_000
SEARCH_SECONDS = 5.0

# The largest file that is read at all.
MAX_READ_BYTES = 5_000_000

# How much of a file is looked at to decide whether it is text.
SNIFF_BYTES = 8_192


def _encoded_size(result: dict) -> int:
    return len(json.dumps(result, ensure_ascii=False).encode("utf-8"))

# Files that hold keys and passwords by convention. They are never
# listed, read or searched, even inside a folder the owner opened.
SECRET_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.kdbx",
    "*.keystore", "*.jks", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".git-credentials", ".netrc", "_netrc", ".npmrc", ".pypirc",
    "credentials", "credentials.*", "secrets.*", "*.secret",
)

# Folders that hold sign-ins and keys by convention, likewise. A
# project's .git is among them: its settings can hold an address with
# a password or token in it.
CLOSED_FOLDER_NAMES = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube", ".docker", ".claude",
    ".codex",
})

# Where Windows programs keep their settings, browser profiles and
# sign-ins. These, as wholes, cannot be opened.
PROGRAM_DATA_FOLDERS = ("", "Local", "LocalLow", "Roaming")

# Folders a search does not go into: other programs' bulk, not the
# owner's own text. They can still be listed and read by path.
SKIPPED_IN_SEARCH = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build",
})

# Said for every path that may not be reached, whether or not anything
# is there, so that a refusal tells nothing about what exists.
OUTSIDE = "That is not inside a folder the user has opened for reading."
NOTHING_OPEN = "The user has not opened any folder for reading."
WITHHELD = "That is a kind of file or folder that is never read."
UNREADABLE = "That could not be read just now."


class FileAccessError(ValueError):
    """
    A path could not be read. The message is written for the model and
    reaches the audit log, so it never holds a path or any content.
    """


def _path_free(method):
    """
    Turn a failure of the disk into words without a path. The system's
    own message names the file, and a tool's failure is written to the
    audit log.
    """

    @functools.wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except OSError:
            raise FileAccessError(UNREADABLE) from None

    return guarded


def _key(path) -> str:
    return os.path.normcase(str(path))


def _inside(path: Path, folder: Path) -> bool:
    try:
        Path(_key(path)).relative_to(_key(folder))
    except ValueError:
        return False

    return True


def is_secret_name(name: str) -> bool:
    lowered = name.lower()

    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in SECRET_FILE_PATTERNS)


def is_closed_folder_name(name: str) -> bool:
    return name.lower() in CLOSED_FOLDER_NAMES


def check_readable_folder(path, closed: Iterable = ()) -> Path:
    """
    A folder the owner asks to open, resolved, or ValueError saying why
    it cannot be opened. A whole drive and the whole user folder are
    refused: they hold far more than anyone means to hand over.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Give the full path of a folder.")

    folder = Path(path.strip().strip('"')).expanduser()

    if not folder.is_absolute():
        raise ValueError("Give the full path of a folder, starting from the drive.")

    try:
        folder = folder.resolve(strict=True)
    except OSError as exc:
        raise ValueError("That folder was not found.") from exc

    if not folder.is_dir():
        raise ValueError("That is a file. Open the folder it is in.")
    if folder.parent == folder:
        raise ValueError("A whole drive cannot be opened. Choose a folder on it.")
    if _key(folder) == _key(Path.home().resolve()):
        raise ValueError("Your whole user folder cannot be opened. Choose a folder in it.")
    if any(is_closed_folder_name(part) for part in folder.parts):
        raise ValueError("That folder holds sign-ins or keys and cannot be opened.")

    program_data = Path.home().resolve() / "AppData"

    if any(_key(folder) == _key(program_data / name) for name in PROGRAM_DATA_FOLDERS):
        raise ValueError(
            "That is where programs keep their settings and sign-ins. "
            "It cannot be opened."
        )

    for kept in closed:
        kept = Path(kept).resolve()
        if _inside(folder, kept):
            raise ValueError("That is where Freya keeps her own data. It cannot be opened.")

    return folder


class FileReader:
    """
    Reads text files in the folders the owner has opened, and nowhere
    else. It never writes, moves or deletes.

    `folders` gives the opened folders each time it is asked, so a
    change takes effect at once. `closed` names places that stay shut
    even inside an opened folder: Freya's own data.

    Every path is resolved to where it really is before it is checked,
    so a link or a shortcut inside an opened folder cannot lead out of
    it.
    """

    def __init__(self, folders: Callable[[], Sequence[str]], closed: Iterable = ()):
        self._folders = folders
        self._closed = [Path(place).resolve() for place in closed]

    def roots(self) -> List[Path]:
        """
        The opened folders that are still what the owner opened.

        A folder was checked when it was opened, and is checked again
        here every time. If its path now leads somewhere else, because
        it was replaced by a link or a junction, it is no longer the
        folder the owner agreed to and is left out: the place it now
        leads to was never opened. A folder that has since become a
        place that may not be opened is left out too.
        """
        roots = []

        for folder in self._folders() or ():
            try:
                resolved = Path(folder).resolve(strict=True)
                named = Path(os.path.abspath(folder))
            except (OSError, TypeError, ValueError):
                continue

            if _key(resolved) != _key(named):
                continue

            try:
                check_readable_folder(str(resolved), self._closed)
            except ValueError:
                continue

            if resolved not in roots:
                roots.append(resolved)

        return roots

    def is_open(self) -> bool:
        return bool(self.roots())

    def _withheld(self, path: Path, root: Path) -> bool:
        inner = Path(_key(path)).relative_to(_key(root)).parts

        return (
            any(is_closed_folder_name(part) for part in inner)
            or (bool(inner) and is_secret_name(path.name))
            or any(_inside(path, kept) for kept in self._closed)
        )

    def _resolve(self, text) -> Path:
        roots = self.roots()

        if not roots:
            raise FileAccessError(NOTHING_OPEN)

        text = str(text).strip().strip('"')

        # A colon after the drive letter names a hidden stream of a
        # file on Windows, which is not what was opened.
        if ":" in text[2:] or "\x00" in text:
            raise FileAccessError(OUTSIDE)

        asked = Path(text)
        tries = (
            [asked] if asked.is_absolute()
            else [root / asked for root in roots]
        )

        for candidate in tries:
            # Where it claims to be, before the disk is touched.
            claimed = Path(os.path.normpath(candidate))

            if not any(_inside(claimed, root) for root in roots):
                continue

            # The name asked for counts as well as where it leads: a
            # link called .env is still a .env.
            if any(
                _inside(claimed, root) and self._withheld(claimed, root)
                for root in roots
            ):
                raise FileAccessError(WITHHELD)

            try:
                real = claimed.resolve(strict=True)
            except OSError:
                continue

            # Where it really is, after following any link.
            for root in roots:
                if _inside(real, root):
                    if self._withheld(real, root):
                        raise FileAccessError(WITHHELD)
                    return real

        raise FileAccessError(OUTSIDE)

    @_path_free
    def list(self, path: str) -> dict:
        """
        What is in one folder. A path of "." gives the opened folders
        themselves.
        """
        if str(path).strip() in (".", ""):
            roots = self.roots()

            if not roots:
                raise FileAccessError(NOTHING_OPEN)

            return {"opened_folders": [str(root) for root in roots]}

        folder = self._resolve(path)

        if not folder.is_dir():
            raise FileAccessError("That is a file, not a folder. Read it instead.")

        root = next(root for root in self.roots() if _inside(folder, root))
        entries = []
        left_out = withheld = 0

        with os.scandir(folder) as found:
            for entry in sorted(found, key=lambda item: item.name.lower()):
                # Past what can be shown, entries are only counted:
                # resolving each one costs several system calls, and a
                # folder of a hundred thousand files must not hold the
                # turn. The count is of names, so it includes any that
                # would have been withheld.
                if len(entries) >= MAX_LISTED:
                    left_out += 1
                    continue

                try:
                    real = Path(entry.path).resolve(strict=True)
                    is_folder = real.is_dir()
                    size = None if is_folder else real.stat().st_size
                except OSError:
                    continue

                # A link that leads out, or anything withheld by its
                # own name or by where it leads, is not shown. That
                # something was left out is said, so that an absent
                # name is not taken to mean there is no such file.
                if (
                    not _inside(real, root)
                    or self._withheld(real, root)
                    or is_secret_name(entry.name)
                    or is_closed_folder_name(entry.name)
                ):
                    withheld += 1
                    continue

                entries.append(
                    {"name": entry.name, "folder": True} if is_folder
                    else {"name": entry.name, "bytes": size}
                )

        result = {"folder": str(folder), "entries": entries}

        if withheld:
            result["withheld"] = (
                f"{withheld} more here are of a kind that is never "
                "shown or read, such as key and sign-in files."
            )

        # Entries past what fits are counted, not cut off.
        while entries and _encoded_size(dict(result, not_shown=left_out + 1)) > MAX_RESULT_BYTES:
            entries.pop()
            left_out += 1

        if left_out:
            result["not_shown"] = left_out

        return result

    @staticmethod
    def _text_of(path: Path) -> str:
        # Only an ordinary file is read: a pipe or a device has no
        # end, and opening one would hold the turn for ever.
        if not stat.S_ISREG(path.stat().st_mode):
            raise FileAccessError(
                "That file is not text, so it cannot be read."
            )

        with path.open("rb") as handle:
            start = handle.read(SNIFF_BYTES)

            if b"\x00" in start:
                raise FileAccessError(
                    "That file is not text, so it cannot be read."
                )

            return (start + handle.read()).decode("utf-8", errors="replace")

    @_path_free
    def read(self, path: str, start_line: str = "1") -> dict:
        """
        Part of one text file, from a given line: at most
        MAX_READ_LINES lines and MAX_READ_CHARACTERS characters.
        """
        try:
            first = int(str(start_line).strip())
        except ValueError as exc:
            raise FileAccessError("start_line must be a whole number, from 1.") from exc

        if first < 1:
            raise FileAccessError("start_line must be a whole number, from 1.")

        file = self._resolve(path)

        if file.is_dir():
            raise FileAccessError("That is a folder, not a file. List it instead.")

        if file.stat().st_size > MAX_READ_BYTES:
            raise FileAccessError("That file is too large to read.")

        lines = self._text_of(file).splitlines()
        chosen, used = [], 0

        for line in lines[first - 1:first - 1 + MAX_READ_LINES]:
            if len(line) > MAX_LINE_CHARACTERS:
                line = line[:MAX_LINE_CHARACTERS] + " [line cut]"
            if used + len(line) > MAX_READ_CHARACTERS and chosen:
                break
            chosen.append(line)
            used += len(line) + 1

        def result():
            last = first + len(chosen) - 1
            return {
                "file": str(file),
                "first_line": first,
                "last_line": last,
                "total_lines": len(lines),
                "more": last < len(lines),
                "text": "\n".join(chosen),
            }

        # The line numbers must describe what the model is given, so
        # lines are dropped, not cut, until the whole result fits. One
        # line is always given, cut if it must be.
        while len(chosen) > 1 and _encoded_size(result()) > MAX_RESULT_BYTES:
            chosen.pop()
        if chosen and _encoded_size(result()) > MAX_RESULT_BYTES:
            excess = _encoded_size(result()) - MAX_RESULT_BYTES + len(" [line cut]")
            chosen[0] = chosen[0][:max(0, len(chosen[0]) - excess)] + " [line cut]"

        return result()

    @_path_free
    def search(self, folder: str, text: str) -> dict:
        """
        The lines holding some text, in the text files under one
        folder, without regard to capitals. Bounded in files, size
        and time; the result says when it stopped early.
        """
        wanted = str(text).strip().lower()

        if not wanted:
            raise FileAccessError("Say what text to look for.")

        start = self._resolve(folder)

        if not start.is_dir():
            raise FileAccessError("That is a file, not a folder. Search the folder it is in.")

        root = next(root for root in self.roots() if _inside(start, root))
        deadline = time.monotonic() + SEARCH_SECONDS
        matches, searched, stopped = [], 0, None

        for current, folders, files in os.walk(start):
            # Checked for every folder, so a tree of empty folders
            # cannot outlast the time allowed.
            if time.monotonic() > deadline:
                stopped = "the time allowed ran out"
                break

            here = Path(current)
            folders[:] = sorted(
                name for name in folders
                if name.lower() not in SKIPPED_IN_SEARCH
                and not is_closed_folder_name(name)
                and not (here / name).is_symlink()
                and not any(_inside(here / name, kept) for kept in self._closed)
            )

            for name in sorted(files):
                if is_secret_name(name):
                    continue
                if time.monotonic() > deadline:
                    stopped = "the time allowed ran out"
                elif searched >= MAX_SEARCHED_FILES:
                    stopped = "the limit on files was reached"
                elif len(matches) >= MAX_MATCHES:
                    stopped = "the limit on matches was reached"
                if stopped:
                    break

                file = here / name

                try:
                    real = file.resolve(strict=True)
                    if (
                        not _inside(real, root)
                        or self._withheld(real, root)
                        or real.stat().st_size > MAX_SEARCHED_BYTES
                    ):
                        continue
                    content = self._text_of(real)
                except (OSError, FileAccessError):
                    continue

                searched += 1

                for number, line in enumerate(content.splitlines(), 1):
                    if wanted in line.lower():
                        matches.append({
                            "file": str(file),
                            "line": number,
                            "text": line.strip()[:MAX_LINE_CHARACTERS],
                        })
                        if len(matches) >= MAX_MATCHES:
                            break

            if stopped:
                break

        result = {
            "folder": str(start),
            "files_searched": searched,
            "matches": matches,
        }

        if stopped or len(matches) >= MAX_MATCHES:
            result["stopped_early"] = stopped or "the limit on matches was reached"

        # Matches past what fits are dropped and said to be, so a
        # narrower search can be asked for.
        while matches and _encoded_size(dict(result, stopped_early="the limit on matches was reached")) > MAX_RESULT_BYTES:
            matches.pop()
            result["stopped_early"] = "the limit on matches was reached"

        return result


def create_file_reader_provider(reader: FileReader) -> ProviderDefinition:
    """
    The provider is healthy only while a folder is open, so the tools
    are not offered to the model at all until the owner opens one.
    """
    return ProviderDefinition(
        provider_id=FILE_READER_PROVIDER,
        name="Files in folders the owner opened",
        capabilities=list(FILE_READER_CAPABILITIES),
        start_handler=lambda: None,
        health_handler=reader.is_open,
    )


def register_file_reader_capabilities(registry, reader: FileReader) -> None:
    path = {
        "description": (
            "The full path, or a path inside one of the opened folders."
        ),
        "max_length": 400,
    }

    registry.register(
        CapabilityDefinition(
            name=FILES_LIST,
            description=(
                "Lists what is inside one folder the user has opened "
                "for you to read: names, sizes in bytes, and which are "
                "folders. Give \".\" to see which folders are opened. "
                "Use it to find a file before reading it. To find "
                "where some text appears, use files_search instead."
            ),
            provider=FILE_READER_PROVIDER,
            handler=reader.list,
            category="files",
            metadata={MODEL_TOOL: True, TOOL_PARAMETERS: {"path": path}},
        )
    )
    registry.register(
        CapabilityDefinition(
            name=FILES_READ,
            description=(
                "Returns the text of one file in a folder the user has "
                f"opened, up to {MAX_READ_LINES} lines at a time from "
                "start_line, with the last line given, the file's total "
                "number of lines and whether there is more; read on from "
                "last_line + 1. Use it whenever the user asks "
                "about a file or its code, and read before you answer "
                "rather than guessing what a file says."
            ),
            provider=FILE_READER_PROVIDER,
            handler=reader.read,
            category="files",
            metadata={
                MODEL_TOOL: True,
                TOOL_PARAMETERS: {
                    "path": path,
                    "start_line": {
                        "description": (
                            "The line to start from, as a number. Use "
                            "1 for the beginning."
                        ),
                        "max_length": 8,
                    },
                },
            },
        )
    )
    registry.register(
        CapabilityDefinition(
            name=FILES_SEARCH,
            description=(
                "Finds the lines that hold some text in the files under "
                "one folder the user has opened, ignoring capitals, and "
                f"returns up to {MAX_MATCHES} of them with their file "
                "and line number. Use it to find where a word, name or "
                "function appears before reading the file around it."
            ),
            provider=FILE_READER_PROVIDER,
            handler=reader.search,
            category="files",
            metadata={
                MODEL_TOOL: True,
                TOOL_PARAMETERS: {
                    "folder": path,
                    "text": {
                        "description": (
                            "The exact text to look for, such as a "
                            "word or a function's name."
                        ),
                        "max_length": 200,
                    },
                },
            },
        )
    )
