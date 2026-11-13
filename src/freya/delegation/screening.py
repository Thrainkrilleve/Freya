import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from freya.paths import data_dir


DATA_DIR = data_dir()
CORRECTIONS_PATH = DATA_DIR / "screening_corrections.json"

# How a brief matched something private.
MATCH_ALWAYS_PRIVATE = "always-private"
MATCH_UNAVAILABLE = "screening-unavailable"
MATCH_PHRASE = "phrase"
MATCH_NAME = "name"
MATCH_MEANING = "meaning"

# A run of this many words copied from a memory is a match.
PHRASE_WORDS = 4
# How close in meaning a sentence must be to a memory to be held.
MEANING_THRESHOLD = 0.82

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'@._+-]*")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

# Words too ordinary to identify anything, even when capitalised.
_COMMON = frozenset("""
a about after all also an and any are as at be because been before but by can
could did do does each for from had has have he her here him his how i if in
into is it its just like may me more most my no not of on one only or other
our out over she should so some than that the their them then there these
they this to up us was we were what when where which who why will with would
you your monday tuesday wednesday thursday friday saturday sunday january
february march april june july august september october november december
today tomorrow yesterday freya type key
""".split())


def _words(text) -> List[str]:
    return [word.strip("'._-").lower() for word in _WORD.findall(text or "")]


def _normal(text) -> str:
    return " ".join(_words(text))


def _distinctive(text) -> List[str]:
    """
    The words in a memory that could identify someone or something:
    names, addresses, and long numbers. A capitalised word counts
    unless it only starts a sentence or is an everyday word.
    """
    found = []

    for sentence in _SENTENCE.split(text or ""):
        tokens = _WORD.findall(sentence)

        for position, token in enumerate(tokens):
            bare = token.strip("'._-")
            lowered = bare.lower()

            if len(bare) < 3 or lowered in _COMMON:
                continue

            digits = sum(character.isdigit() for character in bare)
            is_address = "@" in bare
            is_number = digits >= 4
            is_name = bare[0].isupper() and (
                position > 0 or len(tokens) == 1
            )

            if is_address or is_number or is_name:
                found.append(lowered)

    return found


@dataclass(frozen=True)
class Match:
    """
    One reason a brief was held: what in the brief matched, how, and
    which memory it matched (None for an always-private phrase).
    """

    kind: str
    text: str
    memory_id: Optional[str] = None
    score: Optional[float] = None


@dataclass(frozen=True)
class ScreeningResult:
    matches: Tuple[Match, ...]

    @property
    def held(self) -> bool:
        return bool(self.matches)


