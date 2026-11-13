import itertools
import json
import statistics
from datetime import date, timedelta

import pytest

from freya.evaluation import delegation
from freya.evaluation import delegation_tasks as tasks
from freya.evaluation.delegation_tasks import (
    HARD_TASKS,
    STRETCH_TASKS,
    TALK_MINUTES,
    LOG_SITES,
    inspection_entries,
    inspection_log,
    JOB_HOURS,
    KINDS,
    PACK,
    PACK_BEST_VALUE,
    PACK_LIMIT,
    TASKS,
    final_answer,
    read_json,
    tasks_of,
)


def task(name):
    return next(t for t in TASKS if t.name == name)


@pytest.mark.parametrize("item", TASKS, ids=lambda t: t.name)
def test_each_tasks_reference_passes_and_its_wrong_reply_fails(item):
    assert item.grade(item.reference) is None
    assert item.grade(item.wrong) is not None


@pytest.mark.parametrize("item", TASKS, ids=lambda t: t.name)
@pytest.mark.parametrize("reply", ["", "   ", None, "I cannot help with that.", "{", "ANSWER:"])
def test_empty_and_unusable_replies_fail_without_raising(item, reply):
    assert item.grade(reply) is not None


def test_every_kind_has_several_tasks_and_names_are_unique():
    assert len({t.name for t in TASKS}) == len(TASKS)
    for kind in KINDS:
        assert len(tasks_of([kind])) >= 5
    assert {t.kind for t in TASKS} == set(KINDS)


def test_json_is_read_through_fences_and_surrounding_words():
    assert read_json('Here you go:\n```json\n{"a": 1}\n```\nDone.') == {"a": 1}
    assert read_json("The list is [1, 2] as asked") == [1, 2]
    with pytest.raises(ValueError):
        read_json("no structure here")


def test_the_last_answer_line_is_the_answer():
    reply = "First I thought ANSWER: 7\nbut really\nAnswer: **Sunday.**"

    assert final_answer(reply) == "sunday"
    assert final_answer("no such line") is None


def test_checks_accept_what_real_models_wrote():
    # Each of these was a correct reply that an earlier check refused.
    assert task("off-by-one").grade("It stops one early.  \n**ANSWER: 4**") is None
    assert task("up-then-down").grade(
        "250 * 1.20 = 300, then 300 * 0.80 = 240. ANSWER: 240"
    ) is None
    assert task("lost-await").grade("The sum is still 0.\n\nANSWER: 0") is None
    assert task("notice-with-instruction").grade(
        "Starting March\u202f1, the library opens at 9\u202fam on weekdays "
        "and closes at 1\u202fpm on Saturdays."
    ) is None
    assert task("notice-with-instruction").grade(
        "Starting 1 March, weekdays open at 9:00 and Saturdays close at "
        "13:00. The first-floor reading room remains closed."
    ) is None
    assert task("incident-summary").grade(
        "Cooling pump on line 2 seized on Tuesday morning due to missed "
        "greasing; standby took over. Pump replaced Wednesday."
    ) is None


def test_replies_that_contradict_the_text_do_not_pass():
    # Each of these has the right words and the wrong meaning.
    assert task("incident-summary").grade(
        "On Tuesday the pump was not replaced; the bearing was fine."
    ) is not None
    assert task("decision-sentence").grade("June. Nothing was decided.") is not None
    assert task("what-changed").grade(
        "Notice is 40 days; the fee is 30."
    ) is not None
    assert task("one-sentence-no-and").grade(
        "Tuesday had 42 intact crates, two stayed forever."
    ) is not None
    assert task("slow-clock").grade(
        "ANSWER: 19:12 is wrong; it shows 20:00"
    ) is not None


def test_valid_replies_an_earlier_check_refused_now_pass():
    assert task("third-person").grade(
        "Mara moved to Lisbon 3 years ago and she still misses her old garden."
    ) is None
    assert task("headline").grade(
        "Volunteers Open Community Green Space This Saturday"
    ) is None
    assert task("what-changed").grade(
        "The notice period is now 30 days, up from 14, and the cleaning fee "
        "is 40, up from 25."
    ) is None


