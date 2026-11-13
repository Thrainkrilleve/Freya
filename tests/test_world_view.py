"""
Live world data from a God's Eye View program on this machine. The
program is stood in for by a small server on the loopback address that
answers as it does; nothing here reaches the internet.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from freya.control_plane import build_local_control_plane
from freya.tools import world_view
from freya.tools.world_view import WorldView, WorldViewError


class FakeProgram:
    """Answers `/mcp` the way God's Eye View does and records each call."""

    def __init__(self):
        self.calls = []
        self.answers = {}
        self.errors = {}
        self.accepts = []
        self.redirect_to = None
        self.raw_reply = None
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        program = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                asked = json.loads(self.rfile.read(length))
                program.accepts.append(self.headers.get("Accept"))

                if program.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", program.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if program.raw_reply is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(program.raw_reply)))
                    self.end_headers()
                    self.wfile.write(program.raw_reply)
                    return

                if asked["method"] == "tools/list":
                    result = {"tools": [{"name": "get_earthquakes"}]}
                else:
                    name = asked["params"]["name"]
                    program.calls.append((name, asked["params"]["arguments"]))

                    if name in program.errors:
                        result = {
                            "isError": True,
                            "content": [{"type": "text", "text": program.errors[name]}],
                        }
                    else:
                        summary, data = program.answers.get(name, ("Nothing.", {}))
                        result = {
                            "content": [
                                {"type": "text", "text": summary},
                                {"type": "text", "text": json.dumps(data)},
                            ],
                            "structuredContent": data,
                        }

                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def program():
    fake = FakeProgram()
    yield fake
    fake.stop()


@pytest.fixture
def opened():
    return []


@pytest.fixture
def view(program, opened):
    return WorldView(program.url, open_url=opened.append)


def aircraft_rows(count):
    return [
        {"callsign": f"TEST{index}", "altitude_m": 9000 + index, "type": "B738"}
        for index in range(count)
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:4173",
        "https://localhost:4173",
        "http://192.168.1.20:4173",
        "http://localhost.evil.example:4173",
        "",
    ],
)
def test_only_an_address_on_this_machine_is_accepted(url):
    with pytest.raises(ValueError):
        WorldView(url)


def test_a_question_goes_to_the_program_with_only_the_place(program, view):
    program.answers["aircraft_in_area"] = (
        "3 aircraft over Testville.",
        {"view": {"url": "x"}, "total": 3, "rows": aircraft_rows(3)},
    )

    answer = view.aircraft("  Testville  ")

    assert program.calls == [
        ("aircraft_in_area", {"area": {"place": "Testville"}, "limit": 10})
    ]
    assert answer["summary"] == "3 aircraft over Testville."
    assert answer["total"] == 3 and answer["shown"] == 3
    assert answer["rows"][0]["callsign"] == "TEST0"
    assert "public data" in answer["note"]
    assert "view" not in json.dumps(answer)


def test_a_long_list_is_cut_to_what_fits(program, view):
    program.answers["aircraft_in_area"] = (
        "400 aircraft.",
        {"total": 400, "rows": aircraft_rows(400)},
    )

    answer = view.aircraft("Testville")

    assert len(answer["rows"]) == 10 and answer["total"] == 400


def test_an_answer_too_long_for_the_model_loses_rows_then_details(program, view):
    wordy = [{"text": "x" * 300, "more": ["y" * 300] * 10} for _ in range(10)]
    program.answers["situation_brief"] = ("A lot is happening.", {"sections": wordy})

    answer = view.overview("Testville")

    assert answer["summary"] == "A lot is happening."
    assert len(json.dumps(answer)) <= world_view.MAX_ANSWER_CHARACTERS


