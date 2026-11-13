import json

import pytest

from freya.delegation import (
    ALLOWED,
    ASK,
    DEFAULT_MATERIAL,
    KIND_CODE,
    KIND_CONVERSATION,
    KIND_FILE,
    KIND_MEMORY,
    KIND_PAST_CONVERSATION,
    KIND_REQUEST,
    KIND_TOOL_RESULT,
    NEVER,
    BriefPart,
    CloudSettings,
    kind_of_source,
    review_brief,
)


VENDOR = "anthropic"


@pytest.fixture
def settings(tmp_path):
    return CloudSettings(tmp_path / "cloud_settings.json")


@pytest.fixture
def enabled(settings):
    settings.set_provider(VENDOR, True)
    return settings


def brief():
    return [
        BriefPart(KIND_REQUEST, "Review this function for mistakes."),
        BriefPart(KIND_CODE, "def add(a, b): return a - b", "file:add.py"),
        BriefPart(KIND_CONVERSATION, "We were talking about the adder."),
        BriefPart(KIND_MEMORY, "A made-up fact about a made-up person.", "memory:1"),
        BriefPart(KIND_PAST_CONVERSATION, "A made-up earlier talk.", "conversation:1"),
    ]


def test_the_starting_settings_are_strict(settings):
    assert settings.material == DEFAULT_MATERIAL
    assert settings.setting_for(KIND_REQUEST) == ALLOWED
    assert settings.setting_for(KIND_MEMORY) == NEVER
    assert settings.setting_for(KIND_PAST_CONVERSATION) == NEVER
    assert {
        settings.setting_for(kind)
        for kind in (KIND_CONVERSATION, KIND_TOOL_RESULT, KIND_FILE, KIND_CODE)
    } == {ASK}
    assert settings.enabled_providers() == []
    assert not settings.provider_enabled(VENDOR)


def test_choices_are_remembered(tmp_path):
    path = tmp_path / "cloud_settings.json"
    first = CloudSettings(path)
    first.set_provider(VENDOR, True)
    first.set_provider("openai", True)
    first.set_provider("openai", False)
    first.set_material(KIND_CODE, ALLOWED)

    second = CloudSettings(path)

    assert second.enabled_providers() == [VENDOR]
    assert second.setting_for(KIND_CODE) == ALLOWED
    assert second.setting_for(KIND_MEMORY) == NEVER


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps({"material": "everything", "providers": ["anthropic"]}),
        json.dumps({
            "material": {KIND_MEMORY: "yes", "new-kind": ALLOWED},
            "providers": {VENDOR: "true", "openai": 1},
        }),
    ],
)
def test_a_damaged_file_means_the_strict_defaults(tmp_path, content):
    path = tmp_path / "cloud_settings.json"
    path.write_text(content, encoding="utf-8")

    settings = CloudSettings(path)

    assert settings.material == DEFAULT_MATERIAL
    assert settings.enabled_providers() == []


def test_the_bars_start_at_the_owners_figures_and_can_be_moved(tmp_path):
    path = tmp_path / "cloud_settings.json"
    first = CloudSettings(path)

    assert first.bars == {"low": 0.70, "normal": 0.85, "high": 0.95}

    first.set_bars({"low": 0.6, "normal": 0.8, "high": 0.9})

    assert CloudSettings(path).bars == {"low": 0.6, "normal": 0.8, "high": 0.9}
    with pytest.raises(ValueError):
        first.set_bars({"low": 0.9, "normal": 0.8, "high": 0.95})
    assert first.bars["low"] == 0.6


def test_damaged_bars_mean_the_starting_bars(tmp_path):
    path = tmp_path / "cloud_settings.json"
    path.write_text(
        json.dumps({"bars": {"low": 0.1, "normal": 5, "high": 0.2}}),
        encoding="utf-8",
    )

    assert CloudSettings(path).bars == {"low": 0.70, "normal": 0.85, "high": 0.95}


def test_bad_choices_are_refused(settings):
    with pytest.raises(ValueError):
        settings.set_material("everything", ALLOWED)
    with pytest.raises(ValueError):
        settings.set_material(KIND_MEMORY, "sometimes")
    with pytest.raises(ValueError):
        settings.set_provider("", True)
    with pytest.raises(ValueError):
        settings.set_provider(VENDOR, "yes")


