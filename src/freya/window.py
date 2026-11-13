import os
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from freya.child_environment import child_environment
from freya.core.providers import ProviderHealth
from freya.gateway.server import DEFAULT_PORT, Gateway
from freya.providers.image.comfyui import COMFYUI_PROVIDER
from freya.providers.model.ollama import OLLAMA_PROVIDER


WINDOW_COMMAND = "/window"

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# The window gets a browser profile of its own, so it shares nothing
# with the owner's everyday browsing.
WINDOW_PROFILE_DIR = DATA_DIR / "window_profile"

WINDOW_SIZE = "1100,820"

# How long the window may be gone before a console-free Freya exits.
CLOSE_GRACE_SECONDS = 8
OPEN_TIMEOUT_SECONDS = 120


@dataclass
class WindowActions:
    """
    What the window is allowed to ask Freya to do.
    """

    say: Callable[[str], None]
    generate_image: Callable[[str], None]
    set_muted: Callable[[bool], None]
    interrupt: Callable[[], None]
    status: Callable[[], dict]

    # How loudly Freya speaks, as a percentage from 0 to 100.
    set_volume: Callable[[float], None] = lambda percent: None

    # Run one of Freya's slash commands, as typed in the terminal.
    run_command: Callable[[str], None] = lambda text: None

    # What the terminal has shown so far, one entry per line.
    console_history: Callable[[], list] = lambda: []

    # The Voice tab: its current state, a request made from it (which
    # returns the new state and, for speech, the clip that was made),
    # and the bytes of a generated clip.
    voice_state: Callable[[], dict] = lambda: {}
    voice_request: Callable[[dict], dict] = lambda message: {}
    voice_clip: Callable[[str], Optional[bytes]] = lambda clip_id: None

    # A GUI client of the given kind ("native" for a program Freya
    # paired, such as the Unreal window) has disconnected.
    client_closed: Callable[[str], None] = lambda kind: None

    # The Memory tab: a search of, or change to, what Freya has saved.
    # Returns what the tab should now show.
    memory_request: Callable[[dict], dict] = lambda message: {}

    # The Cloud tab: the owner's choices about cloud models. Returns
    # what the tab should now show.
    cloud_request: Callable[[dict], dict] = lambda message: {
        "error": "Cloud settings are unavailable.",
    }

    # The Avatar tab: its current state, and a request made from it
    # (which returns the new state and an error message, if any).
    media_request: Callable[[dict], dict] = lambda message: {"error": "Media is unavailable."}
    media_stream: Callable[[str, Optional[str]], object] = lambda ticket, header: None
    media_download: Callable[[str, str], object] = lambda media_id, kind: None

    avatar_state: Callable[[], dict] = lambda: {}
    avatar_request: Callable[[dict], dict] = lambda message: {}
    # An avatar model's file ("model") or thumbnail ("thumbnail"), as
    # (media type, bytes), by the model's identifier.
    avatar_file: Callable[[str, str], Optional[tuple]] = lambda kind, model: None


def build_status(listener, settings, control_plane, voice=None) -> dict:
    """
    The few facts the window's status strip shows.
    """
    if listener is None or not listener.running:
        microphone = "off"
    elif listener.muted:
        microphone = "muted"
    else:
        microphone = "listening"

    providers = control_plane.providers
    comfyui = providers.get(COMFYUI_PROVIDER)

    if comfyui.health == ProviderHealth.HEALTHY:
        images = "healthy"
    elif comfyui.metadata.get("can_start"):
        images = "startable"
    else:
        images = "unavailable"

    return {
        "microphone": microphone,
        "names": [name.title() for name in settings.wake_names],
        "model": providers.get(OLLAMA_PROVIDER).health.value,
        "images": images,
        "volume": getattr(settings, "voice_volume", 100),
        **({"voice": "ready" if voice.enabled else "unavailable"} if voice is not None else {}),
    }


def _edge_path():
    for root in (
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
    ):
        if not root:
            continue

        candidate = (
            Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        )

        if candidate.is_file():
            return candidate

    return None


def open_app_window(url, profile_dir=WINDOW_PROFILE_DIR) -> str:
    """
    Show the page in a window of its own, without browser tabs or an
    address bar. Falls back to the default browser when that is not
    possible. Returns "window", "browser", or "none".
    """
    edge = _edge_path()

    if edge is not None:
        flags = 0

        if os.name == "nt":
            flags = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )

        try:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            subprocess.Popen(
                [
                    str(edge),
                    f"--app={url}",
                    f"--user-data-dir={profile_dir}",
                    f"--window-size={WINDOW_SIZE}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_environment(),
                creationflags=flags,
                close_fds=True,
            )
            return "window"
        except OSError:
            pass

    try:
        if webbrowser.open(url):
            return "browser"
    except webbrowser.Error:
        pass

    return "none"


class FreyaWindow:
    """
    Freya's window: a local page with Chat and Images tabs, served by
    the gateway and shown in its own application window.
    """

    def __init__(
        self,
        bus,
        gallery,
        actions: WindowActions,
        port=DEFAULT_PORT,
        opener=open_app_window,
    ):
        self._gateway = Gateway(bus, gallery, actions, port=port)
        self._opener = opener
        self.running = False

    @property
    def port(self):
        return self._gateway.port

    @property
    def speech(self):
        """
        Hands Freya's speech to a client that plays it, when one does.
        """
        return self._gateway.speech

    def new_pairing_key(self) -> str:
        """
        A single-use key for a GUI program Freya is about to start.
        """
        return self._gateway.new_pairing_key()

    def has_window(self) -> bool:
        """Whether a window is connected that could answer a question."""
        return self.running and self._gateway.window_count() > 0

    def ask_owner(self, prompt: dict):
        """
        Ask the owner a question in the window. Returns the id of the
        answer chosen, or None when there is no window or no answer.
        """
        if not self.running:
            return None

        return self._gateway.ask_owner(prompt)

    def start(self) -> bool:
        """
        Start serving the window. Returns False, with Freya otherwise
        unaffected, when the server cannot start.
        """
        try:
            self._gateway.start()
        except Exception as exc:
            print(f"Freya's window could not be started: {exc}")
            return False

        self.running = True
        return True

    def open(self) -> str:
        """
        Open the window and return one line saying what happened.
        """
        if not self.running:
            return "Freya's window is not available."

        shown = self._opener(self._gateway.new_window_url())

        if shown == "window":
            return "Freya's window is open."

        if shown == "browser":
            return "Freya's window is open in your browser."

        return "Freya's window could not be opened."

    def closed(self) -> bool:
        """
        True when Freya's window is gone: every window has been closed
        for a few seconds (a page reload reconnects well inside that),
        or none ever appeared.

        When Freya runs without a console, this is what tells her to
        exit, so she is never left running unseen.
        """
        if not self.running:
            return True

        idle = self._gateway.idle_seconds()

        if idle is None:
            return False

        if self._gateway.ever_connected:
            return idle >= CLOSE_GRACE_SECONDS

        return idle >= OPEN_TIMEOUT_SECONDS

    def stop(self) -> bool:
        """
        Stop serving the window, waiting for a turn it asked for to
        finish. Returns False when that turn was still running.
        """
        if self.running:
            self.running = False

            return self._gateway.stop()

        return True