def test_every_answer_reaches_the_model_whole_through_the_context(program, view):
    # The context clips a tool result at 2,400 bytes; a clipped answer
    # is no longer valid JSON, so the view must stay under it.
    from freya.kernel.context import ContextAuthority

    program.answers["aircraft_in_area"] = (
        "400 aircraft.", {"total": 400, "rows": aircraft_rows(400)},
    )
    answer = view.aircraft("Testville")
    fitted = ContextAuthority().fit_tools([
        {"role": "user", "content": "What is flying over?"},
        {"role": "tool", "content": json.dumps(answer, default=str)},
    ])
    received = json.loads(fitted[-1]["content"])

    assert "excerpt" not in received
    assert received["_context"]["truncated"] is False


def test_details_that_are_not_an_object_are_treated_as_none(program, view):
    # The program's second text part is untrusted. Valid JSON that is
    # a list or a number must not crash the tool.
    program.answers["aircraft_in_area"] = ("Two aircraft.", [1, 2])

    answer = view.aircraft("Testville")

    assert answer["summary"] == "Two aircraft."


def test_a_place_is_required_and_bounded(program, view):
    with pytest.raises(WorldViewError, match="which place"):
        view.aircraft("   ")

    view.fires("p" * 500)

    assert len(program.calls[0][1]["area"]["place"]) == 200


def test_optional_arguments_are_left_out_when_empty(program, view):
    view.earthquakes()
    view.earthquakes("Japan")
    view.satellite_pass("Testville")
    view.satellite_pass("Testville", "Hubble")

    assert [arguments for _, arguments in program.calls] == [
        {"limit": 10},
        {"limit": 10, "area": {"place": "Japan"}},
        {"location": {"place": "Testville"}},
        {"location": {"place": "Testville"}, "satellite": "Hubble"},
    ]


def test_a_missing_key_is_said_plainly_without_naming_it(program, view):
    program.errors["vessels_in_area"] = "AISSTREAM_API_KEY not set"

    with pytest.raises(WorldViewError) as refusal:
        view.ships("Testville")

    assert "needs a key" in str(refusal.value)
    assert "AISSTREAM" not in str(refusal.value)


def test_another_refusal_is_passed_on_in_the_programs_words(program, view):
    program.errors["get_active_fires"] = "area is too large"

    with pytest.raises(WorldViewError, match="area is too large"):
        view.fires("Testville")


def test_a_place_called_key_is_not_mistaken_for_a_missing_key(program, view):
    program.errors["get_active_fires"] = "No fire data for Key West"

    with pytest.raises(WorldViewError, match="Key West"):
        view.fires("Testville")


def test_a_refusal_is_cut_short_and_cleaned_before_it_is_passed_on(program, view):
    program.errors["get_active_fires"] = "bad\x1b[31m request " + "x" * 500

    with pytest.raises(WorldViewError) as refusal:
        view.fires("Testville")

    assert "\x1b" not in str(refusal.value)
    assert len(str(refusal.value)) < 200


def test_a_program_that_is_not_running_is_said_plainly(program, opened):
    view = WorldView(program.url, open_url=opened.append)
    program.stop()

    with pytest.raises(WorldViewError, match="not running"):
        view.aircraft("Testville")


def test_a_redirect_to_another_computer_is_not_followed(program, view):
    elsewhere = FakeProgram()
    elsewhere.answers["aircraft_in_area"] = ("From somewhere else.", {"rows": []})
    program.redirect_to = elsewhere.url + "/mcp"

    try:
        with pytest.raises(WorldViewError, match="not running"):
            view.aircraft("Testville")

        assert elsewhere.calls == [] and elsewhere.accepts == []
        assert WorldView(program.url).is_running() is False
    finally:
        elsewhere.stop()


def test_one_json_answer_is_asked_for_and_a_stream_is_not_read(program, view):
    view.aircraft("Testville")

    assert program.accepts == ["application/json"]

    program.raw_reply = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'

    with pytest.raises(WorldViewError, match="could not be read"):
        view.aircraft("Testville")


def test_running_is_asked_again_only_after_a_while(program):
    now = [100.0]
    view = WorldView(program.url, clock=lambda: now[0])

    assert view.is_running() is True

    program.stop()
    now[0] += 5

    assert view.is_running() is True  # still believed

    now[0] += world_view.RUNNING_CHECK_SECONDS

    assert view.is_running() is False


