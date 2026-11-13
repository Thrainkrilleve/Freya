from typing import Callable, Iterator, List, Optional


LLM_CHAT = "llm.chat"
LLM_CHAT_STREAM = "llm.chat_stream"
LLM_EMBED = "llm.embed"
LLM_BACKGROUND_CHAT = "llm.background.chat"
LLM_BACKGROUND_CHAT_STREAM = "llm.background.chat_stream"

# The kinds of model work a caller can ask for. A caller names the
# role; which capability, provider, and model serve it is decided
# where the control plane is assembled.
ROLE_CONVERSATION = "conversation"
ROLE_BACKGROUND = "background"

DEFAULT_ROUTES = {
    ROLE_CONVERSATION: (LLM_CHAT, LLM_CHAT_STREAM),
    ROLE_BACKGROUND: (LLM_CHAT, LLM_CHAT_STREAM),
}


class ModelRuntimeUnavailable(RuntimeError):
    """
    A model request was not performed, or failed. The message is safe
    to show to the user.
    """


class ModelRuntime:
    """
    Freya's way of asking for model work.

    Every request goes through the capability broker, so it is subject
    to policy, provider availability, and audit like any other
    capability. Nothing here knows which provider serves the request.

    `routes` maps a role to its (chat, streamed chat) capability
    names. A role left out is served like the conversation.
    """

    def __init__(
        self,
        broker,
        refresh_health: Optional[Callable[[], None]] = None,
        routes=None,
    ):
        self._broker = broker
        self._refresh_health = refresh_health
        self._routes = {**DEFAULT_ROUTES, **(routes or {})}

    def _route(self, role):
        if role not in self._routes:
            raise ValueError(f"Unknown model role: {role!r}")
        return self._routes[role]

    def chat(self, messages, role=ROLE_CONVERSATION, **options) -> str:
        return self._invoke(self._route(role)[0], messages, **options)

    def chat_stream(
        self,
        messages,
        role=ROLE_CONVERSATION,
        **options,
    ) -> Iterator[str]:
        chunks = self._invoke(self._route(role)[1], messages, **options)

        try:
            yield from chunks
        except Exception as exc:
            self._refresh()
            raise ModelRuntimeUnavailable(
                f"The language model stopped responding: {exc}"
            ) from exc

    def embed(self, text) -> List[float]:
        return self._invoke(LLM_EMBED, text)

    def _refresh(self):
        if self._refresh_health is not None:
            self._refresh_health()

    def _invoke(self, capability, *args, **kwargs):
        result = self._broker.invoke(capability, *args, **kwargs)

        if result.outcome == "unavailable":
            # The provider may have come up since it was last checked.
            self._refresh()
            result = self._broker.invoke(capability, *args, **kwargs)

        if result.success:
            return result.result

        if result.outcome not in ("denied", "unavailable"):
            # A failed call may mean the provider has gone away.
            self._refresh()

        raise ModelRuntimeUnavailable(
            f"The language model is not available "
            f"({capability}): {result.error}"
        )


_runtime: Optional[ModelRuntime] = None


def configure_model_runtime(runtime: Optional[ModelRuntime]) -> None:
    global _runtime
    _runtime = runtime


def get_model_runtime() -> ModelRuntime:
    if _runtime is None:
        raise ModelRuntimeUnavailable(
            "The model runtime has not been configured."
        )
    return _runtime