def test_nothing_goes_to_a_provider_that_is_off(settings):
    review = review_brief(brief(), VENDOR, settings, frozenset({KIND_CODE}))

    assert review.send == ()
    assert not review.ready
    assert review.describe() == [
        "anthropic is turned off. Nothing would be sent."
    ]


def test_a_brief_is_sorted_by_the_settings(enabled):
    review = review_brief(brief(), VENDOR, enabled)

    assert [part.kind for part in review.send] == [KIND_REQUEST]
    assert review.ask == (KIND_CONVERSATION, KIND_CODE)
    assert [part.kind for part in review.withheld] == [
        KIND_MEMORY,
        KIND_PAST_CONVERSATION,
    ]
    assert not review.ready


def test_approval_releases_only_what_was_approved(enabled):
    review = review_brief(brief(), VENDOR, enabled, frozenset({KIND_CODE}))

    assert [part.kind for part in review.send] == [KIND_REQUEST, KIND_CODE]
    assert review.ask == (KIND_CONVERSATION,)
    assert not review.ready


def test_approval_cannot_release_what_is_set_never(enabled):
    review = review_brief(
        brief(),
        VENDOR,
        enabled,
        frozenset({KIND_CODE, KIND_CONVERSATION, KIND_MEMORY, KIND_PAST_CONVERSATION}),
    )

    assert review.ready
    assert KIND_MEMORY not in {part.kind for part in review.send}
    assert KIND_PAST_CONVERSATION not in {part.kind for part in review.send}
    assert len(review.withheld) == 2


def test_material_of_no_known_kind_never_leaves(enabled):
    parts = [
        BriefPart(None, "unlabelled"),
        BriefPart("something-new", "also unlabelled"),
    ]

    review = review_brief(
        parts, VENDOR, enabled, frozenset({None, "something-new"}),
    )

    assert review.send == ()
    assert len(review.withheld) == 2


def test_only_the_owner_changing_a_setting_releases_memory(enabled):
    enabled.set_material(KIND_MEMORY, ASK)

    asked = review_brief(brief(), VENDOR, enabled)
    approved = review_brief(brief(), VENDOR, enabled, frozenset({KIND_MEMORY}))

    assert KIND_MEMORY in asked.ask
    assert KIND_MEMORY in {part.kind for part in approved.send}


def test_the_description_gives_amounts_and_never_the_text(enabled):
    review = review_brief(brief(), VENDOR, enabled, frozenset({KIND_CODE}))

    lines = review.describe()
    text = "\n".join(lines)

    assert lines[0] == "To anthropic:"
    assert "  the request: 1 piece, about 5 words" in lines
    assert "  Freya's own code: 1 piece, about 7 words" in lines
    assert (
        "  this conversation: 1 piece, about 6 words (needs your permission)"
        in lines
    )
    assert "stored memories: 1 piece" in lines[-1]
    assert "past conversations: 1 piece" in lines[-1]
    for part in brief():
        assert part.text not in text


def test_context_sources_map_to_kinds_and_unknown_ones_to_none():
    assert kind_of_source("saved-memory") == KIND_MEMORY
    assert kind_of_source("conversation-summary") == KIND_PAST_CONVERSATION
    assert kind_of_source("conversation-passage") == KIND_PAST_CONVERSATION
    assert kind_of_source("media") == KIND_FILE
    assert kind_of_source("a-source-added-later") is None


# Standing rules: the owner's "allow permanently"


def test_a_rule_covers_one_provider_one_kind_of_task_and_named_material(settings):
    rule = settings.add_rule(VENDOR, "reasoning", [KIND_CONVERSATION])

    assert settings.rule_kinds(VENDOR, "reasoning") == {KIND_CONVERSATION}
    assert settings.rule_kinds(VENDOR, "planning") == frozenset()
    assert settings.rule_kinds("another-provider", "reasoning") == frozenset()
    assert settings.rules == [rule]
    assert rule["material"] == [KIND_CONVERSATION] and rule["id"]


