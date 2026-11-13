import json
import queue
import threading

import psutil
import pytest

from freya.control_plane import build_local_control_plane
from freya.spatial_avatar import PROTOCOL_VERSION
from freya.spatial_avatar import commands as c
from freya.spatial_avatar.animation import (
    CANCELLED,
    COMPLETED,
    INTERRUPTED,
    MAX_WAITING,
    NORMAL,
    PENDING,
    RUNNING,
    USER,
    Animation,
    Animator,
)
from freya.spatial_avatar.appearance import APPEARANCES, cheapest_appearance
from freya.spatial_avatar.config import (
    AvatarConfig,
    AvatarConfigStore,
    SizeLimits,
)
from freya.spatial_avatar.geometry import (
    DISPLAY,
    LOGICAL,
    NORMALIZED,
    VIRTUAL,
    Display,
    DisplayLayout,
    GeometryError,
    Rect,
    single_display_layout,
)
from freya.spatial_avatar.host import STOPPED, AvatarError, AvatarHost
from freya.spatial_avatar.library import AvatarLibrary
from freya.spatial_avatar.model import AvatarModel
from freya.spatial_avatar.placement import aim, keep_inside, place_beside
from freya.spatial_avatar.renderers import choose_renderer, load_renderer
from freya.spatial_avatar.service import (
    AVATAR_CAPABILITIES,
    AVATAR_DISPLAY,
    AVATAR_LIBRARY,
    AVATAR_POINT,
    FUTURE_PERMISSIONS,
    SCREEN_GEOMETRY,
    AvatarService,
)
from freya.spatial_avatar.targets import (
    INVALID,
    STALE,
    VALID,
    MockTargetProvider,
    SpatialTarget,
    SpatialTargetProvider,
    TargetError,
    TargetRegistry,
    UserRegionProvider,
)


WAIT = 20


def two_displays() -> DisplayLayout:
    """
    A 4K main display at 150%, and a 1080p display to its left and
    slightly lower, at 100%. The second has negative coordinates.
    """
    return DisplayLayout(
        [
            Display(
                id="main",
                bounds=Rect(0, 0, 3840, 2160),
                work_area=Rect(0, 0, 3840, 2088),
                scale=1.5,
                primary=True,
            ),
            Display(
                id="side",
                bounds=Rect(-1920, 300, 0, 1380),
                work_area=Rect(-1920, 300, 0, 1340),
                scale=1.0,
            ),
        ]
    )


def settle(model, seconds=5.0, step=1 / 60):
    """Run the model forward in fixed frames; no real time passes."""
    for _ in range(int(seconds / step)):
        model.tick(step)


def shown(**changes) -> AvatarConfig:
    return AvatarConfig.from_value({"visible": True, **changes})


# -- size limits -------------------------------------------------------


def test_default_limits_are_the_development_examples():
    limits = SizeLimits()

    assert (limits.minimum, limits.maximum, limits.preferred) == (8.0, 65.0, 25.0)
    assert limits.unit == "percent-of-usable-height"


@pytest.mark.parametrize(
    "asked, got",
    [(25, 25), (8, 8), (65, 65), (1, 8), (0, 8), (-50, 8), (66, 65), (500, 65)],
)
def test_requested_size_is_brought_inside_the_limits(asked, got):
    assert SizeLimits().clamp(asked) == got


def test_preferred_size_outside_the_range_is_clamped_safely():
    low = SizeLimits.from_value({"minimum": 20, "maximum": 40, "preferred": 5})
    high = SizeLimits.from_value({"minimum": 20, "maximum": 40, "preferred": 90})

    assert low.preferred == 20
    assert high.preferred == 40


def test_maximum_below_minimum_is_raised_to_it():
    limits = SizeLimits.from_value({"minimum": 50, "maximum": 10, "preferred": 30})

    assert (limits.minimum, limits.maximum, limits.preferred) == (50, 50, 50)


def test_model_never_exceeds_the_limits_whatever_it_is_asked():
    model = AvatarModel(two_displays(), shown())

    for percent in (500, 65.0001, 1e9, -3, 0, 7.99):
        model.apply(c.command(c.RESIZE, percent=percent))
        settle(model, 1)
        assert 8 <= model.size_percent <= 65

        # Not at any moment along the way, either.
        model.apply(c.command(c.RESIZE, percent=30))
        for _ in range(40):
            model.tick(1 / 60)
            assert 8 <= model.size_percent <= 65

    assert model.user_resize(900) == 65
    assert model.user_resize(-1) == 8
    height = model.rect.height
    assert height == pytest.approx(2088 * 0.08)


def test_limits_can_differ_per_display():
    config = shown(
        size={"minimum": 10, "maximum": 60, "preferred": 30},
        displays={"side": {"size": {"minimum": 20, "maximum": 25, "preferred": 22}}},
    )

    assert config.size_percent("main", 50) == 50
    assert config.size_percent("side", 50) == 25
    assert config.size_percent("side", 1) == 20
    assert config.size_percent("side") == 22
    # Pixels follow each display's own usable height.
    assert config.size_pixels(two_displays().display("main"), 50)[1] == 1044
    assert config.size_pixels(two_displays().display("side"), 50)[1] == 260


def test_settings_change_while_running_takes_effect_at_once():
    model = AvatarModel(two_displays(), shown())
    model.apply(c.command(c.RESIZE, percent=60))
    settle(model, 1)
    assert model.size_percent == 60

    model.configure(shown(size={"minimum": 10, "maximum": 30, "preferred": 20}))

    assert model.size_percent <= 30
    assert model.rect.height <= 2088 * 0.30 + 0.5


# -- invalid configuration ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "big",
        [],
        42,
        {"size": "huge"},
        {"size": {"minimum": "8", "maximum": None, "preferred": float("nan")}},
        {"size": {"minimum": True, "maximum": False}},
        {"appearance": "gold-plated", "quality": "ultra"},
        {"boundary": [0.4, 0.4, 0.41, 0.41]},
        {"boundary": {"left": 2, "top": 2, "right": 3, "bottom": 3}},
        {"boundary": "everywhere"},
        {"fps_limit": 100000, "idle_fps": -1},
        {"displays": {"main": "wide"}, "restricted": [{"area": [0, 0, 9, 9]}, 5]},
        {"rest_x": 7, "rest_y": "bottom", "rest_display": 12},
        {"visible": "yes", "click_through": 1},
    ],
)
def test_bad_settings_fall_back_to_safe_values(value):
    config = AvatarConfig.from_value(value)

    assert 2 <= config.size.minimum <= config.size.preferred <= config.size.maximum <= 100
    assert config.appearance in APPEARANCES
    assert config.quality in ("low", "medium", "high")
    assert config.boundary.width >= 0.05 and config.boundary.height >= 0.05
    assert 0 <= config.boundary.left and config.boundary.right <= 1
    assert 5 <= config.fps_limit <= 120 and 1 <= config.idle_fps <= 30
    assert 0 <= config.rest_x <= 1 and 0 <= config.rest_y <= 1
    # Truthy values are not accepted as "on".
    assert config.visible is False and config.click_through is False
    # And it survives being written and read back.
    assert AvatarConfig.from_value(config.as_dict()).as_dict() == config.as_dict()

    # A model built on it still obeys the rules.
    model = AvatarModel(two_displays(), config)
    display = model.layout.display(model.display_id)
    assert config.boundary_for(display).contains(model.rect)


