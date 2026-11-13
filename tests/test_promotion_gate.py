"""The promotion gate (NEW-CB) and its rules for models (NEW-CF) and training data (NEW-CG)."""

import json

import pytest

from freya.delegation.record import DEFAULT_BARS, MINIMUM_ATTEMPTS
from freya.evaluation import gate, workflow
from freya.evaluation.scenarios import SCENARIOS, get_scenario

KNOWN = [s.name for s in SCENARIOS]
CASES = ["cpu-grounding", "ram-grounding", "sensor-routing", "gpu-grounding", "sensor-failure", "gpu-denied"]


def hint_candidate(**changes):
    data = {"name": "say-the-unit", "kind": "hint", "change": {"hint": "Always give the unit."},
            "cases": list(CASES)}
    data.update(changes)
    return data


def result(arm, scenario, status, trial=1):
    return {"arm": arm, "scenario": scenario, "status": status, "trial": trial}


def results_for(scenarios, trials, baseline, candidate):
    """
    `baseline` and `candidate` map a scenario to the statuses of its
    first trials; the rest pass.
    """
    rows = []
    for arm, plan in ((gate.BASELINE, baseline), (gate.CANDIDATE, candidate)):
        for scenario in scenarios:
            statuses = list(plan.get(scenario, []))
            statuses += ["passed"] * (trials - len(statuses))
            rows += [result(arm, scenario, status, i + 1) for i, status in enumerate(statuses)]
    return rows


# Choosing what decides


def test_the_held_out_third_is_fixed_by_name_and_cases_not_their_order():
    first = gate.split_cases("a", CASES)
    assert first == gate.split_cases("a", list(reversed(CASES)))
    assert len(first) == 2 and set(first) < set(CASES)
    assert len(gate.split_cases("a", CASES[:3])) == 1


def test_a_loaded_candidate_keeps_held_out_and_drafting_cases_apart():
    candidate = gate.load_candidate(hint_candidate(), KNOWN)
    assert candidate.held_out == gate.split_cases("say-the-unit", CASES)
    assert set(candidate.drafting_cases) | set(candidate.held_out) == set(CASES)
    assert not set(candidate.drafting_cases) & set(candidate.held_out)


def test_a_candidate_may_name_its_own_held_out_cases():
    candidate = gate.load_candidate(hint_candidate(held_out=["gpu-denied"]), KNOWN)
    assert candidate.held_out == ("gpu-denied",)


@pytest.mark.parametrize("changes, words", [
    ({"extra": 1}, "Unknown candidate fields"),
    ({"name": "Not A Name"}, "lowercase"),
    ({"kind": "skill"}, "step 2"),
    ({"kind": "router"}, "Kind is one of"),
    ({"value": "urgent"}, "Value is one of"),
    ({"change": {"model": "x"}}, "nothing else"),
    ({"change": {"hint": "   "}}, "non-empty"),
    ({"change": {"hint": "x" * (gate.MAX_HINT_BYTES + 1)}}, "at most"),
    ({"cases": CASES[:2]}, "3 to 10"),
    ({"cases": CASES[:2] + CASES[:1]}, "listed twice"),
    ({"cases": CASES[:2] + ["made-up"]}, "Unknown cases"),
    ({"held_out": list(CASES)}, "some, not all"),
    ({"held_out": ["made-up"]}, "some, not all"),
    ({"held_out": []}, "non-empty"),
])
def test_a_malformed_candidate_is_refused(changes, words):
    with pytest.raises(ValueError, match=words):
        gate.load_candidate(hint_candidate(**changes), KNOWN)


def test_a_model_change_has_no_cases_of_its_own():
    with pytest.raises(ValueError, match="no cases of its own"):
        gate.load_candidate({"name": "m", "kind": "model", "change": {"model": "other:7b"},
                             "cases": CASES[:3]}, KNOWN)
    candidate = gate.load_candidate({"name": "m", "kind": "model", "change": {"model": "other:7b"}}, KNOWN)
    assert candidate.cases == () and candidate.held_out == ()


