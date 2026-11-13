import io
import json
import sys

import pytest

from freya.console_tee import ConsoleOutput, ConsoleTee
from freya.events import EventBus
from freya.main import normalize_command
from freya.settings import Settings
from freya.window_commands import CommandContext, run_window_command


@pytest.fixture
def bus():
    return EventBus()


def console_lines(bus_events):
    return [e["text"] for e in bus_events if e["type"] == "console"]


def test_tee_passes_text_through_and_announces_whole_lines(bus):
    seen = []
    bus.subscribe(seen.append)
    underlying = io.StringIO()
    output = ConsoleOutput(bus)
    tee = ConsoleTee(underlying, output)

    tee.write("Freya on")
    assert console_lines(seen) == []

    tee.write("line.\nSecond line\nYou: ")

    assert underlying.getvalue() == "Freya online.\nSecond line\nYou: "
    assert console_lines(seen) == ["Freya online.", "Second line"]
    assert output.history() == ["Freya online.", "Second line"]

    # A spoken turn overwrites the waiting prompt with a carriage return.
    tee.write("\rYou (voice): hello\n")

    assert output.history()[-1] == "You (voice): hello"

    tee.finish()
    assert output.history()[-1] == "You (voice): hello"

    tee.write("no newline at the end")
    tee.finish()
    assert output.history()[-1] == "no newline at the end"


def test_a_window_joining_mid_line_sees_each_console_line_once(bus):
    # The hello's console snapshot and the subscription are one step,
    # and the tee records and announces a line in that same step, so a
    # line can be in the snapshot or among the live events, never both.
    import threading

    output = ConsoleOutput(bus)
    live = []
    joined = threading.Event()
    results = {}

    def announcer():
        for n in range(200):
            output.add_line(f"line {n}")
            if n == 50:
                joined.set()

    def joiner():
        joined.wait(5)
        results["history"], results["console"] = bus.subscribe_with_history(
            live.append, snapshot=output.history,
        )

    threads = [threading.Thread(target=announcer), threading.Thread(target=joiner)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    seen = results["console"] + [e["text"] for e in live if e["type"] == "console"]
    assert seen == [f"line {n}" for n in range(200)]


def test_tee_works_with_no_underlying_stream(bus):
    output = ConsoleOutput(bus)
    tee = ConsoleTee(None, output)

    assert tee.write("hello\n") == 6
    tee.flush()

    assert output.history() == ["hello"]
    assert tee.isatty() is False
    with pytest.raises(io.UnsupportedOperation):
        tee.fileno()


def test_history_is_bounded(bus):
    output = ConsoleOutput(bus, limit=3)
    tee = ConsoleTee(None, output)

    for index in range(5):
        tee.write(f"{index}\n")

    assert output.history() == ["2", "3", "4"]


def test_a_subscriber_that_prints_does_not_loop_forever(bus):
    output = ConsoleOutput(bus)
    tee = ConsoleTee(None, output)
    bus.subscribe(lambda event: tee.write("echo: " + event["text"] + "\n"))

    tee.write("hello\n")

    assert output.history() == ["hello", "echo: hello"]


def test_install_and_uninstall_restore_the_real_streams(bus):
    output = ConsoleOutput(bus)
    before = (sys.stdout, sys.stderr)

    output.install()
    try:
        print("captured line")
        print("to stderr", file=sys.stderr)
    finally:
        output.uninstall()

    assert (sys.stdout, sys.stderr) == before
    assert output.history() == ["captured line", "to stderr"]
    output.uninstall()


class FakeVoice:
    def __init__(self):
        self.interrupted = 0

    def interrupt(self):
        self.interrupted += 1


class FakeListener:
    running = True
    muted = False

    def mute(self):
        self.muted = True

    def unmute(self):
        self.muted = False


class FakeWindow:
    def open(self):
        return "Freya's window is open."


@pytest.fixture
def context(bus, tmp_path):
    context = CommandContext(
        bus=bus,
        voice=FakeVoice(),
        listener=FakeListener(),
        settings=Settings(tmp_path / "settings.json"),
        window=FakeWindow(),
        normalize=normalize_command,
        run_image=lambda prompt: context.images.append(prompt),
        publish_status=lambda: context.status_updates.append(1),
    )
    context.images = []
    context.status_updates = []
    context.quits = []
    return context


def notices(bus):
    return [e["text"] for e in bus.history() if e["type"] == "notice"]


def test_names_commands_answer_in_the_chat(context, bus, capsys):
    run_window_command("/names", context)
    run_window_command("\\name add Computer", context)
    run_window_command("/name remove freya", context)

    assert notices(bus) == [
        "I answer to Freya.",
        "I'll answer to Computer as well.",
        "I'll stop answering to Freya.",
    ]
    assert context.settings.wake_names == ["computer"]
    assert len(context.status_updates) == 3
    # The same exchange is printed, so the Console tab shows it too.
    printed = capsys.readouterr().out
    assert "You (window): /name add Computer" in printed
    assert "I'll answer to Computer as well." in printed


def test_mute_interrupt_and_window(context, bus):
    run_window_command("/mute", context)
    assert context.listener.muted is True

    run_window_command("/unmute", context)
    assert context.listener.muted is False

    run_window_command("/interrupt", context)
    assert context.voice.interrupted == 1

    run_window_command("/window", context)
    assert notices(bus)[-1] == "Freya's window is open."


def test_another_threads_output_is_not_shown_as_the_commands_answer(context, bus, capsys):
    import threading

    # The listener thread prints while a command runs (a wake word
    # heard, the microphone muted). That line belongs to the console,
    # not to the chat as the command's answer.
    started = threading.Event()
    printed = threading.Event()

    def listener():
        started.wait(5)
        print("My window closed, so I have muted the microphone.")
        printed.set()

    class Voice:
        def interrupt(self):
            started.set()
            printed.wait(5)

    context.voice = Voice()
    other = threading.Thread(target=listener)
    other.start()
    run_window_command("/interrupt", context)
    other.join(5)

    assert notices(bus) == ["Speech playback interrupted."]
    assert "muted the microphone" in capsys.readouterr().out


def test_image_command_runs_generation_without_a_chat_notice(context, bus):
    run_window_command("/image a red fox in snow", context)
    run_window_command("/image", context)

    assert context.images == ["a red fox in snow"]
    assert notices(bus) == ["Usage: /image <description>"]


def test_help_and_unknown_commands(context, bus):
    run_window_command("/help", context)
    run_window_command("/frobnicate now", context)

    first, second = notices(bus)
    assert first.startswith("Commands: /names")
    assert "I don't know the command /frobnicate" in second


@pytest.mark.parametrize(
    "command", ["/talk", "/listen", "/conversation", "/voice-file C:\\a.wav"]
)
def test_keyboard_only_commands_are_explained(context, bus, command):
    run_window_command(command, context)

    assert "needs a keyboard in my terminal" in notices(bus)[0]


@pytest.mark.parametrize("command", ["exit", "quit", "/exit", "/QUIT"])
def test_quit_closes_freya_only_when_there_is_no_terminal(
    context, bus, command
):
    run_window_command(command, context)

    assert "Type exit there" in notices(bus)[0]
    assert context.quits == []

    context.request_quit = lambda: context.quits.append(1)
    run_window_command(command, context)

    assert context.quits == [1]
    assert notices(bus)[-1] == "Closing down."
