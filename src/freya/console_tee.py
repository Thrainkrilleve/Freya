import io
import sys
import threading
from collections import deque
from typing import List


class ConsoleOutput:
    """
    Keeps a copy of everything Freya prints, so her window can show
    what the terminal would show.

    Output still goes wherever it went before (the terminal, or the
    log file when there is no terminal). Each finished line is also
    remembered and announced on the event bus.
    """

    def __init__(self, bus, limit=1000):
        self._bus = bus
        self._lock = threading.Lock()
        self._lines = deque(maxlen=limit)
        self._announcing = threading.local()
        self._installed = None

    def history(self) -> List[str]:
        with self._lock:
            return list(self._lines)

    def add_line(self, line: str) -> None:
        # A carriage return means "start this line again", as when a
        # spoken turn replaces the waiting "You: " prompt.
        line = line.rsplit("\r", 1)[-1].rstrip()

        # Announcing must never print, or it would call itself.
        if getattr(self._announcing, "busy", False):
            with self._lock:
                self._lines.append(line)
            return

        self._announcing.busy = True

        try:
            # Remembered and announced in one step under the bus's
            # lock, so a window joining in between finds the line in
            # the history it is given or in its live events, not both.
            with self._bus.locked():
                with self._lock:
                    self._lines.append(line)
                self._bus.publish("console", text=line)
        finally:
            self._announcing.busy = False

    def install(self) -> None:
        """
        Start copying stdout and stderr.
        """
        if self._installed is not None:
            return

        self._installed = (sys.stdout, sys.stderr)
        sys.stdout = ConsoleTee(sys.stdout, self)
        sys.stderr = ConsoleTee(sys.stderr, self)

    def uninstall(self) -> None:
        if self._installed is None:
            return

        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, ConsoleTee):
                stream.finish()

        sys.stdout, sys.stderr = self._installed
        self._installed = None


class ConsoleTee(io.TextIOBase):
    """
    A stream that passes text through to another stream and hands
    each complete line to a ConsoleOutput.
    """

    def __init__(self, stream, output: ConsoleOutput):
        self._stream = stream
        self._output = output
        self._pending = ""
        self._lock = threading.Lock()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", None) or "utf-8"

    def writable(self):
        return True

    def isatty(self):
        try:
            return bool(self._stream and self._stream.isatty())
        except Exception:
            return False

    def fileno(self):
        if self._stream is None:
            raise io.UnsupportedOperation("fileno")

        return self._stream.fileno()

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)

        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                pass

        with self._lock:
            self._pending += text
            *lines, self._pending = self._pending.split("\n")

        for line in lines:
            self._output.add_line(line)

        return len(text)

    def flush(self):
        if self._stream is not None:
            try:
                self._stream.flush()
            except Exception:
                pass

    def finish(self):
        """
        Hand over a last line that never got its newline.
        """
        with self._lock:
            pending, self._pending = self._pending, ""

        if pending.strip():
            self._output.add_line(pending)
