"""
Live public data about the world, read from a God's Eye View program
running on this machine.

God's Eye View (https://github.com/bilawalsidhu/gods-eye-view, MIT) is a
separate program the owner installs and starts. It gathers public feeds
(aircraft transponders, ship beacons, satellite orbits, earthquakes,
fires, storms, public cameras) and answers questions about them through
a local endpoint. Freya asks that endpoint; she does not fetch the feeds
herself, and she never starts or stops the program.

What leaves this machine is decided by that program: for a question
about a place, the place's name goes to a public place-name service and
the area to the feed that is asked. Nothing from memory or the
conversation is sent, only the few words the tool was given.

These are the first of Freya's tools whose answers come from the
internet. They exist only when the owner turns them on
(FREYA_WORLD_VIEW=1), every call goes through the broker, and what
comes back is untrusted text like any other tool result.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable, Optional

from freya.core.capabilities import CapabilityDefinition
from freya.core.providers import ProviderDefinition
from freya.tools.model_tools import MODEL_TOOL, TOOL_PARAMETERS


WORLD_VIEW_PROVIDER = "world-view-local"

WORLD_OVERVIEW = "world.overview"
WORLD_AIRCRAFT = "world.aircraft"
WORLD_SHIPS = "world.ships"
WORLD_SATELLITE_PASS = "world.satellite_pass"
WORLD_EARTHQUAKES = "world.earthquakes"
WORLD_FIRES = "world.fires"
WORLD_STORMS = "world.storms"
WORLD_LAUNCHES = "world.launches"
WORLD_CAMERAS = "world.cameras"
WORLD_MILITARY = "world.military"
WORLD_SHOW = "world.show"

WORLD_VIEW_CAPABILITIES = [
    WORLD_OVERVIEW,
    WORLD_AIRCRAFT,
    WORLD_SHIPS,
    WORLD_SATELLITE_PASS,
    WORLD_EARTHQUAKES,
    WORLD_FIRES,
    WORLD_STORMS,
    WORLD_LAUNCHES,
    WORLD_CAMERAS,
    WORLD_MILITARY,
    WORLD_SHOW,
]

# The program is asked on this machine only.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Some feeds take a while to answer the program.
REQUEST_TIMEOUT_SECONDS = 45
RUNNING_CHECK_TIMEOUT_SECONDS = 2
# How long an answer to "is it running?" is believed.
RUNNING_CHECK_SECONDS = 15
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# What one call may return. The model's whole context is small.
MAX_ROWS = 10
MAX_TEXT_CHARACTERS = 300
MAX_PLACE_CHARACTERS = 200

# The context clips a tool result at 2,400 bytes
# (`ContextAuthority.fit_tools`), and a clipped answer is no longer
# valid JSON, so an answer is kept under that as encoded.
MAX_ANSWER_CHARACTERS = 2_200

# The layers a view can turn on: the word Freya's model uses, and the
# name God's Eye View knows the layer by.
LAYERS = {
    "flights": "flights",
    "military": "military",
    "satellites": "satellites",
    "earthquakes": "earthquakes",
    "fires": "local-firms",
    "ships": "ais-live-vessels",
    "cameras": "cctv",
    "storms": "weather-cyclones",
    "radar": "weather-radar",
    "wind": "wind",
    "launches": "rocket-launches",
    "traffic": "traffic",
    "transit": "transit",
}

NOT_RUNNING_MESSAGE = (
    "God's Eye View is not running on this computer, so live world "
    "data is not available."
)

SOURCE_NOTE = (
    "Live public data from God's Eye View. Positions and times are as "
    "reported by the feeds and may be late or incomplete."
)


# The program's own words for a feed whose key was never added.
_MISSING_KEY = re.compile(r"[A-Z0-9_]*API_KEY\b|\bapi[ _-]?key\b", re.I)


class WorldViewError(RuntimeError):
    """A failure that can be told to the user as it is."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """
    A redirect could send a question on to another computer, so none is
    followed: the program's answer is the one it gives at its own
    address.
    """

    def redirect_request(self, *args, **kwargs):
        return None


def _is_local(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)

    return parts.scheme == "http" and parts.hostname in LOCAL_HOSTS


def _clip(value, rows=MAX_ROWS, limit=MAX_TEXT_CHARACTERS):
    """Keep what a model can use: short text, numbers, small lists."""
    if isinstance(value, str):
        return value[:limit]

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    if isinstance(value, list):
        return [_clip(item, rows, limit) for item in value[:rows]]

    if isinstance(value, dict):
        return {
            str(key)[:60]: _clip(item, rows, limit)
            for key, item in list(value.items())[:40]
            # A view is how to draw the answer, not part of it.
            if key != "view"
        }

    return str(value)[:limit]


def _fit(build):
    """
    Call `build(rows)` with fewer rows until the answer is small enough
    for the model's context, ending with the summary alone.
    """
    for rows in (MAX_ROWS, 5, 2):
        answer = build(rows)

        if len(json.dumps(answer, default=str).encode("utf-8")) <= MAX_ANSWER_CHARACTERS:
            return answer

    answer = build(0)

    return {
        "summary": answer["summary"],
        "note": answer["note"],
        "left_out": "The details were too long to include.",
    }


class WorldView:
    """
    Asks a God's Eye View program on this machine.

    `url` must be an address on this machine; anything else is refused
    when the object is made, so a setting cannot point Freya's
    questions at another computer.
    """

    def __init__(
        self,
        url: str,
        open_url: Optional[Callable[[str], object]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        url = (url or "").rstrip("/")

        if not _is_local(url):
            raise ValueError(
                "The world view address must be on this computer."
            )

        self.url = url
        self._open_url = open_url or webbrowser.open
        self._clock = clock
        self._running = False
        self._checked_at: Optional[float] = None
        # Other programs' proxy settings must not carry a local call
        # off this machine, and neither must a redirect.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirects(),
        )

    # Talking to the program.

    def _post(self, payload: dict, timeout: float) -> dict:
        request = urllib.request.Request(
            self.url + "/mcp",
            data=json.dumps(payload).encode("utf-8"),
            # One JSON document is asked for and read; a stream of
            # events is not, and would be reported as unreadable.
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with self._opener.open(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError) as exc:
            raise WorldViewError(NOT_RUNNING_MESSAGE) from exc

        if len(body) > MAX_RESPONSE_BYTES:
            raise WorldViewError(
                "God's Eye View returned too much data."
            )

        try:
            reply = json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise WorldViewError(
                "God's Eye View returned data that could not be read."
            ) from exc

        if not isinstance(reply, dict):
            raise WorldViewError(
                "God's Eye View returned data that could not be read."
            )

        return reply

    def is_running(self) -> bool:
        """
        Whether the program answers, asked at most every few seconds.
        """
        now = self._clock()

        if (
            self._checked_at is not None
            and now - self._checked_at < RUNNING_CHECK_SECONDS
        ):
            return self._running

        try:
            reply = self._post(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                RUNNING_CHECK_TIMEOUT_SECONDS,
            )
            self._running = isinstance(
                (reply.get("result") or {}).get("tools"), list
            )
        except WorldViewError:
            self._running = False

        self._checked_at = now

        return self._running

    def _ask(self, tool: str, arguments: dict) -> dict:
        """
        Run one of the program's tools and return its sentence and its
        data, cut down to what fits a reply.
        """
        reply = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            REQUEST_TIMEOUT_SECONDS,
        )
        result = reply.get("result")

        if not isinstance(result, dict):
            raise WorldViewError(
                "God's Eye View could not answer that."
            )

        texts = [
            part.get("text", "")
            for part in result.get("content") or []
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        summary = (texts[0] if texts else "")[:MAX_TEXT_CHARACTERS * 2]

        if result.get("isError"):
            raise WorldViewError(self._reason(summary))

        data = result.get("structuredContent")

        if not isinstance(data, dict):
            try:
                data = json.loads(texts[1]) if len(texts) > 1 else {}
            except ValueError:
                data = {}
            # The program's text is untrusted: valid JSON that is not
            # an object is treated as no details, not as a crash.
            if not isinstance(data, dict):
                data = {}

        return {"summary": summary, "data": data}

    @staticmethod
    def _reason(text: str) -> str:
        """
        The program's refusal, in words for the user. A missing key is
        recognised by the program's own form of words (`X_API_KEY not
        set`, "API key"), not by the word "key" alone, which a place
        name can hold. Anything else is passed on, cut short and with
        control characters removed, because it reaches the model and
        the audit log's error summary.
        """
        if _MISSING_KEY.search(text):
            return (
                "That information needs a key that has not been added "
                "to God's Eye View."
            )

        plain = "".join(
            c if c.isprintable() else " " for c in text
        ).strip()[:120]

        return (
            "God's Eye View could not answer that: "
            + (plain or "no reason was given")
        )

    @staticmethod
    def _answer(found: dict) -> dict:
        data = found["data"]

        def build(rows):
            answer = {"summary": found["summary"], "note": SOURCE_NOTE}
            listed = data.get("rows")

            if isinstance(listed, list):
                answer["shown"] = min(len(listed), rows)
                answer["total"] = data.get("total", len(listed))
                answer["rows"] = _clip(listed, rows)
            else:
                answer["details"] = _clip(data, rows)

            return answer

        return _fit(build)

    @staticmethod
    def _place(text) -> dict:
        place = (text or "").strip()[:MAX_PLACE_CHARACTERS]

        if not place:
            raise WorldViewError("Say which place to look at.")

        return {"place": place}

    # What Freya can ask. Each takes and returns plain text and numbers.

    def overview(self, place: str) -> dict:
        return self._answer(
            self._ask("situation_brief", {"area": self._place(place)})
        )

    def aircraft(self, place: str) -> dict:
        return self._answer(
            self._ask(
                "aircraft_in_area",
                {"area": self._place(place), "limit": MAX_ROWS},
            )
        )

    def ships(self, place: str) -> dict:
        return self._answer(
            self._ask(
                "vessels_in_area",
                {"area": self._place(place), "limit": MAX_ROWS},
            )
        )

    def satellite_pass(self, place: str, satellite: str = "") -> dict:
        arguments = {"location": self._place(place)}

        if (satellite or "").strip():
            arguments["satellite"] = satellite.strip()[:80]

        return self._answer(self._ask("next_satellite_pass", arguments))

    def earthquakes(self, place: str = "") -> dict:
        arguments = {"limit": MAX_ROWS}

        if (place or "").strip():
            arguments["area"] = self._place(place)

        return self._answer(self._ask("get_earthquakes", arguments))

    def fires(self, place: str) -> dict:
        return self._answer(
            self._ask(
                "get_active_fires",
                {"area": self._place(place), "limit": MAX_ROWS},
            )
        )

    def storms(self) -> dict:
        return self._answer(self._ask("get_cyclones", {}))

    def launches(self) -> dict:
        return self._answer(
            self._ask("get_recent_launches", {"limit": MAX_ROWS})
        )

    def cameras(self, place: str) -> dict:
        """
        Where public cameras are. It reports locations and names only;
        Freya does not look through them.
        """
        return self._answer(
            self._ask(
                "find_cctv_cameras",
                {"area": self._place(place), "limit": MAX_ROWS},
            )
        )

    def military(self, place: str) -> dict:
        return self._answer(
            self._ask(
                "military_awareness", {"location": self._place(place)}
            )
        )

    def show(self, place: str, layers: str = "") -> dict:
        """
        Open the globe at a place in the user's browser. This changes
        what is on screen and nothing else.
        """
        wanted = [
            name
            for name in (
                part.strip().lower()
                for part in (layers or "").replace(";", ",").split(",")
            )
            if name in LAYERS
        ]
        arguments = {"area": self._place(place)}

        if wanted:
            arguments["layers"] = [LAYERS[name] for name in wanted]

        found = self._ask("show_in_gods_eye_view", arguments)
        link = ((found["data"].get("view") or {}).get("url")) or found[
            "data"
        ].get("url")

        # Only an address of the program itself is ever opened.
        if not isinstance(link, str) or not link.startswith(self.url + "/"):
            raise WorldViewError(
                "God's Eye View did not give a view to open."
            )

        self._open_url(link)

        return {
            "summary": "The globe was opened at " + arguments["area"]["place"] + ".",
            "layers": wanted,
        }


def create_world_view_provider(view: WorldView) -> ProviderDefinition:
    """
    The provider is healthy once it is set up. Whether the program is
    running changes from minute to minute, so that is asked each time
    the tools are offered (see `ControlPlane.register_world_view`).
    """
    return ProviderDefinition(
        provider_id=WORLD_VIEW_PROVIDER,
        name="Live world data from God's Eye View on this machine",
        capabilities=list(WORLD_VIEW_CAPABILITIES),
        start_handler=lambda: None,
        health_handler=lambda: True,
    )


def register_world_view_capabilities(registry, view: WorldView) -> None:
    place = {
        "description": (
            "The place, as a name: a town, city, region, country, "
            "airport or body of water."
        ),
        "max_length": MAX_PLACE_CHARACTERS,
    }
    optional_place = dict(place, optional=True)

    def add(name, handler, description, parameters=None):
        metadata = {MODEL_TOOL: True}

        if parameters:
            metadata[TOOL_PARAMETERS] = parameters

        registry.register(
            CapabilityDefinition(
                name=name,
                description=description,
                provider=WORLD_VIEW_PROVIDER,
                handler=handler,
                category="world",
                metadata=metadata,
            )
        )

    add(
        WORLD_OVERVIEW,
        view.overview,
        "Returns what is happening around one place right now from "
        "live public data: weather, recent earthquakes, fires, "
        "aircraft overhead, ships and storms. Use it for a general "
        "question about a place; use the narrower world tools when "
        "the user asks about one of these only.",
        {"place": place},
    )
    add(
        WORLD_AIRCRAFT,
        view.aircraft,
        "Returns the aircraft reported over one place right now, "
        f"nearest first, up to {MAX_ROWS}: callsign, type, height and "
        "speed. Use it when the user asks what is flying over "
        "somewhere.",
        {"place": place},
    )
    add(
        WORLD_SHIPS,
        view.ships,
        "Returns the ships reported near one place right now, nearest "
        f"first, up to {MAX_ROWS}: name, type, speed and destination. "
        "It needs a key added to God's Eye View and says so when "
        "there is none.",
        {"place": place},
    )
    add(
        WORLD_SATELLITE_PASS,
        view.satellite_pass,
        "Returns when a satellite next rises over one place: rise, "
        "peak and set times in UTC, how high it gets and whether it "
        "can be seen. Give a satellite's name, or leave it out for the "
        "International Space Station.",
        {
            "place": place,
            "satellite": {
                "description": (
                    "The satellite's name, such as ISS or Hubble. "
                    "Leave out for the International Space Station."
                ),
                "max_length": 80,
                "optional": True,
            },
        },
    )
    add(
        WORLD_EARTHQUAKES,
        view.earthquakes,
        "Returns earthquakes of magnitude 2.5 or more from the last "
        f"24 hours, strongest first, up to {MAX_ROWS}, for the whole "
        "world or near one place.",
        {"place": optional_place},
    )
    add(
        WORLD_FIRES,
        view.fires,
        "Returns fires seen by satellite near one place in the last "
        "24 hours, most intense first. It needs a key added to God's "
        "Eye View and says so when there is none.",
        {"place": place},
    )
    add(
        WORLD_STORMS,
        view.storms,
        "Returns the tropical storms and hurricanes active now, from "
        "the US hurricane centres, with position, strength and "
        "movement.",
    )
    add(
        WORLD_LAUNCHES,
        view.launches,
        "Returns rocket launches to orbit from the last 30 days, "
        f"newest first, up to {MAX_ROWS}: provider, rocket, pad and "
        "outcome.",
    )
    add(
        WORLD_CAMERAS,
        view.cameras,
        "Returns where public traffic and city cameras are near one "
        "place: how many, and the names and positions of the nearest. "
        "It gives locations only; you cannot see through them.",
        {"place": place},
    )
    add(
        WORLD_MILITARY,
        view.military,
        "Returns military aircraft reported around one place right "
        "now and the military sites mapped nearby, from public data.",
        {"place": place},
    )
    add(
        WORLD_SHOW,
        view.show,
        "Opens the 3D globe at one place on the user's screen, with "
        "the layers asked for. Use it when the user asks to see or be "
        "shown somewhere on the globe. It changes only what is on "
        "screen. To answer a question from the data, use the other "
        "world tools instead.",
        {
            "place": place,
            "layers": {
                "description": (
                    "Layers to turn on, separated by commas, from: "
                    + ", ".join(sorted(LAYERS))
                    + ". Leave out for the plain globe."
                ),
                "max_length": 120,
                "optional": True,
            },
        },
    )
