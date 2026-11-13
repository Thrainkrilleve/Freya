import ast
import json
import math
from pathlib import Path

import pytest

from freya.control_plane import build_local_control_plane
from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import (
    LOCAL_OWNER,
    CapabilityBroker,
    CapabilityRegistry,
)
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.core.providers import ProviderHealth, ProviderRegistry
from freya.decider.contract import (
    CHOICE,
    MAX_STATE_CHARACTERS,
    NO,
    YES,
    DecisionError,
    DecisionPoint,
    Question,
    checked_decision,
    yesno,
)
from freya.decider.points import IMAGE_REQUEST, PAST_QUESTION, POINTS, TASK_KIND
from freya.decider.rules import RulesDecider
from freya.decider.service import (
    DECIDE,
    DECIDER_PROVIDER,
    create_decider_provider,
    register_decide,
)
from freya.evaluation import decisions
from freya.evaluation.decision_sets import SETS
from freya.tools.model_tools import MODEL_TOOL


COLOURS = DecisionPoint(
    name="test.colour",
    question=Question(CHOICE, ("red", "green", "blue"), "Which colour is named?"),
    threshold=0.6,
    fallback="Nothing; a test point.",
)


class FakeDecider:
    def __init__(self, answer=None, error=None, ready=True):
        self.answer = answer
        self.error = error
        self.is_ready = ready
        self.asked = []

    @property
    def decider_id(self):
        return "fake-1"

    def ready(self):
        return self.is_ready

    def decide(self, point, state):
        self.asked.append((point.name, state))
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture
def brokered(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    decider = FakeDecider(answer={NO: 0.05, YES: 0.95})
    providers = ProviderRegistry()
    providers.register(create_decider_provider(decider))
    providers.start(DECIDER_PROVIDER)
    providers.check_health(DECIDER_PROVIDER)
    registry = CapabilityRegistry(providers)
    register_decide(registry, decider)
    policy = SqlitePolicyEngine(store)
    audit = SqliteAuditLog(store)
    audit.start()
    broker = CapabilityBroker(registry, policy, audit, LOCAL_OWNER)

    yield broker, policy, store, registry, decider

    store.close()


def allow(policy):
    policy.add_rule(LOCAL_OWNER.actor, DECIDE, "allow", "Test: owner allowed it.")


def audit_text(store):
    return " ".join(str(tuple(row)) for row in store.execute("SELECT * FROM audit_event"))


# --- questions and points ---


def test_a_yes_no_question_always_has_no_then_yes():
    with pytest.raises(ValueError):
        Question("yesno", ("yes", "no"), "Is it?")


@pytest.mark.parametrize("options", [("one",), ("a", "a"), ("Bad Name", "ok"), tuple(f"o{i}" for i in range(21))])
def test_bad_option_lists_are_refused(options):
    with pytest.raises(ValueError):
        Question(CHOICE, options, "Which?")


@pytest.mark.parametrize("threshold", [0.4, 1.1, True, "0.9"])
def test_a_threshold_is_between_a_half_and_one(threshold):
    with pytest.raises(ValueError):
        DecisionPoint("test.point", yesno("Is it?"), threshold, "A rule.")


def test_every_registered_point_names_its_fallback():
    assert set(POINTS) == {PAST_QUESTION.name, IMAGE_REQUEST.name, TASK_KIND.name}
    assert all(point.fallback.strip() for point in POINTS.values())


# --- checking what a decider returned ---


def test_a_confident_answer_is_taken():
    decision = checked_decision(COLOURS, {"red": 0.1, "green": 0.8, "blue": 0.1}, "fake", 1.0)

    assert decision.answer == "green" and not decision.abstained
    assert decision.probability == pytest.approx(0.8)


def test_an_answer_below_the_threshold_abstains_but_keeps_the_distribution():
    decision = checked_decision(COLOURS, {"red": 0.3, "green": 0.5, "blue": 0.2}, "fake", 1.0)

    assert decision.abstained and decision.answer is None
    assert dict(decision.distribution)["green"] == pytest.approx(0.5)


def test_a_decider_that_abstains_gives_no_answer():
    decision = checked_decision(COLOURS, None, "fake", 1.0)

    assert decision.abstained and decision.answer is None and decision.distribution == ()


def test_a_tie_goes_to_the_option_listed_first():
    decision = checked_decision(
        DecisionPoint("test.tie", COLOURS.question, 0.5, "A rule."),
        {"blue": 0.5, "green": 0.5, "red": 0.0},
        "fake",
        1.0,
    )

    assert decision.answer == "green"


@pytest.mark.parametrize("answer", [
    {"red": 1.0, "green": 0.0},
    {"red": 0.5, "green": 0.5, "blue": 0.0, "pink": 0.0},
    {"red": math.nan, "green": 0.5, "blue": 0.5},
    {"red": 1.5, "green": -0.5, "blue": 0.0},
    {"red": 0.5, "green": 0.2, "blue": 0.2},
    {"red": True, "green": False, "blue": False},
    ["red"],
])
def test_an_answer_that_is_not_a_distribution_over_the_options_is_refused(answer):
    with pytest.raises(DecisionError):
        checked_decision(COLOURS, answer, "fake", 1.0)


# --- the rules Freya uses today ---


def test_the_rules_decider_answers_with_todays_patterns():
    rules = RulesDecider()

    assert rules.decide(PAST_QUESTION, "Have I ever told you about my car?")[YES] == 1.0
    assert rules.decide(PAST_QUESTION, "What's the weather?")[NO] == 1.0
    assert rules.decide(IMAGE_REQUEST, "Make an image of a fox")[YES] == 1.0
    assert rules.decide(IMAGE_REQUEST, "Make a list of chores")[NO] == 1.0


def test_where_there_is_no_rule_the_rules_decider_abstains():
    assert RulesDecider().decide(TASK_KIND, "Summarise this text.") is None


def test_the_same_question_gets_the_same_answer():
    rules = RulesDecider()
    text = "Do you remember what I said about the trip?"

    assert rules.decide(PAST_QUESTION, text) == rules.decide(PAST_QUESTION, text)


# --- through the broker ---


def test_deciding_is_denied_until_the_owner_allows_it(brokered):
    broker, policy, _, _, decider = brokered

    assert not broker.invoke(DECIDE, PAST_QUESTION.name, "Did we ever talk?").success
    assert decider.asked == []

    allow(policy)
    result = broker.invoke(DECIDE, PAST_QUESTION.name, "Did we ever talk?")

    assert result.success
    assert result.result["answer"] == YES and result.result["decider"] == "fake-1"


def test_the_text_decided_about_never_reaches_the_audit_log(brokered):
    broker, policy, store, _, _ = brokered
    allow(policy)

    broker.invoke(DECIDE, PAST_QUESTION.name, "PRIVATE-CANARY did we ever talk?")

    assert "PRIVATE-CANARY" not in audit_text(store)


def test_what_a_failing_decider_said_stays_out_of_the_audit_log(brokered):
    broker, policy, store, _, decider = brokered
    allow(policy)
    decider.error = RuntimeError("ERROR-CANARY quoting the text")

    result = broker.invoke(DECIDE, PAST_QUESTION.name, "Did we ever talk?")

    assert not result.success
    assert "ERROR-CANARY" not in (result.error or "")
    assert "ERROR-CANARY" not in audit_text(store)


def test_an_unknown_point_or_too_much_text_never_reaches_the_decider(brokered):
    broker, policy, _, _, decider = brokered
    allow(policy)

    assert not broker.invoke(DECIDE, "approvals.grant", "yes").success
    assert not broker.invoke(DECIDE, PAST_QUESTION.name, "x" * (MAX_STATE_CHARACTERS + 1)).success
    assert not broker.invoke(DECIDE, PAST_QUESTION.name, 42).success
    assert decider.asked == []


def test_a_decider_that_is_not_ready_is_unavailable(tmp_path):
    providers = ProviderRegistry()
    providers.register(create_decider_provider(FakeDecider(ready=False)))
    providers.start(DECIDER_PROVIDER)

    assert providers.check_health(DECIDER_PROVIDER).result == ProviderHealth.UNHEALTHY


def test_deciding_is_not_offered_to_the_model(brokered):
    _, _, _, registry, _ = brokered

    assert not registry.get(DECIDE).metadata.get(MODEL_TOOL)


def test_freya_as_she_starts_has_no_decider(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)

    try:
        assert plane.capabilities.get(DECIDE) is None
        assert plane.providers.get(DECIDER_PROVIDER) is None
        assert not plane.policy.decide(LOCAL_OWNER.actor, DECIDE).allow
    finally:
        plane.close()


# Proposed NEW-CL: a decision chooses, it never authorizes. The decider
# package must not be able to reach anything that grants, approves or
# changes a setting.
FORBIDDEN_IMPORTS = (
    "freya.core.policy",
    "freya.core.persistence",
    "freya.core.usage",
    "freya.delegation",
    "freya.gateway",
    "freya.settings",
    "freya.file_access",
)


def test_the_decider_cannot_reach_policy_approvals_or_settings():
    package = Path(__file__).resolve().parents[1] / "src" / "freya" / "decider"
    imported = set()

    for path in package.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

    assert not [
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS)
    ]


