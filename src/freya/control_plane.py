import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from freya import host_checks
from freya.storage import backup
from freya.config import AUDIT_CHECKPOINT_PATH
from freya.config import BACKGROUND_MODEL
from freya.config import COMFYUI_COMMAND
from freya.config import CONTEXT_WINDOW_TOKENS
from freya.config import EMBEDDING_MODEL
from freya.config import MODEL
from freya.config import WORLD_VIEW_ENABLED
from freya.config import WORLD_VIEW_URL
from freya.core.audit import (
    AuditStartup,
    FileCheckpointSink,
    SqliteAuditLog,
)
from freya.core.capabilities import (
    LOCAL_OWNER,
    CapabilityBroker,
    CapabilityDefinition,
    CapabilityRegistry,
)
from freya.core.conversation_memory import CONVERSATION_RECALL
from freya.core.persistence import (
    CONTROL_DATABASE_PATH,
    ControlStore,
)
from freya.core.policy import (
    SqlitePolicyEngine,
    seed_alpha_policy,
)
from freya.core.model_runtime import (
    LLM_BACKGROUND_CHAT,
    ROLE_BACKGROUND,
    ModelRuntime,
    configure_model_runtime,
)
from freya.core.providers import (
    ProviderDefinition,
    ProviderHealth,
    ProviderRegistry,
    ProviderState,
)
from freya.image.image_generator import (
    COMFYUI_URL,
)
from freya.providers.image.comfyui import (
    COMFYUI_PROVIDER,
    IMAGE_GENERATE,
    ComfyUIServer,
    create_comfyui_provider,
)
from freya.providers.model.ollama import (
    OLLAMA_BACKGROUND_CAPABILITIES,
    OLLAMA_CAPABILITIES,
    OLLAMA_PROVIDER,
    create_ollama_provider,
    register_ollama_capabilities,
    warm_ollama_models,
)
from freya.tools.world_view import (
    WORLD_VIEW_CAPABILITIES,
    WORLD_VIEW_PROVIDER,
    WorldView,
    create_world_view_provider,
    register_world_view_capabilities,
)
from freya.tools.file_tools import (
    FILE_READER_CAPABILITIES,
    FILE_READER_PROVIDER,
    create_file_reader_provider,
    register_file_reader_capabilities,
)
from freya.tools.model_tools import (
    MODEL_TOOL,
    TOOL_PARAMETERS,
    ModelToolHost,
)


IMAGE_SERVICE_PROVIDER = "image-service-local"

# Freya's own record of past conversations and saved memories. Reading
# it for the model is the one capability, CONVERSATION_RECALL.
CONVERSATION_PROVIDER = "conversation-memory-local"
from freya.tools.system_tools import (
    SYSTEM_TOOL_CAPABILITIES,
    SYSTEM_TOOLS_PROVIDER,
    create_system_tools_provider,
    register_system_tools,
)


