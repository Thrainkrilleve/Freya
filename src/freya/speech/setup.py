import os
import subprocess

from freya.child_environment import child_environment
from freya.speech.chatterbox_engine import ChatterboxEngine
from freya.speech.engines import planned_engines
from freya.speech.kokoro_engine import KokoroEngine
from freya.speech.qwen_engine import QwenEngine
from freya.speech.references import ReferenceStore
from freya.speech.studio import VoiceStudio


def free_gpu_megabytes():
    """
    Free memory on the graphics card, or None when it cannot be read.
    """
    try:
        output = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=child_environment(),
            creationflags=(
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            ),
        ).stdout

        return int(output.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def make_room_on_gpu(megabytes_needed: int) -> None:
    """
    Before a large speech engine loads: if the graphics card is too
    full for it, unload the language model. Ollama loads it again the
    next time Freya is asked something, as it does after an image.
    """
    free = free_gpu_megabytes()

    if free is None or free >= megabytes_needed:
        return

    try:
        from freya.gpu.resource_manager import GPUResourceManager

        GPUResourceManager().unload_all_ollama_models()
    except Exception as exc:
        print(f"Voice: could not free graphics memory: {exc}")


def build_voice_studio(
    voice,
    profiles=None,
    clips=None,
    references=None,
) -> VoiceStudio:
    """
    Assemble the voice studio around Freya's live voice.

    Kokoro is ready out of the box. Chatterbox and Qwen3-TTS are ready
    once their own environments exist; until then they are listed as
    not installed. ElevenLabs is listed as not set up.
    """
    pipeline = getattr(voice, "pipeline", None)
    references = references or ReferenceStore()

    kokoro = KokoroEngine(
        shared_model=getattr(pipeline, "model", None),
    )

    def apply_live_voice(profile):
        if not getattr(voice, "enabled", True):
            return

        if profile is None:
            voice.reset_voice()
            return

        settings = profile["settings"]

        voice.set_voice(
            kokoro.voice_for(profile["kind"], settings),
            settings["speed"],
            settings["language"],
        )

    cloud = [
        engine for engine in planned_engines() if engine.id == "elevenlabs"
    ]

    studio = VoiceStudio(
        [
            kokoro,
            ChatterboxEngine(references, make_room=make_room_on_gpu),
            QwenEngine(references, make_room=make_room_on_gpu),
        ]
        + cloud,
        profiles=profiles,
        clips=clips,
        apply_live_voice=apply_live_voice,
        references=references,
    )
    # Recordings left over from voices that were never saved.
    studio.forget_unsaved_recordings()

    return studio
