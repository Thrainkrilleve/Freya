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

from freya.gateway.prompts import OwnerPrompts
from freya.gateway.speech_relay import SpeechRelay
from freya.gateway.answer_media import media_document


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
# How long a piece of speech waits for room in a client's queue.
PLAYBACK_SEND_SECONDS = 2.0

# The version of the conversation between Freya and a GUI client. A
# client says which it speaks; one Freya does not speak is refused.
PROTOCOL_VERSION = 1
# What a client may ask for when it introduces itself.
CLIENT_FEATURES = ("speech.playback",)
# How a program Freya started (not a web page) proves who it is: a key
# Freya gave it when she started it, used once, or the session that key
# was exchanged for. Never in an address.
PAIR_SCHEME = "FreyaPair"
SESSION_SCHEME = "FreyaSession"

JAVASCRIPT = "text/javascript; charset=utf-8"

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/rich-chat.js": ("rich-chat.js", JAVASCRIPT),
    # The 3D preview of avatar models, and the libraries it uses.
    # Each file is listed: nothing else under static/ is served.
    "/media.js": ("media.js", JAVASCRIPT),
    "/cloud.js": ("cloud.js", JAVASCRIPT),
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


def _same_secret(given, known) -> bool:
    """
    A constant-time comparison that accepts whatever text a client
    sent: `secrets.compare_digest` refuses non-ASCII strings with an
    error, which would turn a stray character in a key or cookie into
    a failed request rather than a refused one.
    """
    return secrets.compare_digest(
        str(given).encode("utf-8"), str(known).encode("utf-8")
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
        "connect-src 'self' blob:; frame-src 'self' https://www.youtube-nocookie.com; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    )


SECURITY_HEADERS = {
    "Content-Security-Policy": content_security_policy(),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _response(status, body=b"", content_type="text/plain; charset=utf-8", extra=None, security_policy=None):
    if isinstance(body, str):
        body = body.encode("utf-8")

    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Connection"] = "close"

    for name, value in SECURITY_HEADERS.items():
        # Only trusted endpoint code supplies a distinct document policy.
        headers[name] = security_policy if name == "Content-Security-Policy" and security_policy is not None else value

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
        self._pairing_keys = set()
        self._native_sessions = set()

        # Hands Freya's speech to the one client that plays it.
        self.speech = SpeechRelay()

        # The windows (web pages) that are connected, and the questions
        # put to the owner in them. A question is never put on the bus.
        self._web_clients = []
        self._prompts = OwnerPrompts(self._show_to_windows)

        self._work = queue.Queue()
        # Requests about the avatar have a queue of their own, so that
        # she can be moved or hidden while a long reply is under way.
        self._media_work = queue.Queue(maxsize=100)
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
            # Where a connection comes from is checked in
            # _process_request, which knows a web page from a program.
            origins=None,
            max_size=MAX_MESSAGE_BYTES,
        )

        for target, name in (
            (self._serve, "FreyaGateway"),
            (self._run_avatar_work, "FreyaGatewayAvatar"),
            (self._run_media_work, "FreyaGatewayMedia"),
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

        # A turn may be waiting for the owner to answer a question. It
        # must not hold up shutdown: the question is closed, and that
        # is a denial.
        self._prompts.close()

        # Turns that had not started are cancelled.
        while True:
            try:
                self._work.get_nowait()
            except queue.Empty:
                break

        self._work.put(None)
        self._avatar_work.put(None)
        while True:
            try:
                self._media_work.get_nowait()
            except queue.Empty:
                break
        self._media_work.put_nowait(None)

        if self._server is not None:
            self._server.shutdown()

        worker = self._worker

        if worker is None or worker is threading.current_thread():
            return True

        worker.join(timeout)

        media = next((t for t in self._threads if t.name == "FreyaGatewayMedia"), None)
        if media is not None and media is not threading.current_thread():
            media.join(max(0, timeout))
        return not worker.is_alive() and (media is None or not media.is_alive())

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

    def new_pairing_key(self) -> str:
        """
        A key for a GUI program Freya is about to start. It works once,
        in the Authorization header of that program's connection.
        """
        key = secrets.token_urlsafe(32)

        with self._lock:
            self._pairing_keys.add(key)

        return key

    def _native_session(self, authorization: str):
        """
        The session for a program's Authorization header, or None. A
        pairing key is used up and replaced by a session, which that
        program may use to reconnect while Freya keeps running.
        """
        scheme, _, value = str(authorization).partition(" ")
        value = value.strip()

        if not value:
            return None

        with self._lock:
            if scheme == PAIR_SCHEME:
                for key in self._pairing_keys:
                    if _same_secret(value, key):
                        self._pairing_keys.discard(key)
                        session = secrets.token_urlsafe(32)
                        self._native_sessions.add(session)

                        return session
            elif scheme == SESSION_SCHEME:
                for session in self._native_sessions:
                    if _same_secret(value, session):
                        return session

        return None

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

        if url.path == "/ws":
            authorization = request.headers.get("Authorization", "")

            if authorization.split(" ", 1)[0] in (PAIR_SCHEME, SESSION_SCHEME):
                # A program Freya started, proving it with the key she
                # gave it. A web page cannot set this header on a
                # WebSocket, so the Origin does not matter here.
                session = self._native_session(authorization)

                if session is None:
                    return _response(403, "This program has not been paired with Freya.")

                connection.freya_native_session = session

                return None

            # Otherwise it must be Freya's own page, from this server.
            if request.headers.get("Origin") not in self._origins():
                return _response(403, "Forbidden.")

        if not self._has_session(request):
            return _response(
                403,
                "This is Freya's window. Open it from Freya "
                "(type /window in her console).",
            )

        if url.path == "/ws":
            # Continue with the WebSocket handshake.
            return None

        if url.path == "/answer-media":
            parameters = urllib.parse.parse_qs(url.query, keep_blank_values=True)
            if set(parameters) != {"kind", "url"} or any(len(values) != 1 for values in parameters.values()):
                return _response(400, "That media address could not be read.")
            document = media_document(parameters["kind"][0], parameters["url"][0])
            if document is None:
                return _response(400, "That media address is not supported.")
            body, policy = document
            return _response(200, body, "text/html; charset=utf-8", security_policy=policy)

        if url.path in STATIC_FILES:
            name, content_type = STATIC_FILES[url.path]

            return _response(200, _static(name), content_type)

        if url.path.startswith("/media/stream/"):
            answer = self._actions.media_stream(url.path[len("/media/stream/"):], request.headers.get("Range"))
            if answer is not None:
                return _response(answer.status, answer.body, answer.content_type, answer.headers)

        if url.path.startswith("/media/export/"):
            parts = url.path[len("/media/export/"):].split("/")
            if len(parts) == 2:
                answer = self._actions.media_download(*parts)
                if answer is not None:
                    return _response(answer.status, answer.body, answer.content_type, answer.headers)

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

    # -- questions for the owner -------------------------------------

    def _show_to_windows(self, message: dict, recipients=None) -> list:
        """
        Put a message straight into each connected window's own queue
        (or only those of `recipients`) and return the windows it
        reached. It does not touch the event bus, which keeps a
        history that windows opened later are given.
        """
        with self._lock:
            clients = [
                c for c in self._web_clients
                if c["open"] and (recipients is None or c in recipients)
            ]

        reached = []

        for client in clients:
            try:
                client["outbox"].put_nowait(message)
                reached.append(client)
            except queue.Full:
                pass

        return reached

    def window_count(self) -> int:
        """How many windows are connected and able to answer."""
        with self._lock:
            return sum(1 for c in self._web_clients if c["open"])

    def ask_owner(self, prompt: dict):
        """
        Ask the owner a question in the window and wait for the answer.
        Returns the id of the answer chosen, or None for no answer.
        """
        return self._prompts.ask(prompt)

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
                    websocket.send(
                        bytes(event)
                        if isinstance(event, (bytes, bytearray))
                        else json.dumps(event)
                    )
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

        # What is known about this client. A web page is "web"; a
        # program Freya paired is "native" and may be given playback.
        client = {
            "kind": "native" if getattr(websocket, "freya_native_session", None) else "web",
            "session": getattr(websocket, "freya_native_session", None),
            "playback": None,
            "outbox": outbox,
            "open": True,
        }

        with self._lock:
            self._clients += 1
            self.ever_connected = True

            if client["kind"] == "web":
                self._web_clients.append(client)

        try:
            # Subscribing and taking the history are one step, so an
            # event published right now is in exactly one of them.
            # Later events queue up and are sent after the hello.
            history, console = self._bus.subscribe_with_history(
                outbox.put_nowait, snapshot=self._actions.console_history,
            )

            websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "history": history,
                        "images": self._gallery.list(),
                        "status": self._actions.status(),
                        "console": console,
                        "voice": self._actions.voice_state(),
                        "avatar": self._actions.avatar_state(),
                    }
                )
            )

            sender.start()

            for raw in websocket:
                self._handle_message(websocket, raw, upload, outbox, client)
        except Exception:
            # The window closed or the connection broke.
            pass
        finally:
            client["open"] = False

            with self._lock:
                if client in self._web_clients:
                    self._web_clients.remove(client)

            # A question shown only in this window can no longer be
            # answered; the turn waiting on it is released now.
            self._prompts.window_gone(client)

            self.speech.release(client["playback"])
            self._bus.unsubscribe(outbox.put_nowait)

            if client["kind"] == "native":
                try:
                    self._actions.client_closed("native")
                except Exception:
                    pass

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

    def _playback_sender(self, websocket, client):
        """
        What the speech relay uses to send to the client playing her
        voice. The relay gives playback up when a send fails, so a client
        that cannot keep up is disconnected: it comes back with its
        session and takes playback again. Left connected, it would stay
        silent while her voice played on the speakers.
        """
        outbox = client["outbox"]

        def send(item):
            if not client["open"]:
                return False

            try:
                outbox.put(item, timeout=PLAYBACK_SEND_SECONDS)
            except queue.Full:
                client["open"] = False

                try:
                    # Not close(): that waits for the client to answer.
                    websocket.socket.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass

                return False

            return True

        return send

    def _introduce(self, websocket, message, client) -> None:
        """
        A client says what it is and what it would like. It is told what
        it has been given, or refused if it speaks another version.
        """
        if message.get("protocol") != PROTOCOL_VERSION:
            websocket.send(
                json.dumps(
                    {
                        "type": "refused",
                        "protocol": PROTOCOL_VERSION,
                        "reason": f"Freya speaks version {PROTOCOL_VERSION} of this protocol.",
                    }
                )
            )
            websocket.close()
            return

        asked = message.get("features")
        asked = [f for f in asked if isinstance(f, str)] if isinstance(asked, list) else []
        unknown = [f for f in asked if f not in CLIENT_FEATURES]
        granted = []

        # Only a program Freya paired may play her voice, and only one.
        if "speech.playback" in asked and client["kind"] == "native" and client["playback"] is None:
            client["playback"] = self.speech.claim(self._playback_sender(websocket, client))

            if client["playback"] is not None:
                granted.append("speech.playback")

        websocket.send(
            json.dumps(
                {
                    "type": "welcome",
                    "protocol": PROTOCOL_VERSION,
                    "features": granted,
                    "not_available": [f for f in asked if f not in granted and f not in unknown],
                    "unknown": unknown[:20],
                    "session": client["session"],
                    "limits": {
                        "message_bytes": MAX_MESSAGE_BYTES,
                        "text_characters": MAX_TEXT_LENGTH,
                    },
                }
            )
        )

    def _handle_message(self, websocket, raw, upload, outbox=None, client=None) -> None:
        def reject(reason):
            websocket.send(json.dumps({"type": "error", "text": reason}))

        client = client or {"kind": "web", "session": None, "playback": None, "outbox": outbox, "open": True}

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

        if kind == "client.hello":
            return self._introduce(websocket, message, client)

        if kind in ("speech.started", "speech.finished", "speech.interrupted"):
            # Believed only from the client that holds playback.
            if client["playback"] is not None:
                self.speech.acknowledge(client["playback"], message)

            return

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
        elif kind == "cloud":
            # The owner's cloud settings and the briefs that were
            # sent. Like the Memory tab's answers, these go back to
            # this window only.
            if self._stopping.is_set() or outbox is None:
                return reject("Freya is shutting down.")

            self._work.put((kind, (message, outbox)))
        elif kind == "media":
            if self._stopping.is_set() or outbox is None:
                return reject("Freya is shutting down.")
            if message.get("action") == "transcript-import":
                message["upload"] = bytes(upload)
                upload.clear()
            try:
                self._media_work.put_nowait((message, outbox))
            except queue.Full:
                return reject("Too many media requests are waiting.")
        elif kind == "avatar":
            # The avatar service checks every field itself and sends
            # each request through the broker.
            if self._stopping.is_set():
                return reject("Freya is shutting down.")

            self._avatar_work.put(message)
        elif kind == "cloud.answer":
            # The owner's answer to a question Freya put. It is taken
            # here, on this window's own thread: the turn that asked is
            # waiting for it, so it cannot be queued behind that turn.
            # Only a window may answer.
            if client["kind"] == "web":
                self._prompts.answer(message.get("id"), message.get("choice"))
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

    def _run_media_work(self) -> None:
        while True:
            job = self._media_work.get()
            if job is None or self._stopping.is_set():
                return
            message, outbox = job
            try:
                answer = self._actions.media_request(message)
            except Exception:
                answer = {"error": "The media request failed. Try again."}
            try:
                outbox.put_nowait({"type": "media", "request": message.get("request"), **answer})
            except queue.Full:
                pass

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
                elif kind == "cloud":
                    message, outbox = text
                    answer = {
                        "type": "cloud",
                        "request": message.get("request"),
                        **self._actions.cloud_request(message),
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
