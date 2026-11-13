import json
import queue
import threading
import time

import pytest

from freya.events import EventBus
from freya.gateway.prompts import PROMPT, PROMPT_CLOSED, OwnerPrompts
from freya.gateway.server import Gateway
from freya.image.gallery import ImageGallery
from freya.window import WindowActions


QUESTION = {
    "kind": "confirmation",
    "text": "A made-up request about the Harbour Street garden.",
    "choices": [{"id": "send", "label": "Send"}, {"id": "cancel", "label": "Cancel"}],
}


class Windows:
    """Stands in for the gateway's windows: each has its own queue."""

    def __init__(self, count=1):
        self.clients = [queue.Queue() for _ in range(count)]

    def deliver(self, message, recipients=None):
        reached = []

        for client in self.clients:
            if recipients is None or client in recipients:
                client.put(message)
                reached.append(client)

        return reached


def ask_in_thread(prompts, prompt=QUESTION, seconds=None):
    found = {}

    def run():
        found["answer"] = prompts.ask(prompt, seconds)

    thread = threading.Thread(target=run)
    thread.start()

    return thread, found


def shown(window, wait=2):
    return window.get(timeout=wait)


def test_an_answer_to_a_waiting_question_is_returned():
    windows = Windows()
    prompts = OwnerPrompts(windows.deliver, seconds=5)
    thread, found = ask_in_thread(prompts)

    message = shown(windows.clients[0])
    assert message["type"] == PROMPT
    assert message["text"] == QUESTION["text"]

    assert prompts.answer(message["id"], "send") is True
    thread.join(2)

    assert found["answer"] == "send"
    assert shown(windows.clients[0])["type"] == PROMPT_CLOSED


def test_unknown_second_and_late_answers_are_ignored():
    windows = Windows()
    prompts = OwnerPrompts(windows.deliver, seconds=5)
    thread, found = ask_in_thread(prompts)
    message = shown(windows.clients[0])

    # An id nobody asked, a choice that was not offered, wrong types.
    assert prompts.answer("not-an-id", "send") is False
    assert prompts.answer(message["id"], "something-else") is False
    assert prompts.answer(message["id"], None) is False
    assert prompts.answer(None, "send") is False
    assert prompts.answer(["x"], "send") is False

    assert prompts.answer(message["id"], "cancel") is True
    thread.join(2)

    # The first answer won; a second one, even a different one, is not
    # taken.
    assert prompts.answer(message["id"], "send") is False
    assert found["answer"] == "cancel"


def test_an_answer_after_the_time_limit_is_ignored():
    windows = Windows()
    prompts = OwnerPrompts(windows.deliver, seconds=0.05)

    assert prompts.ask(QUESTION) is None

    message = shown(windows.clients[0])
    assert prompts.answer(message["id"], "send") is False


def test_no_window_means_no_question_and_no_answer():
    windows = Windows(count=0)
    prompts = OwnerPrompts(windows.deliver, seconds=5)

    started = time.monotonic()

    assert prompts.ask(QUESTION) is None
    assert time.monotonic() - started < 2


def test_a_question_without_valid_choices_is_not_shown():
    windows = Windows()
    prompts = OwnerPrompts(windows.deliver, seconds=5)

    assert prompts.ask({"text": "x", "choices": []}) is None
    assert prompts.ask({"text": "x"}) is None
    assert prompts.ask({"text": "x", "choices": [{"label": "no id"}]}) is None
    assert windows.clients[0].empty()


def test_the_question_ids_are_random_and_not_reused():
    windows = Windows()
    prompts = OwnerPrompts(windows.deliver, seconds=0.02)

    prompts.ask(QUESTION)
    prompts.ask(QUESTION)

    messages = []
    while not windows.clients[0].empty():
        messages.append(windows.clients[0].get_nowait())

    ids = [m["id"] for m in messages if m["type"] == PROMPT]

    assert len(ids) == 2 and ids[0] != ids[1]
    assert all(len(i) >= 16 for i in ids)


def test_closing_ends_a_waiting_question_at_once_and_refuses_new_ones():
    windows = Windows()
    prompts = OwnerPrompts(windows.deliver, seconds=60)
    thread, found = ask_in_thread(prompts)
    shown(windows.clients[0])

    started = time.monotonic()
    prompts.close()
    thread.join(5)

    assert not thread.is_alive()
    assert time.monotonic() - started < 3
    assert found["answer"] is None
    assert prompts.ask(QUESTION) is None


def test_a_failing_window_is_a_denial_not_a_crash():
    def broken(message, recipients=None):
        raise RuntimeError("socket gone")

    prompts = OwnerPrompts(broken, seconds=5)

    assert prompts.ask(QUESTION) is None


def test_only_the_windows_that_were_asked_are_told_it_closed():
    first, late = queue.Queue(), queue.Queue()
    connected = [first]

    def deliver(message, recipients=None):
        return [
            client for client in connected
            if recipients is None or client in recipients
            for _ in [client.put(message)]
        ]

    prompts = OwnerPrompts(deliver, seconds=5)
    thread, _ = ask_in_thread(prompts)
    message = shown(first)

    connected.append(late)        # opens while the question waits
    prompts.answer(message["id"], "send")
    thread.join(2)

    assert shown(first)["type"] == PROMPT_CLOSED
    assert late.empty()


