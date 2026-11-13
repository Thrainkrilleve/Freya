import json

import pytest

from freya.delegation.material import ASK, NEVER
from freya.delegation.tighten import (
    CLOUD_SETTINGS_TIGHTEN,
    TIGHTEN_SEED,
    find_kind,
    find_provider,
)

from test_delegation_turn import VENDOR, Rig


@pytest.fixture
def rig(tmp_path, fake_ollama):
    made = Rig(tmp_path)
    made.fake = fake_ollama
    made.cloud.settings.set_provider(VENDOR, True)
    made.cloud.unlock(VENDOR)
    yield made
    made.close()


def ask(rig, change, name):
    """Call the tool the way the model does."""
    return json.loads(rig.plane.model_tools.run(
        "cloud_settings_tighten", {"change": change, "target": name},
    ))


def offered(rig):
    return [
        tool["function"]["name"]
        for tool in rig.plane.model_tools.definitions()
    ]


def test_switching_a_provider_off_takes_the_owners_click(rig):
    rig.answers = ["change"]

    answer = ask(rig, "provider-off", "Claude")

    assert answer == {"changed": True, "now": "Claude (Anthropic) is now off."}
    assert not rig.cloud.settings.provider_enabled(VENDOR)
    assert rig.kinds_asked() == ["setting"]
    assert rig.asked[0]["choices"][0]["label"] == "Switch Claude (Anthropic) off"
    # Off through the same path as the tab: the route's rule goes too.
    assert not rig.plane.policy.decide(
        "local-owner", "cloud.anthropic_plan.ask",
    ).allow
    assert "cloud.settings.change" in rig.audit_text()


@pytest.mark.parametrize("answer", ["cancel", None, "yes", "send"])
def test_without_the_click_nothing_changes(rig, answer):
    rig.answers = [answer]

    result = ask(rig, "provider-off", "anthropic")

    assert "did not confirm" in result["error"]
    assert rig.cloud.settings.provider_enabled(VENDOR)


def test_material_can_be_set_to_never_and_no_looser(rig):
    rig.answers = ["change"]

    answer = ask(rig, "material-never", "this conversation")

    assert answer["changed"] is True
    assert rig.cloud.settings.setting_for("conversation") == NEVER
    assert rig.asked[0]["choices"][0]["label"] == "Never send this conversation"

    # Asked again, there is nothing to do and nothing to ask.
    again = ask(rig, "material-never", "conversation")

    assert again["changed"] is False
    assert len(rig.asked) == 1


@pytest.mark.parametrize("change, name", [
    ("provider-on", "anthropic"),
    ("material-allowed", "memory"),
    ("material-ask", "memory"),
    ("limit", "anthropic"),
    ("rule", "anthropic"),
    ("", "anthropic"),
])
def test_nothing_that_loosens_can_be_asked_for(rig, change, name):
    rig.cloud.settings.set_provider(VENDOR, False)
    before = (rig.cloud.settings.material, rig.cloud.settings.enabled_providers())

    result = ask(rig, change, name)

    assert "error" in result
    assert rig.asked == []
    assert (rig.cloud.settings.material, rig.cloud.settings.enabled_providers()) == before


@pytest.mark.parametrize("change, name", [
    ("provider-off", "a provider nobody made"),
    ("material-never", "everything"),
])
def test_a_name_that_means_nothing_is_refused_without_asking(rig, change, name):
    assert "error" in ask(rig, change, name)
    assert rig.asked == []


def test_a_provider_that_is_already_off_needs_no_question(rig):
    rig.cloud.settings.set_provider(VENDOR, False)

    assert ask(rig, "provider-off", "claude")["changed"] is False
    assert rig.asked == []


def test_with_no_window_it_is_not_offered_and_changes_nothing(rig):
    assert "cloud_settings_tighten" in offered(rig)

    rig.window = False

    assert "cloud_settings_tighten" not in offered(rig)
    assert "error" in ask(rig, "provider-off", "claude")
    assert rig.cloud.settings.provider_enabled(VENDOR)


def test_a_change_policy_forbids_is_reported_as_not_made(rig):
    rig.plane.policy.add_rule(
        "local-owner", "cloud.settings.change", "deny", "test",
    )
    rig.answers = ["change"]

    assert "could not be made" in ask(rig, "provider-off", "claude")["error"]
    assert rig.cloud.settings.provider_enabled(VENDOR)


def test_it_is_its_own_capability_with_its_own_seed(rig):
    capability = rig.plane.capabilities.get(CLOUD_SETTINGS_TIGHTEN)

    assert not capability.destructive
    assert TIGHTEN_SEED == "alpha-cloud-tighten"
    assert rig.plane.policy.decide("local-owner", CLOUD_SETTINGS_TIGHTEN).allow

    for rule in rig.plane.policy.list_rules():
        if rule["capability"] == CLOUD_SETTINGS_TIGHTEN:
            rig.plane.policy.remove_rule(rule["rule_id"])

    rig.answers = ["change"]

    assert "error" in ask(rig, "provider-off", "claude")
    assert rig.asked == []
    assert rig.cloud.settings.provider_enabled(VENDOR)


@pytest.mark.parametrize("name, provider", [
    ("anthropic", "anthropic"), ("Claude", "anthropic"),
    ("Claude (Anthropic)", "anthropic"), ("ChatGPT", "openai"),
    ("OpenAI", "openai"), ("", None), ("every provider", None), (None, None),
])
def test_a_provider_is_found_by_its_id_or_a_word_of_its_label(name, provider):
    assert find_provider(name) == provider


@pytest.mark.parametrize("name, kind", [
    ("memory", "memory"), ("stored memories", "memory"),
    ("past conversations", "past-conversation"), ("tool_result", "tool-result"),
    ("Freya's own code", "code"), ("", None), ("all of it", None),
])
def test_a_kind_of_material_is_found_by_its_id_or_label(name, kind):
    assert find_kind(name) == kind
    assert ASK  # the settings a kind may hold are not changed by looking