# --- the labelled sets and the runner ---


def test_every_example_is_labelled_with_one_of_its_points_options():
    for name, examples in SETS.items():
        options = POINTS[name].question.options
        assert examples
        assert all(e.point == name and e.label in options for e in examples)
        assert len({e.text for e in examples}) == len(examples)


def test_each_set_has_held_out_examples_of_more_than_one_answer():
    for examples in SETS.values():
        held = [e for e in examples if e.held_out]
        assert held and len(held) < len(examples)
    labels = {e.label for e in SETS[PAST_QUESTION.name] if not e.held_out}
    assert labels == {YES, NO}


def test_todays_rules_never_say_yes_to_a_message_labelled_no():
    rows = decisions.measure(RulesDecider(), [PAST_QUESTION.name, IMAGE_REQUEST.name], "all")

    for result in rows.values():
        assert not [r for r in result["rows"] if r["label"] == NO and r["answer"] == YES]


def test_calibration_error_is_zero_when_confidence_matches_accuracy():
    rows = [
        {"probability": 1.0, "answer": "a", "label": "a"},
        {"probability": 0.5, "answer": "a", "label": "a"},
        {"probability": 0.5, "answer": "a", "label": "b"},
    ]

    assert decisions.expected_calibration_error(rows) == pytest.approx(0.0)


