import json
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

from freya.delegation.record import DEFAULT_BARS, validate_bars


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
CLOUD_SETTINGS_PATH = DATA_DIR / "cloud_settings.json"

# The kinds of material a brief for a cloud model can be made from.
KIND_REQUEST = "request"
KIND_CONVERSATION = "conversation"
KIND_MEMORY = "memory"
KIND_PAST_CONVERSATION = "past-conversation"
KIND_TOOL_RESULT = "tool-result"
KIND_FILE = "file"
KIND_CODE = "code"

# What may happen to each kind.
ALLOWED = "allowed"
ASK = "ask"
NEVER = "never"
SETTINGS = (ALLOWED, ASK, NEVER)

# The owner's starting choices (2026-10-09): strict, loosened by hand.
DEFAULT_MATERIAL = {
    KIND_REQUEST: ALLOWED,
    KIND_CONVERSATION: ASK,
    KIND_MEMORY: NEVER,
    KIND_PAST_CONVERSATION: NEVER,
    KIND_TOOL_RESULT: ASK,
    KIND_FILE: ASK,
    KIND_CODE: ASK,
}
KINDS = tuple(DEFAULT_MATERIAL)

KIND_NAMES = {
    KIND_REQUEST: "the request",
    KIND_CONVERSATION: "this conversation",
    KIND_MEMORY: "stored memories",
    KIND_PAST_CONVERSATION: "past conversations",
    KIND_TOOL_RESULT: "tool results from this PC",
    KIND_FILE: "files and media",
    KIND_CODE: "Freya's own code",
}

# The kind each context source belongs to. A source missing from this
# table has no kind, and material with no kind never leaves.
SOURCE_KINDS = {
    "saved-memory": KIND_MEMORY,
    "conversation-summary": KIND_PAST_CONVERSATION,
    "conversation-passage": KIND_PAST_CONVERSATION,
    "past-recall": KIND_PAST_CONVERSATION,
    "media": KIND_FILE,
}


def kind_of_source(source: str) -> Optional[str]:
    return SOURCE_KINDS.get(source)


# A standing rule is the owner's "allow permanently": for one provider
# and one kind of task, these kinds of ask-first material go without
# asking. It never reaches a kind set to never, and it is made only by
# the owner's answer to a prompt that showed exactly what it covers.
MAX_RULES = 50
MAX_RULE_WORD = 40


def _clean_rule(entry) -> Optional[dict]:
    """A stored rule if every field is what a rule may hold, else None."""
    if not isinstance(entry, dict):
        return None

    words = [entry.get("id"), entry.get("provider"), entry.get("task")]
    material = entry.get("material")

    if not all(
        isinstance(word, str) and word.strip() and len(word) <= MAX_RULE_WORD
        for word in words
    ):
        return None
    if not isinstance(material, list) or not material:
        return None
    if not all(kind in KINDS for kind in material):
        return None

    made = entry.get("made")

    return {
        "id": words[0],
        "provider": words[1],
        "task": words[2],
        "material": sorted(set(material)),
        "made": made if isinstance(made, str) else "",
    }


