from freya.storage.sqlite import IntegrityError as StoreIntegrityError

import pytest

from freya.core.persistence import ControlStore, control_store
from freya.core.persistence.control_store import MIGRATIONS
from freya.core.policy import SqlitePolicyEngine, seed_alpha_policy


@pytest.fixture
def store(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    yield store
    store.close()


@pytest.fixture
def policy(store):
    return SqlitePolicyEngine(store)


def test_store_creates_schema_and_reopens(tmp_path):
    path = tmp_path / "nested" / "control.db"

    first = ControlStore(path)
    version = first.schema_version
    SqlitePolicyEngine(first).add_rule("owner", "system.info", "allow", "test")
    first.close()

    second = ControlStore(path)

    assert version == len(MIGRATIONS)
    assert second.schema_version == len(MIGRATIONS)
    assert len(SqlitePolicyEngine(second).list_rules()) == 1
    second.close()


def make_version_1_store(path, monkeypatch, seeded):
    """Create a control database as it was before the seed table existed."""
    monkeypatch.setattr(control_store, "MIGRATIONS", MIGRATIONS[:1])
    store = ControlStore(path)
    if seeded:
        engine = SqlitePolicyEngine(store)
        for name in ("system.info", "system.disk", "system.gpu"):
            engine.add_rule("local-owner", name, "allow", "v1 seed")
    store.close()
    monkeypatch.undo()


def test_upgrade_keeps_removed_default_rules_removed(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    make_version_1_store(path, monkeypatch, seeded=True)

    store = ControlStore(path)
    policy = SqlitePolicyEngine(store)
    gpu_rule = next(
        r for r in policy.list_rules() if r["capability"] == "system.gpu"
    )
    policy.remove_rule(gpu_rule["rule_id"])

    system_seed = seed_alpha_policy(
        policy, "alpha-system-tools", "local-owner", ["system.gpu"]
    )
    model_seed = seed_alpha_policy(
        policy, "alpha-model-runtime", "local-owner", ["llm.chat"]
    )

    assert store.schema_version == len(MIGRATIONS)
    assert system_seed is False
    assert model_seed is True
    assert policy.decide("local-owner", "system.gpu").allow is False
    assert policy.decide("local-owner", "system.info").allow is True
    assert policy.decide("local-owner", "llm.chat").allow is True
    store.close()


def test_upgrade_of_unseeded_store_still_gets_defaults(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    make_version_1_store(path, monkeypatch, seeded=False)

    store = ControlStore(path)
    policy = SqlitePolicyEngine(store)

    assert seed_alpha_policy(
        policy, "alpha-system-tools", "local-owner", ["system.info"]
    ) is True
    assert policy.decide("local-owner", "system.info").allow is True
    store.close()


def test_store_refuses_newer_schema(tmp_path):
    path = tmp_path / "control.db"
    store = ControlStore(path)
    store.execute("INSERT INTO schema_version (version) VALUES (99)")
    store.close()

    with pytest.raises(RuntimeError):
        ControlStore(path)


def test_failed_transaction_rolls_back(store, policy):
    with pytest.raises(RuntimeError):
        with store.transaction() as connection:
            connection.execute(
                "UPDATE policy_state SET revision = 50 WHERE id = 1"
            )
            raise RuntimeError("boom")

    assert policy.state() == (0, 1)


def test_denies_by_default(policy):
    decision = policy.decide("local-owner", "system.info")

    assert decision.allow is False
    assert decision.reason == "No policy rule allows this action."
    assert (decision.revision, decision.grant_epoch) == (0, 1)


def test_allow_rule_allows_only_its_subject_and_capability(policy):
    policy.add_rule("local-owner", "system.info", "allow", "Owner may read.")

    allowed = policy.decide(" Local-Owner ", " SYSTEM.INFO ")

    assert allowed.allow is True
    assert allowed.reason == "Owner may read."
    assert allowed.revision == 1
    assert policy.decide("local-owner", "system.disk").allow is False
    assert policy.decide("guest", "system.info").allow is False


def test_wildcard_rules(policy):
    policy.add_rule("local-owner", "*", "allow", "Owner may do anything.")
    policy.add_rule("*", "system.info", "allow", "Anyone may read info.")

    assert policy.decide("local-owner", "files.delete").allow is True
    assert policy.decide("guest", "system.info").allow is True
    assert policy.decide("guest", "files.delete").allow is False


def test_deny_overrides_allow(policy):
    policy.add_rule("local-owner", "*", "allow", "Owner may do anything.")
    policy.add_rule("*", "files.delete", "deny", "Deletion is disabled.")

    decision = policy.decide("local-owner", "files.delete")

    assert decision.allow is False
    assert decision.reason == "Deletion is disabled."
    assert policy.decide("local-owner", "system.info").allow is True


def test_rule_changes_bump_revision_and_removal_bumps_grant_epoch(policy):
    rule_id = policy.add_rule("local-owner", "system.info", "allow", "x")
    policy.add_rule("local-owner", "system.disk", "allow", "x")
    assert policy.state() == (2, 1)

    assert policy.remove_rule(rule_id) is True

    assert policy.state() == (3, 2)
    assert policy.decide("local-owner", "system.info").allow is False
    assert policy.remove_rule(rule_id) is False
    assert policy.state() == (3, 2)


@pytest.mark.parametrize(
    "subject, capability, effect, reason",
    [
        ("", "system.info", "allow", "x"),
        ("owner", " ", "allow", "x"),
        ("owner", "system.info", "permit", "x"),
        ("owner", "system.info", "allow", " "),
    ],
)
def test_add_rule_validates_input(policy, subject, capability, effect, reason):
    with pytest.raises(ValueError):
        policy.add_rule(subject, capability, effect, reason)

    assert policy.state() == (0, 1)


def test_duplicate_rule_is_rejected_without_bumping_revision(policy):
    policy.add_rule("owner", "system.info", "allow", "x")

    with pytest.raises(StoreIntegrityError):
        policy.add_rule("owner", "system.info", "allow", "again")

    assert policy.state() == (1, 1)


def test_decide_raises_when_policy_state_is_unreadable(store, policy):
    store.close()

    with pytest.raises(Exception):
        policy.decide("local-owner", "system.info")


def test_seed_applies_once_per_name(policy):
    names = ["system.info", "system.disk"]

    assert seed_alpha_policy(policy, "tools", "Local-Owner", names) is True
    assert policy.decide("local-owner", "system.info").allow is True
    assert policy.decide("local-owner", "system.disk").allow is True
    # One seed is one policy revision, however many rules it adds.
    assert policy.state() == (1, 1)

    rule_id = policy.list_rules()[0]["rule_id"]
    policy.remove_rule(rule_id)

    # A removed rule must not come back on the next start.
    assert seed_alpha_policy(policy, "tools", "local-owner", names) is False
    assert policy.decide("local-owner", "system.info").allow is False
    assert policy.state() == (2, 2)


def test_seeds_with_different_names_are_independent(policy):
    assert seed_alpha_policy(policy, "tools", "local-owner", ["system.info"])
    assert seed_alpha_policy(policy, "models", "local-owner", ["llm.chat"])

    assert policy.state() == (2, 1)
    assert policy.decide("local-owner", "llm.chat").allow is True


def test_seed_does_not_duplicate_an_existing_rule(policy):
    policy.add_rule("local-owner", "system.info", "allow", "Added by owner.")

    assert seed_alpha_policy(policy, "tools", "local-owner", ["system.info"])

    rules = policy.list_rules()
    assert len(rules) == 1
    assert rules[0]["reason"] == "Added by owner."
    assert policy.state() == (1, 1)
