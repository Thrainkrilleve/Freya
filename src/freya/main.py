import re

import time

import urllib.request

import threading



from freya.core.assistant import FreyaAssistant

from freya.core.model_runtime import ROLE_BACKGROUND
from freya.core.model_runtime import ModelRuntimeUnavailable

from freya.gpu.resource_manager import GPUResourceManager

from freya.image.image_generator import FreyaImageGenerator

from freya.control_plane import build_local_control_plane
from freya.delegation.service import build_cloud_delegation, build_turn_delegation
from freya.storage.backup import back_up_on_close
from freya.config import OPEN_WINDOW, MODEL
from freya.config import COMFYUI_AT_START
from freya.file_access import FOLDER_COMMANDS, FileAccess
from freya.hands_free import LISTENING_COMMANDS
from freya.hands_free import describe_listening
from freya.hands_free import handle_listening_command
from freya.hands_free import load_speech_recognition
from freya.hands_free import start_background_listening
from freya.settings import Settings
from freya.turn_gate import TurnGate
from freya.events import EventBus
from freya.image.gallery import ImageGallery
from freya.window import WINDOW_COMMAND
from freya.window import FreyaWindow
from freya.window import WindowActions
from freya.media.setup import build_media_service
from freya.window import build_status
from freya.unreal_client import UnrealClient, configured_client
from freya.console_tee import ConsoleOutput
from freya.window_commands import CommandContext
from freya.window_commands import run_window_command
from freya.speech.setup import build_voice_studio

from freya.spatial_avatar.service import AvatarService
from freya.media.service import MEDIA_CAPABILITIES

from freya.core.capabilities import LOCAL_OWNER
from freya.single_instance import claim as claim_single_instance
from freya.single_instance import show_message
from freya.tools.tool_router import FreyaToolRouter

from freya.voice.stt import FreyaSTT

from freya.voice.tts import FreyaVoice





VOICE_FILE_COMMAND = "/voice-file"

TALK_COMMAND = "/talk"

LISTEN_COMMAND = "/listen"

CONVERSATION_COMMAND = "/conversation"

INTERRUPT_COMMAND = "/interrupt"

IMAGE_COMMAND = "/image"

COMMANDS = (

    VOICE_FILE_COMMAND,

    TALK_COMMAND,

    LISTEN_COMMAND,

    CONVERSATION_COMMAND,

    INTERRUPT_COMMAND,

    IMAGE_COMMAND,

) + LISTENING_COMMANDS + FOLDER_COMMANDS + (WINDOW_COMMAND,)



# Everything that happens in a conversation is announced here, so the

# window can show it. The console keeps printing as before.

EVENTS = EventBus()

# Freya's own copies of generated images; set up in main().

GALLERY = None

# How long shutting down waits for a spoken turn still in progress.

SHUTDOWN_WAIT_SECONDS = 30.0

# Set when Freya begins to shut down. A reply being written stops at

# its next piece instead of running to the end.

SHUTTING_DOWN = threading.Event()





def normalize_command(user_input):

    """

    Accept a command typed with a backslash, such as \\listen.

    Anything that is not a known command is returned unchanged.

    """

    if not user_input.startswith("\\"):

        return user_input

    words = user_input[1:].split(maxsplit=1)

    if not words:

        return user_input

    command = "/" + words[0].lower()

    if command not in COMMANDS:

        return user_input

    return "/" + user_input[1:]





def extract_complete_sentences(buffer):

    sentences = []

    sentence_end = re.compile(

        r"(.+?[.!?]+)(?:\s+|$)",

        re.DOTALL,

    )



    while True:

        match = sentence_end.match(buffer)



        if match is None:

            break



        sentence = match.group(1).strip()



        if sentence:

            sentences.append(sentence)



        buffer = buffer[

            match.end():

        ]



    return sentences, buffer





