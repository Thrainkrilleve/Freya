"""
The decision contract (proposed NEW-CI).

A decider reads some text and a question with a fixed set of answers
and returns a probability for each answer. It never writes prose.
Freya's code checks what it returned, picks the answer, and abstains
when the answer is not likely enough for the decision point.

A decision chooses between paths Freya's code already allows. Nothing
here grants a permission, approves an action, or stands for the
owner's consent (proposed NEW-CL); that is why a Decision has no field
that could carry one.
"""
import math
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Protocol, Tuple


CHOICE = "choice"
SCORE = "score"
YESNO = "yesno"
QUESTION_KINDS = (CHOICE, SCORE, YESNO)

YES = "yes"
NO = "no"
YESNO_ANSWERS = (NO, YES)

# The most text one decision reads. Longer text is refused rather than
# cut, so a decision is never made on part of what was asked about.
MAX_STATE_CHARACTERS = 8_000
# Encoder deciders do badly with many options (Laya's authors advise
# fewer than about 20); a longer list should be split into questions.
MAX_OPTIONS = 20
# How far a decider's probabilities may add up away from 1 before they
# are refused instead of normalised.
SUM_TOLERANCE = 1e-3

_OPTION = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_POINT = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class DecisionError(ValueError):
    """
    A request or a decider's answer that cannot be used. The message is
    fixed wording and never quotes the text being decided about, because
    the broker records it in the audit log.
    """


@dataclass(frozen=True)
class Question:
    kind: str
    # Choice: the options. Score: the levels, lowest first. Yes/no:
    # always ("no", "yes").
    options: Tuple[str, ...]
    # What is being asked, in words a decider is given with the text.
    wording: str
    # A changed question is a new version, so measurements and any
    # trust earned under the old wording do not carry over.
    version: int = 1

    def __post_init__(self):
        if self.kind not in QUESTION_KINDS:
            raise ValueError(f"Unknown kind of question: {self.kind!r}")
        if not isinstance(self.options, tuple):
            raise ValueError("Options are a tuple.")
        if self.kind == YESNO and self.options != YESNO_ANSWERS:
            raise ValueError("A yes/no question has the options ('no', 'yes').")
        if not 2 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"A question has 2 to {MAX_OPTIONS} options.")
        if len(set(self.options)) != len(self.options):
            raise ValueError("Options must differ.")
        for option in self.options:
            if not isinstance(option, str) or not _OPTION.match(option):
                raise ValueError(f"Bad option name: {option!r}")
        if not isinstance(self.wording, str) or not self.wording.strip():
            raise ValueError("A question needs its wording.")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("A version is a whole number from 1.")


def yesno(wording: str, version: int = 1) -> Question:
    return Question(YESNO, YESNO_ANSWERS, wording, version)


@dataclass(frozen=True)
class DecisionPoint:
    """
    One place in Freya's code where a decider may be asked. Points are
    registered in code; nothing a model or a user writes can make one.
    """

    name: str
    question: Question
    # The probability the chosen answer must reach. Below it the
    # decision abstains and the point's existing path decides.
    threshold: float
    # What decides at this point today, and so when the decider abstains.
    fallback: str

    def __post_init__(self):
        if not isinstance(self.name, str) or not _POINT.match(self.name):
            raise ValueError(f"Bad decision point name: {self.name!r}")
        if not isinstance(self.question, Question):
            raise ValueError("A decision point needs a Question.")
        if (
            isinstance(self.threshold, bool)
            or not isinstance(self.threshold, (int, float))
            or not 0.5 <= self.threshold <= 1
        ):
            raise ValueError("A threshold is a number from 0.5 to 1.")
        if not isinstance(self.fallback, str) or not self.fallback.strip():
            raise ValueError("A decision point names its fallback.")


@dataclass(frozen=True)
class Decision:
    point: str
    version: int
    # The chosen option, or None when the decision abstained.
    answer: Optional[str]
    # The probability of the most likely option, whether or not it
    # reached the threshold. 0 when the decider gave no distribution.
    probability: float
    # Every option's probability, in the question's order; empty when
    # the decider gave none.
    distribution: Tuple[Tuple[str, float], ...]
    abstained: bool
    decider: str
    elapsed_ms: float

    def as_dict(self) -> dict:
        return {
            "point": self.point,
            "version": self.version,
            "answer": self.answer,
            "probability": self.probability,
            "distribution": dict(self.distribution),
            "abstained": self.abstained,
            "decider": self.decider,
            "elapsed_ms": self.elapsed_ms,
        }


class Decider(Protocol):
    """
    Something that answers questions. `decide` returns a probability
    for every option, or None to abstain. It must give the same answer
    for the same point, text and weights (proposed NEW-CO).
    """

    @property
    def decider_id(self) -> str:
        """Names the decider and, for a model, its weights digest."""

    def ready(self) -> bool:
        ...

    def decide(self, point: DecisionPoint, state: str) -> Optional[Mapping[str, float]]:
        ...


def checked_state(state) -> str:
    if not isinstance(state, str):
        raise DecisionError("The text to decide about must be text.")
    if len(state) > MAX_STATE_CHARACTERS:
        raise DecisionError("The text to decide about is too long.")
    return state


def checked_decision(point: DecisionPoint, distribution, decider_id: str, elapsed_ms: float) -> Decision:
    """
    Turn what a decider returned into a Decision, refusing anything that
    is not a probability for exactly the question's options.

    Ties go to the option listed first, so the result does not depend
    on the order a decider happened to return them in.
    """
    question = point.question

    if distribution is None:
        return Decision(point.name, question.version, None, 0.0, (), True, decider_id, elapsed_ms)

    if not isinstance(distribution, Mapping) or set(distribution) != set(question.options):
        raise DecisionError("The decider's answer does not match the question's options.")

    values = []

    for option in question.options:
        value = distribution[option]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise DecisionError("The decider gave a probability outside 0 to 1.")
        values.append(float(value))

    total = sum(values)

    if abs(total - 1) > SUM_TOLERANCE:
        raise DecisionError("The decider's probabilities do not add up to 1.")

    values = [value / total for value in values]
    ordered = tuple(zip(question.options, values))
    best_option, best = max(ordered, key=lambda pair: pair[1])
    # max() keeps the first of equal values, which is the listed order.
    abstained = best < point.threshold

    return Decision(
        point=point.name,
        version=question.version,
        answer=None if abstained else best_option,
        probability=best,
        distribution=ordered,
        abstained=abstained,
        decider=decider_id,
        elapsed_ms=elapsed_ms,
    )


def certain(option: str, question: Question) -> Dict[str, float]:
    """A distribution that puts everything on one option, as a rule does."""
    if option not in question.options:
        raise ValueError(f"Not an option of this question: {option!r}")
    return {name: (1.0 if name == option else 0.0) for name in question.options}
