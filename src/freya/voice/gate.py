"""
The voice path as capabilities.

Wake, a transcript that may lead to tools, speech, and the Voice tab
each go through the capability broker. Policy is deny-by-default and
every call is audited. A spoken turn does not gain a tool: tools still
run only through the model tool host, on the same broker a typed turn
uses.

The alpha seed records the authority this path already had before it
was brokered (the local owner may wake, hear, speak, and use the Voice
tab). It names no tool. Removing the seed withdraws the voice path.
"""

from freya.core.capabilities import CapabilityDefinition, declare
from freya.core.policy.engine import seed_alpha_policy
from freya.core.providers import ProviderDefinition
from freya.core.capabilities.broker import LOCAL_OWNER


VOICE_PROVIDER = "voice-local"
VOICE_WAKE = "voice.wake"
VOICE_TURN = "voice.turn"
VOICE_SPEAK = "voice.speak"
VOICE_STUDIO = "voice.studio"

VOICE_CAPABILITIES = (
    VOICE_WAKE,
    VOICE_TURN,
    VOICE_SPEAK,
    VOICE_STUDIO,
)

VOICE_POLICY_SEED = "alpha-voice"

NOT_AUTHORIZED = "Not authorized."


class VoiceGate:
    """
    The checks a voice-triggered action has to pass.

    ``accept_wake`` and ``accept_turn`` authorize and return. They do
    not run a tool. Speech and the Voice tab run only inside the
    capability the broker already allowed.
    """

    def __init__(self, broker):
        self._broker = broker

    def accept_wake(self) -> bool:
        return self._broker.invoke(VOICE_WAKE).success

    def accept_turn(self, transcript: str) -> bool:
        if not isinstance(transcript, str):
            return False

        text = transcript.strip()

        if not text:
            return False

        return self._broker.invoke(VOICE_TURN, text).success

    def speak(self, text: str):
        result = self._broker.invoke(VOICE_SPEAK, text)

        if not result.success:
            return None

        return result.result

    def studio(self, message: dict) -> dict:
        result = self._broker.invoke(VOICE_STUDIO, message)

        if result.success and isinstance(result.result, dict):
            return result.result

        return {
            "state": None,
            "clip": None,
            "reference": None,
            "error": NOT_AUTHORIZED,
        }


def install_voice_gate(plane, voice, studio_handle, *, seed=True) -> VoiceGate:
    """
    Register the voice capabilities on a control plane and send this
    voice object's ``speak`` through the broker.

    ``studio_handle`` is the Voice tab's existing handler. It runs
    only after ``voice.studio`` is allowed. ``seed`` applies the alpha
    voice seed; tests that prove denial pass ``seed=False``.
    """
    original_speak = voice.speak

    def wake():
        return {"accepted": True}

    def turn(transcript):
        # The words are not a tool request. A tool runs later, if the
        # model asks, through the same host a typed turn uses. Only
        # the length comes back; the words stay out of the result.
        if not isinstance(transcript, str) or not transcript.strip():
            raise ValueError("A spoken turn needs the heard words.")

        return {"accepted": True, "characters": len(transcript.strip())}

    def speak(text):
        original_speak(text)
        return {"spoken": True}

    def studio(message):
        if not isinstance(message, dict):
            return {
                "state": None,
                "clip": None,
                "reference": None,
                "error": NOT_AUTHORIZED,
            }

        return studio_handle(message)

    plane.providers.register(
        ProviderDefinition(
            provider_id=VOICE_PROVIDER,
            name="Local voice",
            capabilities=list(VOICE_CAPABILITIES),
            start_handler=lambda: None,
            health_handler=lambda: True,
        )
    )
    plane.providers.start(VOICE_PROVIDER)
    plane.providers.check_health(VOICE_PROVIDER)

    handlers = {
        VOICE_WAKE: (
            "Accepts that a heard sentence was addressed to Freya, "
            "or that her name alone opened the attention window. "
            "It does not run a tool.",
            wake,
        ),
        VOICE_TURN: (
            "Accepts one transcript as the start of a turn. Tools "
            "the turn later uses are separate capabilities, checked "
            "again on their own.",
            turn,
        ),
        VOICE_SPEAK: (
            "Speaks one piece of text with Freya's voice on this "
            "machine.",
            speak,
        ),
        VOICE_STUDIO: (
            "Carries out one request from the Voice tab: preview, "
            "save, or speak a voice on this machine. It is not a "
            "model tool.",
            studio,
        ),
    }

    # Wake and a transcript only accept what was already heard. Speech
    # drives the speaker, and the Voice tab writes profiles and clips.
    # The alpha seed names each one for the local owner.
    declarations = {
        VOICE_WAKE: declare((
            "none",
            "accepts that a heard sentence was addressed to Freya; it does not read the microphone or run a tool",
        )),
        VOICE_TURN: declare((
            "none",
            "one transcript, which is not stored here and is not a tool request",
        )),
        VOICE_SPEAK: declare(
            ("device", "this machine's audio output"),
            effect="consequential",
        ),
        VOICE_STUDIO: declare(
            (
                "filesystem",
                "saved voice profiles, clips, and reference recordings on this machine",
            ),
            ("device", "playback while a voice is previewed"),
            effect="consequential",
        ),
    }

    for name, (description, handler) in handlers.items():
        plane.capabilities.register(
            CapabilityDefinition(
                name=name,
                description=description,
                provider=VOICE_PROVIDER,
                handler=handler,
                category="voice",
                declaration=declarations[name],
            )
        )

    if seed:
        seed_alpha_policy(
            plane.policy,
            VOICE_POLICY_SEED,
            LOCAL_OWNER.actor,
            VOICE_CAPABILITIES,
        )

    gate = VoiceGate(plane.broker)
    voice.speak = gate.speak
    return gate
