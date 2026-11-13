"""
Things about this PC that can quietly stop Freya's tools from working.
Each check only reads; nothing here changes a setting.
"""
import sys
from typing import Callable, List, Optional


SMART_APP_CONTROL_KEY = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
SMART_APP_CONTROL_VALUE = "VerifiedAndReputablePolicyState"

OFF = "off"
ON = "on"
EVALUATION = "evaluation"
UNKNOWN = "unknown"

_STATES = {0: OFF, 1: ON, 2: EVALUATION}


def _read_smart_app_control() -> Optional[int]:
    if sys.platform != "win32":
        return None

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, SMART_APP_CONTROL_KEY,
        ) as key:
            value, _ = winreg.QueryValueEx(key, SMART_APP_CONTROL_VALUE)
    except OSError:
        return None

    return value if isinstance(value, int) else None


def smart_app_control_state(
    read: Callable[[], Optional[int]] = _read_smart_app_control,
) -> str:
    """
    Whether Windows Smart App Control is off, on, or only watching.

    When it is on, Windows refuses to load program files that are
    unsigned and that it does not recognise. The Python libraries
    behind image generation, the voice engines and Blender are of
    that kind, and no exception can be made for them.
    """
    try:
        return _STATES.get(read(), UNKNOWN)
    except Exception:
        return UNKNOWN


def startup_warnings(
    read: Callable[[], Optional[int]] = _read_smart_app_control,
) -> List[str]:
    """What the owner should be told about this PC as Freya starts."""
    state = smart_app_control_state(read)

    if state == ON:
        return [
            "Windows Smart App Control is on. It can block the files that "
            "image generation, the voice engines and avatar conversion "
            "depend on, without warning, and no exception can be made for "
            "them. If one of those stops working, this is the likely "
            "cause. It is switched off in Windows Security, under App & "
            "browser control; that is your decision, and it removes that "
            "protection for the whole PC."
        ]

    if state == EVALUATION:
        return [
            "Windows Smart App Control is in evaluation mode. Windows may "
            "switch it on by itself, and it would then be able to block "
            "the files that image generation and the voice engines "
            "depend on."
        ]

    return []
