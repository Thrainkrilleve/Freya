import base64
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from freya.spatial_avatar import commands as c
from freya.spatial_avatar.renderers.tk_placeholder import (
    GWL_EXSTYLE,
    HANDLE_SIZE,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TRANSPARENT,
    TkPlaceholderRenderer,
)


def on_windows() -> bool:
    """
    Whether this is the platform the web view window is built for. A
    function, so a test can stand in for it without changing `os.name`
    for the whole process (which switches `pathlib` to Windows paths).
    """
    return os.name == "nt"


# The model file to draw, given by whoever starts the overlay.
MODEL_VARIABLE = "FREYA_AVATAR_MODEL"

PAGE_DIR = Path(__file__).resolve().parent / "web_page"
# The 3D libraries the window's preview already uses, served unchanged.
VENDOR_DIR = Path(__file__).resolve().parents[2] / "gateway" / "static" / "vendor"

PAGE_FILES = {
    "overlay.html": "text/html; charset=utf-8",
    "overlay.css": "text/css; charset=utf-8",
    "overlay.js": "text/javascript; charset=utf-8",
}

# What the tile shows before the page has drawn anything.
BACKDROP = "#141821"

# How far the handle must be dragged before it counts as moving her
# rather than as a click.
DRAG_START_PIXELS = 5
# How often the page is told about her, at most.
TELL_INTERVAL_SECONDS = 1 / 30
PAGE_WAIT_SECONDS = 30

OFF_SCREEN = -30000

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_BORDER_COLOR = 34
BORDER_COLOUR = 0x00FF9C7C   # blue-red order: the window's accent, #7c9cff
HWND_NOTOPMOST = -2