def test_showing_opens_the_programs_own_address_with_its_layer_names(program, view, opened):
    link = program.url + "/#v=2&lat=1&lon=2"
    program.answers["show_in_gods_eye_view"] = ("Open it.", {"url": link, "view": {"url": link}})

    answer = view.show("Testville", "Flights, ships; nonsense")

    assert program.calls == [
        (
            "show_in_gods_eye_view",
            {"area": {"place": "Testville"}, "layers": ["flights", "ais-live-vessels"]},
        )
    ]
    assert opened == [link]
    assert answer == {
        "summary": "The globe was opened at Testville.",
        "layers": ["flights", "ships"],
    }


@pytest.mark.parametrize(
    "link",
    ["https://example.com/#v=2", "file:///C:/Windows/system32/calc.exe", None, 7],
)
def test_no_other_address_is_ever_opened(program, view, opened, link):
    program.answers["show_in_gods_eye_view"] = ("Open it.", {"url": link})

    with pytest.raises(WorldViewError):
        view.show("Testville")

    assert opened == []


def test_the_tools_do_not_exist_unless_the_owner_turned_them_on(tmp_path):
    plane = build_local_control_plane(tmp_path / "control.db", None)

    try:
        assert plane.capabilities.get(world_view.WORLD_AIRCRAFT) is None
        assert plane.providers.get(world_view.WORLD_VIEW_PROVIDER) is None
    finally:
        plane.close()


def test_when_on_they_are_offered_only_while_the_program_answers(tmp_path, program):
    now = [0.0]
    view = WorldView(program.url, clock=lambda: now[0])
    plane = build_local_control_plane(tmp_path / "control.db", None, world_view=view)

    try:
        offered = {d["function"]["name"] for d in plane.model_tools.definitions()}
        assert {"world_aircraft", "world_show", "world_overview"} <= offered

        program.stop()
        now[0] += world_view.RUNNING_CHECK_SECONDS + 1
        offered = {d["function"]["name"] for d in plane.model_tools.definitions()}

        assert not any(name.startswith("world_") for name in offered)
    finally:
        plane.close()


def test_a_call_goes_through_the_broker_and_is_recorded(tmp_path, program):
    program.answers["get_earthquakes"] = ("2 earthquakes.", {"total": 2, "rows": [{"m": 5}]})
    view = WorldView(program.url)
    plane = build_local_control_plane(tmp_path / "control.db", None, world_view=view)

    try:
        reply = json.loads(plane.model_tools.run("world_earthquakes", {"place": "Japan"}))

        assert reply["summary"] == "2 earthquakes."

        # What the model invents beyond the declared arguments is dropped.
        plane.model_tools.run("world_aircraft", {"place": "Testville", "limit": "9999"})
        assert program.calls[-1][1] == {"area": {"place": "Testville"}, "limit": 10}

        refused = json.loads(plane.model_tools.run("world_aircraft", {}))
        assert "error" in refused

        # Each call is in the audit log, with the place left out.
        rows = plane.store.execute(
            "SELECT * FROM audit_event WHERE capability = ?",
            (world_view.WORLD_EARTHQUAKES,),
        ).fetchall()
        assert rows
        assert "Japan" not in json.dumps([tuple(row) for row in rows], default=str)
    finally:
        plane.close()


def test_every_tool_says_what_it_returns_and_takes_only_text(tmp_path, program):
    view = WorldView(program.url)
    plane = build_local_control_plane(tmp_path / "control.db", None, world_view=view)

    try:
        for name in world_view.WORLD_VIEW_CAPABILITIES:
            capability = plane.capabilities.get(name)

            assert capability.provider == world_view.WORLD_VIEW_PROVIDER
            assert capability.destructive is False
            assert capability.description.startswith(("Returns", "Opens"))
    finally:
        plane.close()