# NEW-CG: what may train, by provenance


@pytest.mark.parametrize("provenance", gate.TRAINING_PROVENANCE)
def test_owner_turns_skill_cases_and_gate_reports_may_train(provenance):
    source = {"source": "episode:7", "provenance": provenance}
    candidate = gate.load_candidate(hint_candidate(trained_on=[source]), KNOWN)
    assert candidate.trained_on == (source,)


@pytest.mark.parametrize("provenance", ["tool-result", "web-page", "cloud-reply", "file", "model-answer"])
def test_text_that_came_through_a_tool_never_trains_anything(provenance):
    with pytest.raises(ValueError, match="NEW-CG"):
        gate.load_candidate(hint_candidate(trained_on=[{"source": "episode:7", "provenance": provenance}]), KNOWN)


def test_a_training_source_says_where_it_came_from_and_nothing_else():
    for source in ({"source": "episode:7"}, {"source": "", "provenance": "owner-turn"},
                   {"source": "e", "provenance": "owner-turn", "text": "what it said"}):
        with pytest.raises(ValueError, match="Each training source"):
            gate.load_candidate(hint_candidate(trained_on=[source]), KNOWN)


# The two runs differ only in the candidate


def test_a_hint_change_changes_the_hint_and_nothing_else():
    candidate = gate.load_candidate(hint_candidate(), KNOWN)
    settings = gate.arms(candidate, "qwen3.8:27b")
    assert settings[gate.BASELINE] == {"model": "qwen3.8:27b", "hint": ""}
    assert settings[gate.CANDIDATE] == {"model": "qwen3.8:27b", "hint": "Always give the unit."}


def test_a_model_change_changes_the_model_and_nothing_else():
    candidate = gate.load_candidate({"name": "m", "kind": "model", "change": {"model": "other:7b"}}, KNOWN)
    settings = gate.arms(candidate, "qwen3.8:27b")
    assert settings[gate.BASELINE] == {"model": "qwen3.8:27b", "hint": ""}
    assert settings[gate.CANDIDATE] == {"model": "other:7b", "hint": ""}