def stream_freya_response(

    freya,

    voice,

    user_input,

):

    print(

        "\nFreya: ",

        end="",

        flush=True,

    )

    EVENTS.publish("assistant_start")



    speech_buffer = ""



    reply = freya.respond_stream(

        user_input

    )

    for chunk in reply:

        if SHUTTING_DOWN.is_set():

            # Closing the reply ends the model call and records

            # its outcome while the audit log is still open.

            reply.close()

            speech_buffer = ""

            break

        print(

            chunk,

            end="",

            flush=True,

        )

        EVENTS.publish(

            "assistant_chunk",

            text=chunk,

        )



        speech_buffer += chunk



        sentences, speech_buffer = (

            extract_complete_sentences(

                speech_buffer

            )

        )



        for sentence in sentences:

            voice.speak(

                sentence

            )



    remaining_text = (

        speech_buffer.strip()

    )



    if remaining_text:

        voice.speak(

            remaining_text

        )

    EVENTS.publish("assistant_end")



    print("\n")





def get_voice_file_path(command):

    path = command[

        len(VOICE_FILE_COMMAND):

    ].strip()



    return path.strip('"')





def get_image_prompt(command):

    return command[

        len(IMAGE_COMMAND):

    ].strip()





def announce_image_failure(exc):

    EVENTS.publish("image_failed")

    EVENTS.publish(

        "notice",

        text=f"Image generation failed. {exc}",

    )




def keep_generated_image(prompt, result):

    """

    Copy a finished image into Freya's gallery and announce it.

    """

    if GALLERY is None or not result["image_urls"]:

        EVENTS.publish("image_failed")

        return


    try:

        with urllib.request.urlopen(

            result["image_urls"][0],

            timeout=30,

        ) as response:

            image = GALLERY.add(

                prompt,

                response.read(),

                seed=result.get("seed"),

            )

    except (OSError, ValueError) as exc:

        announce_image_failure(exc)

        return


    EVENTS.publish(

        "image_ready",

        image=image,

    )




def run_image_generation(

    image_generator,

    gpu_manager,

    prompt,

):

    if not prompt:

        print(

            "\nUsage: /image <description>\n"

        )

        return False



    print()

    try:

        # Start ComfyUI first if it is not running, so the language

        # model is not unloaded for an image that cannot be made.

        image_generator.ensure_running()

    except RuntimeError as exc:

        print(

            f"Freya: I can't generate images right now. {exc}\n"

        )

        EVENTS.publish(

            "notice",

            text=f"I can't generate images right now. {exc}",

        )

        return False



    print("Freya: Preparing GPU for image generation...")

    EVENTS.publish(

        "image_started",

        prompt=prompt,

    )

    print(f"Prompt: {prompt}")

    print()



    try:

        # Release Ollama's LLM and embedding models before

        # ComfyUI loads the much larger image model.

        gpu_manager.unload_all_ollama_models()



    except Exception as exc:

        print(

            "Freya: Could not release Ollama GPU resources."

        )

        print(

            f"Error: {exc}\n"

        )

        announce_image_failure(exc)

        return False



    print(

        "Freya: GPU ready. Generating image..."

    )

    print()



    try:

        result = image_generator.generate(

            prompt

        )



    except Exception as exc:

        print(

            "Freya: Image generation failed."

        )

        print(

            f"Error: {exc}\n"

        )

        announce_image_failure(exc)

        return False



    finally:

        # Whether generation succeeds or fails, ask ComfyUI

        # to release its models so the GPU is available for

        # Freya's next LLM request.

        try:

            gpu_manager.free_comfyui_memory()



        except Exception as exc:

            print(

                "GPU Manager warning: "

                "ComfyUI memory cleanup failed."

            )

            print(

                f"Cleanup error: {exc}"

            )



    print(

        "Freya: Image generated successfully."

    )

    keep_generated_image(

        prompt,

        result,

    )

    print(

        f"Seed: {result['seed']}"

    )



    for image in result["images"]:

        filename = image.get(

            "filename",

            "Unknown",

        )



        subfolder = image.get(

            "subfolder",

            "",

        )



        image_type = image.get(

            "type",

            "output",

        )



        print(

            f"Output: {filename}"

        )



        if subfolder:

            print(

                f"Subfolder: {subfolder}"

            )



        print(

            f"Type: {image_type}"

        )



    for url in result["image_urls"]:

        print(

            f"Image URL: {url}"

        )



    print()

    return True





