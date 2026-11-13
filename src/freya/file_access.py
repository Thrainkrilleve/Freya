from pathlib import Path
from typing import Callable, Optional

from freya.storage.locations import bulk_root
from freya.tools.file_tools import FileReader, check_readable_folder


DATA_DIR = Path(__file__).resolve().parents[2] / "data"

FOLDERS_COMMAND = "/folders"
FOLDER_COMMAND = "/folder"

FOLDER_COMMANDS = (
    FOLDERS_COMMAND,
    FOLDER_COMMAND,
)

FOLDER_USAGE = (
    "Usage: /folders, /folder open <full path>, or /folder close <full path>"
)


def kept_shut():
    """
    Where Freya keeps her own data. It stays shut to the file tools
    even when a folder around it is opened.
    """
    places = [DATA_DIR]
    moved = bulk_root()

    if moved is not None:
        places.append(moved)

    return places


class FileAccess:
    """
    The folders the owner has opened for Freya to read, and the
    commands that open and close them. Only the owner's own command
    changes the list: nothing the model can call does.
    """

    def __init__(self, settings, refresh: Optional[Callable[[], None]] = None, closed=None):
        self.settings = settings
        self._refresh = refresh
        self._closed = kept_shut() if closed is None else list(closed)
        self.reader = FileReader(lambda: settings.readable_folders, self._closed)

    def describe(self) -> str:
        folders = self.settings.readable_folders

        if not folders:
            return (
                "No folder is open, so I cannot read any files. Open one "
                "with /folder open <full path>."
            )

        return "I can read text files in: " + "; ".join(folders) + "."

    def command(self, user_input: str) -> bool:
        """
        Handle /folders and /folder. Returns True when the input was
        one of these commands.
        """
        words = user_input.split(maxsplit=2)

        if not words or words[0].lower() not in FOLDER_COMMANDS:
            return False

        if words[0].lower() == FOLDERS_COMMAND:
            print(f"\nFreya: {self.describe()}\n")
            return True

        action = words[1].lower() if len(words) > 1 else ""

        if len(words) < 3 or action not in ("open", "close"):
            print(f"\n{FOLDER_USAGE}\n")
            return True

        try:
            if action == "open":
                folder = check_readable_folder(words[2], self._closed)
                self.settings.add_readable_folder(str(folder))
                answer = (
                    f"I can now read text files in {folder}. I can only "
                    "read there, never change anything."
                )
            else:
                folder = self.settings.remove_readable_folder(words[2])
                answer = f"I can no longer read files in {folder}."
        except ValueError as exc:
            print(f"\nFreya: {exc}\n")
            return True

        if self._refresh is not None:
            self._refresh()

        print(f"\nFreya: {answer}\n")
        return True
