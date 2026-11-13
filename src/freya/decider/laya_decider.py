"""
Laya's English checkpoint as a decider, for measuring it in shadow
(docs/design/local-decider.md, section 4.6, stage 1).

Laya runs as a helper program in its own environment, because its
torch and transformers versions must not be installed into Freya's.
`python -m freya.decider.install laya` sets it up (an owner action:
about 3 GB from PyPI and Hugging Face). Until then this decider is not
ready, and nothing asks it anything.
"""
import os
import unicodedata
from pathlib import Path
from typing import Callable, Mapping, Optional

from freya.decider.contract import (
    CHOICE,
    NO,
    SCORE,
    YES,
    YESNO,
    DecisionPoint,
)


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
WORKERS_DIR = Path(__file__).resolve().parent / "workers"
ENVS_DIR = Path(os.environ.get("FREYA_DECIDER_ENVS") or DATA_DIR / "decider_envs")
MODELS_DIR = Path(os.environ.get("FREYA_DECIDER_MODELS") or DATA_DIR / "decider_models")

LAYA_ENV = "laya"
LAYA_PACKAGE = "laya==0.4.24"
LAYA_REPOSITORY = "convaiinnovations/laya"
# The commit Laya's own authors list as reviewed (laya.revisions); the
# weights are always loaded from this commit and no other.
LAYA_REVISION = "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851"
LAYA_DECIDER = f"laya-en@{LAYA_REVISION[:12]}"

# The English checkpoint answers text in other scripts with high
# confidence and no accuracy (its authors' own finding), so such text
# goes to the fallback (proposed NEW-CN).
MIN_LATIN_SHARE = 0.9
REQUEST_TIMEOUT_SECONDS = 60


def environment_python(envs_dir=None) -> Path:
    root = Path(envs_dir or ENVS_DIR) / LAYA_ENV
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def mostly_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if "LATIN" in unicodedata.name(c, ""))
    return latin / len(letters) >= MIN_LATIN_SHARE


def laya_question(point: DecisionPoint) -> dict:
    """The question in Laya's own form."""
    question = point.question

    if question.kind == YESNO:
        return {"type": "noul", "instructions": question.wording}
    if question.kind == CHOICE:
        return {
            "type": "choice",
            "instructions": question.wording,
            "criteria": {option: option.replace("-", " ") for option in question.options},
        }
    if question.kind == SCORE:
        return {
            "type": "score",
            "instructions": question.wording,
            "criteria": [option.replace("-", " ") for option in question.options],
        }
    raise ValueError("Unknown kind of question.")


def distribution_from(point: DecisionPoint, reply: dict) -> Mapping[str, float]:
    """Laya's answer as a probability for each of the question's options."""
    question = point.question

    if question.kind == YESNO:
        p_true = float(reply["p_true"])
        return {NO: 1.0 - p_true, YES: p_true}

    probabilities = reply["probabilities"]

    if question.kind == SCORE:
        # Laya numbers the levels from 0, lowest first.
        return {option: probabilities[str(i)] for i, option in enumerate(question.options)}

    return {option: probabilities[option] for option in question.options}


def _default_helper():
    from freya.speech.helper import HelperProcess

    return HelperProcess(
        environment_python(),
        WORKERS_DIR / "laya_worker.py",
        log_path=DATA_DIR / "decider_laya.log",
        environment={
            "HF_HOME": str(MODELS_DIR),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "LAYA_REVISION": LAYA_REVISION,
            "USE_TF": "0",
        },
    )


class LayaDecider:
    def __init__(self, helper=None, installed: Optional[Callable[[], bool]] = None):
        self._helper = helper
        self._installed = installed or (lambda: environment_python().is_file())

    @property
    def decider_id(self) -> str:
        return LAYA_DECIDER

    def ready(self) -> bool:
        return bool(self._installed())

    def stop(self) -> None:
        if self._helper is not None:
            self._helper.stop()

    def decide(self, point: DecisionPoint, state: str) -> Optional[Mapping[str, float]]:
        if not mostly_latin(state):
            return None
        if self._helper is None:
            self._helper = _default_helper()

        reply = self._helper.request(
            {"op": "decide", "state": state, "question": laya_question(point)},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        return distribution_from(point, reply)
