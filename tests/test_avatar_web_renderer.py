import http.client
import re
from importlib import resources
from urllib.parse import urlparse

import pytest

from freya.control_plane import build_local_control_plane
from freya.spatial_avatar.appearance import APPEARANCES
from freya.spatial_avatar.config import AvatarConfigStore
from freya.spatial_avatar.geometry import Rect, single_display_layout
from freya.spatial_avatar.host import AvatarHost
from freya.spatial_avatar.library import AvatarLibrary
from freya.spatial_avatar.renderers import RENDERERS, choose_renderer, load_renderer
from freya.spatial_avatar.renderers import web
from freya.spatial_avatar.renderers.web import (
    MODEL_VARIABLE,
    PageServer,
    WebRenderer,
    content_security_policy,
    page_state,
)
from freya.spatial_avatar.service import AvatarService
from test_avatar_library import vrm1


@pytest.fixture
def served(tmp_path):
    model = tmp_path / "model.vrm"
    model.write_bytes(b"glTF" + b"\x00" * 100)
    vendor = tmp_path / "vendor"
    (vendor / "three").mkdir(parents=True)
    (vendor / "three" / "three.module.js").write_text("export {};", encoding="utf-8")
    (vendor / "README.txt").write_text("notes", encoding="utf-8")
    (tmp_path / "secret.js").write_text("secret", encoding="utf-8")

    server = PageServer(model, vendor_dir=vendor)
    address = server.start()
    yield server, urlparse(address)
    server.stop()