def test_update_cannot_leave_settings_inconsistent():
    config = shown().updated({"size": {"minimum": 70}})

    assert config.size.minimum == 70
    assert config.size.maximum == 70
    assert config.size.preferred == 70

    unchanged = config.updated({"no_such_setting": 1, "version": 99})
    assert unchanged.as_dict() == config.as_dict()


# -- coordinates and multi-monitor geometry ----------------------------


def test_rectangles_reject_nonsense():
    for value in (None, "x", [1, 2, 3], [1, 2, 3, "four"], [0, 0, -1, 5],
                  [True, 0, 1, 1], [0, 0, float("inf"), 1], {"left": 0}):
        with pytest.raises(GeometryError):
            Rect.from_value(value)

    assert Rect.from_value({"left": 1, "top": 2, "right": 3, "bottom": 4}) == Rect(1, 2, 3, 4)


def test_conversion_round_trips_on_a_display_with_an_offset_and_scale():
    layout = two_displays()

    for display in ("main", "side"):
        for system in (DISPLAY, LOGICAL, NORMALIZED):
            for point in ((0, 0), (10.5, 20.25), (0.5, 0.5)):
                virtual = layout.point_to_virtual(*point, system, display)
                back = layout.point_from_virtual(*virtual, system, display)
                assert back == pytest.approx(point)


def test_conversion_uses_each_displays_own_offset_scale_and_usable_area():
    layout = two_displays()

    # The side display starts at (-1920, 300).
    assert layout.point_to_virtual(100, 50, DISPLAY, "side") == (-1820, 350)
    assert layout.point_from_virtual(-1820, 350, DISPLAY, "side") == (100, 50)
    # Logical units are physical pixels divided by the scale: 1.5 here.
    assert layout.point_to_virtual(100, 100, LOGICAL, "main") == (150, 150)
    assert layout.point_from_virtual(300, 150, LOGICAL, "main") == (200, 100)
    # Normalized is across the usable area, which excludes the taskbar.
    assert layout.point_to_virtual(0.5, 1.0, NORMALIZED, "main") == (1920, 2088)
    assert layout.point_to_virtual(1.0, 1.0, NORMALIZED, "side") == (0, 1340)
    assert layout.rect_to_virtual(Rect(0, 0, 0.5, 0.5), NORMALIZED, "side") == Rect(
        -1920, 300, -960, 820
    )
    assert layout.point_to_virtual(7, 9, VIRTUAL) == (7, 9)

    with pytest.raises(GeometryError):
        layout.point_to_virtual(0, 0, DISPLAY, "missing")

    with pytest.raises(GeometryError):
        layout.point_to_virtual(0, 0, "furlongs", "main")


def test_finding_the_display_for_a_point_or_rectangle():
    layout = two_displays()

    assert layout.display_at(10, 10).id == "main"
    assert layout.display_at(-5, 400).id == "side"
    # (-5, 100) is above the side display and left of the main one.
    assert layout.display_at(-5, 100) is None
    assert layout.nearest_display(-5, 100).id == "main"
    assert layout.nearest_display(-3000, 800).id == "side"
    assert layout.display_for_rect(Rect(-200, 500, 100, 700)).id == "side"
    assert layout.display_for_rect(Rect(-50, 500, 400, 700)).id == "main"
    assert layout.virtual_bounds == Rect(-1920, 0, 3840, 2160)
    assert layout.primary.id == "main"


def test_layout_fingerprint_changes_with_any_change_to_the_displays():
    base = two_displays()
    same = two_displays()
    moved = DisplayLayout(
        [base.displays[0], Display("side", Rect(-1920, 0, 0, 1080), Rect(-1920, 0, 0, 1040))]
    )
    rescaled = DisplayLayout(
        [Display("main", Rect(0, 0, 3840, 2160), Rect(0, 0, 3840, 2088), 2.0, True),
         base.displays[1]]
    )
    unplugged = DisplayLayout([base.displays[0]])

    assert base.fingerprint == same.fingerprint
    assert len({base.fingerprint, moved.fingerprint, rescaled.fingerprint,
                unplugged.fingerprint}) == 4
    assert DisplayLayout.from_value(base.as_dict()).fingerprint == base.fingerprint


def test_impossible_displays_are_refused():
    with pytest.raises(GeometryError):
        DisplayLayout([])

    with pytest.raises(GeometryError):
        Display("d", Rect(0, 0, 100, 100), Rect(0, 0, 200, 100))

    with pytest.raises(GeometryError):
        Display("d", Rect(0, 0, 100, 100), Rect(0, 0, 100, 100), scale=0)

    with pytest.raises(GeometryError):
        DisplayLayout([two_displays().displays[0]] * 2)


def test_avatar_keeps_to_a_boundary_set_for_one_display():
    layout = two_displays()
    config = shown(
        boundary={"left": 0.5, "top": 0.5, "right": 1.0, "bottom": 1.0},
        displays={"side": {"boundary": {"left": 0, "top": 0, "right": 0.5, "bottom": 0.5}}},
    )
    model = AvatarModel(layout, config)

    main = config.boundary_for(layout.display("main"))
    side = config.boundary_for(layout.display("side"))
    assert main == Rect(1920, 1044, 3840, 2088)
    assert side == Rect(-1920, 300, -960, 820)

    for x, y in ((0, 0), (99999, 99999), (-99999, -99999), (2000, 1100)):
        model.drag_to(x, y)
        display = layout.display(model.display_id)
        assert config.boundary_for(display).contains(model.rect), (x, y)

    model.drag_to(-1900, 320)
    assert model.display_id == "side"
    assert side.contains(model.rect)


def test_she_stays_inside_her_boundary_for_every_frame_of_a_move():
    layout = two_displays()
    config = shown(boundary={"left": 0.1, "top": 0.1, "right": 0.9, "bottom": 0.9})
    model = AvatarModel(layout, config)

    for x, y in ((-5000, -5000), (9000, 9000), (-1000, 700), (1000, 1000)):
        model.apply(c.command(c.MOVE_TO, x=x, y=y))

        for _ in range(120):
            model.tick(1 / 60)
            display = layout.display(model.display_id)
            assert config.boundary_for(display).contains(model.rect)


