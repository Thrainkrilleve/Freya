"""
The decision points a decider may be asked about.

Each is a question Freya already answers today, by a rule or by asking
the main model, and each names that existing path as its fallback.
Thresholds start strict and are lowered only from measurements
(docs/design/local-decider.md, section 5).
"""
from typing import Dict

from freya.decider.contract import CHOICE, DecisionPoint, Question, yesno
from freya.evaluation.delegation_tasks import KINDS


PAST_QUESTION = DecisionPoint(
    name="conversation.asks_about_past",
    question=yesno(
        "The message asks about something the user and Freya said, did or "
        "decided in an earlier conversation, so saved memories and stored "
        "conversations should be searched before answering."
    ),
    threshold=0.9,
    fallback="The ASKS_ABOUT_THE_PAST pattern in core/conversation_memory.py.",
)

IMAGE_REQUEST = DecisionPoint(
    name="image.requested",
    question=yesno(
        "The message asks Freya to make a new picture, image, photo or "
        "illustration."
    ),
    threshold=0.9,
    fallback="The image patterns in tools/tool_router.py.",
)

TASK_KIND = DecisionPoint(
    name="delegation.task_kind",
    question=Question(
        kind=CHOICE,
        options=tuple(KINDS),
        wording=(
            "Which kind of work does this brief ask for: extraction, "
            "summarising, classification, rewriting, reasoning, code "
            "review, planning or analysis?"
        ),
    ),
    threshold=0.8,
    fallback="The main model's choice of kind for work.delegate.",
)

POINTS: Dict[str, DecisionPoint] = {
    point.name: point for point in (PAST_QUESTION, IMAGE_REQUEST, TASK_KIND)
}