class CloudSettings:
    """
    The owner's choices about delegating work: which cloud providers
    may be used at all, what each kind of material may do, and how
    well a model must have measured to be given work of each value.
    Kept in a JSON file next to the databases.

    A missing or unreadable file, an unknown provider, and any value
    that is not understood all mean the default: every provider off,
    each kind at its starting setting, and the starting bars.
    """

    def __init__(self, path=CLOUD_SETTINGS_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._material = dict(DEFAULT_MATERIAL)
        self._providers: Dict[str, bool] = {}
        self._bars = dict(DEFAULT_BARS)
        self._rules: List[dict] = []
        # Goes up with every change, so that a check made under one
        # set of choices cannot be used after they have changed.
        self._revision = 0
        self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        if not isinstance(data, dict):
            return

        material = data.get("material")

        if isinstance(material, dict):
            for kind in KINDS:
                if material.get(kind) in SETTINGS:
                    self._material[kind] = material[kind]

        try:
            self._bars = validate_bars(data.get("bars"))
        except ValueError:
            pass

        revision = data.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            self._revision = max(0, revision)

        providers = data.get("providers")

        if isinstance(providers, dict):
            self._providers = {
                name: True
                for name, enabled in providers.items()
                if isinstance(name, str) and enabled is True
            }

        rules = data.get("rules")

        if isinstance(rules, list):
            # A rule that is not understood is dropped, never guessed at.
            kept = [_clean_rule(entry) for entry in rules[:MAX_RULES]]
            self._rules = [rule for rule in kept if rule is not None]

    def _held(self):
        """The choices as they stand, to go back to if saving fails."""
        return (
            dict(self._material), dict(self._providers),
            dict(self._bars), list(self._rules),
        )

    def _save_or_undo(self, before):
        """
        Save, and if that fails put the choices back as they were. A
        change the owner was told failed must not be in force anyway:
        a rule or a switch that was never written would otherwise let
        work leave until Freya closed.
        """
        try:
            self._save()
        except Exception:
            self._material, self._providers, self._bars, self._rules = before
            raise

    def _save(self):
        self._revision += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "bars": self._bars,
                    "revision": self._revision,
                    "material": self._material,
                    "providers": self._providers,
                    "rules": self._rules,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @property
    def material(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._material)

    def setting_for(self, kind: Optional[str]) -> str:
        with self._lock:
            return self._material.get(kind, NEVER)

    def set_material(self, kind: str, setting: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"Unknown kind of material: {kind!r}")
        if setting not in SETTINGS:
            raise ValueError(f"Unknown setting: {setting!r}")

        with self._lock:
            if self._material[kind] != setting:
                before = self._held()
                self._material[kind] = setting
                self._save_or_undo(before)

    @property
    def rules(self) -> List[dict]:
        with self._lock:
            return [dict(rule, material=list(rule["material"])) for rule in self._rules]

    def add_rule(self, provider: str, task: str, material) -> dict:
        """
        Record the owner's "allow permanently" for one provider, one
        kind of task and these kinds of material. Only kinds set to
        ask first can be in a rule: an allowed kind needs none, and
        no rule may hold a kind set to never.
        """
        for word in (provider, task):
            if not isinstance(word, str) or not word.strip() or len(word) > MAX_RULE_WORD:
                raise ValueError("A rule needs a provider and a kind of task.")

        if isinstance(material, str) or not material:
            raise ValueError("A rule needs the kinds of material it covers.")

        material = sorted(set(material))

        with self._lock:
            for kind in material:
                if kind not in KINDS:
                    raise ValueError(f"Unknown kind of material: {kind!r}")
                if self._material[kind] != ASK:
                    raise ValueError(
                        "A rule can only cover material set to ask first."
                    )

            for rule in self._rules:
                if (rule["provider"], rule["task"], rule["material"]) == (
                    provider, task, material,
                ):
                    return dict(rule)

            if len(self._rules) >= MAX_RULES:
                raise ValueError("There are too many rules; remove one first.")

            rule = {
                "id": secrets.token_hex(4),
                "provider": provider,
                "task": task,
                "material": material,
                "made": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            before = self._held()
            self._rules.append(rule)
            self._save_or_undo(before)

            return dict(rule)

    def remove_rule(self, rule_id: str) -> None:
        with self._lock:
            kept = [rule for rule in self._rules if rule["id"] != rule_id]

            if len(kept) == len(self._rules):
                raise ValueError("That rule could not be found.")

            before = self._held()
            self._rules = kept
            self._save_or_undo(before)

    def rule_kinds(self, provider: str, task: str) -> FrozenSet[str]:
        """
        The kinds of material the owner's rules let a task of this
        kind send to this provider without asking. A kind whose
        setting is no longer ask first is left out: the setting
        decides, and a rule never brings back a kind set to never.
        """
        with self._lock:
            return frozenset(
                kind
                for rule in self._rules
                if rule["provider"] == provider and rule["task"] == task
                for kind in rule["material"]
                if self._material.get(kind) == ASK
            )

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def bars(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._bars)

    def set_bars(self, bars) -> Dict[str, float]:
        """
        Change the pass rates a model needs for low, normal and high
        value work. Raises ValueError unless all three are given, lie
        above 0 and at most 1, and do not fall as value rises.
        """
        bars = validate_bars(bars)

        with self._lock:
            if bars != self._bars:
                before = self._held()
                self._bars = bars
                self._save_or_undo(before)

        return dict(bars)

    def provider_enabled(self, provider: str) -> bool:
        with self._lock:
            return self._providers.get(provider, False)

    def enabled_providers(self) -> List[str]:
        with self._lock:
            return sorted(self._providers)

    def set_provider(self, provider: str, enabled: bool) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("A provider needs a name.")
        if not isinstance(enabled, bool):
            raise ValueError("A provider is either on or off.")

        with self._lock:
            if self._providers.get(provider, False) == enabled:
                return
            before = self._held()
            if enabled:
                self._providers[provider] = True
            else:
                del self._providers[provider]
            self._save_or_undo(before)


@dataclass(frozen=True)
class BriefPart:
    """
    One piece of a brief, labelled with the kind of material it is.
    """

    kind: Optional[str]
    text: str
    source_id: str = ""


@dataclass(frozen=True)
class BriefReview:
    """
    What the owner's settings make of a brief bound for one provider.

    `send` holds the only parts that may be put in the request.
    `ask` lists the kinds that still need the owner's yes; while it is
    not empty, or the provider is off, nothing is ready to go.
    `withheld` holds the parts that were left out and can never go.
    """

    provider: str
    provider_enabled: bool
    send: Tuple[BriefPart, ...]
    ask: Tuple[str, ...]
    waiting: Tuple[BriefPart, ...]
    withheld: Tuple[BriefPart, ...]

    @property
    def ready(self) -> bool:
        return self.provider_enabled and not self.ask

    def describe(self) -> List[str]:
        """
        A description for the owner of what this provider would
        receive: kinds and amounts, never the text itself.
        """
        if not self.provider_enabled:
            return [f"{self.provider} is turned off. Nothing would be sent."]

        lines = [f"To {self.provider}:"]
        lines.extend(
            f"  {_amount(kind, parts)}"
            for kind, parts in _by_kind(self.send)
        )
        lines.extend(
            f"  {_amount(kind, parts)} (needs your permission)"
            for kind, parts in _by_kind(self.waiting)
        )

        if len(lines) == 1:
            lines.append("  nothing")

        if self.withheld:
            lines.append(
                "Left out, because it is set never to leave: "
                + ", ".join(
                    _amount(kind, parts)
                    for kind, parts in _by_kind(self.withheld)
                )
                + "."
            )

        return lines


def review_brief(
    parts,
    provider: str,
    settings: CloudSettings,
    approved_kinds: FrozenSet[str] = frozenset(),
) -> BriefReview:
    """
    Sort a brief's parts by what the owner's settings allow.

    `approved_kinds` are the ask-first kinds the owner has said yes to
    for this brief. An approval cannot release a kind set to never, or
    a part whose kind is not known.
    """
    send, waiting, withheld = [], [], []

    for part in parts:
        setting = (
            settings.setting_for(part.kind)
            if part.kind in KINDS
            else NEVER
        )

        if setting == ALLOWED or (
            setting == ASK and part.kind in approved_kinds
        ):
            send.append(part)
        elif setting == ASK:
            waiting.append(part)
        else:
            withheld.append(part)

    enabled = settings.provider_enabled(provider)

    return BriefReview(
        provider=provider,
        provider_enabled=enabled,
        send=tuple(send) if enabled else (),
        ask=tuple(kind for kind, _ in _by_kind(waiting)),
        waiting=tuple(waiting),
        withheld=tuple(withheld),
    )


def _by_kind(parts):
    """
    Parts grouped by kind, in the fixed order of KINDS, with parts of
    no known kind last.
    """
    groups = []

    for kind in KINDS + (None,):
        matching = [
            part for part in parts
            if (part.kind if part.kind in KINDS else None) == kind
        ]
        if matching:
            groups.append((kind, matching))

    return groups


def _amount(kind, parts):
    words = sum(len(part.text.split()) for part in parts)
    name = KIND_NAMES.get(kind, "unlabelled material")
    pieces = "1 piece" if len(parts) == 1 else f"{len(parts)} pieces"
    return f"{name}: {pieces}, about {words} words"
