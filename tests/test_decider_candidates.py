"""
The open decision models measured through a local Ollaya
(decider/systemone_decider.py) and the comparison runner
(evaluation/decider_bench.py). Ollaya is faked; nothing is contacted.
"""
import json

import pytest

from freya.decider.contract import DecisionError, Question, DecisionPoint, SCORE, checked_decision
from freya.decider.points import PAST_QUESTION, TASK_KIND
from freya.decider.systemone_decider import (
    CANDIDATES,
    OllayaError,
    SystemOneDecider,
    _http_json,
    checked_url,
    systemone_question,
)
from freya.evaluation import decider_bench, decisions


RATING = DecisionPoint(
    name="test.rating",
    question=Question(kind=SCORE, options=("low", "medium", "high"), wording="How urgent is it?"),
    threshold=0.6,
    fallback="None; a test point.",
)


class FakeOllaya:
    """Answers like Ollaya's /api/tags, /api/decide and /api/ps."""

    def __init__(self, answer=None, models=None, truncated=False):
        self.answer = answer
        self.models = models if models is not None else [
            {"name": f"{c.tag}:latest" if ":" not in c.tag else c.tag, "digest": "ab" * 32}
            for c in CANDIDATES.values()
        ]
        self.truncated = truncated
        self.calls = []

    def __call__(self, method, url, body, timeout, key):
        self.calls.append((method, url, body, key))
        path = url.split("11435", 1)[1]

        if path == "/api/tags":
            return {"models": self.models}
        if path == "/api/ps":
            return {"models": [{"name": "d1:latest", "digest": "ab" * 32, "size": 4_200_000_000,
                                "size_vram": 4_000_000_000, "device": "cuda:0",
                                "details": {"quantization_level": "F16"}}]}
        if path == "/api/decide":
            if "state" not in body:
                return {"answers": {}, "done_reason": "load" if body.get("keep_alive") else "unload"}
            return {
                "answers": {"q": self.answer},
                "state_truncated": self.truncated,
                "eval_duration": 12_000_000,
                "load_duration": 0,
            }
        raise AssertionError(f"unexpected path {path}")


def decider(fake, key="d1"):
    return SystemOneDecider(CANDIDATES[key], url="http://127.0.0.1:11435", key="", transport=fake)


def test_five_candidates_each_with_a_licence_and_a_library_name():
    assert len(CANDIDATES) == 5
    for candidate in CANDIDATES.values():
        assert candidate.tag and candidate.licence and candidate.author


@pytest.mark.parametrize("url", [
    "http://192.168.1.5:11435",
    "https://127.0.0.1:11435",
    "http://example.com:11435",
    "http://user:pw@127.0.0.1:11435",
    "ftp://127.0.0.1",
])
def test_the_server_must_be_plain_http_on_this_machine(url):
    with pytest.raises(ValueError):
        checked_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11435", "http://localhost:11435/", "http://[::1]:11435"])
def test_loopback_addresses_are_accepted(url):
    assert checked_url(url).startswith("http://")


def test_a_yes_no_point_is_asked_as_a_true_or_false_question():
    fake = FakeOllaya({"type": "noul", "noul": 0.97})
    d = decider(fake)
    distribution = d.decide(PAST_QUESTION, "Did we ever talk about Rome?")

    assert distribution == pytest.approx({"no": 0.03, "yes": 0.97})
    sent = fake.calls[-1][2]
    assert sent["questions"]["q"]["type"] == "noul"
    assert sent["model"] == "d1"
    assert d.last_eval_ms == pytest.approx(12.0)


def test_a_choice_point_gets_every_option_back_by_name():
    options = TASK_KIND.question.options
    probabilities = {o: 1 / len(options) for o in options}
    fake = FakeOllaya({"type": "choice", "choice": options[0], "probabilities": probabilities})
    distribution = decider(fake).decide(TASK_KIND, "Summarise this report.")

    assert set(distribution) == set(options)
    assert set(fake.calls[-1][2]["questions"]["q"]["criteria"]) == set(options)


def test_score_levels_come_back_numbered_from_zero():
    fake = FakeOllaya({"type": "score", "score": 1.8, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}})
    distribution = decider(fake).decide(RATING, "The server is down.")

    assert distribution == {"low": 0.1, "medium": 0.2, "high": 0.7}
    assert systemone_question(RATING)["criteria"] == ["low", "medium", "high"]


@pytest.mark.parametrize("answer", [
    {"type": "choice", "probabilities": {"planning": 1.0}},
    {"type": "choice", "probabilities": "everything"},
    {"type": "choice"},
    None,
])
def test_an_answer_that_does_not_fit_the_question_is_refused(answer):
    with pytest.raises(OllayaError):
        decider(FakeOllaya(answer)).decide(TASK_KIND, "Plan my week.")


def test_a_probability_that_is_not_a_number_is_refused():
    with pytest.raises(OllayaError):
        decider(FakeOllaya({"type": "noul", "noul": "yes"})).decide(PAST_QUESTION, "Hello")


