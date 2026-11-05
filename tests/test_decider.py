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
