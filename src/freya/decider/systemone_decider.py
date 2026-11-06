"""
Open decision models served by Ollaya, for measuring them in shadow
(docs/design/local-decider.md, section 4.6, stage 1).

Ollaya (https://github.com/ollaya-dev/ollaya, Apache 2.0) runs open
decision models on this machine the way Ollama runs chat models, and
answers TypeSafe's System One questions (choice, score, true/false)
with a probability for every option. One client here can therefore
measure every candidate on the same examples, with each author's own
weights and calibration, without a Python environment per model.

This is a measurement route. Ollaya is a loopback HTTP server, which
the proposed NEW-CK does not allow for the decider Freya would use;
the model that wins is to be served by a helper process like Laya's,
or NEW-CK amended, before any decision point uses it. Installing
Ollaya and pulling the models is an owner action
(`python -m freya.decider.install candidates` lists the steps).
"""
import ipaddress
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, Mapping, Optional

from freya.decider.contract import (
    CHOICE,
    NO,
    SCORE,
    YES,
    YESNO,
    DecisionPoint,
)


DEFAULT_URL = "http://127.0.0.1:11435"
REQUEST_TIMEOUT_SECONDS = 120
MAX_ANSWER_BYTES = 1024 * 1024
QUESTION_ID = "q"


@dataclass(frozen=True)
class Candidate:
    """One open decision model to measure."""

    key: str
    # The model's name in Ollaya's library.
    tag: str
    name: str
    author: str
    licence: str
    parameters: str
    # Ollaya Bench edition 2026.10 (RTX 5090), for orientation only:
    # Freya's own sets decide.
    bench_score: Optional[float]
    bench_vram_gb: Optional[float]


# The five chosen on 2026-11-06 from Ollaya Bench's public results,
# for accuracy, calibration and what fits beside qwen3.8:27b on 24 GB.
CANDIDATES: Dict[str, Candidate] = {
    c.key: c
    for c in (
        Candidate(
            "pplx-decider", "pplx-decider", "pplx-decider-flash v1.2", "Perplexity",
            "Apache-2.0", "4B (Qwen3.5-4B)", 73.7, 10.4,
        ),
        Candidate(
            "d3", "d3", "Decision 3.0 (default, 4B)", "vLLM Semantic Router contributors",
            "Apache-2.0", "4B (Qwen3.5)", 68.0, 10.8,
        ),
        Candidate(
            "jeb", "jeb:4b", "Jebadiah 4B", "Jason Brashear (AINode)",
            "Apache-2.0", "4B (Qwen3.5-4B)", 63.9, 5.4,
        ),
        Candidate(
            "d1", "d1", "d1-3B", "Liquid AI",
            "LFM Open License v1.0 (free below $10M yearly revenue)", "3B (LFM2.5-VL-3B)",
            63.1, 4.0,
        ),
        Candidate(
            "nimble", "nimble", "Bespoke-Nimble-9B v2", "Bespoke Labs",
            "Apache-2.0", "9B (Qwen3.5-9B LoRA)", 61.9, 20.1,
        ),
    )
}


class OllayaError(RuntimeError):
    """
    Ollaya could not answer. The message holds Ollaya's error code or
    the HTTP status only, never its wording, which could quote the
    request.
    """


def server_url() -> str:
    return os.environ.get("FREYA_DECIDER_URL") or DEFAULT_URL


def checked_url(url: str) -> str:
    """
    Only plain HTTP to this machine: a decision must never leave it
    (proposed NEW-CJ).
    """
    parsed = urllib.parse.urlsplit(url)

    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError("The decider server must be an http:// address on this machine.")

    host = parsed.hostname

    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError("The decider server must be on this machine.")
        except ValueError as exc:
            raise ValueError("The decider server must be on this machine.") from exc

    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The decider server address must be plain.")

    return f"http://{parsed.netloc}{parsed.path.rstrip('/')}"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# No proxies from the environment (they would send the text elsewhere)
# and no redirects.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())


def _http_json(method: str, url: str, body: Optional[dict], timeout: float, key: str = "") -> dict:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}

    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = f"Bearer {key}"

    request = urllib.request.Request(url, data=data, method=method, headers=headers)

    try:
        response = _OPENER.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    except (OSError, ValueError) as exc:
        raise OllayaError(f"Ollaya could not be reached ({type(exc).__name__}).") from None

    try:
        raw = response.read(MAX_ANSWER_BYTES + 1) or b""
        status = response.status if hasattr(response, "status") else response.code
    finally:
        response.close()

    if len(raw) > MAX_ANSWER_BYTES:
        raise OllayaError("Ollaya's answer was too large.")

    try:
        answer = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OllayaError(f"Ollaya's answer was not JSON (HTTP {status}).") from None

    if status != 200:
        code = answer.get("code") if isinstance(answer, dict) else None
        code = code if isinstance(code, str) and code.replace("_", "").isalnum() else "unknown"
        raise OllayaError(f"Ollaya refused the request (HTTP {status}, {code[:40]}).")

    if not isinstance(answer, dict):
        raise OllayaError("Ollaya's answer was not an object.")

    return answer


def _label(option: str) -> str:
    return option.replace("-", " ")


