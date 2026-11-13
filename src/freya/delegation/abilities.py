import json
from typing import Dict, List, Optional, Sequence

from freya.delegation.record import (
    DEFAULT_BARS,
    VALUE_HIGH,
    VALUE_LOW,
    VALUE_NORMAL,
    Candidate,
    Choice,
    ModelRecord,
    choose,
    entry_id,
    validate_bars,
)
from freya.evaluation.delegation_tasks import KINDS


LOCAL_ROUTE = "ollama-local"

# The owner's order of preference: this machine before any cloud route.
LOCAL_TIER = 0
CLOUD_TIER = 1

VALUES = (VALUE_LOW, VALUE_NORMAL, VALUE_HIGH)

# The thinking setting Freya's own replies are sent with. They name
# none, so the model's default applies; the evaluation records an
# unset setting under this word.
REPLY_THINKING = "default"

# Work that no evaluation task covers, from the limits written down
# with the measurements (docs/design/cloud-delegation.md and
# docs/design/model-benchmarks-2026-10-09.md). Nobody, local or cloud,
# has a figure for these, so Freya must not claim skill at them or
# assume another model has it. Remove a line only when tasks for it
# exist and have been run.
NOT_MEASURED = (
    "writing or changing code (a model's code cannot be graded until "
    "it can be run in isolation)",
    "documents of real length",
    "large bodies of code",
    "images",
    "work that takes many steps or many tool calls",
)

# Whether she can hand work over depends on the turn: the tool is
# offered only while a provider is switched on and a window is open
# to ask the owner in. Without one of these sentences she offers to
# hand work over when she cannot, or claims to have.
SENDING = (
    "You cannot send work to another model yourself. Say where it "
    "should go and why, and leave arranging it to the user; never "
    "offer to hand it over or say that you have."
)
SENDING_AVAILABLE = (
    "You can hand one piece of work to an outside model in a turn, "
    "with the work delegate tool, when that tool is offered to you. "
    "The user is asked before anything leaves this computer. Never "
    "say work was handed over unless that tool returned an answer."
)

# What the model is told the tool is for. The evaluation offers the
# same words, so it measures the tool Freya really has.
MODEL_RECORD_DESCRIPTION = (
    "Returns what evaluations have measured about your own model and "
    "the other models work could be given to, by kind of task: passes "
    "out of attempts, what value of work each is good enough for, "
    "where a task of each kind should go, and the kinds of work nobody "
    "has measured. Use it before saying how good you are at something, "
    "whether you can do a task well, or whether Claude or another model "
    "would do it better."
)

EVIDENCE = (
    "Pass counts on short, synthetic tasks graded by automatic checks, "
    "with the model working as a worker on one brief and no tools. "
    "They rank models; they do not prove a pass rate. Fewer than 15 "
    "attempts counts as unmeasured. Unmeasured means unknown, not bad "
    "and not good. Nothing here comes from what a model says about "
    "itself."
)


def _percent(share: float) -> str:
    return f"{share:.0%}"


def _label(about: dict) -> str:
    """An entry in words: where it runs, which model, how it was set."""
    place = "this machine" if about["route"] == LOCAL_ROUTE else about["route"]
    parts = [place, about["model"] or "the route's own model"]
    think = about["settings"].get("think")

    if think:
        parts.append(f"thinking {think}")

    return ", ".join(parts)


OTHER_TASKS = "measured on a different set of tasks from today's"
OTHER_BUILD = "measured on a different build of this model from the one installed"


def _with_tag(model):
    return model if ":" in model else f"{model}:latest"


class CurrentFigures:
    """
    The record as it applies today. A figure measured on another set
    of tasks, or on another build of a model than the one installed,
    is not a figure for what would do the work now, so it is treated
    as no figure at all.

    `suite` names today's set of tasks and `identities` gives the
    digest of each installed local model. Either may be left out, and
    then that check is not made.
    """

    def __init__(self, record: ModelRecord, suite=None, identities=None):
        self._record = record
        self._suite = suite
        self._identities = dict(identities or {})

    def entries(self):
        return self._record.entries()

    def about(self, entry):
        return self._record.about(entry)

    def recorded(self, entry, kind):
        """The figure as recorded, whether or not it still applies."""
        return self._record.result(entry, kind)

    def stale(self, entry, kind) -> Optional[str]:
        """Why a recorded figure does not apply today, or None."""
        result = self._record.result(entry, kind)
        about = self._record.about(entry)

        if result is None or about is None:
            return None
        if self._suite and result.suite != self._suite:
            return OTHER_TASKS
        if about["route"] == LOCAL_ROUTE and about.get("identity"):
            installed = self._identities.get(_with_tag(about["model"] or ""))
            if installed and installed != about["identity"]:
                return OTHER_BUILD

        return None

    def result(self, entry, kind):
        if self.stale(entry, kind):
            return None

        return self._record.result(entry, kind)