def handle_tool_request(
    tool_router,
    image_generator,
    gpu_manager,
    user_input,
):
    tool_request = tool_router.route(
        user_input
    )

    if tool_request is None:
        return False

    if tool_request.tool == "image":
        run_image_generation(
            image_generator,
            gpu_manager,
            tool_request.prompt,
        )
        return True

    return False


def handle_normal_input(

    freya,

    voice,

    tool_router,

    image_generator,

    gpu_manager,

    user_input,

    source="typed",

):

    EVENTS.publish(

        "user",

        text=user_input,

        source=source,

    )

    # Kept with the record of the conversation.

    if freya is not None:

        freya.source = source

    tool_was_used = handle_tool_request(

        tool_router,

        image_generator,

        gpu_manager,

        user_input,

    )



    if tool_was_used:

        return



    try:

        stream_freya_response(

            freya,

            voice,

            user_input,

        )

    except ModelRuntimeUnavailable as exc:

        print(

            f"\n\nFreya: {exc}\n"

        )

        EVENTS.publish("assistant_end")

        EVENTS.publish(

            "notice",

            text=str(exc),

        )





def run_conversation_mode(

    freya,

    voice,

    stt,

    tool_router,

    image_generator,

    gpu_manager,

):

    print()

    print("Conversation mode started.")

    print(

        "Say 'Freya stop conversation' "

        "to return to keyboard mode."

    )

    print(

        "If no speech is detected for 10 seconds, "

        "conversation mode will also end."

    )



    while True:

        try:

            audio_path = stt.record_until_silence(

                wait_for_enter=False

            )



            transcript, info = stt.transcribe(

                audio_path

            )



        except RuntimeError as exc:

            if "No speech was detected" in str(exc):

                print(

                    "\nNo speech detected. "

                    "Leaving conversation mode.\n"

                )

                return



            raise



        transcript = transcript.strip()



        if not transcript:

            print(

                "\nNo speech was detected "

                "in that recording.\n"

            )

            continue



        print()

        print(

            "You (voice): "

            f"{transcript}"

        )

        print(

            "Detected language: "

            f"{info.language}"

        )



        normalized = (

            transcript.lower()

            .strip()

            .rstrip(".!?")

        )



        if normalized in {

            "freya stop conversation",

            "freya stop listening",

            "stop conversation",

            "exit conversation",

        }:

            print(

                "\nFreya: Conversation mode ended.\n"

            )

            voice.speak(

                "Conversation mode ended."

            )

            voice.wait_until_done()

            return



        handle_normal_input(

            freya,

            voice,

            tool_router,

            image_generator,

            gpu_manager,

            transcript,

            source="voice",

        )



        # Important for v1: do not reopen the microphone

        # until Freya has finished speaking. This prevents

        # her own TTS audio from being treated as user speech.

        voice.wait_until_done()





