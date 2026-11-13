import pytest

from freya.config import MODEL
from freya.core.model_runtime import configure_model_runtime


class FakeOllama:
    """
    Stands in for the ollama client so tests never need a running
    Ollama server.
    """

    def __init__(self):
        self.installed = [MODEL, "gpt-oss:20b", "nomic-embed-text:latest"]
        self.reachable = True
        self.calls = []
        self.reply = "Hello from the fake model."
        self.json_reply = '{"remember": false}'
        self.fail_mid_stream = False
        # Scripted responses for successive streamed chats. A str is a
        # reply; a list of (name, arguments) pairs is a tool-call request.
        self.stream_script = []

    def _check(self):
        if not self.reachable:
            raise ConnectionError("Failed to connect to Ollama.")

    def list(self):
        self._check()
        return {"models": [{"model": name} for name in self.installed]}

    def generate(self, model, **kwargs):
        self._check()
        self.calls.append(("generate", model, kwargs))
        return {"response": ""}

    def embed(self, model, input, **kwargs):
        self._check()
        self.calls.append(("embed", model, input))
        return {"embeddings": [[0.1, 0.2, 0.3]]}

    def chat(self, model, messages, stream=False, **kwargs):
        self._check()
        self.calls.append(("chat", model, messages, stream, kwargs))

        if not stream:
            content = (
                self.json_reply
                if kwargs.get("format") == "json"
                and self.reply == "Hello from the fake model."
                else self.reply
            )
            return {"message": {"content": content}}

        scripted = self.stream_script.pop(0) if self.stream_script else self.reply

        if isinstance(scripted, list):
            return iter(
                [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {"function": {"name": name, "arguments": arguments}}
                                for name, arguments in scripted
                            ],
                        }
                    }
                ]
            )

        return self._stream(scripted)

    def _stream(self, reply):
        words = reply.split(" ")
        for index, word in enumerate(words):
            if self.fail_mid_stream and index == 2:
                raise ConnectionError("Ollama went away.")
            yield {"message": {"content": word + " "}}
        yield {"message": {"content": ""}}


@pytest.fixture
def fake_ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr("freya.providers.model.ollama.ollama", fake)
    return fake


@pytest.fixture(autouse=True)
def reset_model_runtime():
    yield
    configure_model_runtime(None)