def test_she_stays_out_of_restricted_regions():
    layout = single_display_layout(2000, 1048, taskbar=48)
    config = shown(
        restricted=[{"display_id": "display-1", "area": [0.4, 0.0, 0.6, 1.0]}],
        size={"minimum": 8, "maximum": 65, "preferred": 10},
    )
    model = AvatarModel(layout, config)
    wall = config.restricted_for(layout.primary)[0]
    assert wall == Rect(800, 0, 1200, 1000)

    model.drag_to(990, 500)
    assert model.rect.overlap_area(wall) == 0

    model.apply(c.command(c.MOVE_TO, x=100, y=500))
    settle(model, 2)
    model.apply(c.command(c.MOVE_TO, x=1800, y=500))

    for _ in range(120):
        model.tick(1 / 60)
        assert model.rect.overlap_area(wall) == 0

    assert model.rect.left >= 1200


def test_unplugging_her_display_brings_her_back_to_one_that_exists():
    model = AvatarModel(two_displays(), shown())
    model.drag_to(-1000, 600)
    assert model.display_id == "side"

    model.set_layout(DisplayLayout([two_displays().displays[0]]))

    assert model.display_id == "main"
    assert model.layout.display("main").work_area.contains(model.rect)


# -- targets -----------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_target_has_every_normalized_field():
    target = MockTargetProvider().targets(two_displays())[0].as_dict()

    assert set(target) >= {
        "target_id", "display_id", "coordinate_system", "rect",
        "confidence", "source", "timestamp", "validity",
    }
    assert target["validity"] == VALID
    assert target["coordinate_system"] == NORMALIZED
    assert target["source"] == "mock"


def test_resolved_target_is_in_virtual_coordinates_on_its_own_display():
    layout = two_displays()
    registry = TargetRegistry([MockTargetProvider()])
    registry.refresh(layout)

    centre = registry.resolve("mock:side:center", layout)

    assert centre.coordinate_system == VIRTUAL
    assert centre.rect == layout.rect_to_virtual(
        Rect(0.40, 0.40, 0.60, 0.55), NORMALIZED, "side"
    )
    assert layout.display("side").bounds.contains(centre.rect)


def test_target_goes_stale_when_the_layout_changes_and_is_looked_up_again():
    clock = Clock()
    registry = TargetRegistry([MockTargetProvider(clock)], clock)
    old = two_displays()
    registry.refresh(old)
    before = registry.resolve("mock:main:center", old).rect

    new = DisplayLayout(
        [Display("main", Rect(0, 0, 1920, 1080), Rect(0, 0, 1920, 1040), 1.0, True)]
    )

    assert registry.status(registry.get("mock:main:center"), new) == STALE
    after = registry.resolve("mock:main:center", new).rect
    # Revalidated against the new layout, not reused from the old one.
    assert after != before
    assert new.primary.bounds.contains(after)

    # The other display is gone, so its targets cannot be confirmed.
    with pytest.raises(TargetError):
        registry.resolve("mock:side:center", new)

    assert registry.get("mock:side:center").validity == INVALID


def test_old_target_is_revalidated_before_use():
    clock = Clock()
    asked = []

    class Counting(MockTargetProvider):
        def find(self, target_id, layout):
            asked.append(target_id)
            return super().find(target_id, layout)

    registry = TargetRegistry([Counting(clock)], clock, max_age=30)
    layout = two_displays()
    registry.refresh(layout)

    registry.resolve("mock:main:center", layout)
    assert asked == []

    clock.now += 31
    assert registry.status(registry.get("mock:main:center"), layout) == STALE
    registry.resolve("mock:main:center", layout)
    assert asked == ["mock:main:center"]


def test_reported_change_invalidates_until_confirmed_and_gone_targets_fail():
    class Vanishing(SpatialTargetProvider):
        id = "vanishing"
        permission = AVATAR_POINT
        present = True

        def targets(self, layout):
            if not self.present:
                return []

            return [
                SpatialTarget("v:1", "main", NORMALIZED, Rect(0.1, 0.1, 0.2, 0.2),
                              0.7, self.id, 1000.0, layout=layout.fingerprint)
            ]

    provider = Vanishing()
    registry = TargetRegistry([provider, MockTargetProvider()], lambda: 1000.0)
    layout = two_displays()
    registry.refresh(layout)

    # A window moved, say: everything from that provider is suspect.
    assert registry.invalidate(source="vanishing") == 1
    assert registry.get("v:1").validity == STALE
    assert registry.get("mock:main:center").validity == VALID

    # Still there when looked up again: usable.
    assert registry.resolve("v:1", layout).confidence == 0.7

    registry.invalidate()
    provider.present = False

    with pytest.raises(TargetError, match="moved or gone"):
        registry.resolve("v:1", layout)

    for unknown in ("nothing", None, 5, ["v:1"]):
        with pytest.raises(TargetError):
            registry.resolve(unknown, layout)


def test_user_chosen_region_is_a_target_only_under_the_layout_it_was_chosen_on():
    regions = UserRegionProvider()
    layout = two_displays()
    target = regions.add(Rect(-1500, 500, -1300, 600), layout, "  that   box ")

    assert target.display_id == "side"
    assert target.label == "that box"
    assert regions.targets(layout) == [target]
    assert regions.targets(single_display_layout()) == []

    for bad in ([0, 0, 1, 1], [90000, 90000, 90100, 90100], "here", None):
        with pytest.raises(TargetError):
            regions.add(bad, layout)


def test_provider_needing_another_permission_is_not_asked():
    asked = []

    class Capturing(SpatialTargetProvider):
        id = "screen-vision"
        permission = "screen.capture"

        def targets(self, layout):
            asked.append("captured the screen")
            return []

    registry = TargetRegistry([Capturing(), MockTargetProvider()])
    registry.refresh(
        two_displays(), allowed=lambda provider: provider.permission == AVATAR_POINT
    )

    assert asked == []
    assert registry.get("mock:main:center") is not None


# -- placement ---------------------------------------------------------


BOUNDS = Rect(0, 0, 2000, 1000)
SIZE = (120, 200)


@pytest.mark.parametrize(
    "target",
    [
        Rect(900, 400, 1100, 500),     # middle
        Rect(0, 0, 150, 60),           # top-left corner
        Rect(1850, 940, 2000, 1000),   # bottom-right corner
        Rect(0, 450, 80, 550),         # left edge
        Rect(700, 0, 1300, 40),        # top edge
    ],
)
def test_she_stands_beside_a_target_without_covering_it(target):
    placement = place_beside(target, SIZE, BOUNDS)

    assert placement.covers_target is False
    assert placement.rect.overlap_area(target) == 0
    assert BOUNDS.contains(placement.rect)
    assert (placement.rect.width, placement.rect.height) == SIZE