def systemone_question(point: DecisionPoint) -> dict:
    """The question in TypeSafe's System One form, which Ollaya takes."""
    question = point.question

    if question.kind == YESNO:
        return {"type": "noul", "instructions": question.wording}
    if question.kind == CHOICE:
        return {
            "type": "choice",
            "instructions": question.wording,
            "criteria": {option: _label(option) for option in question.options},
        }
    if question.kind == SCORE:
        return {
            "type": "score",
            "instructions": question.wording,
            "criteria": [_label(option) for option in question.options],
        }
    raise ValueError("Unknown kind of question.")


def _probability(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OllayaError("Ollaya gave a probability that is not a number.")
    return float(value)


def distribution_from(point: DecisionPoint, answer: dict) -> Mapping[str, float]:
    """
    A System One answer as a probability for each of the question's
    options. Anything else is refused; checked_decision then checks
    the numbers themselves.
    """
    question = point.question

    if not isinstance(answer, dict):
        raise OllayaError("Ollaya's answer has no answer for the question.")

    if question.kind == YESNO:
        p_true = _probability(answer.get("noul"))
        return {NO: 1.0 - p_true, YES: p_true}

    probabilities = answer.get("probabilities")

    if not isinstance(probabilities, dict):
        raise OllayaError("Ollaya's answer has no probabilities.")

    if question.kind == SCORE:
        # Levels are numbered from 0, lowest first.
        expected = {str(i): option for i, option in enumerate(question.options)}
    else:
        expected = {option: option for option in question.options}

    if set(probabilities) != set(expected):
        raise OllayaError("Ollaya's answer does not match the question's options.")

    return {expected[key]: _probability(value) for key, value in probabilities.items()}


class SystemOneDecider:
    """
    One Ollaya model behind the decision contract. Each decision is one
    request for one question, so batching cannot change an answer.
    """

    def __init__(
        self,
        candidate: Candidate,
        url: Optional[str] = None,
        key: Optional[str] = None,
        transport: Optional[Callable[..., dict]] = None,
        keep_alive: str = "10m",
    ):
        self.candidate = candidate
        self._url = checked_url(url or server_url())
        self._key = os.environ.get("OLLAYA_API_KEY", "") if key is None else key
        self._transport = transport or _http_json
        self._keep_alive = keep_alive
        self._digest: Optional[str] = None
        # Ollaya's own timing of the last decision, in milliseconds.
        self.last_eval_ms: Optional[float] = None
        self.last_load_ms: Optional[float] = None

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        return self._transport(method, self._url + path, body, REQUEST_TIMEOUT_SECONDS, self._key)

    def _installed(self) -> Optional[dict]:
        tags = self._call("GET", "/api/tags").get("models")
        wanted = self.candidate.tag if ":" in self.candidate.tag else self.candidate.tag + ":latest"

        for model in tags if isinstance(tags, list) else []:
            if isinstance(model, dict) and str(model.get("name", "")).lower() == wanted.lower():
                return model
        return None

    @property
    def decider_id(self) -> str:
        """The model's name and its manifest digest, once known."""
        digest = (self._digest or "unknown")[:12]
        return f"ollaya:{self.candidate.tag}@{digest}"

    def ready(self) -> bool:
        try:
            model = self._installed()
        except OllayaError:
            return False

        if model is None:
            return False

        digest = model.get("digest")
        self._digest = digest if isinstance(digest, str) else None
        return True

    def decide(self, point: DecisionPoint, state: str) -> Optional[Mapping[str, float]]:
        reply = self._call("POST", "/api/decide", {
            "model": self.candidate.tag,
            "state": state,
            "questions": {QUESTION_ID: systemone_question(point)},
            "keep_alive": self._keep_alive,
        })

        if reply.get("state_truncated"):
            # A decision on part of the text is no decision (NEW-CN).
            return None

        answers = reply.get("answers")
        if not isinstance(answers, dict):
            raise OllayaError("Ollaya's answer has no answers.")

        self.last_eval_ms = _nanoseconds_to_ms(reply.get("eval_duration"))
        self.last_load_ms = _nanoseconds_to_ms(reply.get("load_duration"))
        return distribution_from(point, answers.get(QUESTION_ID))

    def load(self) -> None:
        """Load the model now, so the first timed decision is warm."""
        self._call("POST", "/api/decide", {"model": self.candidate.tag, "keep_alive": self._keep_alive})

    def memory(self) -> Optional[dict]:
        """How much memory the loaded model uses, from Ollaya."""
        loaded = self._call("GET", "/api/ps").get("models")

        for model in loaded if isinstance(loaded, list) else []:
            if not isinstance(model, dict):
                continue
            if self._digest and model.get("digest") != self._digest:
                continue
            if not self._digest and not str(model.get("name", "")).startswith(self.candidate.tag):
                continue
            return {
                "total_bytes": model.get("size"),
                "vram_bytes": model.get("size_vram"),
                "device": model.get("device"),
                "precision": (model.get("details") or {}).get("quantization_level"),
            }
        return None

    def stop(self) -> None:
        """Unload the model, so the next one has the graphics memory."""
        try:
            self._call("POST", "/api/decide", {"model": self.candidate.tag, "keep_alive": 0})
        except OllayaError:
            pass


def _nanoseconds_to_ms(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value / 1_000_000


def candidate_decider(key: str) -> Callable[[], SystemOneDecider]:
    return lambda: SystemOneDecider(CANDIDATES[key])
