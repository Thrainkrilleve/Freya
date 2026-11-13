import subprocess
import sys
import traceback

from freya.paths import data_dir


DATA_DIR = data_dir()
LOG_PATH = DATA_DIR / "freya.log"

# Flags that already decide what happens to a child's console.
_CONSOLE_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0)
    | getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    | getattr(subprocess, "DETACHED_PROCESS", 0)
)


def hide_helper_windows() -> None:
    """
    Make every helper program Freya starts run without a window of
    its own, unless the caller asked for something specific.

    A program with no console gives each console helper it starts a
    new, visible terminal window. Freya's own helpers ask to be
    hidden, but libraries she uses may start helpers too.
    """
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if not no_window or getattr(subprocess.Popen, "_freya_hidden", False):
        return

    original = subprocess.Popen.__init__

    def hidden_init(self, *args, creationflags=0, **kwargs):
        if not creationflags & _CONSOLE_FLAGS:
            creationflags |= no_window

        original(self, *args, creationflags=creationflags, **kwargs)

    subprocess.Popen.__init__ = hidden_init
    subprocess.Popen._freya_hidden = True


def main_window():
    """
    Start Freya with no console window: she is used through her own
    window and by voice.

    A program started this way has nowhere to print, and some
    libraries fail when they try. So before anything heavy is loaded,
    everything Freya would have printed is pointed at data/freya.log.
    """
    from freya.single_instance import claim, show_message

    # Checked first, so a second launch does not wipe the log of the
    # Freya that is already running.
    if not claim():
        show_message("Freya is already running.")
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log = open(
        LOG_PATH,
        "w",
        encoding="utf-8",
        buffering=1,
    )
    sys.stdout = log
    sys.stderr = log

    hide_helper_windows()

    try:
        # Imported here, after the log is in place: loading the voice
        # and speech libraries is where most start-up output comes from.
        from freya.main import main

        main(window_only=True)
    except Exception:
        traceback.print_exc()

        show_message(
            "Freya stopped because of an error. "
            f"Details are in {LOG_PATH}."
        )
    finally:
        log.flush()