# -- through the gateway -----------------------------------------------


class FakeSocketClient:
    def __init__(self, kind="web"):
        self.record = {
            "kind": kind,
            "session": None,
            "playback": None,
            "outbox": queue.Queue(maxsize=50),
            "open": True,
        }


@pytest.fixture
def gateway(tmp_path):
    actions = WindowActions(
        say=lambda text: None,
        generate_image=lambda prompt: None,
        set_muted=lambda muted: None,
        interrupt=lambda: None,
        status=lambda: {},
    )
    bus = EventBus()
    gate = Gateway(bus, ImageGallery(tmp_path / "images"), actions, port=0)
    gate.test_bus = bus

    yield gate

    gate.stop()


def connect(gateway, kind="web"):
    client = FakeSocketClient(kind)

    with gateway._lock:
        gateway._web_clients.append(client.record)

    return client.record


def answer(gateway, client, prompt_id, choice):
    gateway._handle_message(
        None,
        json.dumps({"type": "cloud.answer", "id": prompt_id, "choice": choice}),
        None,
        client["outbox"],
        client,
    )


def test_a_prompt_reaches_the_window_and_never_the_bus_history(gateway):
    window = connect(gateway)
    found = {}

    def run():
        found["answer"] = gateway.ask_owner(QUESTION)

    thread = threading.Thread(target=run)
    thread.start()
    message = window["outbox"].get(timeout=2)

    assert message["type"] == PROMPT
    assert gateway.window_count() == 1

    # The bus keeps history for windows opened later. The question,
    # which can carry a whole request, is not in it.
    history = gateway.test_bus.subscribe_with_history(lambda event: None)
    assert QUESTION["text"] not in json.dumps(history, default=str)
    assert PROMPT not in json.dumps(history, default=str)

    answer(gateway, window, message["id"], "send")
    thread.join(2)

    assert found["answer"] == "send"


def test_a_window_opened_later_is_not_shown_an_open_question(gateway):
    first = connect(gateway)
    thread = threading.Thread(target=lambda: gateway.ask_owner(QUESTION))
    thread.start()
    message = first["outbox"].get(timeout=2)

    later = connect(gateway)

    assert later["outbox"].empty()

    answer(gateway, first, message["id"], "cancel")
    thread.join(2)

    assert later["outbox"].empty()


def test_the_first_window_to_answer_wins(gateway):
    one, two = connect(gateway), connect(gateway)
    found = {}
    thread = threading.Thread(
        target=lambda: found.update(answer=gateway.ask_owner(QUESTION))
    )
    thread.start()
    message = one["outbox"].get(timeout=2)
    two["outbox"].get(timeout=2)

    answer(gateway, two, message["id"], "send")
    thread.join(2)
    answer(gateway, one, message["id"], "cancel")

    assert found["answer"] == "send"
    assert two["outbox"].get(timeout=2)["type"] == PROMPT_CLOSED
    assert one["outbox"].get(timeout=2)["type"] == PROMPT_CLOSED


def test_only_a_window_may_answer(gateway):
    window = connect(gateway)
    other = connect(gateway, kind="native")
    found = {}
    thread = threading.Thread(
        target=lambda: found.update(answer=gateway.ask_owner(QUESTION, ))
    )
    thread.start()
    message = window["outbox"].get(timeout=2)

    answer(gateway, other, message["id"], "send")
    assert thread.is_alive()

    answer(gateway, window, message["id"], "cancel")
    thread.join(2)

    assert found["answer"] == "cancel"


def test_with_no_window_a_question_is_a_denial(gateway):
    assert gateway.window_count() == 0
    assert gateway.ask_owner(QUESTION) is None


def test_shutting_down_while_a_question_waits_ends_it_promptly(gateway):
    connect(gateway)
    found = {}
    thread = threading.Thread(
        target=lambda: found.update(answer=gateway.ask_owner(QUESTION))
    )
    thread.start()
    time.sleep(0.1)

    started = time.monotonic()
    gateway.stop()
    thread.join(5)

    assert not thread.is_alive()
    assert time.monotonic() - started < 4
    assert found["answer"] is None


def test_a_question_ends_when_its_last_window_goes():
    # The turn that asked must not wait the whole time limit for an
    # answer that can never come.
    windows = Windows(2)
    prompts = OwnerPrompts(windows.deliver, seconds=30)
    thread, found = ask_in_thread(prompts)
    first, second = windows.clients
    shown(first)
    shown(second)

    started = time.monotonic()
    prompts.window_gone(first)
    thread.join(0.5)
    assert thread.is_alive()  # One window can still answer.

    prompts.window_gone(second)
    thread.join(5)

    assert not thread.is_alive()
    assert found["answer"] is None
    assert time.monotonic() - started < 5
    assert prompts.answer(shown(first)["id"], "send") is False


def test_a_window_that_never_saw_the_question_does_not_end_it():
    windows = Windows(1)
    prompts = OwnerPrompts(windows.deliver, seconds=30)
    thread, found = ask_in_thread(prompts)
    shown(windows.clients[0])

    prompts.window_gone(object())
    thread.join(0.5)
    assert thread.is_alive()

    prompts.close()
    thread.join(5)
    assert not thread.is_alive()