def main(window_only=False):

    """

    Run Freya. With window_only, there is no console: she is used

    through her window and by voice, and exits when it is closed.

    """

    global GALLERY


    if not claim_single_instance():

        message = "Freya is already running."

        if window_only:

            show_message(message)

        else:

            print(message)

        return



    console_output = ConsoleOutput(EVENTS)

    console_output.install()

    quit_requested = threading.Event()

    SHUTTING_DOWN.clear()

    control_plane = build_local_control_plane()

    for message in control_plane.startup_messages():

        print(message)

    # Load the Ollama models while the voice engine is starting up.

    threading.Thread(

        target=control_plane.warm_models,

        name="FreyaModelWarmup",

        daemon=True,

    ).start()

    # ComfyUI is started now, beside the model warm-up, so the first
    # picture does not wait a minute for it. It loads no image model
    # until a picture is asked for.
    if COMFYUI_AT_START:

        threading.Thread(

            target=control_plane.start_image_backend_early,

            name="FreyaImageBackendStart",

            daemon=True,

        ).start()



    freya = FreyaAssistant(

        tool_host=control_plane.model_tools,

        broker=control_plane.broker,

    )

    # The owner's cloud settings and the one gate a brief could leave
    # by. Assembling it sends nothing and allows nothing: no provider
    # is switched on, no policy rule allows a cloud route, and no
    # route has a usage limit until the owner sets them. Nothing in a
    # turn uses it yet; this is here so the Cloud tab works.
    try:

        cloud = build_cloud_delegation(

            control_plane,

            lambda: [

                (row[0], row[3])

                for row in freya.memory.get_all_memories()

            ],

            publish=EVENTS.publish,

        )

    except Exception as exc:

        cloud = None

        print(f"Cloud settings are unavailable: {exc}")

    # Lets the model hand one piece of work to Claude during a turn.
    # The tool is offered only while a provider is switched on and a
    # window is open to ask the owner in. The window is made further
    # on, so it is looked up when a question is asked.
    window = None

    if cloud is not None:

        try:

            freya.delegation = build_turn_delegation(

                control_plane,

                cloud,

                lambda prompt: (
                    window.ask_owner(prompt) if window is not None else None
                ),

                lambda: window is not None and window.has_window(),

            )

        except Exception as exc:

            print(f"Handing work to an outside model is unavailable: {exc}")

    voice = FreyaVoice()

    # The Voice tab: engines, saved voices, and the voice the owner

    # chose for Freya last time.

    voice_studio = build_voice_studio(voice)

    voice_studio.restore_live_voice()

    # The on-screen avatar: a separate overlay program. Its

    # capabilities go through the broker like any others, and it

    # is told when Freya starts and stops speaking.

    avatar = AvatarService(

        control_plane.broker,

        LOCAL_OWNER.actor,

        publish=EVENTS.publish,

    )

    control_plane.register_avatar(avatar)

    voice.speech_listener = avatar

    # Show her again if she was on screen when Freya last closed.

    if avatar.config.visible:

        avatar.handle({"action": "show"})

    stt = load_speech_recognition()

    settings = Settings()
    # The folders the owner has opened for Freya to read. None
    # to start, and then the file tools are not offered at all.
    file_access = FileAccess(
        settings,
        control_plane.refresh_file_reader,
    )
    control_plane.register_file_reader(file_access.reader)

    gate = TurnGate()

    GALLERY = ImageGallery()



    def announce_image_backend(message):

        print(message)

        EVENTS.publish(

            "notice",

            text=message.removeprefix("Freya: "),

        )


    image_generator = FreyaImageGenerator(

        ensure_running=lambda: control_plane.ensure_image_backend(

            announce=announce_image_backend,

        ),

    )

    tool_router = FreyaToolRouter()

    gpu_manager = GPUResourceManager()



    print("Freya online.")

    print("Voice: enabled" if voice.enabled else "Voice: unavailable (text chat ready)")

    print("Streaming: enabled")

    print("Async voice: enabled")

    print("Voice-file input: enabled")

    print("Push-to-talk: enabled")

    print("Automatic silence detection: enabled")

    print("Conversation mode: enabled")

    print("Interruptible TTS: enabled")

    print("Image generation: enabled")

    print("Natural-language tool routing: enabled")

    print("Tool registry: enabled")

    print("System tools: enabled")

    print("GPU resource manager: enabled")

    print()

    print("Use /talk for manual push-to-talk.")

    print("Use /listen for automatic silence detection.")

    print("Use /conversation for hands-free conversation.")

    print("Use /interrupt to stop current speech playback.")

    print(

        "Use /voice-file <path> "

        "to transcribe an audio file."

    )

    print(

        "Use /image <description> "

        "to generate an image."

    )

    print(

        "Or ask Freya naturally to "

        "create an image."

    )

    print("Use /names to see the names Freya answers to.")

    print("Use /window to open Freya's window.")

    print("Type 'exit' to quit.\n")



    def handle_spoken_request(request):

        # Runs on the listener thread while the keyboard waits.

        with gate.spoken_turn() as acquired:

            if not acquired:

                return

            if not request:

                print("\rYou (voice): Freya\n\nFreya: Yes?\n")

                voice.speak("Yes?")

            else:

                print(f"\rYou (voice): {request}")

                handle_normal_input(

                    freya,

                    voice,

                    tool_router,

                    image_generator,

                    gpu_manager,

                    request,

                    source="voice",

                )

            print("You: ", end="", flush=True)



    listener = start_background_listening(

        stt,

        voice,

        gate,

        settings,

        handle_spoken_request,

    )

    print(describe_listening(listener, settings))



    def current_status():

        return build_status(

            listener,

            settings,

            control_plane,

            voice=voice,

        )


    def publish_status():

        EVENTS.publish(

            "status",

            status=current_status(),

        )


    voice.set_volume(settings.voice_volume)



    def volume_from_window(value):

        try:

            voice.set_volume(settings.set_voice_volume(value))

        except ValueError:

            return



        publish_status()



    def client_closed(kind):

        # The Unreal window has gone. Freya keeps running, but with

        # the microphone muted, so it is never open with nothing on

        # screen to show it. It stays muted until the owner unmutes.

        if kind == "native" and listener is not None and listener.running:

            if not listener.muted:

                listener.mute()

                print(

                    "\nFreya: My window closed, so I have muted the "

                    "microphone. Use /unmute to turn it back on."

                )

            publish_status()



    def memory_from_window(message):

        # Searching and changing what she has saved waits its turn

        # like anything else that touches her memory.

        with gate.turn():

            return freya.memory_request(message)



    def say_from_window(text):

        # Runs on the window's worker thread; waits for any turn

        # already in progress.

        with gate.turn():

            print(f"\rYou (window): {text}")

            handle_normal_input(

                freya,

                voice,

                tool_router,

                image_generator,

                gpu_manager,

                text,

                source="window",

            )

            publish_status()

            print("You: ", end="", flush=True)


    def image_from_window(prompt):

        with gate.turn():

            print(f"\rYou (window): /image {prompt}")

            EVENTS.publish(

                "user",

                text=f"Generate an image: {prompt}",

                source="window",

            )

            run_image_generation(

                image_generator,

                gpu_manager,

                prompt,

            )

            publish_status()

            print("You: ", end="", flush=True)


    def mute_from_window(muted):

        if listener is not None and listener.running:

            if muted:

                listener.mute()

            else:

                listener.unmute()

        publish_status()


    def generate_image_tool(prompt):

        # Called when the model decides an image is wanted. The turn

        # that asked for it already holds the gate.

        EVENTS.publish(

            "notice",

            text=f"Generating an image: {prompt}",

        )

        succeeded = run_image_generation(

            image_generator,

            gpu_manager,

            prompt,

        )

        publish_status()

        if not succeeded:

            raise RuntimeError(

                "The image could not be generated."

            )

        return {

            "status": "generated",

            "prompt": prompt,

            "shown_in": "the Images tab of Freya's window",

        }


    control_plane.register_image_generation(

        generate_image_tool,

    )

    control_plane.register_conversation_recall(

        freya.recall,

    )


    def media_chat(messages):
        with gate.turn():
            return control_plane.model_runtime.chat(
                messages,
                role=ROLE_BACKGROUND,
            )

    media = None
    try:
        media = build_media_service(
            control_plane.broker,
            chat=media_chat,
            model_name=MODEL,
            speech_model=lambda: stt if stt is not None else None,
            admission=gate.turn,
            permissions=lambda: {
                name: control_plane.policy.decide(LOCAL_OWNER.actor, name).allow
                for name in MEDIA_CAPABILITIES
            },
        )
        control_plane.register_media(media)
        # Asked only while the owner has enabled it; otherwise every turn
        # would make a refused request and tell the model about it.
        freya.media_recall = lambda query: (
            control_plane.broker.invoke("media.recall", query=query)
            if control_plane.policy.decide(LOCAL_OWNER.actor, "media.recall").allow
            else None
        )
    except Exception:
        if media is not None:
            media.stop()
            media = None
        print("Media could not start; chat remains available. See the media setup guide.")

    def command_from_window(text):

        with gate.turn():

            run_window_command(

                text,

                CommandContext(

                    bus=EVENTS,

                    voice=voice,

                    listener=listener,

                    settings=settings,

                    window=window,

                    normalize=normalize_command,

                    run_image=lambda prompt: run_image_generation(

                        image_generator,

                        gpu_manager,

                        prompt,

                    ),

                    publish_status=publish_status,

                    memory_command=freya.memory_command,
                    folder_command=file_access.command,

                    request_quit=(

                        quit_requested.set

                        if window_only

                        else None

                    ),

                ),

            )

            print("You: ", end="", flush=True)


    window = FreyaWindow(

        EVENTS,

        GALLERY,

        WindowActions(

            say=say_from_window,

            generate_image=image_from_window,

            set_muted=mute_from_window,

            memory_request=memory_from_window,

            **(
                {"cloud_request": cloud.window_request}
                if cloud is not None else {}
            ),

            client_closed=client_closed,

            set_volume=volume_from_window,

            interrupt=voice.interrupt,

            status=current_status,

            run_command=command_from_window,

            console_history=console_output.history,

            voice_state=voice_studio.describe,

            voice_request=voice_studio.handle,

            voice_clip=voice_studio.clips.read,

            media_request=media.handle if media else (lambda message: {"error": "Media is unavailable."}),
            media_stream=media.stream if media else (lambda ticket, header: None),
            media_download=media.download if media else (lambda identifier, kind: None),

            avatar_state=avatar.describe,

            avatar_request=avatar.handle,

            avatar_file=avatar.file,

        ),

    )

    unreal = None



    if window.start():

        # A client that asks to may play her voice in her place.

        voice.remote_playback = window.speech

        program = configured_client()



        if program is not None:

            unreal = UnrealClient(program)



            if unreal.launch(window.port, window.new_pairing_key()):

                print("Freya's Unreal window is starting.")



        if OPEN_WINDOW or window_only:

            print(window.open())

        else:

            print("Use /window to open Freya's window.")

    print()



    def read_next_line():

        if not window_only:

            return gate.read_line("You: ")

        # No console to read from: carry on until the window has

        # been closed, then leave as if 'exit' had been typed.

        while not (

            window.closed()

            or quit_requested.is_set()

        ):

            time.sleep(0.5)

        return "exit"



    try:

        while True:

            user_input = normalize_command(

                read_next_line().strip()

            )



            if user_input.lower() in {

                "exit",

                "quit",

            }:

                print("Freya: Goodbye.")

                voice.speak("Goodbye.")

                voice.wait_until_done()

                break



            if not user_input:

                continue



            if user_input.lower() == INTERRUPT_COMMAND:

                voice.interrupt()

                print(

                    "\nFreya: Speech playback interrupted.\n"

                )

                continue



            if handle_listening_command(

                user_input,

                listener,

                settings,

            ):

                publish_status()

                continue


            if file_access.command(user_input):
                continue
            if user_input.lower() == WINDOW_COMMAND:

                print(f"\n{window.open()}\n")

                continue



            if user_input.lower() == IMAGE_COMMAND:

                print(

                    "\nUsage: /image <description>\n"

                )

                continue



            if user_input.lower().startswith(

                IMAGE_COMMAND + " "

            ):

                prompt = get_image_prompt(

                    user_input

                )



                run_image_generation(

                    image_generator,

                    gpu_manager,

                    prompt,

                )



                continue



            if user_input.lower() == CONVERSATION_COMMAND:

                try:

                    if stt is None:

                        stt = FreyaSTT()



                    run_conversation_mode(

                        freya,

                        voice,

                        stt,

                        tool_router,

                        image_generator,

                        gpu_manager,

                    )



                except Exception as exc:

                    print(

                        "\nConversation mode error: "

                        f"{exc}\n"

                    )



                continue



            if user_input.lower() == LISTEN_COMMAND:

                try:

                    if stt is None:

                        stt = FreyaSTT()



                    audio_path = (

                        stt.record_until_silence()

                    )



                    transcript, info = stt.transcribe(

                        audio_path

                    )



                except Exception as exc:

                    print(

                        "\nAutomatic listening error: "

                        f"{exc}\n"

                    )

                    continue



                transcript = transcript.strip()



                if not transcript:

                    print(

                        "\nNo speech was "

                        "detected from the microphone.\n"

                    )

                    continue



                print()

                print(

                    "You (voice): "

                    f"{transcript}"

                )

                print(

                    "Detected language: "

                    f"{info.language}"

                )



                handle_normal_input(

                    freya,

                    voice,

                    tool_router,

                    image_generator,

                    gpu_manager,

                    transcript,

                    source="voice",

                )



                continue



            if user_input.lower() == TALK_COMMAND:

                try:

                    if stt is None:

                        stt = FreyaSTT()



                    audio_path = stt.record_push_to_talk()



                    transcript, info = stt.transcribe(

                        audio_path

                    )



                except Exception as exc:

                    print(

                        "\nMicrophone error: "

                        f"{exc}\n"

                    )

                    continue



                transcript = transcript.strip()



                if not transcript:

                    print(

                        "\nNo speech was "

                        "detected from the microphone.\n"

                    )

                    continue



                print()

                print(

                    "You (voice): "

                    f"{transcript}"

                )

                print(

                    "Detected language: "

                    f"{info.language}"

                )



                handle_normal_input(

                    freya,

                    voice,

                    tool_router,

                    image_generator,

                    gpu_manager,

                    transcript,

                    source="voice",

                )



                continue



            if user_input.lower().startswith(

                VOICE_FILE_COMMAND

            ):

                audio_path = get_voice_file_path(

                    user_input

                )



                if not audio_path:

                    print(

                        "\nUsage: "

                        "/voice-file "

                        "<audio file path>\n"

                    )

                    continue



                try:

                    if stt is None:

                        stt = FreyaSTT()



                    transcript, info = (

                        stt.transcribe(

                            audio_path

                        )

                    )



                except Exception as exc:

                    print(

                        "\nVoice-file error: "

                        f"{exc}\n"

                    )

                    continue



                transcript = transcript.strip()



                if not transcript:

                    print(

                        "\nNo speech was "

                        "detected in that "

                        "audio file.\n"

                    )

                    continue



                print()

                print(

                    "You (voice): "

                    f"{transcript}"

                )

                print(

                    "Detected language: "

                    f"{info.language}"

                )



                handle_normal_input(

                    freya,

                    voice,

                    tool_router,

                    image_generator,

                    gpu_manager,

                    transcript,

                    source="voice",

                )



                continue



            handle_normal_input(

                freya,

                voice,

                tool_router,

                image_generator,

                gpu_manager,

                user_input,

            )



    except KeyboardInterrupt:

        print(

            "\nFreya: Shutting down."

        )



    finally:

        # Shut down in order: let no new turn begin, cut short anything

        # being spoken, then wait for the turn in progress (from the

        # window or the microphone) before closing what turns use.

        SHUTTING_DOWN.set()

        gate.close()

        voice.interrupt()

        finished = window.stop()

        finished = (media.stop() if media else True) and finished

        finished = gate.drain(SHUTDOWN_WAIT_SECONDS) and finished

        if listener is not None:

            listener.stop()

        if not finished:

            print(

                "Freya: A turn was still running when I shut down."

            )

        # Note what this conversation was about, for next time. If a

        # turn is still running, it is done at the next start instead.

        if finished:

            freya.finish_conversation()



        # Generated clips are only kept while Freya is running.

        voice_studio.clips.clear()

        # Stop the speech helpers and drop recordings no voice uses.

        voice_studio.free_memory()

        voice_studio.forget_unsaved_recordings()

        # Close the avatar overlay. Whether she was showing is

        # remembered, so she comes back next time if she was.

        avatar.stop()

        if unreal is not None:

            unreal.stop()



        console_output.uninstall()

        voice.shutdown()

        # Copy the databases and settings to the other drive. It runs
        # beside the rest of closing and is given a short time: a slow
        # or missing drive must not keep Freya from closing.
        backing_up = threading.Thread(target=back_up_on_close, daemon=True)

        backing_up.start()

        backing_up.join(timeout=20)

        if cloud is not None:

            cloud.close()

        control_plane.close()






if __name__ == "__main__":

    main()
