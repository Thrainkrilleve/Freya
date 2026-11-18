"""
Input simulation is not part of M0.

A later milestone may add one backend per operating system. Until the
owner installs that backend, this one answers. It never reports a key
as sent, and it holds nothing, on every operating system.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InputResult:
    available: bool
    sent: bool
    detail: str


class MissingInputBackend:
    """The backend that is present when no operating-system backend is."""

    def available(self) -> bool:
        return False

    def press(self, key) -> InputResult:
        return InputResult(
            False,
            False,
            "No input backend is available on this system.",
        )

    def release_all(self) -> InputResult:
        return InputResult(
            False,
            False,
            "No input backend is available on this system.",
        )

    def held(self) -> tuple:
        return ()
