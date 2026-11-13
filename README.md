# Freya

Freya is a local-first, modular, multi-user agentic AI platform. The alpha
runs on one Windows 11 workstation. Chat and embeddings go to Ollama on
`127.0.0.1:11434`. The default chat model is `qwen3.8:27b` and the embedding
model is `nomic-embed-text`. `FREYA_MODEL` names another installed Ollama model.

## Run

Python 3.12 (`>=3.12,<3.13`). From a checkout, `uv sync` installs the locked
dependencies into `.venv`.

- `freya` starts the console and the window.
- `freya-window` starts the window only, with no console.
- `python -m freya` is the same entry as `freya`.

Image generation uses ComfyUI on `127.0.0.1:8188` when a picture is asked for.

## Off until the owner turns them on

- Media capabilities stay denied until `python -m freya.media.activate`.
- Cloud send stays off. No provider is switched on, no policy rule allows a
  cloud route, and no route has a usage limit until the owner sets them.
- World-view tools stay off unless `FREYA_WORLD_VIEW=1`.
- The deletion ledger is built and not in use. Startup does not open the
  memory store in protected mode.
- The code sandbox (`code.run`) and the local decider (`decide`) are not
  registered, and neither is in a policy seed.