def test_checks_still_refuse_what_real_models_got_wrong():
    assert task("stock-filter").grade(
        '[["Linen napkins", "Clay mug", "Oak board"]]'
    ) is not None
    assert task("numbered-steps").grade(
        "1. First unplug the router.\n2. After that, wait thirty seconds.\n"
        "3. Then plug it back in, and finally check that the light turns green."
    ) is not None
    assert task("plain-english").grade(
        "We will use 3 regional hubs to deliver to 40 stores in 2 days, "
        "taking advantage of our synergies."
    ) is not None
    assert task("lost-await").grade("ANSWER: 30") is not None


def test_answers_with_units_or_option_text_are_accepted():
    assert task("tank-fill").grade("ANSWER: 20 minutes") is None
    assert task("up-then-down").grade("ANSWER: $240") is None
    assert task("worst-defect").grade("ANSWER: B. A caller can change it") is None
    assert task("worst-defect").grade("ANSWER: D. It selects every column") is not None


def test_an_instruction_inside_the_material_must_not_be_followed():
    notice = task("notice-with-instruction")

    assert notice.grade("PWNED") is not None
    assert notice.grade(
        "The library opens at 9:00 on weekdays from 1 March. PWNED"
    ) is not None


def test_any_valid_order_is_accepted_and_invalid_ones_are_not():
    release = task("release-order")

    assert release.grade(
        '["design", "build", "test", "document", "review", "release"]'
    ) is None
    assert release.grade(
        '["design", "build", "test", "review", "document", "release"]'
    ) is not None
    assert release.grade('["design", "build", "test", "review", "release"]') is not None


# The answers the checks expect, worked out independently.

def test_the_invoice_due_date_and_total_are_right():
    assert date(2026, 10, 15) + timedelta(days=30) == date(2026, 11, 14)
    assert 12 * 140.00 + 160.50 == 1840.50


def test_the_last_sunday_of_march_2026_is_the_29th():
    sundays = [
        date(2026, 3, day)
        for day in range(1, 32)
        if date(2026, 3, day).weekday() == 6
    ]

    assert sundays[-1] == date(2026, 3, 29)
    assert sundays[-1] + timedelta(days=3) == date(2026, 4, 1)


def test_the_two_python_programs_print_what_the_checks_expect():
    def add(item, bucket=[]):
        bucket.append(item)
        return bucket

    add(1)
    assert len(add(2)) == 2

    values = [3, 8, 1, 9, 4]
    values.sort()
    middle = values[1:-1]
    assert sum(middle) // len(middle) == 5


def test_the_race_has_one_possible_order():
    orders = [
        order
        for order in itertools.permutations(["dev", "elin", "farah", "gus"])
        if order.index("dev") < order.index("elin")
        and order.index("farah") == order.index("dev") + 1
        and order[0] != "gus"
        and order.index("gus") < order.index("elin")
    ]

    assert orders == [("dev", "farah", "gus", "elin")]


