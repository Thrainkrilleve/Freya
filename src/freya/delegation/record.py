import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence



DATA_DIR = Path(__file__).resolve().parents[3] / "data"
MODEL_RECORD_PATH = DATA_DIR / "model_record.json"

VALUE_LOW = "low"
VALUE_NORMAL = "normal"
VALUE_HIGH = "high"

# The owner's starting bars (2026-10-09): the share of graded attempts a
# model must pass, for a kind of task, to be given work of each value.
DEFAULT_BARS = {VALUE_LOW: 0.70, VALUE_NORMAL: 0.85, VALUE_HIGH: 0.95}

# Fewer graded attempts than this and a model counts as unmeasured.
MINIMUM_ATTEMPTS = 15

MEASURED = "measured"
UNMEASURED = "unmeasured"


def validate_bars(bars) -> Dict[str, float]:
    """
    Bars as a complete, ordered set of shares between 0 and 1. Raises
    ValueError for anything else.
    """
    if not isinstance(bars, dict) or set(bars) != set(DEFAULT_BARS):
        raise ValueError("Bars are needed for low, normal and high.")

    checked = {}

    for value, bar in bars.items():
        if isinstance(bar, bool) or not isinstance(bar, (int, float)):
            raise ValueError("A bar is a number from 0 to 1.")
        if not 0 < bar <= 1:
            raise ValueError("A bar is a number above 0 and at most 1.")
        checked[value] = float(bar)

    if not (
        checked[VALUE_LOW] <= checked[VALUE_NORMAL] <= checked[VALUE_HIGH]
    ):
        raise ValueError("Low cannot ask more than normal, nor normal than high.")

    return checked


