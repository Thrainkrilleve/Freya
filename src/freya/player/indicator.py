"""
The two words the window may show while Player 2 exists.

They are fixed so a model string cannot be written into the status
strip. The same words are copied into an observation, which a test can
read without a display.
"""

IN_CONTROL = "Freya in control"
PAUSED = "Freya paused"

CONTROL_LABELS = (IN_CONTROL, PAUSED)