def test_she_does_not_move_when_already_beside_the_target():
    target = Rect(900, 400, 1100, 500)
    near = Rect.from_size(1150, 350, *SIZE)
    far = Rect.from_size(20, 20, *SIZE)

    assert place_beside(target, SIZE, BOUNDS, current=near).rect == near
    # From across the screen she walks over instead of pointing from afar.
    moved = place_beside(target, SIZE, BOUNDS, current=far).rect
    assert moved != far
    assert abs(moved.center[0] - target.center[0]) < 400


def test_placement_avoids_restricted_regions():
    target = Rect(900, 400, 1100, 500)
    left_of_target = Rect(0, 0, 900, 1000)

    placement = place_beside(target, SIZE, BOUNDS, restricted=[left_of_target])

    assert placement.covers_target is False
    assert placement.rect.overlap_area(left_of_target) == 0
    assert placement.rect.left >= 1100 or placement.rect.top >= 500 or placement.rect.bottom <= 400


def test_covering_is_reported_only_when_there_is_nowhere_else_to_stand():
    everything = Rect(0, 0, 2000, 1000)

    placement = place_beside(everything, SIZE, BOUNDS)

    assert placement.covers_target is True
    assert BOUNDS.contains(placement.rect)


def test_pointer_ends_at_the_edge_of_the_target():
    target = Rect(100, 100, 200, 200)

    assert aim((0, 150), target) == (100, 150)
    assert aim((300, 300), target) == (200, 200)
    assert aim((150, 150), target) == target.center


def test_keeping_inside_prefers_the_boundary_over_clearing_a_region():
    bounds = Rect(0, 0, 300, 300)
    everything = Rect(0, 0, 300, 300)

    kept = keep_inside(Rect(500, 500, 600, 700), bounds, [everything])

    assert bounds.contains(kept)


# -- animation ---------------------------------------------------------


def recorder():
    seen = []

    return seen, Animation("body", 1.0, seen.append)


def test_animation_runs_to_completion_in_ticks():
    animator = Animator()
    seen, animation = recorder()

    animator.play(animation)
    assert animation.state == RUNNING

    for _ in range(4):
        animator.tick(0.25)

    assert animation.state == COMPLETED
    assert seen[-1] == 1.0
    assert seen == sorted(seen)
    assert animator.busy() is False


def test_animation_can_be_cancelled_midway_and_stays_where_it_was():
    animator = Animator()
    seen, animation = recorder()
    ended = []
    animation._on_end = ended.append

    animator.play(animation)
    animator.tick(0.25)
    reached = seen[-1]

    assert animator.cancel(animation) is True
    animator.tick(0.25)
    animator.tick(0.25)

    assert animation.state == CANCELLED
    assert ended == [CANCELLED]
    assert seen[-1] == reached < 1.0
    # Cancelling again does nothing.
    assert animator.cancel(animation) is False


def test_equal_or_higher_priority_interrupts_and_lower_waits():
    animator = Animator()
    first = Animation("body", 1.0, lambda p: None, priority=NORMAL)
    urgent = Animation("body", 1.0, lambda p: None, priority=USER)
    minor = Animation("body", 0.5, lambda p: None, priority=NORMAL)

    animator.play(first)
    animator.play(urgent)
    assert (first.state, urgent.state) == (INTERRUPTED, RUNNING)

    animator.play(minor)
    assert minor.state == PENDING

    for _ in range(4):
        animator.tick(0.25)

    assert urgent.state == COMPLETED
    assert minor.state == RUNNING

    animator.tick(0.25)
    animator.tick(0.25)
    assert minor.state == COMPLETED


def test_interrupting_also_drops_what_was_waiting_behind():
    animator = Animator()
    running = Animation("body", 1.0, lambda p: None, priority=USER)
    waiting = Animation("body", 1.0, lambda p: None, priority=NORMAL)
    newest = Animation("body", 1.0, lambda p: None, priority=USER)

    animator.play(running)
    animator.play(waiting)
    animator.play(newest)

    assert (running.state, waiting.state, newest.state) == (
        INTERRUPTED, CANCELLED, RUNNING,
    )


def test_channels_are_independent_and_a_backlog_is_bounded():
    animator = Animator()
    body = Animation("body", 1.0, lambda p: None, priority=USER)
    arm = Animation("arm", 1.0, lambda p: None)

    animator.play(body)
    animator.play(arm)
    assert (body.state, arm.state) == (RUNNING, RUNNING)

    queued = [
        animator.play(Animation("body", 1.0, lambda p: None))
        for _ in range(MAX_WAITING + 3)
    ]

    assert [a.state for a in queued].count(PENDING) == MAX_WAITING
    assert [a.state for a in queued].count(CANCELLED) == 3
    assert animator.cancel_all("body") == MAX_WAITING + 1
    assert arm.state == RUNNING


def test_dragging_her_cancels_her_own_movement():
    model = AvatarModel(two_displays(), shown())
    changes = []
    model._emit = changes.append

    move = model.apply(c.command(c.MOVE_TO, x=100, y=100, id="m1"))
    model.tick(0.1)
    model.drag_to(2000, 1000)
    where = model.rect
    settle(model, 2)

    assert move.state == CANCELLED
    assert model.rect == where
    assert {"v": 1, "event": "animation", "id": "m1", "state": CANCELLED,
            "channel": "body"} in changes


def test_cancel_command_stops_a_named_animation():
    model = AvatarModel(two_displays(), shown())
    move = model.apply(c.command(c.MOVE_TO, x=100, y=100, id="m1"))
    model.tick(0.05)

    model.apply(c.command(c.CANCEL, animation="m1"))

    assert move.state == CANCELLED


# -- the model's commands ----------------------------------------------


def test_pointing_moves_her_beside_the_target_raises_her_arm_and_looks():
    layout = two_displays()
    model = AvatarModel(layout, shown())
    start = model.rect
    target = Rect(300, 300, 700, 420)

    model.apply(c.command(c.POINT_AT, rect=target.as_dict(), target_id="t1"))
    settle(model, 3)
    frame = model.frame()

    assert model.rect != start
    assert model.rect.overlap_area(target) == 0
    assert model.covers_target is False
    assert model.point_amount == 1.0
    assert frame["pointer"]["target"] == target
    # The pointer ends on the target's edge, not somewhere inside it.
    tip = frame["pointer"]["to"]
    assert target.left <= tip[0] <= target.right and target.top <= tip[1] <= target.bottom
    assert frame["gaze"] == target.center
    assert model.state()["pointing_target_id"] == "t1"

    model.apply(c.command(c.IDLE))
    settle(model, 1)
    assert model.frame()["pointer"] is None
    assert model.state()["pointing_at"] is None