def test_rules_are_kept_and_making_one_changes_the_revision(settings):
    before = settings.revision
    settings.add_rule(VENDOR, "reasoning", [KIND_CODE, KIND_CONVERSATION])

    again = CloudSettings(settings.path)

    assert again.rules == settings.rules
    assert again.revision > before

    # The same rule a second time is not a second rule or a change.
    settings.add_rule(VENDOR, "reasoning", [KIND_CONVERSATION, KIND_CODE])

    assert len(settings.rules) == 1
    assert settings.revision == again.revision


@pytest.mark.parametrize("kind", [KIND_MEMORY, KIND_PAST_CONVERSATION, KIND_REQUEST])
def test_a_rule_can_only_hold_material_set_to_ask_first(settings, kind):
    with pytest.raises(ValueError):
        settings.add_rule(VENDOR, "reasoning", [KIND_CONVERSATION, kind])

    assert settings.rules == []


@pytest.mark.parametrize("provider, task, material", [
    ("", "reasoning", [KIND_CONVERSATION]),
    (VENDOR, "", [KIND_CONVERSATION]),
    (VENDOR, "x" * 41, [KIND_CONVERSATION]),
    (VENDOR, "reasoning", []),
    (VENDOR, "reasoning", KIND_CONVERSATION),
    (VENDOR, "reasoning", ["no-such-kind"]),
    (None, "reasoning", [KIND_CONVERSATION]),
])
def test_a_rule_that_is_not_one_is_refused(settings, provider, task, material):
    with pytest.raises(ValueError):
        settings.add_rule(provider, task, material)


def test_a_rule_never_brings_back_material_later_set_to_never(enabled):
    enabled.add_rule(VENDOR, "reasoning", [KIND_CONVERSATION, KIND_CODE])
    enabled.set_material(KIND_CONVERSATION, NEVER)

    covered = enabled.rule_kinds(VENDOR, "reasoning")
    review = review_brief(brief(), VENDOR, enabled, covered)

    assert covered == {KIND_CODE}
    assert KIND_CONVERSATION not in [part.kind for part in review.send]
    assert KIND_CODE in [part.kind for part in review.send]


def test_a_removed_rule_covers_nothing(settings):
    rule = settings.add_rule(VENDOR, "reasoning", [KIND_CONVERSATION])
    settings.remove_rule(rule["id"])

    assert settings.rules == []
    assert settings.rule_kinds(VENDOR, "reasoning") == frozenset()

    with pytest.raises(ValueError):
        settings.remove_rule(rule["id"])


def test_stored_rules_that_are_not_understood_are_dropped(settings):
    good = settings.add_rule(VENDOR, "reasoning", [KIND_CONVERSATION])
    data = json.loads(settings.path.read_text("utf-8"))
    data["rules"] += [
        {"id": "a", "provider": VENDOR, "task": "reasoning", "material": [KIND_MEMORY, "everything"]},
        {"id": "b", "provider": VENDOR, "task": "reasoning", "material": "conversation"},
        {"id": "c", "provider": VENDOR, "material": [KIND_CODE]},
        "allow everything",
    ]
    settings.path.write_text(json.dumps(data), "utf-8")

    assert CloudSettings(settings.path).rules == [good]

    data["rules"] = "all of them"
    settings.path.write_text(json.dumps(data), "utf-8")

    assert CloudSettings(settings.path).rules == []


def test_a_change_that_could_not_be_saved_is_not_in_force(enabled, monkeypatch):
    kept = enabled.add_rule(VENDOR, "planning", [KIND_CODE])

    def broken(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr("freya.delegation.material.os.replace", broken)

    for change in (
        lambda: enabled.add_rule(VENDOR, "reasoning", [KIND_CONVERSATION]),
        lambda: enabled.remove_rule(kept["id"]),
        lambda: enabled.set_material(KIND_CONVERSATION, ALLOWED),
        lambda: enabled.set_provider("another-provider", True),
        lambda: enabled.set_provider(VENDOR, False),
    ):
        with pytest.raises(OSError):
            change()

    assert enabled.rules == [kept]
    assert enabled.rule_kinds(VENDOR, "reasoning") == frozenset()
    assert enabled.setting_for(KIND_CONVERSATION) == ASK
    assert enabled.enabled_providers() == [VENDOR]