@dataclass
class ControlPlane:
    """
    The pieces that stand between a request and a capability.
    """

    store: ControlStore
    providers: ProviderRegistry
    capabilities: CapabilityRegistry
    policy: SqlitePolicyEngine
    audit: SqliteAuditLog
    broker: CapabilityBroker
    audit_startup: AuditStartup
    model_runtime: ModelRuntime
    model_tools: ModelToolHost
    # ComfyUI is started from more than one place (as Freya comes
    # online, and when a picture is asked for). Only one may start it
    # at a time, or two servers would be launched.
    image_start_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False,
    )

    def start_image_backend_early(self, announce=print) -> bool:
        """
        Start ComfyUI as Freya comes online, so the first picture
        does not wait for it. Returns whether it is now running.

        Only a server Freya can start without a window is started
        this way, and no image model is loaded: ComfyUI loads one
        when a picture is first asked for. A failure is announced
        once, with the reason, and is not an error: the next request
        for a picture tries again.
        """
        provider = self.providers.get(COMFYUI_PROVIDER)

        if not (provider.metadata["can_start"] and provider.metadata["headless"]):
            return False

        with self.image_start_lock:
            self.providers.check_health(COMFYUI_PROVIDER)

            if self.providers.get(COMFYUI_PROVIDER).health == ProviderHealth.HEALTHY:
                self.providers.set_state(COMFYUI_PROVIDER, ProviderState.RUNNING)
                return True

            result = self.providers.start(COMFYUI_PROVIDER)

            if not result.success:
                announce(
                    "Freya: I could not start ComfyUI, so I cannot make "
                    f"pictures yet. {result.error}"
                )
                return False

            self.providers.check_health(COMFYUI_PROVIDER)
            return True

    def warm_models(self) -> None:
        """
        Load the Ollama models ahead of the first request.
        """
        metadata = self.providers.get(OLLAMA_PROVIDER).metadata

        warm_ollama_models(
            metadata["chat_model"],
            metadata["embedding_model"],
            metadata["background_model"],
            metadata["context_tokens"],
        )

    def register_image_generation(self, generate) -> None:
        """
        Offer image generation as a capability the model can call.

        The capability belongs to Freya's own image service, which
        starts ComfyUI on demand and hands the GPU back afterwards, so
        it is available whenever ComfyUI is running or can be started.
        """
        comfyui = self.providers.get(COMFYUI_PROVIDER)

        def service_health():
            self.providers.check_health(COMFYUI_PROVIDER)

            return bool(
                comfyui.health == ProviderHealth.HEALTHY
                or comfyui.metadata["can_start"]
            )

        self.providers.register(
            ProviderDefinition(
                provider_id=IMAGE_SERVICE_PROVIDER,
                name="Freya image service",
                capabilities=[IMAGE_GENERATE],
                start_handler=lambda: None,
                health_handler=service_health,
            )
        )
        self.providers.start(IMAGE_SERVICE_PROVIDER)
        self.providers.check_health(IMAGE_SERVICE_PROVIDER)

        self.capabilities.register(
            CapabilityDefinition(
                name=IMAGE_GENERATE,
                description=(
                    "Generates a picture from a text description and "
                    "shows it in the Images tab of Freya's window. "
                    "Use this whenever the user asks for an image, "
                    "picture, drawing, illustration, or photo. It "
                    "takes about twenty seconds."
                ),
                provider=IMAGE_SERVICE_PROVIDER,
                handler=generate,
                category="image",
                requires_gpu=True,
                optional_dependencies=[COMFYUI_PROVIDER],
                metadata={
                    MODEL_TOOL: True,
                    TOOL_PARAMETERS: {
                        "prompt": {
                            "description": (
                                "A detailed description of the "
                                "picture to generate."
                            ),
                            "max_length": 1000,
                        },
                    },
                },
            )
        )

        seed_alpha_policy(
            self.policy,
            "alpha-image-generate",
            LOCAL_OWNER.actor,
            [IMAGE_GENERATE],
        )

    def register_media(self, media) -> None:
        """Register media without granting policy permissions."""
        provider = media.provider()
        self.providers.register(provider)
        self.providers.start(provider.provider_id)
        self.providers.check_health(provider.provider_id)
        allowed = self.policy.decide(LOCAL_OWNER.actor, "media.recall").allow
        for capability in media.capabilities(offer_recall=allowed):
            self.capabilities.register(capability)

    def register_conversation_recall(self, recall) -> None:
        """
        Offer the model a way to look up what was said in earlier
        conversations. It only reads Freya's own record of them.

        Freya's own read of her memories and conversations for a
        reply goes through the same capability, so removing its
        policy rule stops both.
        """
        self.providers.register(
            ProviderDefinition(
                provider_id=CONVERSATION_PROVIDER,
                name="Freya conversation memory",
                capabilities=[CONVERSATION_RECALL],
                start_handler=lambda: None,
                health_handler=lambda: True,
            )
        )
        self.providers.start(CONVERSATION_PROVIDER)
        self.providers.check_health(CONVERSATION_PROVIDER)

        self.capabilities.register(
            CapabilityDefinition(
                name=CONVERSATION_RECALL,
                description=(
                    "Searches the word-for-word record of your earlier "
                    "conversations with the user and returns the "
                    "matching passages. Use it when the user asks what "
                    "was said, decided, or mentioned before, or refers "
                    "to an earlier conversation, and you need the exact "
                    "details. Search with a few distinctive words."
                ),
                provider=CONVERSATION_PROVIDER,
                handler=recall,
                category="memory",
                metadata={
                    MODEL_TOOL: True,
                    TOOL_PARAMETERS: {
                        "query": {
                            "description": (
                                "A few words that would appear in the "
                                "conversation being looked for."
                            ),
                            "max_length": 200,
                        },
                    },
                },
            )
        )

        seed_alpha_policy(
            self.policy,
            "alpha-conversation-recall",
            LOCAL_OWNER.actor,
            [CONVERSATION_RECALL],
        )

    def register_file_reader(self, reader) -> None:
        """
        Offer the model a way to list, read and search text files in
        the folders the owner has opened. It only reads.

        The provider is healthy only while a folder is open, so until
        the owner opens one the tools are not offered at all. Call
        `refresh_file_reader` after the owner opens or closes one.
        """
        self.providers.register(create_file_reader_provider(reader))
        self.providers.start(FILE_READER_PROVIDER)
        self.providers.check_health(FILE_READER_PROVIDER)
        register_file_reader_capabilities(self.capabilities, reader)

        seed_alpha_policy(
            self.policy,
            "alpha-file-read",
            LOCAL_OWNER.actor,
            FILE_READER_CAPABILITIES,
        )

    def register_world_view(self, view) -> None:
        """
        Offer the model live public data about the world, read from a
        God's Eye View program on this machine. Every tool only reads,
        except the one that opens the globe in the user's browser.

        The tools are offered only while that program answers, which is
        asked again each time tools are offered, because the owner
        starts and stops it apart from Freya.
        """
        self.providers.register(create_world_view_provider(view))
        self.providers.start(WORLD_VIEW_PROVIDER)
        self.providers.check_health(WORLD_VIEW_PROVIDER)
        register_world_view_capabilities(self.capabilities, view)

        for capability in WORLD_VIEW_CAPABILITIES:
            self.model_tools.add_offer_condition(
                capability, view.is_running
            )

        seed_alpha_policy(
            self.policy,
            "alpha-world-view",
            LOCAL_OWNER.actor,
            WORLD_VIEW_CAPABILITIES,
        )

    def refresh_file_reader(self) -> None:
        if self.providers.get(FILE_READER_PROVIDER) is not None:
            self.providers.check_health(FILE_READER_PROVIDER)

    def register_avatar(self, avatar) -> None:
        """
        Offer the spatial avatar's capabilities: showing her, reading
        the layout of the displays, and pointing.

        These are not offered to the model as tools. They are reached
        from the window, through the broker. The avatar has no
        capability for capturing the screen or sending input, and this
        seeds no permission for either.
        """
        definition = avatar.provider()
        self.providers.register(definition)
        self.providers.start(definition.provider_id)
        self.providers.check_health(definition.provider_id)

        for capability in avatar.capabilities():
            self.capabilities.register(capability)

        # One seed per addition: a seed that has been applied is never
        # applied again, so a capability added later needs its own.
        seed_alpha_policy(
            self.policy,
            "alpha-avatar",
            LOCAL_OWNER.actor,
            ["avatar.display", "screen.geometry.read", "avatar.point"],
        )
        seed_alpha_policy(
            self.policy,
            "alpha-avatar-library",
            LOCAL_OWNER.actor,
            ["avatar.library"],
        )
        seed_alpha_policy(
            self.policy,
            "alpha-avatar-creator",
            LOCAL_OWNER.actor,
            ["avatar.search", "avatar.prepare"],
        )

    def ensure_image_backend(self, announce=print) -> None:
        """
        Make sure ComfyUI is running, starting it if needed. Raises
        RuntimeError when it is not running and cannot be started.
        """
        with self.image_start_lock:
            self._ensure_image_backend(announce)

    def _ensure_image_backend(self, announce) -> None:
        self.providers.check_health(COMFYUI_PROVIDER)
        provider = self.providers.get(COMFYUI_PROVIDER)

        if provider.health != ProviderHealth.HEALTHY:
            if provider.metadata["can_start"]:
                announce(
                    "Freya: ComfyUI is not running, so I am starting "
                    "it. This can take a minute..."
                )

                if not provider.metadata["headless"]:
                    announce(
                        "Freya: Its window may need you to press "
                        "its start button."
                    )

            result = self.providers.start(COMFYUI_PROVIDER)

            if not result.success:
                raise RuntimeError(result.error)

            self.providers.check_health(COMFYUI_PROVIDER)
            announce("Freya: ComfyUI is ready.")
            return

        # It was started outside Freya; record that it is up.
        self.providers.set_state(
            COMFYUI_PROVIDER,
            ProviderState.RUNNING,
        )

    def startup_messages(self) -> List[str]:
        """
        Things the operator should be told at startup.
        """
        verification = self.audit_startup.verification
        messages = []

        health = self.providers.get(OLLAMA_PROVIDER).health

        if health == ProviderHealth.OFFLINE:
            messages.append(
                "Ollama is not reachable. Freya cannot reply until "
                "it is running; she will check again on your next "
                "message."
            )
        elif health != ProviderHealth.HEALTHY:
            messages.append(
                "Ollama is running but a required model is not "
                "installed. Freya cannot reply until all her models "
                "are available."
            )

        if not verification.ok:
            messages.append(
                "AUDIT LOG FAILED VERIFICATION. Tools are disabled "
                "until this is resolved."
            )
            messages.extend(
                f"  - {problem}"
                for problem in verification.problems[:10]
            )

        messages.extend(
            f"Audit warning: {warning}"
            for warning in verification.warnings
        )

        if self.audit_startup.reconciled:
            messages.append(
                f"Audit: {self.audit_startup.reconciled} interrupted "
                "operation(s) were recorded as unknown-outcome."
            )

        # Things about this PC that can quietly stop her tools working.
        messages.extend(
            host_checks.startup_warnings(host_checks._read_smart_app_control)
        )
        # A backup that failed at the last close, or an old one.
        messages.extend(backup.startup_warning(self.store.path.parent))

        return messages

    def close(self) -> None:
        configure_model_runtime(None)

        # Stops the ComfyUI server only if Freya started it.
        self.providers.stop(COMFYUI_PROVIDER)

        try:
            if self.audit.integrity_ok:
                self.audit.checkpoint()
        finally:
            self.store.close()


