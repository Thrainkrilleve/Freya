"""
Labelled synthetic examples for measuring deciders, one set per
decision point (docs/design/local-decider.md, section 5).

Nothing here is personal: every message is made up. The labels were
written by hand for this suite and have not yet been checked by the
owner, which NEW-CM asks for before any calibration is fitted on them.

Each set deliberately includes messages today's rules get wrong (a
past question worded without the pattern's phrases, a picture asked
for in other words), so a measurement shows what a decider would add,
and messages that look close but are not (a reminder to remember
something, a question about a document).

Every example is either for training and calibration or held out, by
a fixed hash of its point and text, so the split never changes when
examples are added and a held-out example is never trained on.
"""
import hashlib
from dataclasses import dataclass
from typing import Dict, Tuple

from freya.decider.contract import NO, YES
from freya.decider.points import IMAGE_REQUEST, PAST_QUESTION, TASK_KIND
from freya.evaluation.delegation_tasks import TASKS


# The share of each set held out, in tenths.
HELD_OUT_TENTHS = 3


@dataclass(frozen=True)
class Example:
    point: str
    text: str
    label: str

    @property
    def held_out(self) -> bool:
        digest = hashlib.sha256(f"{self.point}\n{self.text}".encode("utf-8")).digest()
        return digest[0] % 10 < HELD_OUT_TENTHS


def _set(point, pairs) -> Tuple[Example, ...]:
    return tuple(Example(point.name, text, label) for text, label in pairs)


PAST_QUESTION_SET = _set(PAST_QUESTION, (
    # Asked in the pattern's own words.
    ("Have I ever told you what my sister's name is?", YES),
    ("Did we ever talk about upgrading the power supply?", YES),
    ("Do you remember which film I said I wanted to see?", YES),
    ("What did I say about the garden last week?", YES),
    ("When did we decide to move the server to the closet?", YES),
    ("Can you recall the name I gave the new cat?", YES),
    ("What did we agree on for the holiday budget?", YES),
    ("Have we discussed the backup drive before?", YES),
    # Asked in other words; the pattern misses most of these.
    ("Remind me what we settled on for the kitchen colour.", YES),
    ("Wasn't there a restaurant I mentioned liking a while back?", YES),
    ("Which book was it that I was halfway through when we last spoke?", YES),
    ("Earlier this month I gave you my bike's frame size. What was it?", YES),
    ("What was the name of that podcast I recommended to you?", YES),
    ("Go back to our chat about the router. What model did I end up choosing?", YES),
    ("Didn't I tell you my dentist appointment got moved?", YES),
    ("Any idea what I decided about the gym membership?", YES),
    ("Pull up what I said on Sunday about the trip.", YES),
    ("We spoke about a birthday present for Sam. What was the idea?", YES),
    # Not about the past.
    ("What's the weather going to be like tomorrow?", NO),
    ("Remember to remind me about the bins on Thursday.", NO),
    ("Please remember that my favourite colour is green.", NO),
    ("How much free space is left on my drives?", NO),
    ("What did the report in this file say about sales?", NO),
    ("Make me an image of a lighthouse at dusk.", NO),
    ("What is the capital of Portugal?", NO),
    ("Tell me a joke about cats.", NO),
    ("Can you summarise this article for me?", NO),
    ("What time is it in Tokyo right now?", NO),
    ("How hot is the graphics card running?", NO),
    ("Write a short poem about autumn rain.", NO),
    ("What did Napoleon say before Waterloo?", NO),
    ("Do you know a good recipe for lentil soup?", NO),
    ("When did the Berlin Wall fall?", NO),
    ("What's the difference between RAM and storage?", NO),
    ("I want to plan a weekend in the mountains.", NO),
    ("Is it going to rain on Saturday?", NO),
))

IMAGE_REQUEST_SET = _set(IMAGE_REQUEST, (
    ("Make an image of a fox in the snow.", YES),
    ("Can you generate a picture of a red bicycle?", YES),
    ("Draw me an illustration of a dragon reading a book.", YES),
    ("Create a photo of a beach at sunrise.", YES),
    # Asked in other words; the patterns miss these.
    ("I'd love a painting of a castle on a hill.", YES),
    ("Show me what a cyberpunk city street might look like, as a picture.", YES),
    ("Could you sketch a cartoon owl for me?", YES),
    ("Render a 3D-style scene of a cosy cabin.", YES),
    ("Paint a watercolour of tulips in a vase.", YES),
    ("Give me some artwork of a ship in a storm.", YES),
    # Not asking for a new picture.
    ("What's in the image I made yesterday?", NO),
    ("How do I take a good photo of the moon?", NO),
    ("Describe the Mona Lisa.", NO),
    ("Delete the last picture.", NO),
    ("Draw up a plan for my week.", NO),
    ("Make a list of things to pack.", NO),
    ("Create a reminder for my meeting.", NO),
    ("How much video memory does image generation use?", NO),
    ("Picture this: you're on a beach. What would you do?", NO),
    ("What does the word illustration mean?", NO),
))

# The 91 delegated tasks already carry their kind: a ready labelled set.
TASK_KIND_SET = tuple(Example(TASK_KIND.name, task.brief, task.kind) for task in TASKS)

SETS: Dict[str, Tuple[Example, ...]] = {
    PAST_QUESTION.name: PAST_QUESTION_SET,
    IMAGE_REQUEST.name: IMAGE_REQUEST_SET,
    TASK_KIND.name: TASK_KIND_SET,
}