def test_the_runner_writes_a_report_and_never_overwrites_one(tmp_path, capsys):
    report = tmp_path / "decisions.json"

    assert decisions.main(["--point", PAST_QUESTION.name, "--output", str(report)]) == 0
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["decider"] == "rules-v1"
    assert written["split"] == "held-out"
    assert PAST_QUESTION.name in written["points"]

    with pytest.raises(SystemExit):
        decisions.main(["--output", str(report)])


# --- Laya, through its helper ---


from freya.decider import laya_decider  # noqa: E402
from freya.decider.laya_decider import (  # noqa: E402
    LAYA_REVISION,
    LayaDecider,
    laya_question,
    mostly_latin,
)


class FakeHelper:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        self.stopped = False

    def request(self, message, timeout=None):
        self.requests.append(message)
        return {"ok": True, **self.reply}

    def stop(self):
        self.stopped = True


def test_a_yes_no_point_is_asked_as_layas_true_or_false_question():
    helper = FakeHelper({"p_true": 0.93})
    decider = LayaDecider(helper=helper, installed=lambda: True)

    distribution = decider.decide(PAST_QUESTION, "Did we ever talk about the trip?")

    assert distribution == {NO: pytest.approx(0.07), YES: pytest.approx(0.93)}
    sent = helper.requests[0]
    assert sent["op"] == "decide" and sent["question"]["type"] == "noul"
    assert sent["question"]["instructions"] == PAST_QUESTION.question.wording


def test_a_choice_point_gets_every_option_back_by_name():
    probabilities = {kind: 0.0 for kind in TASK_KIND.question.options}
    probabilities["planning"] = 1.0
    decider = LayaDecider(helper=FakeHelper({"probabilities": probabilities}), installed=lambda: True)

    decision = checked_decision(TASK_KIND, decider.decide(TASK_KIND, "Plan my week."), "laya", 1.0)

    assert decision.answer == "planning"
    assert set(laya_question(TASK_KIND)["criteria"]) == set(TASK_KIND.question.options)


