import ollama as ollama_library

from freya.core.capabilities import (
    CapabilityDefinition,
)
from freya.core.model_runtime import (
    LLM_BACKGROUND_CHAT,
    LLM_BACKGROUND_CHAT_STREAM,
    LLM_CHAT,
    LLM_CHAT_STREAM,
    LLM_EMBED,
)
from freya.core.providers import (
    ProviderDefinition,
    ProviderHealth,
)


OLLAMA_PROVIDER = "ollama-local"

# "Local" has to be true whatever the environment says, because full
# conversation and memory context goes to this provider. The client is
# pinned to this machine and ignores OLLAMA_HOST and proxy settings.
LOCAL_OLLAMA_HOST = "http://127.0.0.1:11434"
ollama = ollama_library.Client(host=LOCAL_OLLAMA_HOST, trust_env=False)

OLLAMA_CAPABILITIES = [
    LLM_CHAT,
    LLM_CHAT_STREAM,
    LLM_EMBED,
]

# Offered only when background work is given a model of its own.
OLLAMA_BACKGROUND_CAPABILITIES = [
    LLM_BACKGROUND_CHAT,
    LLM_BACKGROUND_CHAT_STREAM,
]


def _with_tag(model):
    return model if ":" in model else f"{model}:latest"


def runs_elsewhere(model) -> bool:
    """
    Whether a model served by the local Ollama program actually runs
    on someone else's servers. Ollama offers such models under a
    "cloud" tag and reports where they run.
    """
    name = str(model).lower()

    if name.endswith(("-cloud", ":cloud")) or ":cloud-" in name:
        return True

    show = getattr(ollama, "show", None)

    if show is None:
        return False

    try:
        details = show(model)
    except Exception:
        # Where it runs could not be established, so it is not
        # treated as local.
        return True

    def field(name):
        if isinstance(details, dict):
            return details.get(name)
        return getattr(details, name, None)

    return bool(field("remote_host") or field("remote_model"))


def check_ollama_health(models):
    """
    OFFLINE when the Ollama server cannot be reached, UNHEALTHY when
    it is up but a required model is not installed or would run on
    someone else's servers, else HEALTHY.
    """
    try:
        listed = ollama.list()
    except Exception:
        return ProviderHealth.OFFLINE

    if any(runs_elsewhere(model) for model in models):
        return ProviderHealth.UNHEALTHY

    installed = {
        _with_tag(entry["model"])
        for entry in listed["models"]
    }

    if all(_with_tag(model) in installed for model in models):
        return ProviderHealth.HEALTHY

    return ProviderHealth.UNHEALTHY


def installed_digests() -> dict:
    """
    The digest of each installed model, by name with its tag. A digest
    names one exact build of a model. Empty when the server cannot be
    asked.
    """
    try:
        listed = ollama.list()
    except Exception:
        return {}

    digests = {}

    for entry in listed["models"]:
        try:
            name, digest = entry["model"], entry["digest"]
        except (KeyError, TypeError):
            continue
        if name and digest:
            digests[_with_tag(name)] = digest

    return digests


def _has_own_model(chat_model, background_model):
    return bool(background_model) and (
        _with_tag(background_model) != _with_tag(chat_model)
    )


def warm_ollama_models(
    chat_model,
    embedding_model,
    background_model=None,
    context_tokens=None,
):
    """
    Ask Ollama to load the models so the first real request does not
    wait for them. An empty generate request loads a model without
    producing output. They are loaded at the context size the real
    requests will use; at any other size the first request would
    make Ollama load the model again.
    """
    sized = {"options": {"num_ctx": context_tokens}} if context_tokens else {}

    try:
        ollama.generate(model=chat_model, **sized)
        if _has_own_model(chat_model, background_model):
            ollama.generate(model=background_model, **sized)
        ollama.embed(
            model=embedding_model,
            input="warm-up",
        )
    except Exception:
        # Ollama may not be running yet; the first real call reports that.
        pass


