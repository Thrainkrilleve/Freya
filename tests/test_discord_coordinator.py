from tools.discord_coordinator.core import Config, allowed, clean_prompt, parse_ids, route, system_prompt
import pytest

A = "100000000000000001"
B = "100000000000000002"

def test_id_validation():
    assert parse_ids(A + "," + B) == {int(A), int(B)}
    for value in ("", "abc", "123", A + ","):
        with pytest.raises(ValueError):
            parse_ids(value)

def test_scope_and_bot_loop_prevention():
    c = Config("openai", int(A), frozenset({int(B)}), frozenset({int(A)}))
    assert allowed(c, int(A), int(B), False)
    assert not allowed(c, int(A), int(B), True)
    assert not allowed(c, int(B), int(B), False)
    assert not allowed(c, int(A), int(A), False)

def test_routes():
    assert route("!review proposed design", False) == "review"
    assert route("!review ", False) is None
    assert route("hello", True) == "chat"
    assert route("hello", False) is None

def test_prompt_and_identity():
    assert clean_prompt("<@100000000000000001> hello", int(A)) == "hello"
    assert "not the user's ChatGPT session" in system_prompt("openai", "safe")
    assert len(clean_prompt("x" * 9000, int(A))) == 6000
