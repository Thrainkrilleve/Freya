import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from freya.spatial_avatar.appearance import APPEARANCES, DEFAULT_APPEARANCE
from freya.spatial_avatar.geometry import Display, GeometryError, Rect
from freya.paths import data_dir


DATA_DIR = data_dir()
SETTINGS_PATH = DATA_DIR / "avatar_settings.json"

CONFIG_VERSION = 1

# Sizes are a percentage of the display's usable height. These are
# starting values for development, not limits of the design: every one
# of them is a setting.
DEFAULT_MINIMUM = 8.0
DEFAULT_MAXIMUM = 65.0
DEFAULT_PREFERRED = 25.0

# The widest range any setting may take.
SMALLEST_PERCENT = 2.0
LARGEST_PERCENT = 100.0

SIZE_UNIT = "percent-of-usable-height"

# The avatar's width as a share of its height.
ASPECT = 0.62

FULL_AREA = Rect(0.0, 0.0, 1.0, 1.0)

QUALITIES = ("low", "medium", "high")

MAX_RESTRICTED_REGIONS = 32


def _number(value, default, low, high) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default

    if value != value:
        return default

    return float(min(max(value, low), high))


def _normalized_rect(value, default: Optional[Rect]) -> Optional[Rect]:
    """
    A rectangle in 0..1 of a display's usable area, or the default
    when the value is not one.
    """
    try:
        rect = Rect.from_value(value)
    except GeometryError:
        return default

    rect = rect.intersection(FULL_AREA)

    if rect is None or rect.width < 0.05 or rect.height < 0.05:
        return default

    return rect


@dataclass
class SizeLimits:
    """
    How small and large the avatar may be, and the size she prefers.
    """

    minimum: float = DEFAULT_MINIMUM
    maximum: float = DEFAULT_MAXIMUM
    preferred: float = DEFAULT_PREFERRED
    unit: str = SIZE_UNIT

    @classmethod
    def from_value(cls, value, default: Optional["SizeLimits"] = None) -> "SizeLimits":
        """
        Limits from untrusted data. Anything unusable falls back to the
        default, a maximum below the minimum is raised to it, and the
        preferred size is moved inside the range.
        """
        default = default or cls()
        value = value if isinstance(value, dict) else {}

        minimum = _number(
            value.get("minimum"), default.minimum, SMALLEST_PERCENT, LARGEST_PERCENT
        )
        maximum = _number(
            value.get("maximum"), default.maximum, SMALLEST_PERCENT, LARGEST_PERCENT
        )
        maximum = max(maximum, minimum)
        preferred = _number(
            value.get("preferred"), default.preferred, SMALLEST_PERCENT, LARGEST_PERCENT
        )

        return cls(minimum, maximum, min(max(preferred, minimum), maximum))

    def clamp(self, percent) -> float:
        """
        A requested size, brought inside the permitted range.
        """
        return _number(percent, self.preferred, self.minimum, self.maximum)

    def as_dict(self) -> dict:
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "preferred": self.preferred,
            "unit": self.unit,
        }


@dataclass
class DisplaySettings:
    """
    Settings that apply on one display only.
    """

    size: Optional[SizeLimits] = None
    # Where the avatar may go, in 0..1 of the display's usable area.
    boundary: Optional[Rect] = None

    def as_dict(self) -> dict:
        return {
            "size": self.size.as_dict() if self.size else None,
            "boundary": self.boundary.as_dict() if self.boundary else None,
        }


@dataclass
class RestrictedRegion:
    """
    Part of a display the avatar must stay out of.
    """

    display_id: str
    area: Rect  # 0..1 of the display's usable area

    def as_dict(self) -> dict:
        return {"display_id": self.display_id, "area": self.area.as_dict()}