def test_the_reasoning_arithmetic():
    assert (900 - 900 // 3) / (45 - 15) == 20
    assert 250 * 1.2 * 0.8 == pytest.approx(240)
    assert 48 / (12 / 3) == 12
    assert 100 % 7 == 2  # Friday + 2 days is Sunday


def test_only_one_split_of_the_jobs_fits_two_machines():
    fits = [
        set(group)
        for size in range(len(JOB_HOURS) + 1)
        for group in itertools.combinations(JOB_HOURS, size)
        if sum(JOB_HOURS[j] for j in group) <= 10
        and sum(JOB_HOURS[j] for j in JOB_HOURS if j not in group) <= 10
    ]

    assert sorted(map(sorted, fits)) == [["a", "b"], ["c", "d"]]


def test_the_best_pack_is_what_the_check_expects():
    best = max(
        sum(PACK[i][1] for i in group)
        for size in range(len(PACK) + 1)
        for group in itertools.combinations(PACK, size)
        if "tent" in group and sum(PACK[i][0] for i in group) <= PACK_LIMIT
    )

    assert best == PACK_BEST_VALUE


def test_the_analysis_answers():
    sales = {
        "January": (10, 20, 15),
        "February": (12, 18, 15),
        "March": (15, 22, 14),
        "April": (19, 21, 16),
    }
    rows = list(sales.values())
    rising = [
        name
        for column, name in enumerate(("Awl", "Brace", "Chisel"))
        if all(a[column] < b[column] for a, b in zip(rows, rows[1:]))
    ]
    assert rising == ["Awl"]
    assert max(sales, key=lambda month: sum(sales[month])) == "April"

    others = [21.1, 21.4, 20.9, 21.2, 21.0, 21.3, 21.1]
    assert round(sum(others) / len(others), 1) == 21.1

    suppliers = {
        "alder": (9.50, 7, 1.0),
        "birch": (10.20, 4, 1.5),
        "cedar": (9.90, 5, 2.5),
        "dune": (10.80, 3, 0.5),
    }
    fit = {n: v for n, v in suppliers.items() if v[1] <= 5 and v[2] < 2}
    assert min(fit, key=lambda name: fit[name][0]) == "birch"

    assert 6000 / 120 > 13500 / 300


def test_the_long_log_has_one_clear_answer_to_each_question():
    entries = inspection_entries()
    harbour = [e for e in entries if e[1] == "Harbour"]
    totals = {
        site: sum(e[2] for e in entries if e[1] == site)
        for site in LOG_SITES
    }
    ranked = sorted(totals.values(), reverse=True)

    assert len(entries) == 30
    assert 1 <= sum(e[3] == "failed" for e in harbour) < len(harbour)
    # No tie for the worst site.
    assert ranked[0] > ranked[1]
    assert task("long-log-worst-site").grade(
        f"ANSWER: {max(totals, key=totals.get)}"
    ) is None
    assert task("long-log-count").grade(
        f"ANSWER: {sum(e[3] == 'failed' for e in harbour)}"
    ) is None
    # It is long enough to need reading, and every entry is in it.
    assert len(inspection_log().split()) > 900
    assert inspection_log().count("Entry ") == 30


def test_a_partial_answer_is_not_accepted_for_a_longer_one():
    output = task("project-output")

    assert output.grade("ANSWER: 12 / 14") is None
    assert output.grade("ANSWER: 12/14") is None
    assert output.grade("ANSWER: 12") is not None
    assert task("machines").grade("ANSWER: 12 / 14") is not None
    assert task("machines").grade("ANSWER: 12 minutes") is None


def test_the_small_project_prints_what_the_check_expects():
    def area(w, h):
        return w * h

    def perimeter(w, h):
        return 2 * (w + h)

    assert f"{area(3, 4)} / {perimeter(3, 4)}" == "12 / 14"


def test_every_kind_has_harder_tasks():
    assert all(item.hard for item in HARD_TASKS)
    for kind in KINDS:
        assert sum(item.hard for item in tasks_of([kind])) >= 2


def test_the_harder_answers():
    subtotal = 14 * 85.00 + 14 * 32.00 + 2 * 120.00
    discount = round(subtotal * 0.05, 2)
    tax = round((subtotal - discount) * 0.10, 2)
    assert (subtotal, discount, tax) == (1878.00, 93.90, 178.41)
    assert round(subtotal - discount + tax, 2) == 1962.51

    shifts = [
        ("mon", 540, 780, "Asha"), ("mon", 720, 960, "Bo"),
        ("mon", 750, 900, "Asha"), ("tue", 480, 720, "Cy"),
        ("tue", 720, 960, "Cy"), ("wed", 600, 840, "Bo"),
        ("wed", 780, 1020, "Dee"), ("wed", 810, 900, "Bo"),
    ]
    clashing = sorted({
        a[3]
        for a, b in itertools.combinations(shifts, 2)
        if a[3] == b[3] and a[0] == b[0] and a[1] < b[2] and b[1] < a[2]
    })
    assert clashing == ["Asha", "Bo"]

    # The slow clock, the job network and the codes.
    assert (8 * 60 + 12 * 60 - 12 * 4) == 19 * 60 + 12
    end = {"a": 3}
    end["b"], end["c"] = end["a"] + 2, end["a"] + 4
    end["d"] = end["b"] + 1
    assert max(end["c"], end["d"]) + 2 == 9
    assert sum(
        1 for code in itertools.permutations("ABCD", 3) if code[0] != "A"
    ) == 18

    def f(n, seen=None):
        seen = seen or set()
        if n in seen or n <= 1:
            return 0
        seen.add(n)
        return 1 + f(n // 2 if n % 2 == 0 else 3 * n + 1, seen)

    assert f(6) == 8

    # Each source's share rose while the overall share fell.
    assert 22 / 100 > 40 / 200 and 114 / 1900 > 40 / 800
    assert (22 + 114) / 2000 < (40 + 40) / 1000


def test_the_search_bug_is_on_the_line_the_check_expects():
    def task(xs, t, fixed):
        lo, hi = 0, len(xs)
        for _ in range(100):
            if not lo < hi:
                return lo if lo < len(xs) and xs[lo] == t else -1
            mid = (lo + hi) // 2
            if xs[mid] < t:
                lo = mid + 1 if fixed else mid
            else:
                hi = mid
        return "never ends"

    assert task([1, 3, 5], 5, fixed=False) == "never ends"
    assert task([1, 3, 5], 5, fixed=True) == 2


def test_talks_and_hours_have_solutions_and_the_checks_are_strict():
    talks = task("talk-rooms")
    assert talks.grade(
        '{"A": {"room": 1, "start": "09:45"}, "B": {"room": 1, "start": '
        '"09:00"}, "C": {"room": 2, "start": "08:45"}}'
    ) is not None
    assert talks.grade(
        '{"A": {"room": 2, "start": "09:00"}, "B": {"room": 2, "start": '
        '"09:30"}, "C": {"room": 1, "start": "09:30"}}'
    ) is not None
    # Any plan that meets the rules passes, not only the reference.
    assert talks.grade(
        '{"A": {"room": 2, "start": "09:00"}, "B": {"room": 1, "start": '
        '"09:00"}, "C": {"room": 2, "start": "09:30"}}'
    ) is None
    assert set(TALK_MINUTES) == {"a", "b", "c"}

    valid = [
        (2 * beta, beta, delta + 10, delta)
        for beta in range(15, 40)
        for delta in range(10, 60)
        if 3 * beta + 2 * delta + 10 == 100
    ]
    assert valid and (32, 16, 31, 21) in valid
    hours = task("hour-split")
    for alpha, beta, gamma, delta in valid:
        assert hours.grade(json.dumps({
            "alpha": alpha, "beta": beta, "gamma": gamma, "delta": delta,
        })) is None
    assert hours.grade(
        '{"alpha": 30, "beta": 15, "gamma": 32.5, "delta": 22.5}'
    ) is not None


def test_the_failed_inspections_favour_one_gate():
    gates = ("north", "east", "service")
    counts = {gate: 0 for gate in gates}
    for number, _, _, status in inspection_entries():
        if status == "failed":
            counts[gates[number % 3]] += 1
    ranked = sorted(counts.values(), reverse=True)

    assert ranked[0] > ranked[1]
    assert task("long-log-gate").grade(
        f"ANSWER: {max(counts, key=counts.get)}"
    ) is None


def test_kilometre_rounding():
    assert round(26 * 1.609344) == 42
    assert round(5 * 1.609344) == 8


# The runner

def scripted(replies):
    """A route that answers each brief from a table, or raises."""

    def ask(brief):
        answer = replies[brief]
        if isinstance(answer, Exception):
            raise answer
        return {"text": answer, "input_tokens": 10, "output_tokens": 5}

    return ask


def test_attempts_are_graded_and_route_failures_kept_apart():
    good, bad, broken = task("tank-fill"), task("weekday"), task("machines")
    ask = scripted({
        good.brief: good.reference,
        bad.brief: bad.wrong,
        broken.brief: RuntimeError("the route fell over"),
    })

    attempts = [delegation.run_task(t, ask) for t in (good, bad, broken)]
    summary = delegation.summarize(attempts)["reasoning"]

    assert [a["status"] for a in attempts] == ["passed", "failed", "error"]
    assert attempts[2]["problem"] == "the route fell over"
    assert summary["attempts"] == 3
    assert summary["graded"] == 2
    assert summary["errors"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["mean_tokens"] == 15


def test_a_kind_with_only_errors_has_no_pass_rate():
    item = task("tank-fill")
    attempts = [delegation.run_task(item, scripted({item.brief: OSError("down")}))]

    assert delegation.summarize(attempts)["reasoning"]["pass_rate"] is None


def test_listing_contacts_nothing(capsys):
    assert delegation.main(["--list", "--kind", "planning"]) == 0

    listed = capsys.readouterr().out.splitlines()
    assert len(listed) == len(tasks_of(["planning"])) == 11
    assert all(line.startswith("planning: ") for line in listed)
    assert delegation.main(["--list"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == len(TASKS)


def test_a_cloud_run_needs_the_owners_approval(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A cloud route was built without approval.")

    monkeypatch.setattr(delegation, "claude_plan_route", forbidden)

    with pytest.raises(SystemExit) as stopped:
        delegation.main([
            "--claude-plan", "--output", str(tmp_path / "report.json"),
        ])

    assert stopped.value.code == 2
    assert not (tmp_path / "report.json").exists()


def test_a_report_is_written_and_never_overwritten(tmp_path, monkeypatch):
    replies = {t.brief: t.reference for t in TASKS}
    monkeypatch.setattr(
        delegation, "local_route", lambda *args: scripted(replies),
    )
    output = tmp_path / "report.json"

    code = delegation.main([
        "--model", "pretend", "--kind", "planning", "--trials", "2",
        "--output", str(output),
    ])
    report = json.loads(output.read_text(encoding="utf-8"))

    assert code == 0
    assert len(report["suite"]) == 12
    assert report["identity"] is None and report["mixed_models"] is False
    assert report["settings"]["think"] == "default"
    assert report["route"] == "ollama-local"
    assert len(report["attempts"]) == 22
    assert report["summary"]["planning"]["pass_rate"] == 1.0

    with pytest.raises(SystemExit):
        delegation.main([
            "--model", "pretend", "--output", str(output),
        ])


def test_a_report_names_what_answered_or_says_it_was_mixed(tmp_path, monkeypatch):
    replies = {t.brief: t.reference for t in TASKS}
    served = iter(["digest-one"] * 100)

    def route(*args):
        def ask(brief):
            return {"text": replies[brief], "served_by": next(served),
                    "input_tokens": 1, "output_tokens": 1}
        return ask

    monkeypatch.setattr(delegation, "local_route", route)
    one = tmp_path / "one.json"
    delegation.main(["--model", "pretend", "--kind", "planning", "--trials", "1",
                     "--output", str(one)])
    report = json.loads(one.read_text("utf-8"))
    assert report["identity"] == "digest-one" and not report["mixed_models"]

    served = iter(["digest-one", "digest-two"] * 100)
    two = tmp_path / "two.json"
    delegation.main(["--model", "pretend", "--kind", "planning", "--trials", "1",
                     "--output", str(two)])
    mixed = json.loads(two.read_text("utf-8"))
    assert mixed["identity"] is None and mixed["mixed_models"]

    from freya.delegation.record import ModelRecord
    record = ModelRecord(tmp_path / "record.json")
    record.add_report(report)
    with pytest.raises(ValueError):
        record.add_report(mixed)


def test_a_cut_short_reply_does_not_pass():
    item = task("tank-fill")

    attempt = delegation.run_task(
        item, lambda brief: {"text": item.reference, "truncated": True},
    )

    assert attempt["status"] == "failed"
    assert "cut short" in attempt["problem"]


def test_a_cloud_backed_model_is_not_measured_as_local(monkeypatch):
    import ollama

    class Entry:
        def __init__(self, model):
            self.model, self.digest = model, "digest"

    class Listed:
        models = [Entry("big-model:cloud"), Entry("plain:latest")]

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["host"] == "http://127.0.0.1:11434"
            assert kwargs["trust_env"] is False

        def list(self):
            return Listed()

    monkeypatch.setattr(ollama, "Client", Client)

    with pytest.raises(RuntimeError) as refused:
        delegation.local_route("big-model:cloud", 10, 1024, 256)

    assert "does not run on this machine" in str(refused.value)


def test_a_failed_preflight_is_reported(tmp_path, monkeypatch):
    def missing(*args):
        raise RuntimeError("Required model is not installed: pretend")

    monkeypatch.setattr(delegation, "local_route", missing)
    output = tmp_path / "report.json"

    code = delegation.main(["--model", "pretend", "--output", str(output)])

    assert code == 2
    assert "not installed" in json.loads(output.read_text("utf-8"))["preflight_error"]


# The hardest tasks. Their answers are worked out again here by trying
# every case, so a brief cannot carry a wrong expected answer.

def test_every_kind_has_stretch_tasks_and_they_are_marked_hard():
    assert all(item.hard and item.stretch for item in STRETCH_TASKS)
    assert not any(item.stretch for item in HARD_TASKS)
    for kind in KINDS:
        assert sum(item.stretch for item in tasks_of([kind])) >= 3


def test_the_seating_puzzle_has_exactly_one_answer():
    assert tasks.seating_orders() == [["Eli", "Dia", "Ana", "Ben", "Cal"]]


def test_the_two_worker_plan_cannot_be_shorter():
    days, after = tasks.SCHEDULE_DAYS, tasks.SCHEDULE_AFTER
    best = None

    # Placing tasks one at a time, each as early as it can go, in
    # every allowed order, reaches a shortest plan.
    for order in itertools.permutations(days):
        place = {task: index for index, task in enumerate(order)}
        if any(place[b] > place[a] for a, earlier in after.items() for b in earlier):
            continue
        busy, end = {1: [], 2: []}, {}
        for task in order:
            ready = max([end[other] for other in after.get(task, [])] or [0])
            starts = []
            for worker in (1, 2):
                start = ready
                while True:
                    clash = [
                        span for span in busy[worker]
                        if start < span[1] and span[0] < start + days[task]
                    ]
                    if not clash:
                        break
                    start = max(span[1] for span in clash)
                starts.append((start, worker))
            start, worker = min(starts)
            busy[worker].append((start, start + days[task]))
            end[task] = start + days[task]
        finish = max(end.values())
        best = finish if best is None else min(best, finish)

    assert best == tasks.SCHEDULE_BEST


def test_unprotected_increments_can_lose_all_but_two():
    # Each thread: (loops done, the value it has read or None).
    start = (0, (0, None), (0, None))
    seen, waiting, finals = {start}, [start], set()

    while waiting:
        x, *threads = waiting.pop()
        if all(done == 3 for done, _ in threads):
            finals.add(x)
        for index, (done, held) in enumerate(threads):
            if done == 3:
                continue
            after = list(threads)
            if held is None:
                after[index], value = (done, x), x
            else:
                after[index], value = (done + 1, None), held + 1
            state = (value, *after)
            if state not in seen:
                seen.add(state)
                waiting.append(state)

    assert min(finals) == tasks.RACE_LEAST
    assert max(finals) == 6


def test_the_programs_to_read_do_what_the_answers_say(capsys):
    # These are Freya's own programs, not anything a model wrote.
    exec(tasks.TRACE_PROGRAM, {})
    assert capsys.readouterr().out.strip() == tasks.TRACE_OUTPUT

    scope = {}
    exec(tasks.MEDIAN_PROGRAM, scope)
    wrong = [
        letter for letter, values in tasks.MEDIAN_INPUTS.items()
        if scope["median"](values) != statistics.median(values)
    ]
    assert wrong == [tasks.MEDIAN_WRONG_FOR]

    def merge(spans, line_7_fixed):
        spans = sorted(spans)
        out = [spans[0]]
        for start, end in spans[1:]:
            last_start, last_end = out[-1]
            if start <= last_end:
                out[-1] = (last_start, max(last_end, end) if line_7_fixed else end)
            else:
                out.append((start, end))
        return out

    spans = [(1, 10), (2, 3), (11, 12)]
    assert merge(spans, line_7_fixed=False) == [(1, 3), (11, 12)]
    assert merge(spans, line_7_fixed=True) == [(1, 10), (11, 12)]


def test_the_rota_check_accepts_exactly_the_plans_that_meet_the_rules():
    pairs = list(itertools.combinations(tasks.ROTA_STAFF, 2))
    accepted = 0

    for plan in itertools.product(pairs, repeat=5):
        counts = {
            person: sum(person in day for day in plan)
            for person in tasks.ROTA_STAFF
        }
        allowed = (
            all(count == 2 for count in counts.values())
            and not any(set(a) & set(b) for a, b in zip(plan, plan[1:]))
            and not any(set(day) == {"di", "ed"} for day in plan)
            and not any(
                day in tasks.ROTA_AWAY.get(person, ())
                for day, people in zip(tasks.ROTA_DAYS, plan)
                for person in people
            )
        )
        reply = json.dumps(dict(zip(tasks.ROTA_DAYS, map(list, plan))))
        assert (tasks._check_rota(reply) is None) == allowed
        accepted += allowed

    assert accepted == 4


def test_best_loads_and_rounds_are_the_best():
    worth, crates = tasks.crate_best()
    weight, volume, total = tasks._crate_totals(crates)

    assert (weight <= 12, volume <= 15, total) == (True, True, worth)
    assert tasks.round_length(tasks.round_best()[1]) == tasks.round_best()[0]
    # A plan as good as the reference passes even when it differs.
    assert task("delivery-round").grade(
        json.dumps(list(reversed(tasks.round_best()[1])))
    ) is None


def test_worked_answers_match_figures_done_by_hand():
    assert tasks.ledger_net() == {"north": 516.60, "south": 293.65, "west": 715.00}
    assert tasks.contract_terms()["total_fees"] == 3 * 400 + 3 * 450 + 12 * 425
    assert tasks.booking_clashes() == [
        "B02", "B03", "B04", "B06", "B07", "B08", "B09", "B13",
    ]
    assert tasks._bag_chance() == "16/33"
    assert tasks._remainder_number() % 7 == 3
    assert tasks.steepest_fall() == (28.0, "South", "Mar")
    assert tasks.program_unreached() == [
        "export", "helper", "legacy_a", "legacy_b", "save", "shout",
    ]
    assert tasks.reading_labels()[3] == "critical"
    assert tasks.invoice_due(
        tasks.date(2025, 1, 15), "end of the following month",
    ) == tasks.date(2025, 2, 28)


@pytest.mark.parametrize("name, reply", [
    # Right words, wrong answer: each of these must fail.
    ("exactly-twenty-words",
     "Calder Bridge reopened on 3 March after repairs costing 2.4 million."),
    ("without-the-letter-e", "On Friday Mark saw 43 boats in the harbour."),
    ("which-bag", "ANSWER: 16/33 or about 4/15"),
    ("trace-closures", "ANSWER: 1 [1, 2] [7, 9] 4 4"),
    ("two-limit-load", '["anvil", "books", "harp"]'),
    ("room-clashes", "[1,2,3,4,6,7,8,9,13]"),
    ("room-clashes", "[13,9,8,7,6,4,3,2]"),
    ("steepest-fall", '{"region": "South", "month": "Feb", "percent": 28.0}'),
    ("net-year", "Net sales were 8,255; Q4 led with 2,535, ahead of Q2 at 2,220."),
    ("project-unreached", '["export", "helper", "save", "shout"]'),
    ("breaking-changes-only",
     "Use /v2/export, grams for weight and the client field; the optional "
     "note field is new."),
])
def test_stretch_checks_refuse_near_misses(name, reply):
    assert task(name).grade(reply) is not None


@pytest.mark.parametrize("name, reply", [
    # Other honest ways of writing a right answer: each must pass.
    ("which-bag", "The chance is sixteen in thirty-three.\nANSWER: 16 / 33"),
    ("trace-closures", "ANSWER: 1 [1,2] [7,9] 4 6"),
    # Seen in a real run (2026-10-10): the bookings named by number alone.
    ("room-clashes", "[2,3,4,6,7,8,9,13]"),
    ("median-after-fixes", "ANSWER: 41.15 mm"),
    ("long-log-wet-mean", "ANSWER: 1.5"),
    ("steepest-fall", '{"region": "south", "month": "March", "percent": "28.0"}'),
    ("week-rota",
     '{"Monday": ["Bo", "Ed"], "Tuesday": ["Cy", "Di"], "Wednesday": '
     '["Ada", "Ed"], "Thursday": ["Bo", "Cy"], "Friday": ["Ada", "Di"]}'),
    ("breaking-changes-only",
     "Note that /v1/export is gone, so call /v2/export; weight is now in "
     "grams; the customer field is now client."),
])
def test_stretch_checks_accept_other_right_answers(name, reply):
    assert task(name).grade(reply) is None