def build_local_control_plane(
    database_path=CONTROL_DATABASE_PATH,
    checkpoint_path=AUDIT_CHECKPOINT_PATH,
    world_view=None,
) -> ControlPlane:
    """
    Assemble the single-workstation control plane: providers,
    capabilities, policy, audit, and the broker in front of them.
    """
    store = ControlStore(database_path)

    providers = ProviderRegistry()
    providers.register(create_system_tools_provider())
    providers.start(SYSTEM_TOOLS_PROVIDER)
    providers.check_health(SYSTEM_TOOLS_PROVIDER)

    providers.register(
        create_ollama_provider(
            MODEL, EMBEDDING_MODEL, BACKGROUND_MODEL, CONTEXT_WINDOW_TOKENS,
        )
    )
    providers.start(OLLAMA_PROVIDER)
    providers.check_health(OLLAMA_PROVIDER)

    # ComfyUI is started on demand, when an image is first requested.
    comfyui = ComfyUIServer(
        COMFYUI_URL,
        COMFYUI_COMMAND,
        log_path=Path(database_path).parent / "comfyui_server.log",
    )
    providers.register(create_comfyui_provider(comfyui))

    capabilities = CapabilityRegistry(providers)
    register_system_tools(capabilities)
    register_ollama_capabilities(
        capabilities,
        MODEL,
        EMBEDDING_MODEL,
        BACKGROUND_MODEL,
        CONTEXT_WINDOW_TOKENS,
    )
    own_background = capabilities.get(LLM_BACKGROUND_CHAT) is not None

    policy = SqlitePolicyEngine(store)
    seed_alpha_policy(
        policy,
        "alpha-system-tools",
        LOCAL_OWNER.actor,
        SYSTEM_TOOL_CAPABILITIES,
    )
    # Tools added after the first seed need a seed of their own, because
    # a seed that has already been applied is never applied again.
    seed_alpha_policy(
        policy,
        "alpha-system-hardware",
        LOCAL_OWNER.actor,
        ["system.cpu", "system.memory", "system.hardware"],
    )
    seed_alpha_policy(
        policy,
        "alpha-system-sensors",
        LOCAL_OWNER.actor,
        ["system.sensors"],
    )
    seed_alpha_policy(
        policy,
        "alpha-model-runtime",
        LOCAL_OWNER.actor,
        OLLAMA_CAPABILITIES,
    )
    if own_background:
        seed_alpha_policy(
            policy,
            "alpha-model-background",
            LOCAL_OWNER.actor,
            OLLAMA_BACKGROUND_CAPABILITIES,
        )

    sink = (
        FileCheckpointSink(checkpoint_path)
        if checkpoint_path
        else None
    )
    audit = SqliteAuditLog(store, sink=sink)
    audit_startup = audit.start()

    broker = CapabilityBroker(
        capabilities,
        policy,
        audit,
        LOCAL_OWNER,
    )

    # Everything in this process that needs model work asks for it
    # through the broker.
    model_runtime = ModelRuntime(
        broker,
        refresh_health=lambda: providers.check_health(OLLAMA_PROVIDER),
        routes=(
            {ROLE_BACKGROUND: tuple(OLLAMA_BACKGROUND_CAPABILITIES)}
            if own_background
            else None
        ),
    )
    configure_model_runtime(model_runtime)

    plane = ControlPlane(
        model_runtime=model_runtime,
        model_tools=ModelToolHost(capabilities, broker),
        store=store,
        providers=providers,
        capabilities=capabilities,
        policy=policy,
        audit=audit,
        broker=broker,
        audit_startup=audit_startup,
    )

    # Off unless the owner turned it on: these tools' answers come from
    # the internet.
    if world_view is None and WORLD_VIEW_ENABLED:
        world_view = WorldView(WORLD_VIEW_URL)

    if world_view is not None:
        plane.register_world_view(world_view)

    return plane
