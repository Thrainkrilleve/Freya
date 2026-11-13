import os


# Freya's main model. Chosen on 2026-10-09 from the measurements in
# docs/design/model-benchmarks-2026-10-09.md. FREYA_MODEL names another
# installed Ollama model, for example gpt-oss:20b, the model before it.
MODEL = os.environ.get("FREYA_MODEL") or "qwen3.8:27b"
EMBEDDING_MODEL = "nomic-embed-text"

# The model for the unseen work around a turn: deciding what to
# remember, comparing and ranking memories, and writing summaries.
# Unset means the same model as Freya's replies, so nothing changes
# until the owner points this role at another installed model.
BACKGROUND_MODEL = os.environ.get("FREYA_BACKGROUND_MODEL") or MODEL

# Context capacity is an explicit per-request model setting. These do
# not alter Ollama's system-wide settings or the owner's configuration.
CONTEXT_WINDOW_TOKENS = int(os.environ.get("FREYA_CONTEXT_WINDOW_TOKENS", "16384"))
CONTEXT_OUTPUT_TOKENS = int(os.environ.get("FREYA_CONTEXT_OUTPUT_TOKENS", "2048"))

# File that receives audit-log checkpoints. It only makes tampering
# detectable when it is somewhere this machine cannot rewrite, such as
# an append-only share on another host. Unset means local checkpoints only.
AUDIT_CHECKPOINT_PATH = os.environ.get(
    "FREYA_AUDIT_CHECKPOINT_PATH"
) or None

# Whether Freya opens her window when she starts. Set FREYA_WINDOW=0 to
# start with the console only; /window opens it later.
OPEN_WINDOW = os.environ.get("FREYA_WINDOW", "1") != "0"

# Program that starts ComfyUI when an image is requested and it is not
# running. Unset means the standard Comfy Desktop install, if present.
COMFYUI_COMMAND = os.environ.get(
    "FREYA_COMFYUI_COMMAND"
) or None

# Whether Freya starts the ComfyUI server as she comes online, so the
# first image does not wait for it. No image model is loaded until a
# picture is asked for. Set FREYA_COMFYUI_AT_START=0 to start it only
# when a picture is first asked for.
COMFYUI_AT_START = os.environ.get("FREYA_COMFYUI_AT_START", "1") != "0"

# LibreHardwareMonitor's web server on this machine, which supplies
# temperatures, fan speeds, and other sensor readings.
SENSOR_URL = os.environ.get(
    "FREYA_SENSOR_URL"
) or "http://127.0.0.1:8085/data.json"

# Live world data from a God's Eye View program on this machine. These
# are the only tools whose answers come from the internet, so they are
# off unless the owner sets FREYA_WORLD_VIEW=1.
WORLD_VIEW_ENABLED = os.environ.get("FREYA_WORLD_VIEW", "0") == "1"
WORLD_VIEW_URL = os.environ.get(
    "FREYA_WORLD_VIEW_URL"
) or "http://localhost:4173"

SYSTEM_PROMPT = """
You are Freya, a locally hosted personal AI assistant.

Your name is Freya.

You are helpful, conversational, capable, and concise.

Treat everything you are asked as equal in priority, unless the user says
something is urgent or a priority, or one piece of work has to be settled
before another can be finished. When a requirement is added to work that is
not finished and the rest depends on it, settle that requirement first: work
out what it needs and how it would be done, then finish the rest around it.
"""