def content_security_policy(page: str) -> str:
    """
    The page may load only its own files from this server. Its one
    inline script, the import map saying where its library files are,
    is allowed by the hash of its exact text.
    """
    found = re.search(r'<script type="importmap">(.*?)</script>', page, re.S)
    digest = base64.b64encode(
        hashlib.sha256((found.group(1) if found else "").encode("utf-8")).digest()
    ).decode("ascii")

    return (
        "default-src 'none'; "
        f"script-src 'self' 'sha256-{digest}'; "
        "style-src 'self'; "
        "img-src 'self' blob:; "
        "connect-src 'self' blob:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


class PageServer:
    """
    Serves the overlay's page, its libraries, and the one model file to
    the overlay's own window, on this computer only.

    Every address begins with a key made up when the overlay starts, so
    another program on the computer cannot fetch the model by guessing
    the port. Only the listed page files, script files under the
    library folder, and the model are ever served.
    """

    def __init__(self, model_path, page_dir=PAGE_DIR, vendor_dir=VENDOR_DIR):
        self.model_path = Path(model_path)
        self.page_dir = Path(page_dir)
        self.vendor_dir = Path(vendor_dir).resolve()
        self.key = secrets.token_urlsafe(24)
        self._server = None

    def find(self, path: str):
        """
        The file an address refers to, as (path, media type), or None.
        """
        prefix = f"/{self.key}/"

        if not path.startswith(prefix):
            return None

        name = path[len(prefix):].split("?", 1)[0]

        if name in PAGE_FILES:
            return self.page_dir / name, PAGE_FILES[name]

        if name == "model.vrm":
            return self.model_path, "model/gltf-binary"

        if name.startswith("vendor/") and name.endswith(".js"):
            target = (self.vendor_dir / name[len("vendor/"):]).resolve()

            if self.vendor_dir in target.parents and target.is_file():
                return target, "text/javascript; charset=utf-8"

        return None

    def start(self) -> str:
        """
        Start serving and return the page's address.
        """
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                found = owner.find(self.path)
                host = self.headers.get("Host", "")

                if found is None or host != f"127.0.0.1:{self.server.server_port}":
                    self.send_error(404)
                    return

                path, media_type = found

                try:
                    size = path.stat().st_size
                    source = open(path, "rb")
                except OSError:
                    self.send_error(404)
                    return

                with source:
                    self.send_response(200)
                    self.send_header("Content-Type", media_type)
                    self.send_header("Content-Length", str(size))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")

                    if path.name == "overlay.html":
                        source.seek(0)
                        self.send_header(
                            "Content-Security-Policy",
                            content_security_policy(source.read().decode("utf-8")),
                        )
                        source.seek(0)

                    self.end_headers()

                    try:
                        shutil.copyfileobj(source, self.wfile)
                    except OSError:
                        pass

            def log_message(self, *arguments):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(
            target=self._server.serve_forever, name="AvatarPage", daemon=True
        ).start()

        return f"http://127.0.0.1:{self._server.server_port}/{self.key}/overlay.html"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def page_state(frame: dict, fps: float) -> dict:
    """
    What the page is told about one frame: only what it needs to draw.
    Positions on screen stay here; the page gets a direction to look
    in, not a place.
    """
    pointer = frame["pointer"]
    look = None

    if pointer is not None:
        rect = frame["rect"]
        # From her face toward the target, with up as positive.
        x = pointer["to"][0] - (rect.left + rect.width / 2)
        y = (rect.top + rect.width / 2) - pointer["to"][1]
        look = {"x": round(x, 1), "y": round(y, 1), "amount": round(pointer["amount"], 3)}

    return {
        "expression": frame["expression"],
        "speaking": bool(frame["speaking"]),
        "level": round(float(frame["speech_level"] or 0), 2),
        "appearance": frame["appearance"].id,
        "fps": round(fps),
        "look": look,
        "wave": frame["wave"] is not None,
    }


class WebRenderer(TkPlaceholderRenderer):
    """
    Draws the face of the owner's chosen 3D avatar model on the
    desktop, in a small tile with rounded corners.

    It is the placeholder renderer with her body replaced: the pointing
    line, the handle beside her, its menu, and everything about where
    she is and how big she is are unchanged. Her face is drawn by a
    page (web_page/) using the same 3D libraries as the window's
    preview, in a square tile at the top of the space the figure
    would take. She is moved and resized by the handle.

    The tile has a plain backdrop because a web view window cannot be
    see-through on Windows, or cut to another shape: both were tried.
    The page's empty areas come out solid, and the web view draws past
    a shape cut out of its window. Showing her whole body standing on the
    desktop needs a renderer that can.

    The page is served by this process to its own window only, and is
    told what to draw; it sends nothing back.
    """

    id = "web"
    name = "3D face"
    cost = 2
    # Used only when a model has been chosen and it is asked for by name.
    on_request = True
    appearances = ("realistic", "holographic", "lightweight")

    @classmethod
    def available(cls):
        if not on_windows():
            return "The 3D avatar window is only built for Windows so far."

        problem = super().available()

        if problem:
            return problem

        if importlib.util.find_spec("webview") is None:
            return "The web view library (pywebview) is not installed."

        model = os.environ.get(MODEL_VARIABLE, "")

        if not model or not Path(model).is_file():
            return "No avatar model has been chosen."

        return None

    # -- starting ------------------------------------------------------------------

    def run(self, session) -> None:
        import webview

        self._server = PageServer(os.environ[MODEL_VARIABLE])
        self._web = webview.create_window(
            "Freya",
            self._server.start(),
            width=200,
            height=200,
            # Small enough never to stop her being made small.
            min_size=(16, 16),
            frameless=True,
            background_color=BACKDROP,
            on_top=True,
            easy_drag=False,
            shadow=False,
            focus=False,
            # Off every display until there is something to show: a
            # web view will not start in a window that is hidden.
            x=OFF_SCREEN,
            y=OFF_SCREEN,
        )
        self._web_handle = None
        self._web_shown = False
        self._web_place = None
        self._web_style = None
        self._told = None
        self._told_at = 0.0
        self._page_ready = False
        self._page_deadline = time.perf_counter() + PAGE_WAIT_SECONDS

        try:
            # The web view owns this thread; the rest of the overlay
            # (the placeholder's own loop) runs beside it.
            webview.start(self._run_beside, (session,), private_mode=True)
        finally:
            self._server.stop()

    def _run_beside(self, session) -> None:
        try:
            self._web.events.shown.wait(15)
            self._prepare_web_window()
            super().run(session)
        except Exception as exc:
            session.emit(c.event(c.ERROR, text=f"The 3D avatar failed: {exc}"))
        finally:
            session.running = False
            self._web.destroy()

    def _prepare_web_window(self) -> None:
        import ctypes

        # Only the window's styles are changed, through Windows itself.
        # Changing the form's own properties here would make it build a
        # new window, and the web view inside would fail to start.
        self._web_handle = self._web.native.Handle.ToInt64()

        user32 = ctypes.windll.user32
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._user32 = user32

        # Windows rounds the window's corners and draws its border. A
        # web view ignores a window's own shape, so a circle cut out of
        # it would still show its square corners; this does not.
        try:
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
            ]

            for attribute, value in (
                (DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND),
                (DWMWA_BORDER_COLOR, BORDER_COLOUR),
            ):
                value = ctypes.c_uint(value)
                dwm.DwmSetWindowAttribute(
                    self._web_handle, attribute, ctypes.byref(value), ctypes.sizeof(value)
                )
        except (AttributeError, OSError):
            pass

        self._base_style = (
            user32.GetWindowLongW(self._web_handle, GWL_EXSTYLE)
            | WS_EX_TOOLWINDOW
            | WS_EX_NOACTIVATE
        )
        self._style_web(False)

    def _style_web(self, click_through: bool) -> None:
        if click_through == self._web_style:
            return

        style = self._base_style

        if click_through:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT

        self._user32.SetWindowLongW(self._web_handle, GWL_EXSTYLE, style)
        self._web_style = click_through

    def _prepare(self) -> None:
        # Drag the handle to move her, click it for the menu, scroll
        # over it to resize her.
        self._moved = False
        self._handle_canvas.bind("<ButtonPress-1>", self._handle_press)
        self._handle_canvas.bind("<B1-Motion>", self._handle_move)
        self._handle_canvas.bind("<ButtonRelease-1>", self._handle_release)
        self._handle_canvas.bind("<MouseWheel>", self._wheel)

    def _configured(self, config) -> None:
        for window in (self._pointer, self._handle):
            window.attributes("-topmost", config.always_on_top)

        self._style_web(bool(config.click_through))
        self._web_place = None
        self._drawn = None

    # -- the handle ---------------------------------------------------------------

    def _handle_press(self, event) -> None:
        self._moved = False
        self._press = (event.x_root, event.y_root)
        self._drag_start(event)

    def _handle_move(self, event) -> None:
        if not self._moved:
            far = abs(event.x_root - self._press[0]) + abs(event.y_root - self._press[1])

            if far < DRAG_START_PIXELS:
                return

            self._moved = True

        self._drag_move(event)

    def _handle_release(self, event) -> None:
        if self._moved:
            self._drag_end(event)
        else:
            self._drag = None
            self._open_menu(event)

    # -- drawing ------------------------------------------------------------------

    def _page_is_ready(self) -> bool:
        if self._page_ready:
            return True

        try:
            state = json.loads(
                self._web.evaluate_js(
                    "JSON.stringify(window.freya ? "
                    "{ready: window.freya.ready, error: window.freya.error} : {})"
                )
                or "{}"
            )
        except Exception:
            state = {}

        if state.get("error"):
            raise RuntimeError(f"the model could not be shown ({str(state['error'])[:200]})")

        if state.get("ready"):
            self._page_ready = True
        elif time.perf_counter() > self._page_deadline:
            raise RuntimeError("the model took too long to load")

        return self._page_ready

    def _show_web(self, shown: bool) -> None:
        if shown != self._web_shown:
            if not shown:
                self._user32.SetWindowPos(
                    self._web_handle, None, OFF_SCREEN, OFF_SCREEN, 0, 0,
                    SWP_NOACTIVATE | SWP_NOSIZE | SWP_NOZORDER,
                )

            self._web_shown = shown
            self._web_place = None

    def _draw(self, seconds: float) -> None:
        frame = self._model.frame()

        if not frame["visible"]:
            if self._drawn != "hidden":
                for window in (self._pointer, self._handle):
                    window.withdraw()
                self._show_web(False)
                self._drawn = "hidden"
            return

        if self._drawn == "hidden" or self._drawn is None:
            self._handle.deiconify()

        rect = frame["rect"]
        width = max(int(round(rect.width)), 8)
        height = max(int(round(rect.height)), 8)
        left, top = int(round(rect.left)), int(round(rect.top))
        look = frame["appearance"]

        # Her face is a square as wide as the figure would be, at the
        # top of the figure's space. It appears once her model has
        # loaded; until then only the handle shows, so she can still
        # be moved or dismissed.
        side = min(width, height)

        if self._page_is_ready():
            place = (left, top, side, self._model.config.always_on_top)

            if place != self._web_place:
                self._user32.SetWindowPos(
                    self._web_handle,
                    HWND_TOPMOST if place[3] else HWND_NOTOPMOST,
                    left, top, side, side,
                    SWP_NOACTIVATE,
                )
                self._web_place = place

            self._show_web(True)

            now = time.perf_counter()
            state = page_state(frame, 1.0 / self._session.frame_interval())

            if state != self._told and now - self._told_at >= TELL_INTERVAL_SECONDS:
                self._web.run_js("window.freya.set(" + json.dumps(state) + ")")
                self._told = state
                self._told_at = now

        display = self._model.layout.display_for_rect(rect)
        handle_left = min(left + side - HANDLE_SIZE // 2, int(display.bounds.right) - HANDLE_SIZE)
        handle_top = max(top - HANDLE_SIZE // 3, int(display.bounds.top))
        self._handle.geometry(
            f"{HANDLE_SIZE}x{HANDLE_SIZE}{handle_left:+d}{handle_top:+d}"
        )
        self._draw_handle(look)
        self._draw_pointer(frame, look)
        self._drawn = "shown"
