import json

import pytest

from freya.delegation.record import (
    DEFAULT_BARS,
    MEASURED,
    MINIMUM_ATTEMPTS,
    UNMEASURED,
    Candidate,
    ModelRecord,
    choose,
    entry_id,
    validate_bars,
)


def report(model, passed, graded=20, kind="extraction", think="off", route="ollama-local", seconds=2.0):
    return {
        "route": route,
        "model": model,
        "settings": {"think": think, "context_tokens": 16384, "output_tokens": 8192},
        "created_at": "2026-10-09T12:00:00+00:00",
        "summary": {
            kind: {
                "attempts": graded, "graded": graded, "passed": passed,
                "errors": 0, "pass_rate": passed / graded if graded else None,
                "median_seconds": seconds, "mean_tokens": 300,
            },
        },
    }


@pytest.fixture
def record(tmp_path):
    return ModelRecord(tmp_path / "model_record.json")


def add(record, *args, **kwargs):
    return record.add_report(report(*args, **kwargs))


def test_the_same_model_with_other_settings_is_another_entry(record):
    thinking = add(record, "small", 18, think="on")
    direct = add(record, "small", 12, think="off")

    assert thinking != direct
    assert record.entries() == sorted([thinking, direct])
    assert record.result(thinking, "extraction").pass_rate == 0.9
    assert record.result(direct, "extraction").pass_rate == 0.6


def test_a_newer_report_replaces_the_same_kind_and_keeps_the_others(record):
    entry = add(record, "small", 10, kind="extraction")
    add(record, "small", 19, kind="planning")
    add(record, "small", 16, kind="extraction")

    assert record.result(entry, "extraction").passed == 16
    assert record.result(entry, "planning").passed == 19


def test_the_record_is_kept_and_holds_no_replies(tmp_path):
    path = tmp_path / "model_record.json"
    entry = add(ModelRecord(path), "small", 18)

    assert ModelRecord(path).result(entry, "extraction").passed == 18
    stored = path.read_text(encoding="utf-8")
    assert "reply" not in stored and "brief" not in stored


@pytest.mark.parametrize("content", ["not json", "[]", '{"entries": 5}', '{"entries": {"x": "y"}}'])
def test_a_damaged_record_is_an_empty_one(tmp_path, content):
    path = tmp_path / "model_record.json"
    path.write_text(content, encoding="utf-8")

    assert ModelRecord(path).entries() == []


def test_a_report_without_results_is_refused(record):
    with pytest.raises(ValueError):
        record.add_report({"route": "ollama-local", "model": "small"})


def test_few_attempts_do_not_count_as_measured(record):
    entry = add(record, "small", MINIMUM_ATTEMPTS - 1, graded=MINIMUM_ATTEMPTS - 1)

    assert not record.result(entry, "extraction").measured
    assert record.result(entry, "summarising") is None


def test_the_least_costly_model_that_reaches_the_bar_is_chosen(record):
    fast = add(record, "fast", 18)       # 90%
    strong = add(record, "strong", 20)   # 100%
    weak = add(record, "weak", 12)       # 60%
    candidates = [
        Candidate(strong, cost=10.0),
        Candidate(weak, cost=0.5),
        Candidate(fast, cost=1.5),
    ]

    normal = choose(candidates, "extraction", "normal", record)
    high = choose(candidates, "extraction", "high", record)

    assert (normal.entry, normal.basis) == (fast, MEASURED)
    assert normal.pass_rate == 0.9
    assert (high.entry, high.basis) == (strong, MEASURED)


def test_local_is_preferred_over_cloud_when_both_qualify(record):
    local = add(record, "strong", 19)
    cloud = add(record, "claude", 20, route="anthropic-plan")

    result = choose(
        [Candidate(cloud, tier=1, cost=0.1), Candidate(local, tier=0, cost=9.0)],
        "extraction", "normal", record,
    )

    assert result.entry == local


def test_cloud_is_chosen_when_no_local_model_reaches_the_bar(record):
    local = add(record, "fast", 17)                        # 85%
    cloud = add(record, "claude", 20, route="anthropic-plan")
    candidates = [Candidate(local, tier=0), Candidate(cloud, tier=1)]

    assert choose(candidates, "extraction", "normal", record).entry == local
    assert choose(candidates, "extraction", "high", record).entry == cloud


def test_among_equal_cost_the_better_measured_model_wins(record):
    good = add(record, "good", 18)
    better = add(record, "better", 20)

    result = choose(
        [Candidate(good, cost=1.0), Candidate(better, cost=1.0)],
        "extraction", "low", record,
    )

    assert result.entry == better


def test_an_unmeasured_model_is_a_last_resort_and_is_labelled(record):
    weak = add(record, "weak", 12)
    new = entry_id("ollama-local", "new", {"think": "off"})
    newer = entry_id("ollama-local", "newer", {"think": "off"})

    result = choose(
        [Candidate(weak, cost=0.1), Candidate(newer, cost=5.0), Candidate(new, cost=2.0)],
        "extraction", "normal", record,
    )
    other_kind = choose([Candidate(weak)], "planning", "low", record)

    assert (result.entry, result.basis) == (new, UNMEASURED)
    assert "not been measured" in result.reason
    # Measured on one kind says nothing about another.
    assert other_kind.basis == UNMEASURED


def test_a_measured_model_beats_an_unmeasured_cheaper_one(record):
    measured = add(record, "known", 19)
    unknown = entry_id("ollama-local", "unknown", {})

    result = choose(
        [Candidate(unknown, cost=0.1), Candidate(measured, cost=5.0)],
        "extraction", "normal", record,
    )

    assert (result.entry, result.basis) == (measured, MEASURED)


