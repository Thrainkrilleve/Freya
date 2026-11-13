import pytest

from freya import host_checks
from freya.control_plane import build_local_control_plane
from freya.host_checks import (
    EVALUATION,
    OFF,
    ON,
    UNKNOWN,
    smart_app_control_state,
    startup_warnings,
)


@pytest.mark.parametrize(
    "value,state",
    [(0, OFF), (1, ON), (2, EVALUATION), (7, UNKNOWN), (None, UNKNOWN), ("1", UNKNOWN)],
)
def test_the_state_is_read_from_what_windows_reports(value, state):
    assert smart_app_control_state(lambda: value) == state


def test_a_failed_read_is_unknown_not_an_error():
    def broken():
        raise OSError("the registry could not be read")

    assert smart_app_control_state(broken) == UNKNOWN
    assert startup_warnings(broken) == []


def test_the_owner_is_warned_when_it_is_on():
    warnings = startup_warnings(lambda: 1)

    assert len(warnings) == 1
    assert "Smart App Control is on" in warnings[0]
    assert "image generation" in warnings[0]
    # It says where the switch is and whose decision it is.
    assert "Windows Security" in warnings[0] and "your decision" in warnings[0]


def test_evaluation_mode_is_mentioned_and_off_is_silent():
    assert "evaluation mode" in startup_warnings(lambda: 2)[0]
    assert startup_warnings(lambda: 0) == []
    assert startup_warnings(lambda: None) == []


def test_the_check_only_reads(monkeypatch):
    import inspect

    source = inspect.getsource(host_checks)

    for forbidden in ("SetValue", "CreateKey", "DeleteKey", "DeleteValue", "subprocess"):
        assert forbidden not in source


def test_freya_says_it_at_startup(tmp_path, fake_ollama, monkeypatch):
    monkeypatch.setattr(host_checks, "_read_smart_app_control", lambda: 1)
    plane = build_local_control_plane(tmp_path / "control.db", None)

    try:
        assert any(
            "Smart App Control is on" in message
            for message in plane.startup_messages()
        )
        monkeypatch.setattr(host_checks, "_read_smart_app_control", lambda: 0)
        assert not any(
            "Smart App Control" in message
            for message in plane.startup_messages()
        )
    finally:
        plane.close()