def test_pointing_on_another_display_takes_her_there():
    layout = two_displays()
    model = AvatarModel(layout, shown())
    target = layout.rect_to_virtual(Rect(0.4, 0.4, 0.6, 0.55), NORMALIZED, "side")

    model.apply(c.command(c.POINT_AT, rect=target.as_dict()))
    settle(model, 3)

    assert model.display_id == "side"
    assert layout.display("side").work_area.contains(model.rect)
    assert model.rect.overlap_area(target) == 0


def test_return_to_rest_goes_back_and_stops_pointing():
    model = AvatarModel(two_displays(), shown())
    rest = model.rest_rect()
    model.apply(c.command(c.POINT_AT, rect=[300, 300, 700, 420]))
    settle(model, 3)

    model.apply(c.command(c.RETURN_TO_REST))
    settle(model, 3)

    assert model.rect == rest
    assert model.point_amount == 0


def test_appearances_share_one_character_and_switch_while_running():
    model = AvatarModel(two_displays(), shown())
    rect = model.rect
    assert {"realistic", "holographic", "lightweight"} <= set(APPEARANCES)

    for appearance in APPEARANCES:
        model.apply(c.command(c.SET_APPEARANCE, appearance=appearance))
        frame = model.frame()
        assert frame["appearance"].id == appearance
        # Same place, same size, same expression: only the look changed.
        assert frame["rect"] == rect
        assert frame["expression"] == "neutral"

    assert APPEARANCES["holographic"].opacity < 1 and APPEARANCES["holographic"].scanlines
    assert cheapest_appearance() == "lightweight"


def test_speech_and_expression_and_wave():
    model = AvatarModel(two_displays(), shown())

    model.apply(c.command(c.SPEECH, phase="started"))
    assert model.frame()["speaking"] is True and model.moving() is True
    model.apply(c.command(c.SPEECH, phase="interrupted"))
    assert model.frame()["speaking"] is False

    model.apply(c.command(c.SET_EXPRESSION, expression="happy"))
    assert model.frame()["expression"] == "happy"

    model.apply(c.command(c.WAVE))
    model.tick(0.2)
    assert model.frame()["wave"] is not None
    settle(model, 2)
    assert model.frame()["wave"] is None


@pytest.mark.parametrize(
    "message",
    [
        {"v": 1, "op": "teleport"},
        {"v": 1, "op": c.MOVE_TO},
        {"v": 1, "op": c.MOVE_TO, "x": "left", "y": 1},
        {"v": 1, "op": c.POINT_AT},
        {"v": 1, "op": c.POINT_AT, "rect": [9e5, 9e5, 9e5 + 10, 9e5 + 10]},
        {"v": 1, "op": c.RESIZE, "percent": "big"},
        {"v": 1, "op": c.SET_APPEARANCE, "appearance": "gold"},
        {"v": 1, "op": c.SET_EXPRESSION, "expression": "furious"},
        {"v": 1, "op": c.SPEECH, "phase": "shouting"},
        {"v": 99, "op": c.WAVE},
        {"op": c.WAVE},
        "wave",
    ],
)
def test_bad_commands_are_refused_and_change_nothing(message):
    model = AvatarModel(two_displays(), shown())
    before = model.state()

    with pytest.raises(ValueError):
        model.apply(message)

    assert model.state() == before


def test_idle_frame_rate_is_lower_and_hidden_draws_nothing():
    from freya.spatial_avatar.overlay import Session

    session = Session(two_displays(), events=open_null())
    session.model.configure(shown(fps_limit=30, idle_fps=5))
    settle(session.model, 1)

    assert session.frame_interval() == pytest.approx(1 / 5)

    session.model.apply(c.command(c.MOVE_TO, x=0, y=0))
    assert session.frame_interval() == pytest.approx(1 / 30)

    session.model.apply(c.command(c.HIDE))
    assert session.frame_interval() >= 0.1


class open_null:
    """Stands in for the overlay's event stream: collects what is sent."""

    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass

    def events(self):
        return [json.loads(line) for line in self.lines]


def test_session_reports_errors_as_events_and_carries_on():
    from freya.spatial_avatar.overlay import Session

    stream = open_null()
    session = Session(two_displays(), stream)

    session.put({"v": 1, "op": "teleport", "id": "x1"})
    session.put({"v": 5, "op": c.WAVE})
    session.put(c.command(c.CONFIGURE, config={"visible": True, "appearance": "holographic"}))
    session.put(c.command(c.SET_LAYOUT, layout={"displays": "none"}))
    session.put(c.command(c.REPORT))
    assert session.pump() is True

    kinds = [event["event"] for event in stream.events()]
    assert kinds.count("error") == 3
    assert "state" in kinds
    assert session.model.appearance == "holographic"

    session.put(c.command(c.SHUTDOWN))
    assert session.pump() is False


# -- renderers ---------------------------------------------------------


def test_renderer_choice_falls_back_and_explains():
    pytest.importorskip("tkinter", reason="the placeholder renderer needs tkinter, which this Python lacks")
    chosen, notes = choose_renderer("tk-placeholder")
    assert chosen == "tk-placeholder" and notes == []

    fallback, _ = choose_renderer("unreal-engine")
    assert fallback == "tk-placeholder"

    with pytest.raises(ValueError):
        load_renderer("unreal-engine")

    placeholder = load_renderer("tk-placeholder")
    assert placeholder.needs_gpu is False
    assert set(placeholder.appearances) == set(APPEARANCES)


def test_core_does_not_depend_on_the_avatar_or_any_engine():
    """
    Freya Core must not import the avatar, and the avatar must not
    import a rendering engine except inside a renderer.
    """
    import pathlib

    import freya

    root = pathlib.Path(freya.__file__).parent

    for path in (root / "core").rglob("*.py"):
        assert "spatial_avatar" not in path.read_text(encoding="utf-8"), path

    for path in (root / "spatial_avatar").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import tkinter" not in text, path

        for engine in ("unreal", "godot", "bpy", "audio2face", "metahuman"):
            assert f"import {engine}" not in text.lower(), (path, engine)

    # The overlay program shares nothing with Core.
    overlay = (root / "spatial_avatar" / "overlay.py").read_text(encoding="utf-8")
    assert "freya.core" not in overlay and "control_plane" not in overlay


def test_no_avatar_code_captures_the_screen_or_sends_input():
    import pathlib

    import freya

    forbidden = (
        "ImageGrab", "BitBlt", "PrintWindow", "mss", "pyautogui", "SendInput",
        "mouse_event", "keybd_event", "SetCursorPos", "uiautomation", "pywinauto",
        "GetWindowText", "EnumWindows",
    )

    for path in (pathlib.Path(freya.__file__).parent / "spatial_avatar").rglob("*.py"):
        text = path.read_text(encoding="utf-8")

        for name in forbidden:
            assert name not in text, (path.name, name)