def fetch(address, path, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", address.port, timeout=10)
    connection.request("GET", path, headers={"Host": host or address.netloc})
    response = connection.getresponse()
    body = response.read()
    connection.close()

    return response, body


def test_the_page_its_libraries_and_the_model_are_served(served):
    server, address = served
    root = f"/{server.key}"

    page, body = fetch(address, address.path)
    assert page.status == 200 and b'id="avatar"' in body
    assert page.getheader("Content-Type").startswith("text/html")

    script, _ = fetch(address, f"{root}/overlay.js")
    assert script.status == 200
    assert script.getheader("Content-Type").startswith("text/javascript")
    assert script.getheader("X-Content-Type-Options") == "nosniff"

    library, body = fetch(address, f"{root}/vendor/three/three.module.js")
    assert library.status == 200 and body == b"export {};"

    model, body = fetch(address, f"{root}/model.vrm")
    assert model.status == 200 and body.startswith(b"glTF")
    assert model.getheader("Content-Type") == "model/gltf-binary"


def test_nothing_else_is_served(served):
    server, address = served
    root = f"/{server.key}"

    for path in (
        "/overlay.html",                       # no key
        "/wrong-key/overlay.html",
        f"{root}/",
        f"{root}/web.py",
        f"{root}/vendor/README.txt",           # not a script
        f"{root}/vendor/../secret.js",         # outside the library folder
        f"{root}/vendor/..%2fsecret.js",
        f"{root}/vendor/missing.js",
        f"{root}/../{server.key}/model.vrm",
    ):
        assert fetch(address, path)[0].status == 404, path

    # Another name for this computer is not accepted either.
    assert fetch(address, address.path, host=f"localhost:{address.port}")[0].status == 404


def test_the_page_may_only_load_its_own_files(served):
    server, address = served
    policy = fetch(address, address.path)[0].getheader("Content-Security-Policy")

    assert "default-src 'none'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert re.search(r"script-src 'self' 'sha256-[A-Za-z0-9+/=]+'", policy)
    # A different import map would not be allowed by the same policy.
    assert policy != content_security_policy('<script type="importmap">{}</script>')


def test_the_page_files_exist_and_only_draw():
    folder = resources.files("freya.spatial_avatar") / "renderers" / "web_page"
    page = (folder / "overlay.html").read_text(encoding="utf-8")
    script = (folder / "overlay.js").read_text(encoding="utf-8")

    assert page.count("<script") == 2 and "importmap" in page
    assert 'src="overlay.js"' in page

    for name in ("innerHTML", "outerHTML", "document.write", "eval(", "fetch(",
                 "XMLHttpRequest", "WebSocket", "pywebview"):
        assert name not in script, name


def frame(**changes):
    values = {
        "visible": True,
        "rect": Rect(100, 200, 300, 700),
        "appearance": APPEARANCES["holographic"],
        "expression": "happy",
        "speaking": True,
        "speech_level": 0.456,
        "pointer": None,
        "wave": None,
    }
    values.update(changes)

    return values


def test_the_page_is_told_only_what_it_needs():
    state = page_state(frame(), 30.0)

    assert state == {
        "expression": "happy",
        "speaking": True,
        "level": 0.46,
        "appearance": "holographic",
        "fps": 30,
        "look": None,
        "wave": False,
    }
    assert page_state(frame(wave=0.3), 30.0)["wave"] is True


def test_pointing_reaches_the_page_as_a_direction_not_a_place():
    # Her face is the circle at the top of her space: centre (200, 300).
    up_right = page_state(
        frame(pointer={"from": (0, 0), "to": (900, 100), "amount": 0.5, "target": None}), 30.0
    )["look"]

    assert up_right == {"x": 700.0, "y": 200.0, "amount": 0.5}

    down_left = page_state(
        frame(pointer={"from": (0, 0), "to": (50, 800), "amount": 1, "target": None}), 30.0
    )["look"]

    assert down_left["x"] < 0 and down_left["y"] < 0


def test_the_3d_renderer_is_only_used_when_asked_for(tmp_path, monkeypatch):
    pytest.importorskip("tkinter", reason="the placeholder renderer needs tkinter, which this Python lacks")
    model = tmp_path / "model.vrm"
    model.write_bytes(b"x")
    monkeypatch.setenv(MODEL_VARIABLE, str(model))
    monkeypatch.setattr(web.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(web, "on_windows", lambda: True)

    assert "web" in RENDERERS
    assert load_renderer("web") is WebRenderer
    assert WebRenderer.available() is None

    # Even though it could run, it is never picked unasked.
    assert choose_renderer(None)[0] == "tk-placeholder"
    assert choose_renderer("tk-placeholder")[0] == "tk-placeholder"
    assert choose_renderer("web")[0] == "web"


def test_the_3d_renderer_says_why_it_cannot_run(tmp_path, monkeypatch):
    pytest.importorskip("tkinter", reason="the placeholder renderer needs tkinter, which this Python lacks")
    monkeypatch.setattr(web, "on_windows", lambda: True)
    monkeypatch.setattr(web.importlib.util, "find_spec", lambda name: object())
    monkeypatch.delenv(MODEL_VARIABLE, raising=False)
    assert "No avatar model" in WebRenderer.available()

    monkeypatch.setenv(MODEL_VARIABLE, str(tmp_path / "gone.vrm"))
    assert "No avatar model" in WebRenderer.available()

    model = tmp_path / "model.vrm"
    model.write_bytes(b"x")
    monkeypatch.setenv(MODEL_VARIABLE, str(model))
    monkeypatch.setattr(web.importlib.util, "find_spec", lambda name: None)
    assert "pywebview" in WebRenderer.available()

    monkeypatch.setattr(web, "on_windows", lambda: False)
    assert "Windows" in WebRenderer.available()


# -- which renderer Freya asks for --------------------------------------------


@pytest.fixture
def plane(tmp_path, fake_ollama):
    plane = build_local_control_plane(tmp_path / "control.db", None)
    yield plane
    plane.close()


@pytest.fixture
def service(plane, tmp_path, monkeypatch):
    monkeypatch.setattr(web.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(web, "on_windows", lambda: True)
    models = tmp_path / "avatar_models"
    models.mkdir()
    (models / "one.vrm").write_bytes(vrm1())

    service = AvatarService(
        plane.broker,
        "local-owner",
        store=AvatarConfigStore(tmp_path / "avatar.json"),
        host=AvatarHost("null", log_path=tmp_path / "overlay.log"),
        probe=single_display_layout,
        library=AvatarLibrary(models, tmp_path / "avatar_library.json"),
    )
    plane.register_avatar(service)
    yield service
    service.stop()


def save_avatar(service):
    model = service.handle({"action": "library-scan"})["state"]["library"]["models"][0]
    state = service.handle(
        {"action": "library-save", "value": {"name": "Mine", "model": model["sha256"]}}
    )["state"]

    return state["library"]["profiles"][0]["id"]


def test_marking_an_avatar_as_hers_asks_for_the_3d_renderer(service, tmp_path):
    pytest.importorskip("tkinter", reason="the placeholder renderer needs tkinter, which this Python lacks")
    assert service._wanted_renderer() == ("null", {})

    avatar = save_avatar(service)
    assert service._wanted_renderer() == ("null", {})

    assert service.handle({"action": "library-use", "value": {"avatar": avatar}})["error"] is None
    renderer, environment = service._wanted_renderer()
    assert renderer == "web"
    assert environment == {MODEL_VARIABLE: str(tmp_path / "avatar_models" / "one.vrm")}

    # Going back to the placeholder, or losing the file, undoes it.
    service.handle({"action": "library-use", "value": {"avatar": None}})
    assert service._wanted_renderer() == ("null", {})

    service.handle({"action": "library-use", "value": {"avatar": avatar}})
    (tmp_path / "avatar_models" / "one.vrm").unlink()
    service.handle({"action": "library-scan"})
    assert service._wanted_renderer() == ("null", {})


def test_the_usual_renderer_is_kept_when_the_3d_one_cannot_run(service, monkeypatch):
    avatar = save_avatar(service)
    service.handle({"action": "library-use", "value": {"avatar": avatar}})
    monkeypatch.setattr(web.importlib.util, "find_spec", lambda name: None)

    assert service._wanted_renderer() == ("null", {})


def test_a_host_handed_to_the_service_keeps_its_renderer(service):
    avatar = save_avatar(service)
    service.handle({"action": "library-use", "value": {"avatar": avatar}})

    assert service.handle({"action": "show"})["error"] is None
    assert service.describe()["renderer"] == "null"


def test_the_overlay_is_given_its_extra_settings(tmp_path):
    host = AvatarHost("null", log_path=tmp_path / "overlay.log", environment={"A": "1"})
    assert host.environment == {"A": "1"}

    host.use("tk-placeholder", {"B": "2"})
    assert (host.renderer, host.environment) == ("tk-placeholder", {"B": "2"})

    host.use("null")
    assert host.environment == {}