def _recorded(record, route: str, models=None) -> List[tuple]:
    found = []

    for entry in record.entries():
        about = record.about(entry)

        if about is None or about["route"] != route:
            continue
        if models is not None and about["model"] not in models:
            continue

        found.append((entry, about))

    return found


def candidates(
    record,
    kind: str,
    local_models: Sequence[str],
    cloud_routes: Sequence[str] = (),
) -> List[Candidate]:
    """
    The entries a task of this kind could be given to: every measured
    setting of the models Freya runs here, and every entry on a cloud
    route the caller says is open. A model or route with no figures at
    all is still a candidate, as an unmeasured one.

    Within this machine the quicker entry is the less costly; on a
    cloud route, the one that used fewer tokens. This checks no usage
    limit and sends nothing: a caller that goes on to send must still
    pass the dispatcher.
    """
    found = []

    def add(entries, tier, measure):
        for entry, _ in entries:
            result = record.result(entry, kind)
            cost = getattr(result, measure, None) if result else None
            found.append(Candidate(entry, tier, float(cost or 0.0)))

    for model in dict.fromkeys(local_models):
        entries = _recorded(record, LOCAL_ROUTE, [model])
        add(entries, LOCAL_TIER, "median_seconds")

        if not entries:
            found.append(
                Candidate(entry_id(LOCAL_ROUTE, model, None), LOCAL_TIER)
            )

    for route in cloud_routes:
        entries = _recorded(record, route)
        add(entries, CLOUD_TIER, "mean_tokens")

        if not entries:
            found.append(Candidate(entry_id(route, None, None), CLOUD_TIER))

    return found


def advise(
    record,
    kind: str,
    value: str,
    local_models: Sequence[str],
    cloud_routes: Sequence[str] = (),
    bars=None,
    unmeasured_allowed=None,
) -> Choice:
    """
    Where a task of this kind and value should go, by the record
    alone. It is advice: it sends nothing and reserves nothing.
    """
    return choose(
        candidates(record, kind, local_models, cloud_routes),
        kind,
        value,
        record,
        bars,
        unmeasured_allowed,
    )