def create_ollama_provider(
    chat_model,
    embedding_model,
    background_model=None,
    context_tokens=None,
):
    """
    Describe the local Ollama server as a model runtime provider.

    Ollama runs as its own service, so Freya has nothing to start or
    stop; the provider is usable whenever the health check passes.
    """
    own_background = _has_own_model(chat_model, background_model)
    models = [chat_model, embedding_model]
    capabilities = list(OLLAMA_CAPABILITIES)

    if own_background:
        models.append(background_model)
        capabilities.extend(OLLAMA_BACKGROUND_CAPABILITIES)

    return ProviderDefinition(
        provider_id=OLLAMA_PROVIDER,
        name="Local Ollama model runtime",
        capabilities=capabilities,
        start_handler=lambda: None,
        health_handler=lambda: check_ollama_health(models),
        metadata={
            "context_tokens": context_tokens,
            "chat_model": chat_model,
            "embedding_model": embedding_model,
            "background_model": (
                background_model if own_background else chat_model
            ),
        },
    )


def _chat_handlers(model, context_tokens=None):
    """
    The complete and streamed chat handlers for one model.

    Ollama loads a model for one context size and loads it again,
    which takes several seconds, whenever a request asks for another.
    A request that names no size gets Ollama's own default, so a turn
    that mixes sized and unsized requests reloads the model each time
    they alternate. Every request is therefore given `context_tokens`
    unless it names a size of its own.
    """

    def sized(options):
        if not context_tokens:
            return options

        return {
            **options,
            "options": {
                "num_ctx": context_tokens,
                **(options.get("options") or {}),
            },
        }

    def chat(messages, **options):
        response = ollama.chat(
            model=model,
            messages=messages,
            **sized(options),
        )
        return response["message"]["content"]

    def chat_stream(messages, **options):
        stream = ollama.chat(
            model=model,
            messages=messages,
            stream=True,
            **sized(options),
        )

        # Text arrives as str chunks. When the model asks for tools
        # instead, that request arrives as one dict.
        for chunk in stream:
            message = chunk.get("message", {})
            tool_calls = message.get("tool_calls")

            if tool_calls:
                yield {
                    "tool_calls": [
                        {
                            "name": call["function"]["name"],
                            "arguments": dict(
                                call["function"]["arguments"] or {}
                            ),
                        }
                        for call in tool_calls
                    ]
                }

            content = message.get("content", "")
            if content:
                yield content

    return chat, chat_stream


def register_ollama_capabilities(
    registry,
    chat_model,
    embedding_model,
    background_model=None,
    context_tokens=None,
):
    """
    Register the model capabilities Ollama provides. Callers name the
    capability, never the model or the Ollama client.

    Background work gets capabilities of its own only when it is given
    a different model, so each model's use is separately allowed and
    separately audited.
    """
    chat, chat_stream = _chat_handlers(chat_model, context_tokens)

    def embed(text):
        response = ollama.embed(
            model=embedding_model,
            input=text,
        )
        return response["embeddings"][0]

    entries = [
        (
            LLM_CHAT,
            "Generates a complete chat reply from the local "
            "language model.",
            chat,
        ),
        (
            LLM_CHAT_STREAM,
            "Generates a chat reply from the local language "
            "model as a stream of text chunks, or a request "
            "to call one of the tools it was offered.",
            chat_stream,
        ),
        (
            LLM_EMBED,
            "Returns an embedding vector for a piece of text.",
            embed,
        ),
    ]

    if _has_own_model(chat_model, background_model):
        background_chat, background_chat_stream = _chat_handlers(
            background_model, context_tokens,
        )
        entries.extend([
            (
                LLM_BACKGROUND_CHAT,
                "Generates a complete reply from the local model "
                "kept for background work such as summaries and "
                "memory decisions.",
                background_chat,
            ),
            (
                LLM_BACKGROUND_CHAT_STREAM,
                "Generates a reply from the local background model "
                "as a stream of text chunks.",
                background_chat_stream,
            ),
        ])

    for name, description, handler in entries:
        registry.register(
            CapabilityDefinition(
                name=name,
                description=description,
                provider=OLLAMA_PROVIDER,
                handler=handler,
                category="llm",
                requires_gpu=True,
            )
        )
