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
- The code sandbox (`code.run`) and the local decider (`decide`) are not
  registered, and neither is in a policy seed.

## Memory protection

Startup calls `ensure_memory_protection()` before the assistant is built.
The first time the memory database is unprotected, Freya creates her tables
if needed, keeps a copy from before protection beside the ledger, and gives
every row an identifier. A database that is already protected is left as it
is. The assistant then opens it with that ledger: the two files are checked
against each other, and every recorded deletion is applied, before anything
else is read. If that setup fails, or a protected database has no ledger,
startup prints the reason and does not open memory.

The ledger is `freya_deletion_ledger/deletions.db` beside the data folder,
never inside it. `FREYA_DELETION_LEDGER` names another file and is refused
when that path is inside the memory database's folder. A fact forgotten
after a backup stays forgotten when that backup is restored, because the
ledger is replayed and is not rolled backwards. Other copies can still
bring a fact back. That is not full MEM-008 compliance.
