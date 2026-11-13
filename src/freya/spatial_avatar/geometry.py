import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple


# Coordinate systems a rectangle or point may be expressed in.
#
# VIRTUAL is the canonical one: physical pixels across the whole
# desktop, with the primary display's top-left corner at (0, 0), so a
# display to the left of or above the primary has negative coordinates.
VIRTUAL = "virtual-physical"
# Physical pixels from one display's top-left corner.
DISPLAY = "display-physical"
# Device-independent units from one display's top-left corner
# (physical pixels divided by that display's scale factor).
LOGICAL = "display-logical"
# 0.0 to 1.0 across one display's usable area.
NORMALIZED = "display-normalized"

COORDINATE_SYSTEMS = (VIRTUAL, DISPLAY, LOGICAL, NORMALIZED)


class GeometryError(ValueError):
    """
    A rectangle, point, or display reference that makes no sense.
    """


@dataclass(frozen=True)
class Rect:
    """
    A rectangle. `right` and `bottom` are exclusive.
    """

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self):
        # Always floats, so that two descriptions of the same rectangle
        # are equal however they were written.
        for name in ("left", "top", "right", "bottom"):
            object.__setattr__(self, name, float(getattr(self, name)))

        if self.right < self.left or self.bottom < self.top:
            raise GeometryError("A rectangle cannot have negative size.")

    @classmethod
    def from_size(cls, left, top, width, height) -> "Rect":
        return cls(left, top, left + width, top + height)

    @classmethod
    def from_value(cls, value) -> "Rect":
        """
        Build a rectangle from untrusted data: a mapping with left,
        top, right, bottom, or a sequence of those four numbers.
        """
        if isinstance(value, Rect):
            return value

        try:
            if isinstance(value, dict):
                numbers = [value[key] for key in ("left", "top", "right", "bottom")]
            else:
                numbers = list(value)

            if len(numbers) != 4 or any(
                isinstance(number, bool) for number in numbers
            ):
                raise GeometryError("A rectangle needs four numbers.")

            numbers = [float(number) for number in numbers]
        except (TypeError, ValueError, KeyError):
            raise GeometryError("A rectangle needs four numbers.")

        if any(number != number or abs(number) > 1e7 for number in numbers):
            raise GeometryError("A rectangle's numbers are out of range.")

        return cls(*numbers)

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center(self) -> Tuple[float, float]:
        return (self.left + self.width / 2, self.top + self.height / 2)

    def as_dict(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
        }

    def contains_point(self, x, y) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def contains(self, other: "Rect") -> bool:
        return (
            self.left <= other.left
            and self.top <= other.top
            and self.right >= other.right
            and self.bottom >= other.bottom
        )

    def intersection(self, other: "Rect") -> Optional["Rect"]:
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)

        if right <= left or bottom <= top:
            return None

        return Rect(left, top, right, bottom)

    def overlap_area(self, other: "Rect") -> float:
        shared = self.intersection(other)

        return shared.area if shared else 0.0

    def moved_to(self, left, top) -> "Rect":
        return Rect.from_size(left, top, self.width, self.height)

    def inset(self, amount) -> "Rect":
        amount = min(amount, self.width / 2, self.height / 2)

        return Rect(
            self.left + amount,
            self.top + amount,
            self.right - amount,
            self.bottom - amount,
        )

    def clamped_into(self, bounds: "Rect") -> "Rect":
        """
        This rectangle moved, without resizing, as little as possible
        so that it lies inside `bounds`. If it is larger than `bounds`
        it is aligned to the top-left of them.
        """
        left = min(max(self.left, bounds.left), max(bounds.left, bounds.right - self.width))
        top = min(max(self.top, bounds.top), max(bounds.top, bounds.bottom - self.height))

        return self.moved_to(left, top)