def test_an_answer_missing_an_option_is_refused():
    decider = LayaDecider(helper=FakeHelper({"probabilities": {"planning": 1.0}}), installed=lambda: True)

    with pytest.raises(KeyError):
        decider.decide(TASK_KIND, "Plan my week.")


def test_text_mostly_outside_the_latin_alphabet_goes_to_the_fallback():
    helper = FakeHelper({"p_true": 0.99})
    decider = LayaDecider(helper=helper, installed=lambda: True)

    assert not mostly_latin("你还记得我说过什么吗")
    assert decider.decide(PAST_QUESTION, "你还记得我说过什么吗") is None
    assert helper.requests == []


def test_laya_is_not_ready_until_it_is_installed():
    assert not LayaDecider(installed=lambda: False).ready()


def test_laya_runs_offline_from_the_one_reviewed_commit(tmp_path, monkeypatch):
    helper = laya_decider._default_helper()

    assert helper._environment["HF_HUB_OFFLINE"] == "1"
    assert helper._environment["LAYA_REVISION"] == LAYA_REVISION
    assert len(LAYA_REVISION) == 40


def test_the_laya_worker_does_not_import_freya():
    worker = Path(laya_decider.WORKERS_DIR) / "laya_worker.py"
    tree = ast.parse(worker.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)

    assert "freya" not in names


def test_a_failing_helper_is_an_error_in_the_measurement_not_a_crash():
    class Broken:
        decider_id = "broken"

        def decide(self, point, state):
            raise RuntimeError("helper died")

    rows = decisions.decide_all(Broken(), SETS[PAST_QUESTION.name][:2])

    assert all(row["error"] == "RuntimeError" for row in rows)


def test_the_runner_says_how_to_install_a_decider_that_is_missing(monkeypatch, capsys):
    monkeypatch.setitem(decisions.DECIDERS, "laya", lambda: LayaDecider(installed=lambda: False))

    assert decisions.main(["--decider", "laya"]) == 2
    assert "python -m freya.decider.install laya" in capsys.readouterr().err


STUB_TORCH = '''
import contextlib
class cuda:
    @staticmethod
    def is_available():
        return False
def manual_seed(seed):
    pass
def inference_mode():
    return contextlib.nullcontext()
'''

STUB_LAYA = '''
class _Agent:
    def system_one(self, state, questions):
        q = questions["q"]
        if q["type"] == "noul":
            return {"answers": {"q": {"noul": 0.8 if "ever" in state else 0.1}}}
        keys = list(q["criteria"])
        return {"answers": {"q": {"probabilities": {k: (1.0 if i == 0 else 0.0) for i, k in enumerate(keys)}}}}
def load(name, device=None, revision=None):
    assert revision
    return _Agent()
'''


def test_the_laya_worker_speaks_one_json_line_per_request(tmp_path):
    import os
    import subprocess
    import sys

    (tmp_path / "torch.py").write_text(STUB_TORCH, encoding="utf-8")
    (tmp_path / "laya.py").write_text(STUB_LAYA, encoding="utf-8")
    worker = Path(laya_decider.WORKERS_DIR) / "laya_worker.py"
    requests = "\n".join([
        json.dumps({"op": "decide", "state": "Did we ever?", "question": laya_question(PAST_QUESTION)}),
        json.dumps({"op": "decide", "state": "Plan it.", "question": laya_question(TASK_KIND)}),
        json.dumps({"op": "unknown"}),
    ]) + "\n"
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "LAYA_REVISION": LAYA_REVISION}

    done = subprocess.run(
        [sys.executable, str(worker)], input=requests, capture_output=True,
        text=True, env=env, timeout=30,
    )
    answers = [json.loads(line) for line in done.stdout.splitlines()]

    assert answers[0]["ready"]
    assert answers[1] == {"ok": True, "p_true": 0.8}
    assert distribution_from_reply(TASK_KIND, answers[2])[TASK_KIND.question.options[0]] == 1.0
    assert answers[3] == {"ok": False, "error": "ValueError"}


def distribution_from_reply(point, reply):
    return laya_decider.distribution_from(point, reply)