def entry_id(route, model, settings) -> str:
    """
    One measured thing: a model on a route with particular settings.
    The same model with different settings is a different entry.
    """
    # Written as JSON so that no choice of names or values can make
    # two different sets of settings look the same.
    return json.dumps(
        [route, model or "default", settings or {}],
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class KindResult:
    graded: int
    passed: int
    errors: int = 0
    median_seconds: Optional[float] = None
    mean_tokens: Optional[float] = None
    measured_at: str = ""
    # The set of tasks it was measured on, when the report said.
    suite: str = ""

    @property
    def attempts(self) -> int:
        return self.graded + self.errors

    @property
    def measured(self) -> bool:
        return self.attempts >= MINIMUM_ATTEMPTS

    @property
    def pass_rate(self) -> Optional[float]:
        """
        Passes out of every attempt. An attempt that got no reply at
        all (a timeout, a failed route) counts against the model: a
        worker that often fails to answer is not a reliable one.
        """
        return self.passed / self.attempts if self.attempts else None


class ModelRecord:
    """
    What has been measured about each model, by kind of task.

    Filled from evaluation reports. It holds counts and timings only;
    the tasks, prompts and replies stay in the reports. An unreadable
    file is an empty record, which makes every model unmeasured.
    """

    def __init__(self, path=MODEL_RECORD_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            self._entries = {
                key: entry
                for key, entry in data["entries"].items()
                if isinstance(entry, dict)
                and isinstance(entry.get("kinds"), dict)
            }

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"entries": self._entries}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def add_report(self, report) -> str:
        """
        Take in one evaluation report. Its figures replace what was
        recorded for the same entry and kinds, because an entry
        describes one exact model and a newer run is the newer truth.
        Returns the entry's id.
        """
        summary = report.get("summary")

        if not isinstance(summary, dict) or not summary:
            raise ValueError("The report has no results to record.")

        if report.get("mixed_models"):
            raise ValueError(
                "More than one model answered in this report, so it "
                "measures none of them."
            )

        rows = {}

        for kind, row in summary.items():
            try:
                graded = int(row["graded"])
                passed = int(row["passed"])
                errors = int(row.get("errors", 0))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"The results for {kind} are unreadable.") from exc

            if graded < 0 or errors < 0 or not 0 <= passed <= graded:
                raise ValueError(f"The results for {kind} are impossible.")

            rows[kind] = {
                "graded": graded,
                "passed": passed,
                "errors": errors,
                "median_seconds": row.get("median_seconds"),
                "mean_tokens": row.get("mean_tokens"),
                "measured_at": report.get("created_at", ""),
                "suite": report.get("suite", ""),
            }

        key = entry_id(
            report.get("route"), report.get("model"), report.get("settings"),
        )

        with self._lock:
            entry = self._entries.setdefault(
                key,
                {
                    "route": report.get("route"),
                    "model": report.get("model"),
                    "settings": report.get("settings") or {},
                    "kinds": {},
                },
            )
            # What actually answered, when the report says: a local
            # model's digest, or the model a cloud route served.
            identity = report.get("identity")
            if identity and entry.get("identity") not in (None, identity):
                # Another exact model now answers to this name; what
                # the old one scored says nothing about it.
                entry["kinds"] = {}
            if identity:
                entry["identity"] = identity
            entry["kinds"].update(rows)
            self._save()

        return key

    def refresh(self) -> None:
        """
        Read the file again, for figures an evaluation added while
        Freya was running. An unreadable file changes nothing.
        """
        with self._lock:
            self._load()

    def entries(self) -> List[str]:
        with self._lock:
            return sorted(self._entries)

    def about(self, entry: str) -> Optional[dict]:
        """
        Which route, model and settings an entry is, and the kinds of
        task it has figures for.
        """
        with self._lock:
            found = self._entries.get(entry)

            if found is None:
                return None

            settings = found.get("settings")

            return {
                "route": found.get("route"),
                "model": found.get("model"),
                "settings": dict(settings) if isinstance(settings, dict) else {},
                "identity": found.get("identity"),
                "kinds": sorted(found["kinds"]),
            }

    def result(self, entry: str, kind: str) -> Optional[KindResult]:
        with self._lock:
            row = self._entries.get(entry, {}).get("kinds", {}).get(kind)

        if not isinstance(row, dict):
            return None

        try:
            result = KindResult(
                graded=int(row["graded"]),
                passed=int(row["passed"]),
                errors=int(row.get("errors", 0)),
                median_seconds=row.get("median_seconds"),
                mean_tokens=row.get("mean_tokens"),
                measured_at=str(row.get("measured_at", "")),
                suite=str(row.get("suite") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

        if (
            result.graded < 0
            or result.errors < 0
            or not 0 <= result.passed <= result.graded
        ):
            # A damaged record measures nothing.
            return None

        return result

    def forget(self, entry: str) -> None:
        """Drop an entry, for a model version that is no longer in use."""
        with self._lock:
            if self._entries.pop(entry, None) is not None:
                self._save()


@dataclass(frozen=True)
class Candidate:
    """
    An entry the caller is allowed to use for this task. `tier` puts
    routes in the owner's order of preference (local, then plan, then
    paid); `cost` orders entries within a tier, lower being cheaper.
    """

    entry: str
    tier: int = 0
    cost: float = 0.0


@dataclass(frozen=True)
class Choice:
    entry: Optional[str]
    basis: Optional[str]
    reason: str
    pass_rate: Optional[float] = None


def choose(
    candidates: Sequence[Candidate],
    kind: str,
    value: str,
    record: ModelRecord,
    bars=None,
    unmeasured_allowed=None,
) -> Choice:
    """
    The least costly allowed entry that is measured good enough for
    this kind of task at this value.

    Only measured results count; nothing a model says about itself is
    looked at. If no measured entry reaches the bar, an unmeasured one
    may be used, least costly first, and the choice says so. If every
    candidate is measured and none reaches the bar, nothing is chosen.

    High-value work is not given to an unmeasured model unless the
    caller passes `unmeasured_allowed=True`, which stands for the
    owner having said yes to that for this task.
    """
    bars = validate_bars(DEFAULT_BARS if bars is None else bars)

    if value not in bars:
        raise ValueError(f"Unknown task value: {value!r}")

    bar = bars[value]
    # Equal candidates keep the caller's order, which puts the model
    # already loaded first: an arbitrary tie-break would advise loading
    # another model for no measured reason.
    ordered = sorted(candidates, key=lambda c: (c.tier, c.cost))
    qualified, unmeasured = [], []

    for candidate in ordered:
        result = record.result(candidate.entry, kind)

        if result is None or not result.measured:
            unmeasured.append(candidate)
        elif result.pass_rate >= bar:
            qualified.append((candidate, result.pass_rate))

    if qualified:
        best = qualified[0][0]
        # Among equally preferred, equally costly entries, the better one.
        same = [
            (candidate, rate)
            for candidate, rate in qualified
            if (candidate.tier, candidate.cost) == (best.tier, best.cost)
        ]
        candidate, rate = max(same, key=lambda pair: pair[1])
        return Choice(
            candidate.entry,
            MEASURED,
            f"Measured at {rate:.0%} on {kind}, against a bar of {bar:.0%}.",
            rate,
        )

    if unmeasured and not (
        value != VALUE_HIGH if unmeasured_allowed is None else unmeasured_allowed
    ):
        return Choice(
            None,
            None,
            f"No measured model reaches {bar:.0%} on {kind}. An unmeasured "
            "one could be tried, with your permission.",
        )

    if unmeasured:
        return Choice(
            unmeasured[0].entry,
            UNMEASURED,
            f"No measured model reaches {bar:.0%} on {kind}; "
            "this one has not been measured on it.",
        )

    if not ordered:
        return Choice(None, None, "No model is allowed for this task.")

    return Choice(
        None,
        None,
        f"No allowed model reaches {bar:.0%} on {kind}.",
    )