def delegation_notes(
    record,
    kind: str,
    local_models: Sequence[str],
    route: str,
    route_label: str,
    bars=None,
    suite: Optional[str] = None,
    identities: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    What the record says when work of one kind is about to be handed
    to a cloud route, as sentences for the owner and the model.

    It informs; it never refuses or redirects. Only figures for
    today's tasks and the installed builds count, and nothing here is
    a model's opinion of itself.
    """
    figures = CurrentFigures(record, suite, identities)
    notes = []

    local = advise(
        figures, kind, "normal", local_models, (), bars,
        unmeasured_allowed=False,
    )

    if local.entry is not None and local.pass_rate is not None:
        notes.append(
            f"Measured: work done on this PC passed {_percent(local.pass_rate)} "
            f"of the test tasks for {kind}, which is good enough for "
            "ordinary work, so handing this over may not be needed."
        )

    measured = [
        result
        for result in (
            figures.result(entry, kind)
            for entry, _ in _recorded(figures, route)
        )
        if result is not None and result.measured
    ]

    if measured:
        best = max(result.pass_rate for result in measured)
        notes.append(
            f"Measured: {route_label} passed {_percent(best)} of the test "
            f"tasks for {kind}."
        )
    else:
        notes.append(
            f"{route_label} has not been measured on {kind} work, so how "
            "well it does this is not known."
        )

    return notes


def _standing(result, bars) -> str:
    """The highest value of work a result is good enough for."""
    if result is None or not result.measured:
        return "unmeasured"

    reached = [value for value in VALUES if result.pass_rate >= bars[value]]

    return reached[-1] if reached else "none"


def _entry_report(record, entry, about, bars, kinds) -> dict:
    measured = {}

    for kind in kinds:
        result = record.recorded(entry, kind)

        if result is None:
            continue

        stale = record.stale(entry, kind)
        measured[kind] = {
            "passed": result.passed,
            "attempts": result.attempts,
            "good_enough_for": (
                "unmeasured" if stale else _standing(result, bars)
            ),
        }

        if stale:
            measured[kind]["does_not_count"] = stale

    dates = sorted(
        result.measured_at[:10]
        for result in (record.recorded(entry, kind) for kind in kinds)
        if result and result.measured_at
    )

    return {
        "what": _label(about),
        "measured_on": dates[-1] if dates else "unknown",
        "kinds": measured,
    }


def _whose(choice: Choice, record) -> Optional[str]:
    if choice.entry is None:
        return None

    about = record.about(choice.entry)

    if about is None:
        # A candidate with no figures at all is named by its id.
        route, model, _ = json.loads(choice.entry)
        about = {
            "route": route,
            "model": None if model == "default" else model,
            "settings": {},
        }

    return _label(about)


def describe_abilities(
    record: ModelRecord,
    model: str,
    local_models: Sequence[str],
    cloud_routes: Optional[Dict[str, bool]] = None,
    bars=None,
    context_tokens: Optional[int] = None,
    kinds: Sequence[str] = KINDS,
    suite: Optional[str] = None,
    identities: Optional[Dict[str, str]] = None,
    can_delegate: bool = False,
) -> dict:
    """
    What has been measured about the model Freya answers with and the
    models she could hand work to, written for that model to read.

    `cloud_routes` maps each cloud route to whether the owner has
    switched its provider on. A route that is off is still described,
    so Freya can say what is known about it, but no work is advised
    towards it.

    With `suite` and `identities` (see `CurrentFigures`), a figure
    from another set of tasks or another build is shown but marked as
    not counting, and no work is advised on it.
    """
    record = CurrentFigures(record, suite, identities)
    bars = validate_bars(DEFAULT_BARS if bars is None else bars)
    cloud_routes = dict(cloud_routes or {})
    local_models = list(dict.fromkeys([model, *local_models]))
    open_routes = [route for route, on in cloud_routes.items() if on]

    own = _recorded(record, LOCAL_ROUTE, [model])
    report = {
        "how_to_read": EVIDENCE,
        "pass_rate_needed": {value: _percent(bars[value]) for value in VALUES},
        "you": {
            "model": model,
            "runs_on": "this machine",
            "reply_thinking": REPLY_THINKING,
            "measured": [
                _entry_report(record, entry, about, bars, kinds)
                for entry, about in own
            ],
        },
        "other_models": [],
        "sending_work": SENDING_AVAILABLE if can_delegate else SENDING,
        "where_work_should_go": {},
        "never_measured_for_any_model": list(NOT_MEASURED),
    }

    if context_tokens:
        report["you"]["context_tokens"] = context_tokens

    counted = [
        (entry, about) for entry, about in own
        if any(record.result(entry, kind) for kind in kinds)
    ]

    if not counted:
        report["you"]["note"] = (
            "Nothing that counts today has been measured for this model. "
            "Its ability at every kind of task is unknown."
        )
    elif not any(
        about["settings"].get("think") == REPLY_THINKING for _, about in counted
    ):
        report["you"]["note"] = (
            "These figures are for the thinking settings named. Your own "
            "replies use the model's default setting, which has not been "
            "measured, and they are conversation with tools and memory, "
            "which these tasks are not."
        )

    for other in local_models:
        if other == model:
            continue

        for entry, about in _recorded(record, LOCAL_ROUTE, [other]):
            report["other_models"].append(
                _entry_report(record, entry, about, bars, kinds)
            )

    for route, on in sorted(cloud_routes.items()):
        entries = _recorded(record, route)

        for entry, about in entries:
            described = _entry_report(record, entry, about, bars, kinds)
            described["switched_on"] = on
            report["other_models"].append(described)

        if not entries:
            report["other_models"].append({
                "what": route,
                "switched_on": on,
                "kinds": {},
                "note": (
                    "Never measured. Whether it does any kind of task "
                    "better or worse than you is unknown."
                ),
            })

    for kind in kinds:
        advice = {}

        for value in VALUES:
            choice = advise(
                record, kind, value, local_models, open_routes, bars,
            )
            who = _whose(choice, record)
            advice[value] = (
                f"{who}. {choice.reason}" if who else choice.reason
            )

        report["where_work_should_go"][kind] = advice

    return report


# What the model itself is given. A tool result longer than the
# context's clip (2,400 bytes, `ContextAuthority.fit_tools`) reaches
# the model as a broken excerpt, and the full description above is
# over 4,000 bytes even with nothing recorded. This says the same
# things in fewer words and is always under the limit; when something
# has to be left out to stay there, it says what.
MODEL_ANSWER_BYTES = 2_200

EVIDENCE_SHORT = (
    "Each figure is passes/attempts, then the highest value of work "
    "that is good enough for (none: not good enough for any). Short "
    "synthetic tasks, graded automatically, one brief, no tools: they "
    "rank models and do not prove a pass rate. Unmeasured (under 15 "
    "attempts) means unknown, not bad and not good. Nothing here is a "
    "model's own claim."
)
NOT_MEASURED_SHORT = (
    "writing or changing code; documents of real length; large bodies "
    "of code; images; work of many steps or many tool calls"
)
NOBODY = "no measured model is good enough; ask the user before trying an unmeasured one"


def _size(value) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def _short_kinds(kinds: dict) -> dict:
    short = {}

    for kind, figures in kinds.items():
        standing = figures["good_enough_for"]

        if figures.get("does_not_count"):
            standing = "does not count: " + figures["does_not_count"]

        short[kind] = f"{figures['passed']}/{figures['attempts']} {standing}"

    return short


def _short_entry(entry: dict, own_model: Optional[str] = None) -> dict:
    what = entry["what"]

    if own_model is not None:
        # Her own entries differ only in how thinking was set.
        what = what.split(", ")[-1] if ", thinking " in what else "as run"

    short = {"what": what}

    if entry.get("measured_on") and entry["kinds"]:
        short["measured_on"] = entry["measured_on"]

    if "switched_on" in entry:
        short["switched_on"] = entry["switched_on"]

    if entry["kinds"]:
        short["kinds"] = _short_kinds(entry["kinds"])

    if entry.get("note"):
        short["note"] = entry["note"]

    return short


def _destination(choice: Choice, record, model: str) -> str:
    if choice.entry is None:
        return NOBODY

    about = record.about(choice.entry)
    who = _whose(choice, record)

    if about is None:
        # A candidate with no figures at all.
        route, named, _ = json.loads(choice.entry)
        about = {"route": route, "model": named, "settings": {}}

    if about["route"] == LOCAL_ROUTE and about["model"] == model:
        think = about["settings"].get("think")
        who = "you" if think in (None, REPLY_THINKING) else f"you, thinking {think}"

    if choice.pass_rate is None:
        return f"{who} (unmeasured on it)"

    return f"{who} ({_percent(choice.pass_rate)})"


def _short_routing(record, model, local_models, open_routes, bars, kinds) -> dict:
    """Kinds that get the same advice share a line, and so do values."""
    lines: Dict[str, List[str]] = {}

    for kind in kinds:
        runs: List[list] = []

        for value in VALUES:
            where = _destination(
                advise(record, kind, value, local_models, open_routes, bars),
                record,
                model,
            )

            if runs and runs[-1][1] == where:
                runs[-1][0].append(value)
            else:
                runs.append([[value], where])

        if len(runs) == 1:
            line = "any value: " + runs[0][1]
        else:
            line = "; ".join(f"{', '.join(values)}: {where}" for values, where in runs)

        lines.setdefault(line, []).append(kind)

    return {", ".join(same): line for line, same in lines.items()}


def abilities_for_model(
    record: ModelRecord,
    model: str,
    local_models: Sequence[str],
    cloud_routes: Optional[Dict[str, bool]] = None,
    bars=None,
    context_tokens: Optional[int] = None,
    kinds: Sequence[str] = KINDS,
    suite: Optional[str] = None,
    identities: Optional[Dict[str, str]] = None,
    can_delegate: bool = False,
    limit: int = MODEL_ANSWER_BYTES,
) -> dict:
    """
    `describe_abilities` as the model is given it: the same figures
    and advice, short enough to pass the context's clip whole.
    """
    full = describe_abilities(
        record, model, local_models, cloud_routes, bars, context_tokens,
        kinds, suite, identities, can_delegate,
    )
    current = CurrentFigures(record, suite, identities)
    bars = validate_bars(DEFAULT_BARS if bars is None else bars)
    everyone = list(dict.fromkeys([model, *local_models]))
    open_routes = [route for route, on in (cloud_routes or {}).items() if on]

    you = {
        "model": model,
        "measured": [_short_entry(entry, model) for entry in full["you"]["measured"]],
    }

    for key in ("context_tokens", "note"):
        if key in full["you"]:
            you[key] = full["you"][key]

    answer = {
        "how_to_read": EVIDENCE_SHORT,
        "pass_rate_needed": ", ".join(
            f"{value} {share}" for value, share in full["pass_rate_needed"].items()
        ),
        "you": you,
        "other_models": [_short_entry(entry) for entry in full["other_models"]],
        "sending_work": full["sending_work"],
        "where_work_should_go": _short_routing(
            current, model, everyone, open_routes, bars, kinds,
        ),
        "never_measured_for_any_model": NOT_MEASURED_SHORT,
    }

    # Keep it under the limit, saying what was left out. The advice on
    # where work should go still names a model whose figures were.
    left_out = 0

    while _size(answer) > limit and answer["other_models"]:
        answer["other_models"].pop()
        left_out += 1
        answer["left_out"] = (
            f"The figures of {left_out} other model(s) did not fit here. "
            "Say so if asked about one of them."
        )

    while _size(answer) > limit and len(you["measured"]) > 1:
        you["measured"].pop()
        answer["left_out_of_you"] = (
            "Figures for some of your other thinking settings did not fit here."
        )

    while _size(answer) > limit and len(answer["where_work_should_go"]) > 1:
        answer["where_work_should_go"].popitem()
        answer["advice_left_out"] = (
            "Advice for some kinds of task did not fit here. Say so if asked about one."
        )

    return answer
