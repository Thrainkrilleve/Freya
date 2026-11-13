import base64
import hashlib
import http
import json
import queue
import re
import secrets
import socket
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from importlib import resources

from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import serve


DEFAULT_PORT = 8770

LOOPBACK = "127.0.0.1"

SESSION_COOKIE = "freya_session"

MAX_MESSAGE_BYTES = 64 * 1024
MAX_TEXT_LENGTH = 4000

# A recording for the Voice tab arrives as binary messages, each within
# MAX_MESSAGE_BYTES, and is held until the request that uses it. This
# allows a little over 30 seconds of 48 kHz mono 16-bit sound.
MAX_UPLOAD_BYTES = 3 * 1024 * 1024

# How long stopping waits for the turn in progress, and how many events
# may wait to be sent to one window before it is given up on.
STOP_TIMEOUT_SECONDS = 30.0
MAX_WAITING_EVENTS = 5000

JAVASCRIPT = "text/javascript; charset=utf-8"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    # The 3D preview of avatar models, and the libraries it uses.
    # Each file is listed: nothing else under static/ is served.
    "/avatar-view.js": ("avatar-view.js", JAVASCRIPT),
    "/vendor/three/three.module.js": ("vendor/three/three.module.js", JAVASCRIPT),
    "/vendor/three/three.core.js": ("vendor/three/three.core.js", JAVASCRIPT),
    "/vendor/three/addons/loaders/GLTFLoader.js": (
        "vendor/three/addons/loaders/GLTFLoader.js", JAVASCRIPT,
    ),
    "/vendor/three/addons/controls/OrbitControls.js": (
        "vendor/three/addons/controls/OrbitControls.js", JAVASCRIPT,
    ),
    "/vendor/three/addons/utils/BufferGeometryUtils.js": (
        "vendor/three/addons/utils/BufferGeometryUtils.js", JAVASCRIPT,
    ),
    "/vendor/three/addons/utils/SkeletonUtils.js": (
        "vendor/three/addons/utils/SkeletonUtils.js", JAVASCRIPT,
    ),
    "/vendor/three-vrm/three-vrm.module.min.js": (
        "vendor/three-vrm/three-vrm.module.min.js", JAVASCRIPT,
    ),
}

IMPORT_MAP = re.compile(
    r'<script type="importmap">(.*?)</script>', re.DOTALL
)


def _static(name: str) -> bytes:
    return (
        resources.files("freya.gateway")
        .joinpath("static", *name.split("/"))
        .read_bytes()
    )