# -- the overlay as a separate program ---------------------------------


class Listener:
    def __init__(self):
        self.events = queue.Queue()

    def __call__(self, message):
        self.events.put(message)

    def next(self, kind, test=lambda message: True):
        while True:
            message = self.events.get(timeout=WAIT)

            if message.get("event") == kind and test(message):
                return message


@pytest.fixture
def overlay(tmp_path):
    listener = Listener()
    host = AvatarHost("null", on_event=listener, log_path=tmp_path / "overlay.log")
    host.listener = listener
    yield host
    host.stop()


def test_overlay_starts_obeys_and_stops_as_its_own_process(overlay):
    assert overlay.running is False
    assert overlay.send(c.command(c.WAVE)) is False

    overlay.start()
    ready = overlay.listener.next(c.READY)
    process = overlay._process

    assert overlay.running is True
    assert ready["protocol"] == PROTOCOL_VERSION
    assert ready["renderer"] == "null"
    assert overlay.layout is not None
    assert process.pid != psutil.Process().pid

    overlay.send(c.command(c.CONFIGURE, config={"visible": True}))
    overlay.send(c.command(c.RESIZE, percent=999, id="r1"))
    done = overlay.listener.next(
        c.ANIMATION, lambda m: m["id"] == "r1" and m["state"] == COMPLETED
    )
    state = overlay.listener.next(
        c.STATE, lambda m: m["state"]["size_percent"] == 65 and not m["state"]["moving"]
    )

    assert done["channel"] == "size"
    assert state["state"]["visible"] is True

    overlay.stop()
    stopped = overlay.listener.next(STOPPED)

    assert stopped["expected"] is True
    assert overlay.running is False
    assert not psutil.pid_exists(process.pid)
    # Stopping again, or sending afterwards, is harmless.
    overlay.stop()
    assert overlay.send(c.command(c.WAVE)) is False


def test_overlay_exits_when_whoever_started_it_goes_away(overlay):
    overlay.start()
    process = overlay._process

    # Closing its input is what Freya exiting looks like to it.
    process.stdin.close()
    process.wait(timeout=WAIT)

    assert overlay.listener.next(STOPPED)["code"] == 0


def test_renderer_that_fails_takes_only_the_overlay_with_it(tmp_path):
    listener = Listener()
    host = AvatarHost(
        "null", on_event=listener, extra_arguments=["--fail"],
        log_path=tmp_path / "overlay.log",
    )

    try:
        host.start()
        stopped = listener.next(STOPPED)

        assert stopped["expected"] is False
        assert stopped["code"] != 0
        assert host.running is False
        assert host.send(c.command(c.WAVE)) is False
        assert "failed, as it was asked to" in (tmp_path / "overlay.log").read_text()
    finally:
        host.stop()


def test_renderer_that_cannot_start_is_a_clear_error(tmp_path):
    host = AvatarHost("no-such-renderer", log_path=tmp_path / "overlay.log")

    with pytest.raises(AvatarError, match="could not start"):
        host.start()

    assert host.running is False


def test_killed_overlay_is_noticed_and_a_broken_listener_cannot_stop_events(tmp_path):
    seen = Listener()

    def listener(message):
        seen(message)
        raise RuntimeError("the listener is broken")

    host = AvatarHost("null", on_event=listener, log_path=tmp_path / "overlay.log")

    try:
        host.start()
        seen.next(c.READY)
        victim = psutil.Process(host._process.pid)

        for member in victim.children(recursive=True) + [victim]:
            member.kill()

        assert seen.next(STOPPED)["expected"] is False
        assert host.running is False
    finally:
        host.stop()


# -- the service: capabilities, permissions, persistence ----------------


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


@pytest.fixture
def service(plane, tmp_path):
    published = Listener()
    service = AvatarService(
        plane.broker,
        "local-owner",
        store=AvatarConfigStore(tmp_path / "avatar.json"),
        host=AvatarHost("null", log_path=tmp_path / "overlay.log"),
        publish=lambda kind, **fields: published({"event": kind, **fields}),
        probe=single_display_layout,
        library=AvatarLibrary(tmp_path / "avatar_models", tmp_path / "avatar_library.json"),
    )
    service.published = published
    plane.register_avatar(service)
    yield service
    service.stop()


def audited(plane, capability):
    return [
        row
        for row in plane.store.execute(
            "SELECT capability, kind, outcome FROM audit_event WHERE capability = ?",
            (capability,),
        ).fetchall()
    ]


def test_only_the_listed_avatar_capabilities_exist(plane, service):
    assert AVATAR_CAPABILITIES == [
        AVATAR_DISPLAY, SCREEN_GEOMETRY, AVATAR_POINT, AVATAR_LIBRARY,
        "avatar.search", "avatar.prepare",
    ]

    for name in AVATAR_CAPABILITIES:
        assert plane.broker.exists(name)

    for name in FUTURE_PERMISSIONS:
        assert not plane.broker.exists(name)
        result = plane.broker.invoke(name)
        assert result.success is False
        assert result.outcome == "denied"

    assert set(FUTURE_PERMISSIONS) == {
        "screen.accessibility.read", "screen.capture", "screen.analyze",
        "input.mouse", "input.keyboard", "app.action",
    }


def test_avatar_capabilities_are_not_offered_to_the_model(plane, service):
    offered = json.dumps(plane.model_tools.definitions()).lower()

    assert "avatar" not in offered
    assert "screen" not in offered
    assert "system" in offered


def test_each_permission_is_separate_and_refusal_does_nothing(plane, service):
    rules = {rule["capability"]: rule["rule_id"] for rule in plane.policy.list_rules()}
    plane.policy.remove_rule(rules[AVATAR_POINT])

    refused = service.handle({"action": "point", "display": "display-1", "target": "center"})

    assert "not authorized" in refused["error"].lower()
    # Being refused pointing did not start her, and showing still works.
    assert service.running is False
    assert service.handle({"action": "show"})["error"] is None
    assert service.running is True
    assert plane.broker.invoke(SCREEN_GEOMETRY).success is True

    plane.policy.remove_rule(rules[AVATAR_DISPLAY])
    assert service.handle({"action": "wave"})["error"]
    assert plane.broker.invoke(SCREEN_GEOMETRY).success is True