def test_a_probability_outside_zero_to_one_is_refused_by_the_contract():
    distribution = decider(FakeOllaya({"type": "noul", "noul": 1.4})).decide(PAST_QUESTION, "Hello")
    with pytest.raises(DecisionError):
        checked_decision(PAST_QUESTION, distribution, "test", 1.0)


def test_text_that_did_not_fit_is_not_decided_on():
    fake = FakeOllaya({"type": "noul", "noul": 0.99}, truncated=True)
    assert decider(fake).decide(PAST_QUESTION, "A very long message") is None


def test_ready_only_when_ollaya_has_the_model_and_the_id_names_its_digest():
    fake = FakeOllaya(models=[{"name": "d1:latest", "digest": "cd" * 32}])
    assert decider(fake, "d1").ready()
    assert decider(fake, "d1").decider_id.startswith("ollaya:d1@")
    d = decider(fake, "d1")
    d.ready()
    assert d.decider_id == "ollaya:d1@" + ("cd" * 6)
    assert not decider(fake, "nimble").ready()


def test_not_ready_when_ollaya_is_not_running():
    def unreachable(*args):
        raise OllayaError("Ollaya could not be reached (ConnectionRefusedError).")

    assert not decider(unreachable).ready()


def test_the_api_key_is_sent_only_as_a_bearer_header():
    fake = FakeOllaya({"type": "noul", "noul": 0.5})
    d = SystemOneDecider(CANDIDATES["d1"], url="http://127.0.0.1:11435", key="k3y", transport=fake)
    d.decide(PAST_QUESTION, "Hello")
    assert fake.calls[-1][3] == "k3y"
    assert "k3y" not in json.dumps(fake.calls[-1][2])


def test_stop_unloads_the_model():
    fake = FakeOllaya()
    decider(fake).stop()
    method, url, body, _ = fake.calls[-1]
    assert url.endswith("/api/decide") and body == {"model": "d1", "keep_alive": 0}


def test_an_error_from_ollaya_never_carries_its_wording(monkeypatch):
    class Response:
        status = 422
        code = 422

        def read(self, n):
            return json.dumps({"error": "SECRET TEXT FROM THE STATE", "code": "INVALID_REQUEST"}).encode()

        def close(self):
            pass

    import freya.decider.systemone_decider as module

    monkeypatch.setattr(module._OPENER, "open", lambda request, timeout: Response())
    with pytest.raises(OllayaError) as raised:
        _http_json("POST", "http://127.0.0.1:11435/api/decide", {"state": "x"}, 1, "")
    assert "SECRET" not in str(raised.value)
    assert "INVALID_REQUEST" in str(raised.value)


def test_no_proxy_is_ever_used(monkeypatch):
    import urllib.request

    import freya.decider.systemone_decider as module

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    # An opener built with ProxyHandler({}) has no proxy handler at all,
    # so the environment's proxy settings are never read.
    assert not any(
        isinstance(h, urllib.request.ProxyHandler) and h.proxies for h in module._OPENER.handlers
    )


def test_the_decision_runner_knows_every_candidate():
    for key in CANDIDATES:
        assert key in decisions.DECIDERS
    assert "install candidates" in decisions.not_ready_message("d1")


def test_the_bench_compares_deciders_and_skips_the_missing(tmp_path, capsys):
    fake = FakeOllaya({"type": "noul", "noul": 0.95}, models=[{"name": "d1:latest", "digest": "ab" * 32}])
    factories = {
        "rules": decider_bench.RulesDecider,
        "d1": lambda: decider(fake, "d1"),
        "nimble": lambda: decider(fake, "nimble"),
    }
    output = tmp_path / "bench.json"
    code = decider_bench.main(
        ["--point", "conversation.asks_about_past", "--output", str(output)], factories=factories,
    )
    assert code == 0
    report = json.loads(output.read_text())
    names = {e["name"]: e for e in report["deciders"]}
    assert names["nimble"]["skipped"] == "not available"
    assert names["d1"]["memory"]["vram_bytes"] == 4_000_000_000
    assert names["d1"]["candidate"]["licence"].startswith("LFM")
    assert names["rules"]["overall"]["answered"] > 0
    # The model was unloaded after its run.
    assert {"model": "d1", "keep_alive": 0} in [call[2] for call in fake.calls]
    printed = capsys.readouterr().out
    assert "nimble" in printed and "skipped" in printed

    with pytest.raises(SystemExit):
        decider_bench.main(["--output", str(output)], factories=factories)


def test_a_candidate_that_fails_is_reported_not_raised(capsys):
    def broken():
        raise RuntimeError("SECRET")

    decider_bench.main(["--point", "image.requested"], factories={"broken": broken})
    out = capsys.readouterr().out
    assert "failed (RuntimeError)" in out and "SECRET" not in out
