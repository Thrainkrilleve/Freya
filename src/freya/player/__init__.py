"""
Player 2, milestone M0.

A schema, a mock adapter, and the loop between a planner and that
adapter. No game is played. The control plane does not register a
capability from this package, and Core does not import it.
"""

from freya.player.indicator import IN_CONTROL, PAUSED

__all__ = ["IN_CONTROL", "PAUSED"]