@dataclass(frozen=True)
class Display:
    """
    One monitor.

    bounds     the whole display, in virtual coordinates
    work_area  the part not covered by the taskbar and docked bars
    scale      physical pixels per logical unit (1.5 at "150%")
    """

    id: str
    bounds: Rect
    work_area: Rect
    scale: float = 1.0
    primary: bool = False

    def __post_init__(self):
        if not self.id:
            raise GeometryError("A display needs an identifier.")

        if not 0.25 <= self.scale <= 8:
            raise GeometryError("A display's scale factor is out of range.")

        if self.bounds.area <= 0 or not self.bounds.contains(self.work_area):
            raise GeometryError("A display's usable area must lie inside it.")

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "bounds": self.bounds.as_dict(),
            "work_area": self.work_area.as_dict(),
            "scale": self.scale,
            "primary": self.primary,
        }

    @classmethod
    def from_value(cls, value) -> "Display":
        try:
            return cls(
                id=str(value["id"]),
                bounds=Rect.from_value(value["bounds"]),
                work_area=Rect.from_value(value["work_area"]),
                scale=float(value.get("scale", 1.0)),
                primary=value.get("primary") is True,
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise GeometryError(f"That display description is not valid: {exc}")


class DisplayLayout:
    """
    Every display, and conversion between coordinate systems.

    `fingerprint` changes whenever a display is added, removed, moved,
    resized, or rescaled. Anything remembered about the screen, such
    as where a target was, is only trusted while it is unchanged.
    """

    def __init__(self, displays: List[Display]):
        if not displays:
            raise GeometryError("There must be at least one display.")

        if len({display.id for display in displays}) != len(displays):
            raise GeometryError("Display identifiers must be unique.")

        self.displays = list(displays)
        self.fingerprint = hashlib.sha256(
            repr(
                sorted(
                    (
                        display.id,
                        display.bounds,
                        display.work_area,
                        round(float(display.scale), 4),
                        bool(display.primary),
                    )
                    for display in displays
                )
            ).encode("utf-8")
        ).hexdigest()[:16]

    def as_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "displays": [display.as_dict() for display in self.displays],
        }

    @classmethod
    def from_value(cls, value) -> "DisplayLayout":
        try:
            return cls([Display.from_value(item) for item in value["displays"]])
        except (TypeError, KeyError) as exc:
            raise GeometryError(f"That display layout is not valid: {exc}")

    @property
    def primary(self) -> Display:
        for display in self.displays:
            if display.primary:
                return display

        return self.displays[0]

    @property
    def virtual_bounds(self) -> Rect:
        return Rect(
            min(display.bounds.left for display in self.displays),
            min(display.bounds.top for display in self.displays),
            max(display.bounds.right for display in self.displays),
            max(display.bounds.bottom for display in self.displays),
        )

    def display(self, display_id) -> Display:
        for display in self.displays:
            if display.id == display_id:
                return display

        raise GeometryError(f"There is no display called {display_id!r}.")

    def has_display(self, display_id) -> bool:
        return any(display.id == display_id for display in self.displays)

    def display_at(self, x, y) -> Optional[Display]:
        """
        The display containing a virtual point, or None when the point
        is in a gap between displays or off the desktop.
        """
        for display in self.displays:
            if display.bounds.contains_point(x, y):
                return display

        return None

    def nearest_display(self, x, y) -> Display:
        """
        The display containing a virtual point, or failing that the
        one whose edge is closest to it.
        """
        found = self.display_at(x, y)

        if found is not None:
            return found

        def distance(display):
            dx = max(display.bounds.left - x, 0, x - display.bounds.right)
            dy = max(display.bounds.top - y, 0, y - display.bounds.bottom)

            return dx * dx + dy * dy

        return min(self.displays, key=distance)

    def display_for_rect(self, rect: Rect) -> Display:
        """
        The display a virtual rectangle is mostly on.
        """
        best = max(self.displays, key=lambda d: d.bounds.overlap_area(rect))

        if best.bounds.overlap_area(rect) > 0:
            return best

        return self.nearest_display(*rect.center)

    # -- conversion ------------------------------------------------------

    def point_to_virtual(self, x, y, system, display_id=None):
        if system == VIRTUAL:
            return (float(x), float(y))

        if system not in COORDINATE_SYSTEMS:
            raise GeometryError(f"Unknown coordinate system {system!r}.")

        display = self.display(display_id)

        if system == DISPLAY:
            return (display.bounds.left + x, display.bounds.top + y)

        if system == LOGICAL:
            return (
                display.bounds.left + x * display.scale,
                display.bounds.top + y * display.scale,
            )

        area = display.work_area

        return (area.left + x * area.width, area.top + y * area.height)

    def point_from_virtual(self, x, y, system, display_id=None):
        if system == VIRTUAL:
            return (float(x), float(y))

        if system not in COORDINATE_SYSTEMS:
            raise GeometryError(f"Unknown coordinate system {system!r}.")

        display = self.display(display_id)

        if system == DISPLAY:
            return (x - display.bounds.left, y - display.bounds.top)

        if system == LOGICAL:
            return (
                (x - display.bounds.left) / display.scale,
                (y - display.bounds.top) / display.scale,
            )

        area = display.work_area

        if area.width <= 0 or area.height <= 0:
            raise GeometryError("That display has no usable area.")

        return (
            (x - area.left) / area.width,
            (y - area.top) / area.height,
        )

    def rect_to_virtual(self, rect: Rect, system, display_id=None) -> Rect:
        left, top = self.point_to_virtual(rect.left, rect.top, system, display_id)
        right, bottom = self.point_to_virtual(
            rect.right, rect.bottom, system, display_id
        )

        return Rect(left, top, right, bottom)

    def rect_from_virtual(self, rect: Rect, system, display_id=None) -> Rect:
        left, top = self.point_from_virtual(
            rect.left, rect.top, system, display_id
        )
        right, bottom = self.point_from_virtual(
            rect.right, rect.bottom, system, display_id
        )

        return Rect(left, top, right, bottom)


