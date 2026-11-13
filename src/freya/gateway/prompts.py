import secrets
import threading
from typing import Callable, Dict, Optional


# How long a question waits for the owner. No answer is a denial.
PROMPT_SECONDS = 120.0

PROMPT = "cloud.prompt"
PROMPT_CLOSED = "cloud.prompt.closed"


class _Waiting:
    """One question that has been put and not yet answered."""

    def __init__(self, choices):
        self.choices = frozenset(choices)
        self.answer: Optional[str] = None
        self.done = threading.Event()
        # The windows the question was shown in. When the last of
        # them goes, nobody can answer, and waiting on is pointless.
        self.reached: list = []


class OwnerPrompts:
    """
    Puts a question to the owner in the window and takes back one
    answer.

    A question can carry the full text of a brief, which is personal
    data. So it is handed straight to each connected window and is
    never published on the event bus, whose history is replayed to
    every window opened later. A window that connects while a question
    is waiting is not shown it; a question that finds no window to show
    it in is a denial.

    Each question has a random id. An answer counts only for an id that
    is still waiting, only once, and only if it is one of the answers
    the question offered. The first answer wins; the other windows are
    told the question is closed. No answer within the time limit, and
    Freya closing, are denials.

    `deliver(message, recipients=None)` sends a message to every
    connected window, or only to `recipients` (windows it returned
    earlier), and returns the windows it reached.
    """

    def __init__(
        self,
        deliver: Callable[..., list],
        seconds: float = PROMPT_SECONDS,
    ):
        self._deliver = deliver
        self._seconds = seconds
        self._lock = threading.Lock()
        self._waiting: Dict[str, _Waiting] = {}
        self._closed = False

    def ask(self, prompt: dict, seconds: Optional[float] = None) -> Optional[str]:
        """
        Show `prompt` and wait for the owner. Returns the id of the
        answer chosen, or None when there was no answer.
        """
        choices = [
            choice.get("id") for choice in prompt.get("choices", [])
            if isinstance(choice, dict)
        ]
        if not choices or not all(isinstance(c, str) for c in choices):
            return None

        waiting = _Waiting(choices)
        prompt_id = secrets.token_urlsafe(16)

        with self._lock:
            if self._closed:
                return None

            self._waiting[prompt_id] = waiting

        try:
            reached = self._deliver({"type": PROMPT, "id": prompt_id, **prompt})
        except Exception:
            reached = []

        with self._lock:
            waiting.reached = list(reached)

        if reached:
            waiting.done.wait(self._seconds if seconds is None else seconds)

        with self._lock:
            # If the answer arrived first it was removed when it was
            # taken, and `waiting.answer` is already set.
            self._waiting.pop(prompt_id, None)
            answer = waiting.answer

        # Only the windows that were shown the question are told it is
        # closed; a window opened since has never heard of it.
        try:
            self._deliver({"type": PROMPT_CLOSED, "id": prompt_id}, reached)
        except Exception:
            pass

        return answer

    def answer(self, prompt_id, choice) -> bool:
        """
        An answer from a window. Returns whether it was taken: it is
        not if the id is not waiting (unknown, already answered, or
        timed out) or the choice is not one the question offered.
        """
        if not isinstance(prompt_id, str) or not isinstance(choice, str):
            return False

        with self._lock:
            waiting = self._waiting.get(prompt_id)

            if waiting is None or choice not in waiting.choices:
                return False

            del self._waiting[prompt_id]
            waiting.answer = choice

        waiting.done.set()

        return True

    def window_gone(self, window) -> None:
        """
        A window has disconnected. A question that was shown only in
        windows now gone can never be answered, so it ends now, as a
        denial, instead of holding the turn for the whole time limit.
        """
        ended = []

        with self._lock:
            for prompt_id, waiting in list(self._waiting.items()):
                waiting.reached = [w for w in waiting.reached if w is not window]

                if not waiting.reached:
                    del self._waiting[prompt_id]
                    ended.append(waiting)

        for waiting in ended:
            waiting.done.set()

    def close(self) -> None:
        """
        Freya is closing. Every question waiting is ended at once as a
        denial, and no new one is put.
        """
        with self._lock:
            self._closed = True
            waiting = list(self._waiting.values())

        for entry in waiting:
            entry.done.set()