def test_requests_go_through_the_broker_and_are_audited(plane, service):
    assert service.handle({"action": "show"})["error"] is None
    assert service.handle({"action": "point", "display": "display-1", "target": "center"})["error"] is None

    assert ("avatar.display", "outcome", "succeeded") in [tuple(r) for r in audited(plane, AVATAR_DISPLAY)]
    assert ("avatar.point", "dispatch", None) in [tuple(r) for r in audited(plane, AVATAR_POINT)]
    assert ("avatar.point", "outcome", "succeeded") in [tuple(r) for r in audited(plane, AVATAR_POINT)]


def test_geometry_returns_layout_and_nothing_about_content(plane, service):
    result = plane.broker.invoke(SCREEN_GEOMETRY)

    assert result.success is True
    assert set(result.result) == {"fingerprint", "displays"}
    assert set(result.result["displays"][0]) == {"id", "bounds", "work_area", "scale", "primary"}


def test_pointing_from_the_window_moves_her_without_covering_the_target(service):
    answer = service.handle({"action": "point", "display": "display-1", "target": "center"})
    assert answer["error"] is None

    state = service.published.next(
        "avatar",
        lambda m: m["state"]["state"]
        and m["state"]["state"]["pointing"] == 1.0
        and not m["state"]["state"]["moving"],
    )["state"]["state"]
    target, rect = Rect.from_value(state["pointing_at"]), Rect.from_value(state["rect"])

    assert state["pointing_target_id"] == "mock:display-1:center"
    assert rect.overlap_area(target) == 0
    assert state["covers_target"] is False


def test_pointing_at_a_region_the_user_chose(service):
    answer = service.handle({
        "action": "point",
        "region": {"display_id": "display-1", "area": [0.1, 0.1, 0.2, 0.2]},
    })

    assert answer["error"] is None
    state = service.published.next(
        "avatar", lambda m: m["state"]["state"] and m["state"]["state"]["pointing"] == 1.0
    )["state"]["state"]
    assert state["pointing_target_id"].startswith("region:")
    assert Rect.from_value(state["pointing_at"]) == Rect(192, 103.2, 384, 206.4)


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"action": "take-screenshot"},
        {"action": "click", "value": {"x": 1, "y": 1}},
        {"action": "point"},
        {"action": "point", "display": "display-1", "target": "the-bank-login"},
        {"action": "point", "region": {"display_id": "nowhere", "area": [0, 0, 1, 1]}},
        {"action": "point", "region": "everything"},
        {"action": "appearance", "value": "gold"},
        {"action": "expression", "value": "furious"},
        {"action": "move", "value": "over there"},
        "show",
    ],
)
def test_bad_window_requests_are_refused_with_a_message(service, message):
    answer = service.handle(message)

    assert answer["error"]
    assert answer["state"]["config"]["appearance"] == "lightweight"


def test_settings_persist_per_user(plane, tmp_path):
    store = AvatarConfigStore(tmp_path / "avatar.json")
    store.save("alice", shown(appearance="holographic", size={"minimum": 10, "maximum": 30, "preferred": 20}))
    store.save("bob", shown(appearance="realistic", click_through=True))

    again = AvatarConfigStore(tmp_path / "avatar.json")

    assert again.load("alice").appearance == "holographic"
    assert again.load("alice").size.maximum == 30
    assert again.load("bob").click_through is True
    assert again.load("alice").click_through is False
    # Someone with no settings gets the defaults, not another user's.
    assert again.load("carol").as_dict() == AvatarConfig().as_dict()


def test_damaged_settings_file_gives_defaults(tmp_path):
    path = tmp_path / "avatar.json"

    for content in ("not json", "[]", '{"users": "none"}', '{"users": {"alice": 5}}'):
        path.write_text(content, encoding="utf-8")
        assert AvatarConfigStore(path).load("alice").as_dict() == AvatarConfig().as_dict()


def test_changes_from_the_window_are_saved_and_restored(plane, service, tmp_path):
    service.handle({"action": "configure", "value": {
        "size": {"minimum": 12, "maximum": 40, "preferred": 30},
        "click_through": True,
        "boundary": {"left": 0.2, "top": 0.0, "right": 1.0, "bottom": 1.0},
    }})
    service.handle({"action": "appearance", "value": "holographic"})
    service.handle({"action": "resize", "value": 95})

    saved = AvatarConfigStore(tmp_path / "avatar.json").load("local-owner")

    assert (saved.size.minimum, saved.size.maximum, saved.size.preferred) == (12, 40, 30)
    assert saved.current_size == 40
    assert saved.appearance == "holographic"
    assert saved.click_through is True
    assert saved.boundary.left == 0.2
    assert saved.visible is True
    # Another user is unaffected.
    assert AvatarConfigStore(tmp_path / "avatar.json").load("someone-else").visible is False


def test_what_the_user_does_to_the_overlay_is_remembered(service, tmp_path):
    service.handle({"action": "show"})
    service.published.next("avatar", lambda m: m["state"]["state"] is not None)

    service._overlay_event({"event": c.USER_RESIZED, "percent": 33})
    service._overlay_event({"event": c.USER_SETTING, "key": "click_through", "value": True})
    service._overlay_event({"event": c.USER_SETTING, "key": "size", "value": {"maximum": 100}})
    service._overlay_event({"event": c.USER_MOVED, "x": 100.0, "y": 300.0, "display": "display-1"})

    saved = AvatarConfigStore(tmp_path / "avatar.json").load("local-owner")

    assert saved.current_size == 33
    assert saved.click_through is True
    # Only a few settings can be changed from the overlay itself.
    assert saved.size.maximum == 65
    assert saved.rest_display == "display-1"
    assert 0 < saved.rest_x < 0.2


def test_hiding_and_closing_always_work_even_without_permission(plane, service, tmp_path):
    service.handle({"action": "show"})
    rules = {rule["capability"]: rule["rule_id"] for rule in plane.policy.list_rules()}
    plane.policy.remove_rule(rules[AVATAR_DISPLAY])

    # The user choosing Hide or Close on the overlay itself is honoured.
    service._overlay_event({"event": c.USER_SETTING, "key": "visible", "value": False})
    assert AvatarConfigStore(tmp_path / "avatar.json").load("local-owner").visible is False

    service._overlay_event({"event": c.EXIT_REQUESTED})
    service.published.next("avatar", lambda m: m["state"]["running"] is False)
    assert service.running is False


def test_overlay_failure_leaves_the_service_and_broker_working(plane, service):
    service.handle({"action": "show"})
    service.published.next("avatar", lambda m: m["state"]["running"])
    victim = psutil.Process(service._host._process.pid)

    for member in victim.children(recursive=True) + [victim]:
        member.kill()

    stopped = service.published.next("avatar", lambda m: m["state"]["running"] is False)
    assert "stopped unexpectedly" in stopped["state"]["error"]

    # Freya's own capabilities are untouched, and she can be shown again.
    assert plane.broker.invoke("system.info").success is True
    assert service.handle({"action": "show"})["error"] is None
    assert service.running is True
    assert service.published.next("avatar", lambda m: m["state"]["error"] == "")