class Corrections:
    """
    The owner's corrections to screening, kept on this machine.

    "Not private" stops a phrase, or one memory, from holding a brief.
    "Always private" holds any brief containing a phrase, or matching
    one memory, and wins over a not-private entry. Entries are made by
    the owner; nothing here is changed by a model.

    A new missing file means no corrections. An unreadable existing
    file blocks cloud screening and edits until the file is recovered.
    """

    _LISTS = (
        "not_private_phrases",
        "not_private_memories",
        "always_private_phrases",
        "always_private_memories",
    )

    def __init__(self, path=CORRECTIONS_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._lists: Dict[str, List[str]] = {name: [] for name in self._LISTS}
        self.available = True
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.available = False
            return

        if not isinstance(data, dict) or any(
            not isinstance(data.get(name, []), list)
            or any(not isinstance(entry, str) or not entry for entry in data.get(name, []))
            for name in self._LISTS
        ):
            self.available = False
            return

        self._lists = {name: list(data.get(name, [])) for name in self._LISTS}

    def _require_available(self):
        if not self.available:
            raise ValueError("Screening corrections must be recovered before editing.")

    def _save(self):
        self._require_available()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self._lists, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def _add(self, name, entry):
        if not entry:
            raise ValueError("There is nothing to mark.")
        with self._lock:
            self._require_available()
            if entry not in self._lists[name]:
                self._lists[name].append(entry)
                self._save()

    def mark_not_private(self, phrase=None, memory_id=None) -> None:
        self._mark("not_private", phrase, memory_id)

    def mark_always_private(self, phrase=None, memory_id=None) -> None:
        self._mark("always_private", phrase, memory_id)

    def _mark(self, prefix, phrase, memory_id):
        if (phrase is None) == (memory_id is None):
            raise ValueError("Mark either a phrase or a memory.")
        if phrase is not None:
            self._add(f"{prefix}_phrases", _normal(phrase))
        else:
            self._add(f"{prefix}_memories", str(memory_id))

    def remove(self, list_name, entry) -> None:
        if list_name not in self._LISTS:
            raise ValueError(f"Unknown list: {list_name!r}")
        with self._lock:
            self._require_available()
            if entry in self._lists[list_name]:
                self._lists[list_name].remove(entry)
                self._save()

    def listing(self) -> Dict[str, List[str]]:
        with self._lock:
            return {name: list(entries) for name, entries in self._lists.items()}


def screen_brief(
    brief: str,
    memories: Iterable[Tuple[str, str]],
    corrections: Optional[Corrections] = None,
    closeness: Optional[Callable[[str], Iterable[Tuple[str, float]]]] = None,
    meaning_threshold: float = MEANING_THRESHOLD,
) -> ScreeningResult:
    """
    Compare a brief bound for a cloud model with the personal store.

    `memories` are (id, text) pairs. `closeness`, when given, returns
    for one sentence the memories it is close to in meaning, with a
    score from 0 to 1; it runs on this machine like everything here.

    This is a filter, not a proof: it catches copied words, names and
    close restatement. A loose paraphrase can pass.
    """
    if corrections is not None and not corrections.available:
        return ScreeningResult((Match(
            MATCH_UNAVAILABLE, "Screening corrections are unreadable; recover them before sending.",
        ),))
    lists = corrections.listing() if corrections is not None else {
        name: [] for name in Corrections._LISTS
    }
    always_memories = set(lists["always_private_memories"])
    cleared_memories = set(lists["not_private_memories"]) - always_memories
    cleared_phrases = lists["not_private_phrases"]

    brief_words = _words(brief)
    brief_text = " ".join(brief_words)
    padded = f" {brief_text} "
    matches: List[Match] = []
    seen = set()

    def add(kind, text, memory_id=None, score=None):
        if memory_id in always_memories:
            # However it matched, it matched something the owner
            # marked always private, and is treated as that.
            kind = MATCH_ALWAYS_PRIVATE
        if (kind, text, memory_id) not in seen:
            seen.add((kind, text, memory_id))
            matches.append(Match(kind, text, memory_id, score))

    def cleared(text, memory_id):
        if memory_id in always_memories:
            return False
        if memory_id in cleared_memories:
            return True
        return any(
            f" {text} " in f" {phrase} " for phrase in cleared_phrases
        )

    for phrase in lists["always_private_phrases"]:
        if f" {phrase} " in padded:
            add(MATCH_ALWAYS_PRIVATE, phrase)

    for memory_id, text in memories:
        memory_id = str(memory_id)
        words = _words(text)

        for start in range(len(words) - PHRASE_WORDS + 1):
            run = " ".join(words[start:start + PHRASE_WORDS])
            if f" {run} " in padded and not cleared(run, memory_id):
                add(MATCH_PHRASE, run, memory_id)

        for word in _distinctive(text):
            if f" {word} " in padded and not cleared(word, memory_id):
                add(MATCH_NAME, word, memory_id)

    if closeness is not None:
        for sentence in _SENTENCE.split(brief or ""):
            if len(_words(sentence)) < 3:
                continue
            for memory_id, score in closeness(sentence):
                memory_id = str(memory_id)
                if score >= meaning_threshold and not cleared(
                    _normal(sentence), memory_id
                ):
                    add(MATCH_MEANING, sentence.strip(), memory_id, score)

    return ScreeningResult(tuple(matches))
