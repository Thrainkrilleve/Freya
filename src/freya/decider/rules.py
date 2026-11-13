"""
A decider made of the rules Freya uses today.

It puts the existing patterns behind the decision contract, so that
the contract, the capability and the evaluation can be built and
measured before any model exists, and so a model can later be compared
with exactly what it would replace. A rule is certain when it answers;
where there is no rule, it abstains.
"""
from typing import Mapping, Optional

from freya.core.conversation_memory import ASKS_ABOUT_THE_PAST
from freya.decider.contract import NO, YES, DecisionPoint, certain
from freya.decider.points import IMAGE_REQUEST, PAST_QUESTION
from freya.tools.tool_router import FreyaToolRouter


RULES_DECIDER = "rules-v1"


class RulesDecider:
    def __init__(self):
        self._router = FreyaToolRouter()

    @property
    def decider_id(self) -> str:
        return RULES_DECIDER

    def ready(self) -> bool:
        return True

    def decide(self, point: DecisionPoint, state: str) -> Optional[Mapping[str, float]]:
        question = point.question

        if point.name == PAST_QUESTION.name:
            return certain(YES if ASKS_ABOUT_THE_PAST.search(state) else NO, question)

        if point.name == IMAGE_REQUEST.name:
            return certain(YES if self._router.route(state) else NO, question)

        return None