def content_security_policy() -> str:
    """
    The page may only load its own files and talk to this server.

    Two narrow additions serve the 3D avatar preview. The page's one
    inline script is its import map (it only says where the page's
    own library files are); it is allowed by the hash of its exact
    text, so any other inline script is still refused. And `blob:`
    is allowed for images and fetches, because a 3D model's textures
    are unpacked inside the page and loaded from memory. A blob can
    only be made by the page's own script, from data it already has.
    """
    found = IMPORT_MAP.search(_static("index.html").decode("utf-8"))
    script = "'self'"

    if found:
        digest = base64.b64encode(
            hashlib.sha256(found.group(1).encode("utf-8")).digest()
        ).decode("ascii")
        script += f" 'sha256-{digest}'"

    return (
        f"default-src 'none'; script-src {script}; style-src 'self'; "
        "img-src 'self' blob:; media-src 'self'; "
        "connect-src 'self' blob:; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )


SECURITY_HEADERS = {
    "Content-Security-Policy": content_security_policy(),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _same_secret(given: str, known: str) -> bool:
    """
    Compare a secret from a request with one Freya issued, in constant
    time. Text from a request may hold any character, which
    compare_digest refuses for strings, so both are compared as UTF-8.
    """
    return secrets.compare_digest(
        given.encode("utf-8", "surrogateescape"), known.encode("utf-8")
    )


def _response(status, body=b"", content_type="text/plain; charset=utf-8", extra=None):
    if isinstance(body, str):
        body = body.encode("utf-8")

    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Connection"] = "close"

    for name, value in SECURITY_HEADERS.items():
        headers[name] = value

    for name, value in (extra or {}).items():
        headers[name] = value

    status = http.HTTPStatus(status)

    return Response(status.value, status.phrase, headers, body)


class Gateway:
    """
    Serves Freya's window on this computer only.

    Pages and images are plain HTTP GETs; everything interactive goes
    over one WebSocket. It listens on the loopback address and accepts
    a request only when:

      - its Host header names this server (blocks DNS rebinding),
      - it carries the session cookie, which is handed out once in
        exchange for a single-use key that Freya puts in the address
        she opens, and
      - for the WebSocket, its Origin is this server (blocks other
        web pages from connecting).

    This is a first step towards the authenticated gateway in the
    architecture, not a substitute for it: anything running as the
    same Windows user could still read the key.
    """

    def __init__(self, bus, gallery, actions, port=DEFAULT_PORT):
        self._bus = bus
        self._gallery = gallery
        self._actions = actions
        self._requested_port = port

        self._lock = threading.Lock()
        self._keys = set()
        self._sessions = set()

        self._work = queue.Queue()
        # Requests about the avatar have a queue of their own, so that
        # she can be moved or hidden while a long reply is under way.
        self._avatar_work = queue.Queue()
        self._worker = None
        self._stopping = threading.Event()
        self._server = None
        self._threads = []
        self.port = None

        # Which windows are connected, for knowing when the last one
        # has been closed.
        self._clients = 0
        self._no_clients_since = time.monotonic()
        self.ever_connected = False

    # -- lifecycle ---------------------------------------------------

    def start(self) -> None:
        listener = self._bind()
        self.port = listener.getsockname()[1]

        self._server = serve(
            self._handle_socket,
            sock=listener,
            process_request=self._process_request,
            origins=self._origins(),
            max_size=MAX_MESSAGE_BYTES,
        )

        for target, name in (
            (self._serve, "FreyaGateway"),
            (self._run_avatar_work, "FreyaGatewayAvatar"),
            (self._run_work, "FreyaGatewayWork"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

        self._worker = self._threads[-1]

    def _serve(self) -> None:
        try:
            self._server.serve_forever()
        except OSError:
            # On Windows, shutting the server down can close its
            # socket while this thread is still waiting on it. That
            # is the server stopping, not a failure. Anything else is.
            if not self._stopping.is_set():
                raise

    def _bind(self):
        for port in (self._requested_port, 0):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            try:
                listener.bind((LOOPBACK, port))
            except OSError:
                listener.close()
                continue

            listener.listen()

            return listener

        raise RuntimeError("No free port for Freya's window.")

    def stop(self, timeout=STOP_TIMEOUT_SECONDS) -> bool:
        """
        Stop serving the window, in order: refuse new work, drop the
        turns that were waiting, and wait for the one in progress.

        Returns False when that turn is still running after `timeout`
        seconds; it is never waited for indefinitely. Whatever the
        turn depends on should only be closed after this returns True.
        """
        self._stopping.set()

        # Turns that had not started are cancelled.
        while True:
            try:
                self._work.get_nowait()
            except queue.Empty:
                break

        self._work.put(None)
        self._avatar_work.put(None)

        if self._server is not None:
            self._server.shutdown()

        worker = self._worker

        if worker is None or worker is threading.current_thread():
            return True

        worker.join(timeout)

        return not worker.is_alive()

    def _origins(self):
        return [
            f"http://{host}:{self.port}"
            for host in (LOOPBACK, "localhost")
        ]

    def _hosts(self):
        return {
            f"{host}:{self.port}"
            for host in (LOOPBACK, "localhost")
        }

    def new_window_url(self) -> str:
        """
        An address that opens the window once. The key in it is
        exchanged for a session cookie and then stops working.
        """
        key = secrets.token_urlsafe(32)

        with self._lock:
            self._keys.add(key)

        return f"http://{LOOPBACK}:{self.port}/open?key={key}"

    # -- HTTP --------------------------------------------------------

    def _has_session(self, request) -> bool:
        cookie = SimpleCookie()

        try:
            cookie.load(request.headers.get("Cookie", ""))
        except Exception:
            return False

        morsel = cookie.get(SESSION_COOKIE)

        if morsel is None:
            return False

        with self._lock:
            return any(
                _same_secret(morsel.value, session)
                for session in self._sessions
            )

    def _process_request(self, connection, request):
        if request.headers.get("Host", "") not in self._hosts():
            return _response(403, "Forbidden.")

        url = urllib.parse.urlsplit(request.path)

        if url.path == "/open":
            return self._open(url)

        if not self._has_session(request):
            return _response(
                403,
                "This is Freya's window. Open it from Freya "
                "(type /window in her console).",
            )

        if url.path == "/ws":
            # Continue with the WebSocket handshake.
            return None

        if url.path in STATIC_FILES:
            name, content_type = STATIC_FILES[url.path]

            return _response(200, _static(name), content_type)

        if url.path.startswith("/images/"):
            image = self._gallery.read(url.path[len("/images/"):])

            if image is not None:
                media_type, body = image
                return _response(200, body, media_type)

        for prefix, kind in (
            ("/avatar-models/", "model"),
            ("/avatar-thumbnails/", "thumbnail"),
        ):
            if url.path.startswith(prefix):
                found = self._actions.avatar_file(kind, url.path[len(prefix):])

                if found is not None:
                    return _response(200, found[1], found[0])

        if url.path.startswith("/audio/"):
            clip = self._actions.voice_clip(url.path[len("/audio/"):])

            if clip is not None:
                return _response(200, clip, "audio/wav")

        return _response(404, "Not found.")

    def _open(self, url):
        key = urllib.parse.parse_qs(url.query).get("key", [""])[0]

        with self._lock:
            valid = any(
                _same_secret(key, known)
                for known in self._keys
            )

            if valid:
                self._keys.discard(key)
                session = secrets.token_urlsafe(32)
                self._sessions.add(session)

        if not valid:
            return _response(
                403,
                "That link has already been used. Type /window in "
                "Freya's console for a new one.",
            )

        return _response(
            302,
            extra={
                "Location": "/",
                "Set-Cookie": (
                    f"{SESSION_COOKIE}={session}; Path=/; HttpOnly; "
                    "SameSite=Strict"
                ),
            },
        )

    # -- WebSocket ---------------------------------------------------

    def idle_seconds(self):
        """
        How long there has been no window connected, or None while at
        least one is.
        """
        with self._lock:
            if self._clients:
                return None

            return time.monotonic() - self._no_clients_since

    def _handle_socket(self, websocket) -> None:
        # Events for this window wait here. The event bus only ever
        # puts them in the queue, which never blocks it; a thread of
        # this window's own does the sending.
        outbox = queue.Queue(maxsize=MAX_WAITING_EVENTS)

        def send_events():
            while True:
                event = outbox.get()

                if event is None:
                    return

                try:
                    websocket.send(json.dumps(event))
                except Exception:
                    # The window has gone; the reading loop notices too.
                    return

        sender = threading.Thread(
            target=send_events,
            name="FreyaGatewaySend",
            daemon=True,
        )

        # Binary data this window has sent and not yet used.
        upload = bytearray()

        with self._lock:
            self._clients += 1
            self.ever_connected = True

        try:
            # Subscribing and taking the history are one step, so an
            # event published right now is in exactly one of them.
            # Later events queue up and are sent after the hello.
            history = self._bus.subscribe_with_history(outbox.put_nowait)

            websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "history": history,
                        "images": self._gallery.list(),
                        "status": self._actions.status(),
                        "console": self._actions.console_history(),
                        "voice": self._actions.voice_state(),
                        "avatar": self._actions.avatar_state(),
                    }
                )
            )

            sender.start()

            for raw in websocket:
                self._handle_message(websocket, raw, upload, outbox)
        except Exception:
            # The window closed or the connection broke.
            pass
        finally:
            self._bus.unsubscribe(outbox.put_nowait)

            # Let the sending thread finish, even if the queue is full.
            while sender.is_alive():
                try:
                    outbox.put(None, timeout=0.2)
                    break
                except queue.Full:
                    try:
                        outbox.get_nowait()
                    except queue.Empty:
                        pass

            with self._lock:
                self._clients -= 1

                if not self._clients:
                    self._no_clients_since = time.monotonic()

    def _handle_message(self, websocket, raw, upload, outbox=None) -> None:
        def reject(reason):
            websocket.send(json.dumps({"type": "error", "text": reason}))

        if isinstance(raw, (bytes, bytearray)):
            if len(upload) + len(raw) > MAX_UPLOAD_BYTES:
                upload.clear()
                return reject("That recording is too large.")

            upload.extend(raw)
            return

        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return reject("That message could not be read.")

        if not isinstance(message, dict):
            return reject("That message could not be read.")

        kind = message.get("type")

        if kind in ("say", "image", "command"):
            text = message.get("text")

            if not isinstance(text, str) or not text.strip():
                return reject("There was nothing to send.")

            if len(text) > MAX_TEXT_LENGTH:
                return reject(
                    f"That is longer than {MAX_TEXT_LENGTH} characters."
                )

            if self._stopping.is_set():
                return reject("Freya is shutting down.")

            self._work.put((kind, text.strip()))
        elif kind == "voice":
            # The studio checks every field itself; it is run on the
            # work thread because speech takes a moment to generate.
            # It is handed whatever binary data this window sent.
            message["upload"] = bytes(upload)
            upload.clear()

            if self._stopping.is_set():
                return reject("Freya is shutting down.")

            self._work.put((kind, message))
        elif kind == "memory":
            # Freya checks every field herself. The answer holds
            # personal text, so it goes back to this window only and
            # never onto the bus that every window hears.
            if self._stopping.is_set() or outbox is None:
                return reject("Freya is shutting down.")

            self._work.put((kind, (message, outbox)))
        elif kind == "avatar":
            # The avatar service checks every field itself and sends
            # each request through the broker.
            if self._stopping.is_set():
                return reject("Freya is shutting down.")

            self._avatar_work.put(message)
        elif kind == "mute":
            self._actions.set_muted(message.get("muted") is True)
        elif kind == "volume":
            value = message.get("value")

            # Only a plain number is passed on; Freya limits its range.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return reject("A volume is a number from 0 to 100.")

            self._actions.set_volume(value)
        elif kind == "interrupt":
            self._actions.interrupt()
        elif kind == "status":
            websocket.send(
                json.dumps(
                    {"type": "status", "status": self._actions.status()}
                )
            )
        else:
            reject("Freya's window sent something she did not expect.")

    def _run_avatar_work(self) -> None:
        while True:
            message = self._avatar_work.get()

            if message is None or self._stopping.is_set():
                return

            try:
                answer = self._actions.avatar_request(message)
            except Exception as exc:
                answer = {"state": None, "error": f"That did not work: {exc}"}

            self._bus.publish(
                "avatar",
                request=message.get("request"),
                **answer,
            )

    def _run_work(self) -> None:
        """
        Turns asked for from the window run here one after another, so
        a long reply never blocks the connection.
        """
        while True:
            job = self._work.get()

            if job is None or self._stopping.is_set():
                return

            kind, text = job

            try:
                if kind == "say":
                    self._actions.say(text)
                elif kind == "image":
                    self._actions.generate_image(text)
                elif kind == "voice":
                    self._bus.publish(
                        "voice",
                        request=text.get("request"),
                        **self._actions.voice_request(text),
                    )
                elif kind == "memory":
                    message, outbox = text
                    answer = {
                        "type": "memory",
                        "request": message.get("request"),
                        **self._actions.memory_request(message),
                    }

                    try:
                        outbox.put_nowait(answer)
                    except queue.Full:
                        pass
                else:
                    self._actions.run_command(text)
            except Exception as exc:
                if self._stopping.is_set():
                    # A turn refused because Freya is closing down.
                    return

                self._bus.publish(
                    "notice",
                    text=f"That did not work: {exc}",
                )