def single_display_layout(width=1920, height=1080, taskbar=48) -> DisplayLayout:
    """
    A stand-in layout for when the real one cannot be read.
    """
    return DisplayLayout(
        [
            Display(
                id="display-1",
                bounds=Rect(0, 0, width, height),
                work_area=Rect(0, 0, width, height - taskbar),
                scale=1.0,
                primary=True,
            )
        ]
    )


def enable_dpi_awareness() -> bool:
    """
    Ask Windows to give this process real pixel coordinates on every
    monitor, instead of coordinates scaled for the primary one. Must
    be called before the process creates a window. Returns whether it
    took effect.
    """
    if os.name != "nt":
        return False

    try:
        import ctypes

        # 2 = aware of each monitor's own scale factor.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

        return True
    except Exception:
        return False


def read_windows_layout() -> DisplayLayout:
    """
    The current display layout, read from Windows.

    Reads geometry only: positions, sizes, usable areas, and scale
    factors of the monitors. It reads nothing that is shown on them.
    Raises GeometryError when the layout cannot be read. Coordinates
    are physical pixels only if the calling process is DPI-aware
    (enable_dpi_awareness).
    """
    if os.name != "nt":
        raise GeometryError("Display layout can only be read on Windows.")

    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    user32 = ctypes.windll.user32
    displays: List[Display] = []

    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def visit(monitor, _context, _rect, _data):
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(MonitorInfo)

        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return True

        scale = 1.0

        try:
            dpi_x = ctypes.c_uint()
            dpi_y = ctypes.c_uint()

            # 0 = the scale factor Windows applies to this monitor.
            if (
                ctypes.windll.shcore.GetDpiForMonitor(
                    wintypes.HANDLE(monitor),
                    0,
                    ctypes.byref(dpi_x),
                    ctypes.byref(dpi_y),
                )
                == 0
                and dpi_x.value
            ):
                scale = dpi_x.value / 96.0
        except Exception:
            pass

        def rect(value):
            return Rect(value.left, value.top, value.right, value.bottom)

        try:
            displays.append(
                Display(
                    id=info.szDevice.strip("\\.").lower() or f"display-{len(displays) + 1}",
                    bounds=rect(info.rcMonitor),
                    work_area=rect(info.rcWork),
                    scale=scale,
                    primary=bool(info.dwFlags & 1),
                )
            )
        except GeometryError:
            pass

        return True

    try:
        user32.EnumDisplayMonitors(None, None, callback_type(visit), 0)
    except Exception as exc:
        raise GeometryError(f"The display layout could not be read: {exc}")

    if not displays:
        raise GeometryError("Windows reported no displays.")

    return DisplayLayout(displays)


PROBE_TIMEOUT_SECONDS = 10


def probe_layout() -> DisplayLayout:
    """
    The display layout in true pixels, read by a short-lived helper
    process.

    A process only sees real per-monitor coordinates if it declared
    itself DPI-aware before doing anything else, which Freya's own
    process has not. Rather than change that, the layout is read by a
    separate process that does declare it and prints the result.
    Raises GeometryError when that fails.
    """
    import json
    import subprocess
    import sys

    from freya.child_environment import child_environment

    try:
        output = subprocess.run(
            [sys.executable, "-m", "freya.spatial_avatar.geometry"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=child_environment(),
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        ).stdout

        return DisplayLayout.from_value(json.loads(output))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise GeometryError(f"The display layout could not be read: {exc}")


if __name__ == "__main__":
    import json

    enable_dpi_awareness()

    try:
        print(json.dumps(read_windows_layout().as_dict()))
    except GeometryError:
        raise SystemExit(1)