def test_each_trial_uses_the_same_seed_with_and_without_the_candidate():
    candidate = gate.load_candidate(hint_candidate(), KNOWN)
    seen = []

    def runner(scenario, arm, seed):
        seen.append((scenario, arm["hint"], seed))
        return {"status": "passed"}

    results = gate.run_gate(candidate, runner, model="m", regression=["general-no-tool"],
                            trials=2, base_seed=100)
    scenarios = ["general-no-tool", *CASES]
    assert len(results) == 2 * 2 * len(scenarios)
    for scenario in scenarios:
        without = [s for name, hint, s in seen if name == scenario and not hint]
        with_it = [s for name, hint, s in seen if name == scenario and hint]
        assert without == with_it == [100, 101]
    # The baseline is run in full before the candidate.
    assert [hint for _, hint, _ in seen] == [""] * len(seen[:len(seen) // 2]) + ["Always give the unit."] * (len(seen) // 2)


def test_cases_also_in_the_regression_suite_are_run_once_per_arm_and_trial():
    candidate = gate.load_candidate(hint_candidate(), KNOWN)
    results = gate.run_gate(candidate, lambda *a: {"status": "passed"}, model="m",
                            regression=CASES, trials=1, base_seed=0)
    assert len(results) == 2 * len(CASES)


# Deciding


def decide(baseline, candidate_plan, trials=5, regression=("general-no-tool", "cpu-grounding", "ram-grounding"),
           held_out=("gpu-denied", "sensor-failure", "sensor-routing"), partial=False, bars=DEFAULT_BARS,
           value="normal"):
    candidate = gate.load_candidate(hint_candidate(held_out=list(held_out), value=value), KNOWN)
    scenarios = list(dict.fromkeys([*regression, *candidate.cases]))
    results = results_for(scenarios, trials, baseline, candidate_plan)
    return gate.decide(results, candidate, list(regression), bars, partial)


def test_a_candidate_that_helps_its_held_out_cases_and_harms_nothing_passes():
    verdict, reasons, summary = decide({"gpu-denied": ["failed"] * 5}, {})
    assert verdict == gate.PASSED
    assert summary["held_out"][gate.CANDIDATE]["passed"] == 15
    assert summary["held_out"][gate.BASELINE]["passed"] == 10
    assert summary["per_scenario"]["gpu-denied"] == {gate.BASELINE: "0/5", gate.CANDIDATE: "5/5"}
    assert any("Held-out" in r for r in reasons)


def test_a_candidate_that_costs_the_regression_suite_a_pass_fails():
    verdict, reasons, _ = decide({"gpu-denied": ["failed"] * 5},
                                 {"cpu-grounding": ["passed"] * 4 + ["failed"]})
    assert verdict == gate.FAILED
    assert reasons[0].startswith("Regression suite")


def test_a_candidate_must_do_better_not_as_well_on_its_own_cases():
    verdict, reasons, _ = decide({}, {})
    assert verdict == gate.FAILED
    assert any("did not do better" in r for r in reasons)


def test_a_candidate_that_helps_but_stays_below_the_bar_fails():
    # 9/15 against 8/15: better, but 60% is below the 85% bar for normal work.
    verdict, reasons, _ = decide({"gpu-denied": ["failed"] * 5, "sensor-failure": ["failed"] * 2},
                                 {"gpu-denied": ["failed"] * 5, "sensor-failure": ["failed"] * 1})
    assert verdict == gate.FAILED
    assert any("below the bar" in r for r in reasons)


def test_the_bar_is_the_one_for_the_candidates_value():
    # Held-out: 5/15 without, 11/15 (73%) with. Over the 70% bar for
    # low work, under the 85% bar for normal work.
    plans = ({"gpu-denied": ["failed"] * 5, "sensor-failure": ["failed"] * 5},
             {"gpu-denied": ["failed"] * 4})
    verdict, _, summary = decide(*plans, value="low")
    assert summary["bar"] == DEFAULT_BARS["low"] and verdict == gate.PASSED
    assert decide(*plans, value="normal")[0] == gate.FAILED


def test_fewer_than_fifteen_attempts_is_unmeasured_not_failed():
    verdict, reasons, _ = decide({"gpu-denied": ["failed"] * 4}, {}, trials=4)
    assert verdict == gate.UNMEASURED
    assert all(str(MINIMUM_ATTEMPTS) in r for r in reasons)


def test_trials_with_no_reply_count_against_and_most_of_them_say_nothing():
    # Timeouts count as attempts not passed, as in the model record.
    verdict, _, summary = decide({"gpu-denied": ["failed"] * 5}, {"cpu-grounding": ["timeout"] + ["passed"] * 4})
    assert verdict == gate.FAILED
    assert summary["regression"][gate.CANDIDATE]["no_reply"] == 1
    errors = {name: ["error"] * 5 for name in CASES + ["general-no-tool"]}
    verdict, reasons, _ = decide({}, errors)
    assert verdict == gate.UNMEASURED
    assert any("runtime was failing" in r for r in reasons)


def test_a_partial_regression_suite_can_fail_but_never_pass():
    verdict, reasons, _ = decide({"gpu-denied": ["failed"] * 5}, {}, partial=True)
    assert verdict == gate.UNMEASURED
    assert "part of the regression suite" in reasons[0]
    verdict, _, _ = decide({}, {}, partial=True)
    assert verdict == gate.FAILED


def test_a_model_change_is_judged_on_the_regression_suite_alone():
    candidate = gate.load_candidate({"name": "m", "kind": "model", "change": {"model": "other:7b"}}, KNOWN)
    regression = KNOWN[:5]
    same = results_for(regression, 3, {}, {})
    assert gate.decide(same, candidate, regression, DEFAULT_BARS)[0] == gate.PASSED
    worse = results_for(regression, 3, {}, {regression[0]: ["failed", "passed", "passed"]})
    assert gate.decide(worse, candidate, regression, DEFAULT_BARS)[0] == gate.FAILED


def test_default_trials_reach_fifteen_attempts_on_what_decides():
    candidate = gate.load_candidate(hint_candidate(), KNOWN)
    assert gate.default_trials(candidate, KNOWN) * len(candidate.held_out) >= MINIMUM_ATTEMPTS
    model = gate.load_candidate({"name": "m", "kind": "model", "change": {"model": "o"}}, KNOWN)
    assert gate.default_trials(model, KNOWN) == 3


# The command and its report


def run_main(tmp_path, monkeypatch, candidate, statuses=None, extra=()):
    monkeypatch.setattr(gate, "owner_bars", lambda: dict(DEFAULT_BARS))
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    statuses = statuses or (lambda scenario, arm: "passed")
    calls = []

    def factory(folder):
        assert folder.parent == tmp_path

        def runner(scenario, arm, seed):
            calls.append((scenario, dict(arm), seed))
            return {"status": statuses(scenario, arm)}

        return runner

    output = tmp_path / "report.json"
    code = gate.main(["--candidate", str(path), "--output", str(output), *extra], runner_factory=factory)
    return code, output, calls


def test_the_report_records_what_was_run_and_what_a_promotion_would_replace(tmp_path, monkeypatch, capsys):
    candidate = hint_candidate(trained_on=[{"source": "episode:3", "provenance": "owner-turn"}])

    def statuses(scenario, arm):
        loaded = gate.load_candidate(candidate, KNOWN)
        return "failed" if scenario in loaded.held_out and not arm["hint"] else "passed"

    code, output, calls = run_main(tmp_path, monkeypatch, candidate, statuses)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0 and report["verdict"] == gate.PASSED
    assert report["replaces"] == {"hint": ""}
    assert report["candidate"]["trained_on"] == [{"source": "episode:3", "provenance": "owner-turn"}]
    assert set(report["candidate"]["held_out"]) | set(report["candidate"]["drafting_cases"]) == set(CASES)
    assert report["regression"] == KNOWN and not report["partial_regression"]
    assert len(report["results"]) == len(calls)
    assert "PASSED" in capsys.readouterr().out
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("freya-gate-")]


def test_a_model_report_says_which_model_it_would_replace(tmp_path, monkeypatch):
    code, output, calls = run_main(
        tmp_path, monkeypatch, {"name": "m", "kind": "model", "change": {"model": "other:7b"}},
        extra=["--model", "qwen3.8:27b", "--trials", "1"],
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["replaces"] == {"model": "qwen3.8:27b"}
    assert {arm["model"] for _, arm, _ in calls} == {"qwen3.8:27b", "other:7b"}
    assert code == 0


def test_a_partial_run_is_marked_and_cannot_pass(tmp_path, monkeypatch):
    code, output, _ = run_main(
        tmp_path, monkeypatch, {"name": "m", "kind": "model", "change": {"model": "o"}},
        extra=["--regression-scenario", "cpu-grounding", "--trials", "15"],
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["partial_regression"] and report["verdict"] == gate.UNMEASURED
    assert code == 1


def test_a_refused_candidate_runs_nothing(tmp_path, monkeypatch, capsys):
    code, output, calls = run_main(tmp_path, monkeypatch, hint_candidate(kind="skill"))
    assert code == 2 and not calls and not output.exists()
    assert "refused" in capsys.readouterr().err


def test_an_existing_report_is_never_overwritten(tmp_path, monkeypatch):
    (tmp_path / "report.json").write_text("kept", encoding="utf-8")
    with pytest.raises(SystemExit):
        run_main(tmp_path, monkeypatch, hint_candidate())
    assert (tmp_path / "report.json").read_text(encoding="utf-8") == "kept"


def test_a_failed_preflight_writes_an_error_report_and_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "owner_bars", lambda: dict(DEFAULT_BARS))

    def unavailable(*args):
        raise RuntimeError("Required model is not installed: other:7b")

    monkeypatch.setattr(workflow, "model_inventory", unavailable)
    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: pytest.fail("Worker should not start"))
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps({"name": "m", "kind": "model", "change": {"model": "other:7b"}}), encoding="utf-8")
    output = tmp_path / "report.json"
    assert gate.main(["--candidate", str(path), "--output", str(output)]) == 2
    assert "not installed" in json.loads(output.read_text(encoding="utf-8"))["preflight_error"]