def test_speech_reaches_the_avatar_and_is_harmless_when_she_is_not_running(service):
    # Not running: nothing happens, nothing raises.
    service.speech_started("hello")
    service.speech_completed()
    assert service.running is False

    service.handle({"action": "show"})
    service.speech_started("hello")
    service.published.next("avatar", lambda m: (m["state"]["state"] or {}).get("speaking") is True)
    service.speech_interrupted()
    service.published.next("avatar", lambda m: (m["state"]["state"] or {}).get("speaking") is False)


def test_voice_tells_its_listener_and_survives_a_broken_one():
    from freya.voice.tts import FreyaVoice

    heard = []

    class Broken:
        def speech_started(self, text):
            heard.append("started")
            raise RuntimeError("listener broke")

    voice = FreyaVoice.__new__(FreyaVoice)
    voice.speech_listener = Broken()

    voice._tell_listener("speech_started", "")
    voice._tell_listener("speech_completed")

    assert heard == ["started"]

    voice.speech_listener = None
    voice._tell_listener("speech_started", "")


def test_window_describes_permissions_honestly(service):
    state = service.describe()
    built = {p["id"] for p in state["permissions"] if p["built"]}
    not_built = {p["id"] for p in state["permissions"] if not p["built"]}

    assert built == set(AVATAR_CAPABILITIES)
    assert not_built == set(FUTURE_PERMISSIONS)
    assert state["protocol"] == PROTOCOL_VERSION
    json.dumps(state)


# -- the gateway and the page ------------------------------------------


class FakeSocket:
    """Stands in for a connected window."""

    def __init__(self):
        self.sent = queue.Queue()
        self.incoming = queue.Queue()

    def send(self, raw):
        self.sent.put(json.loads(raw))

    def __iter__(self):
        while True:
            raw = self.incoming.get()

            if raw is None:
                return

            yield raw

    def next(self, kind):
        while True:
            message = self.sent.get(timeout=WAIT)

            if message["type"] == kind:
                return message


def test_avatar_requests_are_answered_while_a_long_turn_is_running(tmp_path):
    from freya.events import EventBus
    from freya.gateway.server import Gateway
    from freya.image.gallery import ImageGallery
    from freya.window import WindowActions

    started = threading.Event()
    release = threading.Event()
    asked = []

    def say(text):
        started.set()
        release.wait(WAIT)

    def avatar_request(message):
        asked.append(message["action"])
        return {"state": {"config": {}, "running": False}, "error": None}

    actions = WindowActions(
        say=say,
        generate_image=lambda prompt: None,
        set_muted=lambda muted: None,
        interrupt=lambda: None,
        status=lambda: {},
        avatar_state=lambda: {"config": {"appearance": "lightweight"}},
        avatar_request=avatar_request,
    )
    gateway = Gateway(EventBus(), ImageGallery(tmp_path / "images"), actions, port=0)
    gateway.start()
    socket = FakeSocket()
    connection = threading.Thread(
        target=gateway._handle_socket, args=(socket,), daemon=True
    )
    connection.start()

    try:
        hello = socket.next("hello")
        assert hello["avatar"] == {"config": {"appearance": "lightweight"}}

        socket.incoming.put(json.dumps({"type": "say", "text": "a long question"}))
        assert started.wait(WAIT)

        # The reply is still under way; hiding her must not wait for it.
        socket.incoming.put(
            json.dumps({"type": "avatar", "action": "hide", "request": "a1"})
        )
        answer = socket.next("avatar")

        assert answer["request"] == "a1"
        assert answer["error"] is None
        assert asked == ["hide"]
        assert not release.is_set()
    finally:
        release.set()
        gateway.stop()
        socket.incoming.put(None)
        connection.join(WAIT)


def test_page_has_an_avatar_tab_that_only_asks():
    from importlib import resources

    static = resources.files("freya.gateway") / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")

    for control in (
        'id="tab-avatar"', 'id="avatar-show"', 'id="avatar-hide"', 'id="avatar-stop"',
        'id="avatar-size"', 'id="avatar-min"', 'id="avatar-max"',
        'id="avatar-clickthrough"', 'id="avatar-point"', 'id="avatar-map"',
        'id="avatar-permissions"',
    ):
        assert control in page, control

    assert 'type: "avatar"' in script
    assert "innerHTML" not in script
    # The click-through setting says how she can still be reached.
    assert "still works" in page


# -- turning her off completely ------------------------------------------


def test_turned_off_avatar_cannot_be_shown_or_started(plane, service):
    assert service.handle({"action": "show"})["error"] is None
    assert service.running

    answer = service.handle({"action": "disable"})
    assert answer["error"] is None
    assert answer["state"]["config"]["enabled"] is False
    assert answer["state"]["config"]["visible"] is False
    assert not service.running

    for request in (
        {"action": "show"},
        {"action": "wave"},
        {"action": "resize", "value": 30},
        {"action": "expression", "value": "happy"},
        {"action": "point", "display": "display-1", "target": "top-left"},
        # Settings may change, but not so as to bring her back.
        {"action": "configure", "value": {"visible": True}},
        {"action": "configure", "value": {"enabled": True, "visible": True}},
    ):
        answer = service.handle(request)
        assert not service.running, request
        assert answer["state"]["config"]["enabled"] is False, request

        if request["action"] != "configure":
            assert "turned off" in answer["error"], request

    # Speech is not passed to a program that is not there.
    service.speech_started("hello")
    service.speech_completed()
    assert not service.running

    # Hiding and closing are harmless while she is off.
    assert service.handle({"action": "hide"})["error"] is None
    assert service.handle({"action": "stop"})["error"] is None


def test_turning_her_off_is_remembered_and_on_does_not_show_her(plane, service, tmp_path):
    service.handle({"action": "show"})
    service.handle({"action": "disable"})

    stored = AvatarConfigStore(tmp_path / "avatar.json").load("local-owner")
    assert stored.enabled is False and stored.visible is False

    answer = service.handle({"action": "enable"})
    assert answer["error"] is None
    assert answer["state"]["config"]["enabled"] is True
    # Turning her on allows her to be shown; it does not show her.
    assert not service.running

    assert service.handle({"action": "show"})["error"] is None
    assert service.running


def test_settings_cannot_say_off_and_showing_at_once():
    config = AvatarConfig.from_value({"enabled": False, "visible": True})
    assert config.enabled is False and config.visible is False

    # Settings saved before this existed mean she is on.
    assert AvatarConfig.from_value({"visible": True}).enabled is True
    assert AvatarConfig().updated({"enabled": False}).as_dict()["enabled"] is False
