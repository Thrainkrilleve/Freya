import contextlib
import io
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from freya.file_access import FOLDER_COMMANDS
from freya.hands_free import LISTENING_COMMANDS, handle_listening_command


HELP_COMMAND = "/help"
TERMINAL_COMMANDS = ("/terminal", "/console")
QUIT_COMMANDS = ("exit", "quit", "/exit", "/quit")

# Commands that only make sense with a keyboard attached to a terminal.
KEYBOARD_ONLY = ("/talk", "/listen", "/conversation", "/voice-file")

HELP_TEXT = (
    "Commands: /names, /name add <name>, /name remove <name>, /mute, "
    "/unmute, /interrupt, /image <description>, /folders, /folder open "
    "<path>, /folder close <path>, /terminal, /help. "
    "/memory-help lists the memory commands."
)


@dataclass
class CommandContext:
    """
    What a command typed in the window may act on.
    """

    bus: object
    voice: object
    listener: object
    settings: object
    window: object
    normalize: Callable[[str], str]
    run_image: Callable[[str], None]
    publish_status: Callable[[], None]

    # Set only when Freya runs without a terminal, where the window is
    # the one place she can be told to quit.
    request_quit: Optional[Callable[[], None]] = None

    # The assistant's memory-command handler: given a line, it returns
    # the answer, or None when the line is not a memory command.
    memory_command: Optional[Callable[[str], Optional[str]]] = None

    # Opens and closes the folders Freya may read: given a line, it
    # answers on the console and returns True if it was its command.
    folder_command: Optional[Callable[[str], bool]] = None


class _Both(io.TextIOBase):
    """
    Writes to two streams at once: everything to the first, and what
    the thread that made it writes to the second as well.

    `redirect_stdout` swaps the stream for the whole process, so while
    a command runs, the listener thread's notices (a wake word heard,
    the microphone muted) would otherwise be captured too and shown in
    the chat as the command's answer.
    """

    def __init__(self, first, second):
        self._streams = (first, second)
        self._thread = threading.get_ident()

    def writable(self):
        return True

    def write(self, text):
        self._streams[0].write(text)

        if threading.get_ident() == self._thread:
            self._streams[1].write(text)

        return len(text)

    def flush(self):
        for stream in self._streams:
            stream.flush()


def _dispatch(line: str, context: CommandContext) -> bool:
    """
    Run one command. Returns True when what it printed should also be
    shown in the chat.
    """
    lowered = line.lower()
    command = lowered.split()[0] if lowered.split() else ""

    if lowered in QUIT_COMMANDS:
        if context.request_quit is None:
            print(
                "Freya: I am running in a terminal. Type exit there "
                "to quit."
            )
        else:
            print("Freya: Closing down.")
            context.request_quit()

        return True

    if command == HELP_COMMAND:
        print(f"Freya: {HELP_TEXT}")
        return True

    if command in TERMINAL_COMMANDS:
        print("Freya: The Console tab shows what my terminal shows.")
        return True

    if command == "/interrupt":
        context.voice.interrupt()
        print("Freya: Speech playback interrupted.")
        return True

    if command in LISTENING_COMMANDS:
        handle_listening_command(
            line,
            context.listener,
            context.settings,
        )
        context.publish_status()
        return True

    if command in FOLDER_COMMANDS:
        if context.folder_command is None:
            print("Freya: Folders cannot be opened from here.")
        else:
            context.folder_command(line)
        return True

    if command == "/image":
        prompt = line[len("/image"):].strip()

        if not prompt:
            print("Usage: /image <description>")
            return True

        context.run_image(prompt)
        context.publish_status()

        # The Images tab and the image events already report this.
        return False

    if command == "/window":
        print(context.window.open())
        return True

    if command in KEYBOARD_ONLY:
        print(
            f"Freya: {command} needs a keyboard in my terminal. My "
            "microphone is already listening here: say my name, or "
            "type in the chat."
        )
        return True

    # Memory commands (/memories, /forget, /memory-help and the rest)
    # are the assistant's own; it answers None for anything else.
    if context.memory_command is not None:
        # The window treats a leading backslash as a slash throughout.
        if line.startswith("\\"):
            line = "/" + line[1:]

        answer = context.memory_command(line)

        if answer is not None:
            print(f"Freya: {answer}")
            return True

    print(f"Freya: I don't know the command {command}. {HELP_TEXT}")
    return True


def run_window_command(text: str, context: CommandContext) -> None:
    """
    Run a slash command typed in the window, exactly as the terminal
    would, and show its answer in the chat as well as the console.
    """
    line = context.normalize(text.strip())

    print(f"\rYou (window): {line}")

    captured = io.StringIO()

    with contextlib.redirect_stdout(_Both(sys.stdout, captured)):
        announce = _dispatch(line, context)

    if not announce:
        return

    # Each line is tidied but lines are kept, so a list stays a list.
    answer = "\n".join(
        " ".join(part.split())
        for part in captured.getvalue().splitlines()
        if part.strip()
    )

    if answer.startswith("Freya: "):
        answer = answer[len("Freya: "):]

    if answer:
        context.bus.publish("notice", text=answer)