def test_the_worker_is_given_the_seed_and_the_hint_by_file(tmp_path, monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["hint"] = (tmp_path / "hint-1.txt").read_text(encoding="utf-8")
        output = command[command.index("--output") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump({"status": "passed"}, handle)
        return type("Done", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    monkeypatch.setattr("freya.child_environment.child_environment", lambda: {})
    runner = gate.worker_runner(embedding_model="e", background_model=None, request_timeout=5,
                                timeout=5, scratch=tmp_path)
    assert runner("cpu-grounding", {"model": "m", "hint": "Say the unit."}, 41)["status"] == "passed"
    command = seen["command"]
    assert command[command.index("--seed") + 1] == "41"
    assert "Say the unit." not in command
    assert seen["hint"] == "Say the unit."


def test_a_worker_that_overruns_is_a_timeout(tmp_path, monkeypatch):
    def slow(command, **kwargs):
        raise gate.subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(gate.subprocess, "run", slow)
    runner = gate.worker_runner(embedding_model="e", background_model=None, request_timeout=5,
                                timeout=5, scratch=tmp_path)
    assert runner("cpu-grounding", {"model": "m", "hint": ""}, 1)["status"] == "timeout"


# The workflow trial's two new controls


def test_a_trial_sends_its_seed_with_every_model_request(tmp_path, monkeypatch, fake_ollama):
    import ollama

    monkeypatch.setattr(ollama, "Client", lambda **kwargs: fake_ollama)
    fake_ollama.stream_script = [[("system_cpu", {})], "CPU load is 23 percent."]
    result = workflow.run_trial(get_scenario("cpu-grounding"), tmp_path, "gpt-oss:20b", "nomic-embed-text", 2, seed=77)
    assert result["status"] == "passed"
    chats = [kwargs for kind, *_, kwargs in fake_ollama.calls if kind == "chat"]
    assert chats and all(kwargs["options"]["seed"] == 77 for kwargs in chats)


def test_a_trial_without_a_seed_sends_none(tmp_path, monkeypatch, fake_ollama):
    import ollama

    monkeypatch.setattr(ollama, "Client", lambda **kwargs: fake_ollama)
    fake_ollama.stream_script = [[("system_cpu", {})], "CPU load is 23 percent."]
    workflow.run_trial(get_scenario("cpu-grounding"), tmp_path, "gpt-oss:20b", "nomic-embed-text", 2)
    chats = [kwargs for kind, *_, kwargs in fake_ollama.calls if kind == "chat"]
    assert chats and not any("seed" in (kwargs.get("options") or {}) for kwargs in chats)


def test_a_trials_hint_change_reaches_the_model(tmp_path, monkeypatch, fake_ollama):
    import ollama

    monkeypatch.setattr(ollama, "Client", lambda **kwargs: fake_ollama)
    fake_ollama.stream_script = [[("system_cpu", {})], "CPU load is 23 percent."]
    workflow.run_trial(get_scenario("cpu-grounding"), tmp_path, "gpt-oss:20b", "nomic-embed-text", 2,
                       hint_addition="GATE-HINT-MARKER")
    sent = json.dumps([messages for kind, _, messages, *_ in fake_ollama.calls if kind == "chat"])
    assert "GATE-HINT-MARKER" in sent