@dataclass
class AvatarConfig:
    """
    One user's avatar settings.
    """

    # Off means there is no avatar at all: nothing may show or start
    # her until the owner turns her back on.
    enabled: bool = True
    visible: bool = False
    appearance: str = DEFAULT_APPEARANCE
    size: SizeLimits = field(default_factory=SizeLimits)
    # The size the user last chose, if they resized her by hand.
    current_size: Optional[float] = None
    auto_scale: bool = True
    smooth: bool = True
    always_on_top: bool = True
    click_through: bool = False
    # Where the avatar may go on a display with no boundary of its own.
    boundary: Rect = FULL_AREA
    restricted: List[RestrictedRegion] = field(default_factory=list)
    displays: Dict[str, DisplaySettings] = field(default_factory=dict)
    # Where she rests: a display and a point in 0..1 of its usable area
    # for the centre of her feet.
    rest_display: Optional[str] = None
    rest_x: float = 0.9
    rest_y: float = 1.0
    fps_limit: float = 30.0
    idle_fps: float = 5.0
    quality: str = "medium"

    # -- reading untrusted data ------------------------------------------

    @classmethod
    def from_value(cls, value) -> "AvatarConfig":
        """
        Settings from untrusted data (a file, or a request from the
        window). Nothing in it is trusted: every field is checked, and
        an unusable one is replaced by its default.
        """
        value = value if isinstance(value, dict) else {}
        default = cls()

        size = SizeLimits.from_value(value.get("size"))
        current = value.get("current_size")

        displays: Dict[str, DisplaySettings] = {}
        raw_displays = value.get("displays")

        if isinstance(raw_displays, dict):
            for display_id, raw in list(raw_displays.items())[:16]:
                if not isinstance(display_id, str) or not isinstance(raw, dict):
                    continue

                displays[display_id[:64]] = DisplaySettings(
                    size=(
                        SizeLimits.from_value(raw.get("size"), size)
                        if isinstance(raw.get("size"), dict)
                        else None
                    ),
                    boundary=_normalized_rect(raw.get("boundary"), None),
                )

        restricted: List[RestrictedRegion] = []
        raw_restricted = value.get("restricted")

        if isinstance(raw_restricted, list):
            for raw in raw_restricted[:MAX_RESTRICTED_REGIONS]:
                if not isinstance(raw, dict):
                    continue

                area = _normalized_rect(raw.get("area"), None)
                display_id = raw.get("display_id")

                if area is not None and isinstance(display_id, str):
                    restricted.append(RestrictedRegion(display_id[:64], area))

        appearance = value.get("appearance")
        quality = value.get("quality")
        rest_display = value.get("rest_display")

        return cls(
            enabled=value.get("enabled") is not False,
            # She cannot be both turned off and showing.
            visible=value.get("visible") is True and value.get("enabled") is not False,
            appearance=appearance if appearance in APPEARANCES else default.appearance,
            size=size,
            current_size=(
                size.clamp(current)
                if isinstance(current, (int, float)) and not isinstance(current, bool)
                else None
            ),
            auto_scale=value.get("auto_scale", default.auto_scale) is True,
            smooth=value.get("smooth", default.smooth) is True,
            always_on_top=value.get("always_on_top", default.always_on_top) is True,
            click_through=value.get("click_through") is True,
            boundary=_normalized_rect(value.get("boundary"), FULL_AREA),
            restricted=restricted,
            displays=displays,
            rest_display=rest_display[:64] if isinstance(rest_display, str) else None,
            rest_x=_number(value.get("rest_x"), default.rest_x, 0.0, 1.0),
            rest_y=_number(value.get("rest_y"), default.rest_y, 0.0, 1.0),
            fps_limit=_number(value.get("fps_limit"), default.fps_limit, 5.0, 120.0),
            idle_fps=_number(value.get("idle_fps"), default.idle_fps, 1.0, 30.0),
            quality=quality if quality in QUALITIES else default.quality,
        )

    def as_dict(self) -> dict:
        return {
            "version": CONFIG_VERSION,
            "enabled": self.enabled,
            "visible": self.visible,
            "appearance": self.appearance,
            "size": self.size.as_dict(),
            "current_size": self.current_size,
            "auto_scale": self.auto_scale,
            "smooth": self.smooth,
            "always_on_top": self.always_on_top,
            "click_through": self.click_through,
            "boundary": self.boundary.as_dict(),
            "restricted": [region.as_dict() for region in self.restricted],
            "displays": {
                display_id: settings.as_dict()
                for display_id, settings in self.displays.items()
            },
            "rest_display": self.rest_display,
            "rest_x": self.rest_x,
            "rest_y": self.rest_y,
            "fps_limit": self.fps_limit,
            "idle_fps": self.idle_fps,
            "quality": self.quality,
        }

    def updated(self, changes) -> "AvatarConfig":
        """
        These settings with some fields replaced, re-checked as a
        whole so that a change cannot leave them inconsistent.
        """
        merged = self.as_dict()

        if isinstance(changes, dict):
            for key, value in changes.items():
                if key == "size" and isinstance(value, dict):
                    merged["size"] = {**merged["size"], **value}
                elif key in merged and key != "version":
                    merged[key] = value

        return AvatarConfig.from_value(merged)

    # -- what the settings mean on a particular display --------------------

    def limits_for(self, display_id) -> SizeLimits:
        settings = self.displays.get(display_id)

        if settings is not None and settings.size is not None:
            return settings.size

        return self.size

    def size_percent(self, display_id, requested=None) -> float:
        """
        The size to use on a display: what was asked for, else what the
        user last chose, else the preferred size; always inside the
        limits for that display.
        """
        limits = self.limits_for(display_id)

        if requested is None:
            requested = (
                self.current_size
                if self.current_size is not None
                else limits.preferred
            )

        return limits.clamp(requested)

    def size_pixels(self, display: Display, requested=None):
        """
        (width, height) in physical pixels on a display.
        """
        height = display.work_area.height * self.size_percent(display.id, requested) / 100.0

        return (max(height * ASPECT, 1.0), max(height, 1.0))

    def boundary_for(self, display: Display) -> Rect:
        """
        Where the avatar may be on a display, in virtual coordinates.
        """
        settings = self.displays.get(display.id)
        area = (
            settings.boundary
            if settings is not None and settings.boundary is not None
            else self.boundary
        )
        work = display.work_area

        return Rect(
            work.left + area.left * work.width,
            work.top + area.top * work.height,
            work.left + area.right * work.width,
            work.top + area.bottom * work.height,
        )

    def restricted_for(self, display: Display) -> List[Rect]:
        """
        The regions the avatar must stay out of on a display, in
        virtual coordinates.
        """
        work = display.work_area

        return [
            Rect(
                work.left + region.area.left * work.width,
                work.top + region.area.top * work.height,
                work.left + region.area.right * work.width,
                work.top + region.area.bottom * work.height,
            )
            for region in self.restricted
            if region.display_id == display.id
        ]


class AvatarConfigStore:
    """
    Avatar settings on disk, kept separately for each user.
    """

    def __init__(self, path=SETTINGS_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

        users = data.get("users") if isinstance(data, dict) else None

        return users if isinstance(users, dict) else {}

    def load(self, user: str) -> AvatarConfig:
        with self._lock:
            return AvatarConfig.from_value(self._read().get(user))

    def save(self, user: str, config: AvatarConfig) -> None:
        with self._lock:
            users = self._read()
            users[user] = config.as_dict()

            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"version": CONFIG_VERSION, "users": users}, indent=1),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