def test_high_value_work_needs_permission_before_an_unmeasured_model(record):
    new = entry_id("ollama-local", "new", {"think": "off"})

    refused = choose([Candidate(new)], "extraction", "high", record)
    allowed = choose(
        [Candidate(new)], "extraction", "high", record, unmeasured_allowed=True,
    )
    never = choose(
        [Candidate(new)], "extraction", "low", record, unmeasured_allowed=False,
    )

    assert refused.entry is None and "permission" in refused.reason
    assert (allowed.entry, allowed.basis) == (new, UNMEASURED)
    assert never.entry is None


def test_attempts_that_got_no_reply_count_against_a_model(record):
    flaky = report("flaky", 15, graded=15)
    flaky["summary"]["extraction"]["errors"] = 15
    entry = record.add_report(flaky)

    result = record.result(entry, "extraction")

    assert result.attempts == 30
    assert result.pass_rate == 0.5
    assert choose([Candidate(entry)], "extraction", "low", record).entry is None


def test_settings_cannot_be_dressed_up_as_other_settings():
    assert entry_id("r", "m", {"a": "x,b=y"}) != entry_id("r", "m", {"a": "x", "b": "y"})
    assert entry_id("r", "m", {"b": 1, "a": 2}) == entry_id("r", "m", {"a": 2, "b": 1})


@pytest.mark.parametrize(
    "change",
    [
        {"passed": 20, "graded": 15},
        {"passed": -1},
        {"graded": "many"},
        {"errors": -3},
    ],
)
def test_impossible_results_are_refused_and_nothing_is_recorded(record, change):
    bad = report("small", 10)
    bad["summary"]["extraction"].update(change)
    good_kind = report("small", 18, kind="planning")["summary"]["planning"]
    bad["summary"]["planning"] = good_kind

    with pytest.raises(ValueError):
        record.add_report(bad)

    assert record.entries() == []


def test_results_do_not_carry_over_to_a_different_exact_model(record):
    first = report("small", 19, kind="extraction")
    first["identity"] = "digest-one"
    entry = record.add_report(first)
    second = report("small", 12, kind="planning")
    second["identity"] = "digest-two"

    record.add_report(second)

    assert record.result(entry, "extraction") is None
    assert record.result(entry, "planning").passed == 12


def test_damaged_stored_counts_measure_nothing(tmp_path):
    path = tmp_path / "model_record.json"
    entry = add(ModelRecord(path), "small", 18)
    stored = json.loads(path.read_text("utf-8"))
    stored["entries"][entry]["kinds"]["extraction"]["passed"] = 500
    path.write_text(json.dumps(stored), encoding="utf-8")

    damaged = ModelRecord(path)

    assert damaged.result(entry, "extraction") is None
    assert choose([Candidate(entry)], "extraction", "low", damaged).basis == UNMEASURED


def test_a_report_answered_by_more_than_one_model_is_refused(record):
    mixed = report("claude", 18, route="anthropic-plan")
    mixed["mixed_models"] = True

    with pytest.raises(ValueError):
        record.add_report(mixed)


def test_nothing_is_chosen_when_every_model_is_measured_too_weak(record):
    weak = add(record, "weak", 12)

    result = choose([Candidate(weak)], "extraction", "normal", record)
    nobody = choose([], "extraction", "normal", record)

    assert result.entry is None and result.basis is None
    assert "85%" in result.reason
    assert nobody.entry is None


def test_the_owner_can_move_the_bars(record):
    fast = add(record, "fast", 18)  # 90%
    strict = {"low": 0.90, "normal": 0.95, "high": 1.0}

    assert choose([Candidate(fast)], "extraction", "normal", record).entry == fast
    assert choose([Candidate(fast)], "extraction", "normal", record, strict).entry is None
    assert choose([Candidate(fast)], "extraction", "low", record, strict).entry == fast


@pytest.mark.parametrize(
    "bars",
    [
        {"low": 0.7, "normal": 0.85},
        {"low": 0.9, "normal": 0.85, "high": 0.95},
        {"low": 0.7, "normal": 0.85, "high": 1.2},
        {"low": 0, "normal": 0.85, "high": 0.95},
        {"low": "70%", "normal": 0.85, "high": 0.95},
        {"low": True, "normal": 0.85, "high": 0.95},
        "high",
    ],
)
def test_bad_bars_are_refused(bars):
    with pytest.raises(ValueError):
        validate_bars(bars)


def test_the_starting_bars_are_the_owners(record):
    assert validate_bars(DEFAULT_BARS) == {"low": 0.70, "normal": 0.85, "high": 0.95}
    with pytest.raises(ValueError):
        choose([], "extraction", "urgent", record)


def test_a_real_report_can_be_recorded(record):
    from freya.evaluation import delegation
    from freya.evaluation.delegation_tasks import tasks_of

    tasks = tasks_of(["planning"])
    attempts = [
        delegation.run_task(
            task,
            lambda brief, task=task: {"text": task.reference, "input_tokens": 1, "output_tokens": 1},
        )
        for task in tasks
        for _ in range(3)
    ]
    entry = record.add_report({
        "route": "ollama-local", "model": "pretend",
        "settings": {"think": "off"}, "created_at": "now",
        "summary": delegation.summarize(attempts),
    })

    result = record.result(entry, "planning")
    assert result.graded == len(tasks) * 3 and result.pass_rate == 1.0
    assert json.loads(record.path.read_text("utf-8"))["entries"][entry]["model"] == "pretend"
