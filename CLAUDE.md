# Freya — project guide for Claude

Freya is a local-first, modular, multi-user agentic AI platform. The alpha targets a
single Windows 11 workstation (RTX 4090); the architecture must later scale to a
heterogeneous Linux/Windows rack without changing Core contracts.

## Architecture documents

- **Authoritative baseline:** *Freya Foundation v1.1 — System Architecture and Design
  Specification* (the owner's reconciled v1.1), at
  `docs/specs/Freya_Foundation_v1.1_baseline.docx`. Read it before making architecture
  decisions.
- **Pending amendments:** `docs/specs/Freya_Foundation_v1.1_Claude_amendments_redline.docx`
  — tracked-change proposals (NEW-A to NEW-L) awaiting Codex review and reconciliation.
  Not yet adopted.
- **Superseded:** `docs/specs/Claude_proposal_superseded.docx` — an earlier proposal.
  Reference only. Its requirement and ADR IDs collide with the baseline; never cite them.

Rules for the spec:
- Never renumber requirements or reuse an existing requirement/ADR ID for a new meaning.
  New proposals carry provisional labels (NEW-x) until reconciliation assigns IDs.
- ADR-001 to ADR-018 are pending completion and approval. Do not describe any ADR as
  approved, the architecture as frozen, or any acceptance test as passed unless the
  owner says so.
- Agreed decisions (change only with an explicit, justified proposal): Hyper-V as the
  preferred Windows alpha isolation profile (untrusted execution disabled until
  validated); SQLite alpha persistence behind replaceable contracts; loopback
  HTTP/WebSocket alpha gateway profile; Ollama as a replaceable model runtime;
  configurable 24h RPO / 4h RTO / 24h purge / 30-day retention targets; SEC-035
  federated sovereignty; deferred features stay disabled.

## Repository layout (current)

- `src/freya/main.py` — CLI entry point (text, voice, image commands).
- `src/freya/control_plane.py` — assembles providers, capabilities, policy, audit, and
  the broker for the single-workstation alpha.
- `src/freya/providers/model/ollama.py` — the `ollama-local` provider and its
  `llm.chat`, `llm.chat_stream`, and `llm.embed` capabilities.
- `src/freya/core/` — `assistant.py`, `llm.py` (memory prompts; model calls go through
  `model_runtime.py`, which asks the broker),
  `capabilities/` (CapabilityRegistry, CapabilityBroker), `providers/`
  (ProviderRegistry), `policy/` (deny-by-default SQLite policy engine), `audit/`
  (hash-chained SQLite audit log, checkpoint sinks), `persistence/` (ControlStore and
  its migrations).
- `src/freya/memory/memory_manager.py` — SQLite memory with embeddings and history.
  `src/freya/memory/conversations.py` — `ConversationStore`: every exchange kept word
  for word, a summary per conversation, and a word search (SQLite FTS5), in the same
  database. `src/freya/core/conversation_memory.py` — the part of `FreyaAssistant`
  that uses it.
- `src/freya/tools/` — read-only system tools registered as capabilities (host info,
  GPU, CPU, RAM, all drives, a Windows-only hardware inventory in
  `hardware_inventory.py`, and temperatures/fans/power in `sensors.py`);
  `model_tools.py` (`ModelToolHost`, which offers marked
  capabilities to the model and runs the ones it asks for); `tool_router.py` (regex,
  image requests only). A tool added after first release needs its own named policy
  seed in `control_plane.py`. Its description is what the model chooses by, so say
  what it returns and when to prefer a neighbouring tool.
- `src/freya/gpu/resource_manager.py` — Ollama/ComfyUI VRAM handoff.
- `src/freya/voice/` — faster-whisper STT, Kokoro TTS, `wake.py` (deciding whether a
  heard sentence is addressed to Freya), `listener.py` (utterance segmenting, the
  attention window, and the background listener thread).
- `src/freya/spatial_avatar/` — the on-screen avatar (Phase 1; design and manual
  demonstration in `docs/design/spatial-avatar.md`). `service.py` (the three
  capabilities and the window's requests) and `host.py` run in Freya; `overlay.py`
  is a separate program holding `model.py` (all behaviour, no drawing) and a
  renderer from `renderers/`. `geometry.py`, `config.py`, `targets.py`,
  `commands.py`, `animation.py`, `placement.py`, `appearance.py`, `speech.py`.
- `src/freya/speech/` — the voice studio behind the window's Voice tab: `engines.py`
  (the `SpeechEngine` contract and the roles preset / blend / clone / design),
  `kokoro_engine.py`, `store.py` (saved voice profiles in `data/voice_profiles.json`,
  generated clips in `data/voice_clips/`), `studio.py` (`VoiceStudio`), `setup.py`;
  `helper.py` (`HelperProcess`, `HelperEngine`: an engine running as a separate program
  in its own environment), `chatterbox_engine.py`, `qwen_engine.py`, `workers/` (the
  helper programs; they run in the engines' environments and must not import `freya`),
  `references.py` (recordings voices are made from, in `data/voice_references/`),
  `install.py` (sets an engine up), `benchmark.py`.
- `src/freya/gateway/` — `server.py` (the loopback gateway: HTTP GET for the page and
  images, one WebSocket for everything interactive) and `static/` (the window's page:
  Chat and Images tabs). `src/freya/window.py` — opens that page in a standalone Edge
  app window with its own browser profile, and builds the status strip.
  `src/freya/events.py` — the event bus the console loop publishes to and the window
  subscribes to. `src/freya/image/gallery.py` — Freya's own copies of generated images
  in `data/images/`.
- `src/freya/hands_free.py` — starts background listening and handles `/mute`,
  `/unmute`, `/names`, `/name add|remove`. `src/freya/settings.py` — owner preferences
  in `data/freya_settings.json` (the names Freya answers to, and how loudly she
  speaks).
  `src/freya/turn_gate.py` — lets one turn run at a time, typed or spoken.
- `src/freya/image/` — ComfyUI client. `src/freya/providers/image/comfyui.py` — the
  `comfyui-local` provider. When an image is requested and ComfyUI is not answering,
  Freya runs the ComfyUI server that Comfy Desktop installed, directly and with no
  window (the Desktop app waits for a click before starting its server), using the
  app's own model-path config and shared input/output folders. Output goes to
  `data/comfyui_server.log`. Freya stops that server when she exits, and a Windows job
  object ends it if her process dies without a normal exit; she never stops a ComfyUI
  the owner started. `FREYA_COMFYUI_COMMAND` overrides how it is started. The
  paths inside Comfy Desktop's install are discovered, not documented by its vendor, so
  an app update could move them. Image generation itself still calls ComfyUI directly,
  not through the broker.

Known state: the system tools and every chat and embedding call run only through
`CapabilityBroker`, which checks policy (deny by default), checks availability, writes
an audit record before executing, and records the outcome (for a streamed reply, when
the stream ends). A capability is available only while its provider is running and
healthy; the Ollama provider's health is re-checked when a model call is unavailable or
fails. The broker attributes everything to a fixed `local-owner` principal because
there are no authenticated sessions yet. `core/llm.py` and `memory_manager.py` reach the
model through a process-wide `ModelRuntime` set up by `build_local_control_plane()`, so
the control plane must be built before `FreyaAssistant`. Still calling their backends
directly: Whisper, Kokoro, ComfyUI, the memory store, the GPU manager's Ollama unload,
and the startup model warm-up. `CapabilityDefinition` is far smaller than the
baseline's capability schema; its `destructive`/`requires_gpu` flags are interim.

Model tool calling: `FreyaAssistant.respond_stream` offers the model the capabilities
whose metadata has `model_tool: True` (never destructive ones) using the model's native
tool calling. Requested tools run through the broker, their JSON results go back to
the model, and it answers in its own words; at most three tool rounds per turn. The
offered tools take no arguments, and arguments the model invents are discarded. Tool
results are untrusted content from the model's point of view. The tool exchange is not
kept in conversation history. A tool that takes arguments declares them in its metadata
(`tool_parameters`); `ModelToolHost` passes only declared arguments and requires each
to be non-empty text within its declared length. `image.generate` is the one such tool:
it is provided by Freya's in-process image service (`image-service-local`), which starts
ComfyUI on demand and unloads the Ollama models while it runs, so the reply after an
image waits for the model to reload. It has a local, reversible effect; before adding a
tool with a consequential effect, decide how it is authorized, including whether a
spoken request may trigger it.

Conversation memory: one run of Freya is one conversation. Each exchange is stored
as it happens (`conversation`, `conversation_turn`, and the `conversation_search`
index in `data/freya_memory.db`), with how it arrived (typed, voice, window).
Commands are not stored. A summary is written when Freya closes, or at the next start
if that was missed, and she starts with the summaries of the last three as a system
note marked as notes, not instructions. When the live conversation passes 40 messages
the older part is folded into its summary and only the last 16 are sent to the model;
the words stay stored. The model can look passages up with the `conversation.recall`
tool (provider `conversation-memory-local`, policy seed `alpha-conversation-recall`),
which goes through the broker and is audited; its results are untrusted text. The
assistant does not rely on the model choosing to look: a question about the past
("have I ever", "did we ever", "do you remember", "what did I say"; the pattern is
`ASKS_ABOUT_THE_PAST`) makes her search every memory and every stored conversation
first and give the model the results for that one reply, or tell it nothing was
found. The
window's Memory tab and the `/find` and `/history` commands search memories and
conversations, edit and delete memories, and delete an exchange or a conversation.
Deleting conversation text is permanent (`secure_delete` is on, and the FTS5 index's own
`secure-delete` option, without which the index keeps deleted words), removes both halves
of an exchange, clears that conversation's summary so it is rewritten from what is
left, and drops the words from the live conversation. A deleted *memory* is still
archived in `memory_history`, as before. Memory tab answers go only to the window
that asked, never onto the event bus. The first time the conversation tables are
created the database is copied to `data/backups/`. This is the most personal data
Freya holds: never print it, commit it, or send it anywhere.

Always-on listening: the microphone stays open on a background thread while the
keyboard prompt waits. Every utterance is transcribed locally and then discarded unless
it is addressed to one of Freya's names at the start or end of the sentence; the name
alone opens a ten-second window for one follow-up sentence. Typed turns run on the main
thread and spoken turns on the listener thread, serialized by `TurnGate`, which is why
the memory database connection allows cross-thread use. The microphone is ignored
while Freya is speaking or handling a turn; she cannot be interrupted by voice. Voice
is ambient input and is not authentication: before any tool with effects exists,
decide what spoken requests may trigger (see proposed NEW-I).

Two ways to start: `freya` (console plus window) and `freya-window`
(`freya/launcher.py`, no console). The console-free launcher points stdout and stderr at
`data/freya.log` before importing anything heavy, because a program without a console
has nowhere to print and some libraries fail when they try. In that mode Freya exits
about eight seconds after her last window closes, or after two minutes if none ever
connects, so she is never left running unseen with the microphone open. Only one Freya
runs per user (`single_instance.py`, a named mutex; `FREYA_ALLOW_MULTIPLE=1` lifts it
for tests).

The terminal inside the window: `console_tee.py` copies everything printed (to the
real terminal or the log) into a bounded history and onto the event bus. Typing
`/terminal` or `/console` in the window's chat reveals a Console tab that shows it.
Text starting with a slash in the chat or the Console tab is sent as a `command`
message and run by `window_commands.py`, which handles the same commands as the
terminal loop (`/names`, `/name add|remove`, `/mute`, `/unmute`, `/interrupt`,
`/image`, `/help`, and `exit` when there is no terminal). `/talk`, `/listen`,
`/conversation`, and `/voice-file` need a real keyboard prompt and are explained
rather than run. The two command paths (the loop in `main.py` and
`window_commands.py`) are separate code; keep them in step.

The window: Freya opens it at startup (`FREYA_WINDOW=0` disables that; `/window`
reopens it). The gateway binds `127.0.0.1` only, port 8770 or a free one. A request is
accepted only with a matching Host header and a session cookie, which is issued once in
exchange for a single-use key in the address Freya opens; the WebSocket also requires
this server as its Origin. This is a first step, not the authenticated gateway of the
baseline: there is no pairing, no per-client scope, and window turns are attributed to
the same fixed `local-owner` principal. The page shows everything with `textContent`
and a strict Content-Security-Policy, because replies and tool results are untrusted.
The window can only ask for a chat turn, an image, mute/unmute, stopping speech, a
voice volume, a Memory tab request, a command, or a Voice tab request (with a recording sent as binary messages);
add new message types deliberately. `main.py` publishes conversation and image events
on a module-level `EVENTS` bus; the console still prints as before. The bus calls its
subscribers in publish order while it is locked, so a subscriber must never block: the
gateway's only puts the event on that window's queue, and a thread per window does the
sending. A window joins with `subscribe_with_history`, which is what guarantees it
gets every event exactly once. Memory commands (`/memories`, `/forget`, `/memory-help`
and the rest) typed in the window are passed to `FreyaAssistant.memory_command`.

Shutdown order matters, because a turn may be running on the window's worker thread or
the listener thread: `TurnGate.close()` (no new turn may begin), then
`FreyaWindow.stop()` (refuses new work, drops waiting turns, waits for the running
one), then `TurnGate.drain()` for a spoken turn, and only then are the voice and the
control database closed. Each wait has a time limit.

Spatial avatar: a client-side capability, not part of Core. Core must not import
`freya.spatial_avatar`, and the overlay program must not import Core or open Freya's
data; tests enforce both. The overlay is a separate process so that a renderer crash
cannot affect Freya; all behaviour lives in `AvatarModel`, so a renderer only draws.
Everything asked of the avatar on screen is a broker call to one of three capabilities,
`avatar.display`, `screen.geometry.read`, and `avatar.point` (policy seed
`alpha-avatar`); they are not offered to the model. **The avatar has no code for
capturing the screen, reading other programs' windows, or sending input, and a test
fails if such calls appear in the package.** Six further permissions are named for
later phases and deliberately not built. Do not add screen reading or input control
as part of other work: each needs its own permission, a provider that declares it,
and an owner decision first. What the user does directly to the overlay (drag,
resize, hide, close) is honoured without a policy check so she can always be
dismissed. Her size is always within the user's limits and she is always inside the
user's boundary, on every frame. Targets are revalidated before each use. The
avatar can be turned off completely (`enabled` in her settings, the "Turn off"
button): her program is closed and showing, pointing, and starting her are refused
until the owner turns her on, which only the `enable` action does. The
placeholder renderer uses `tkinter` and no GPU. Settings are in
`data/avatar_settings.json`, keyed by user (one user today).

Avatar models: `spatial_avatar/vrm.py` reads the description inside a VRM file and
`library.py` keeps the models in `data/avatar_models/` (filled by the owner; Freya
never changes those files) and the owner's saved avatars. Downloaded VRM files are
untrusted: keep every length and offset check, show text from them only as text, and
serve a thumbnail only when its bytes match its claimed type. A model whose terms
restrict who may use it needs the owner's recorded confirmation before it becomes an
avatar. The 3D preview is `gateway/static/avatar-view.js`, using three.js and
three-vrm copied unmodified into `static/vendor/` (checksums in `vendor/README.txt`,
checked by a test; `.gitattributes` keeps them byte-exact). Every served file is
listed in `STATIC_FILES`. The page's one inline script is its import map, allowed in
the content security policy by hash; do not add `unsafe-inline` or `unsafe-eval`. A
model marked as Freya's is shown on the desktop by the `web` renderer
(`renderers/web.py`, a WebView2 window through `pywebview`): her face only, in a
rounded tile, with the placeholder's handle and pointing line. The overlay serves
the page, the libraries, and the one model file to its own window on loopback under
a random key; the page only draws and sends nothing back. A web view window cannot
be made see-through or cut to a shape on Windows (both were tried), so a whole-body
figure needs a different renderer, and the converted model's shoulders need fixing
first. With no model marked, the placeholder figure is used.

Finding and converting models: `spatial_avatar/sources.py` lists files in the
owner's Downloads folder (`FREYA_AVATAR_SEARCH` overrides) that are or could become
avatars, reading names only (`avatar.search`). `converter.py` makes one ready
(`avatar.prepare`, policy seed `alpha-avatar-creator`): it copies a VRM, or unpacks
and converts a character file by running Blender on the two scripts in
`spatial_avatar/blender/`. Those scripts run inside Blender's own Python, not
Freya's, and import nothing from Freya. Keep these rules: only a file the last
search found may be prepared; Blender is always started with `--disable-autoexec`
and the scripts never enable scripts from a model file; the original is never
changed and an existing avatar is never overwritten; archive limits and the
outside-the-folder check stay. Blender and its VRM add-on are installed by the
owner (`FREYA_BLENDER` names the program). Converted models may be under terms that
forbid passing them on: they live under `data/` and must never be committed or
shipped to other nodes.

Voice studio: the Voice tab offers each kind of voice work only through engines that
declare that role, and shows only the controls an engine reports. Kokoro runs inside
Freya (preset voices, and an experimental blend of its own supplied voices — a blend is
never to be described as cloning or as voice design). Chatterbox (cloning: Turbo, Nano,
Multilingual V3) and Qwen3-TTS (voice design, cloning) each pin torch/transformers/numpy
versions that conflict with Freya's and with each other, so each lives in its own
environment under `data/speech_envs/` and runs as a helper process speaking JSON lines
over its pipes; never install them into `.venv`. `python -m freya.speech.install
<engine>` sets one up (owner action: several GB from PyPI, GitHub, and Hugging Face);
until then it shows as not installed. Model files are in `data/speech_models/` and the
helpers run with the model hub offline. A helper starts on first use, stops after five
idle minutes or on "Free memory", and only one large engine is loaded at a time; if the
graphics card is too full, the Ollama models are unloaded first, as for an image.
ElevenLabs is listed as not set up and has no code yet.

A saved profile belongs to the engine that made it and is never handed to another. A
cloning recording is accepted only with the owner's statement that they have the right
to use the voice, which is stored with it; the page decodes the file and sends plain
samples as binary WebSocket messages, so Freya never parses an audio file she was
given. A designed voice is kept as the sample the design model generated and is spoken
only by the engine that made it. Chatterbox watermarks its own output and Freya does
nothing to that audio but store it; do not add processing that would remove the mark.
Only an English Kokoro profile can become Freya's live voice. The studio is reached
from the window directly, not yet through the broker, so it is not policy-checked or
audited. `python -m freya.speech.benchmark` compares the engines; its default
references are synthetic Kokoro recordings, and a real recording needs
`--i-have-the-right`.

Audit log: `data/freya_control.db` holds policy rules and the append-only, hash-chained
`audit_event` table. It is verified at startup; if verification fails, the broker
refuses every invocation. Checkpoints of the chain head go to the file named by
`FREYA_AUDIT_CHECKPOINT_PATH` when set, which only adds tamper evidence if that file is
somewhere this machine cannot rewrite. This does not protect against a host
administrator. SQLite durability settings are provisional pending ADR-015; each audited
invocation currently costs roughly 30–60 ms in synchronous writes. Default policy rules
are applied once per named seed (`policy_seed` table), so removed rules stay removed.

`tests/` covers the registries, tool modules, policy, audit, broker, model runtime,
name matching, the listener, and the listening commands, using a fake Ollama client
(`tests/conftest.py`) and scripted microphone blocks; `voice/stt_test.py` and
`voice/tts_test.py` are manual scripts, not automated tests. `memory_manager.py` locates
`data/` relative to the source tree, so it only works from an editable install.

## Running locally (Windows)

- Python 3.12 virtual environment at `.venv`.
- `freya` is an installable package (src layout) and imports are absolute
  (e.g. `from freya.core.llm import ...`). It is installed in `.venv` in editable mode;
  run it from any directory with `freya` or `python -m freya`.
- Requires Ollama on `127.0.0.1:11434` with `gpt-oss:20b` and `nomic-embed-text`.
  Image generation requires ComfyUI on `127.0.0.1:8188`.
- Direct dependencies are declared in `pyproject.toml`; `uv.lock` pins the full resolved
  set for Windows and Linux (Python 3.12). After changing dependencies, run `uv lock`
  and commit both files. `uv sync` rebuilds `.venv` from the lock.
- Run the tests with `python -m pytest` from the repository root (pytest is in the `dev`
  dependency group).
- Optional: `system.sensors` reads LibreHardwareMonitor (MPL-2.0, installed by the
  owner with winget, not bundled) over its local web server at
  `http://127.0.0.1:8085/data.json` (`FREYA_SENSOR_URL` overrides). It must be running
  as administrator with Options > Remote Web Server > Run enabled; otherwise the tool
  reports that sensors are unavailable.
- Kokoro also needs the spaCy model `en_core_web_sm`, which is downloaded outside the
  lockfile.

## Working rules

- Work on a branch and open a pull request; never commit directly to `main`.
- `data/freya_memory.db` holds the owner's personal memories. Never commit it, print its
  contents, or change its schema without first making a backup copy.
- `data/freya_control.db` holds policy and the audit log. Never edit or delete its
  rows, drop its triggers, or change shipped entries in `MIGRATIONS`; add a new
  migration instead. Policy changes are owner actions.
- No secrets, API keys, or tokens in code, config, commits, or logs.
- Generated or untrusted code must never run inside Freya Core or with access to Core
  storage or credentials (ARCH-017).
- Agents must not modify Freya's own policy, configuration, or Core code as a side effect
  of a task; changes go through review (proposed NEW-D).
- Ask before installing system software, enabling Windows features (e.g. Hyper-V), or
  changing ACLs or service accounts — these are owner actions.
- Keep the existing code style; repository files use LF line endings.

## Next planned work (alpha step 1)

Done: `pyproject.toml`, the lockfile, the package layout, tests for the registries, the
`ToolRegistry` merge, linking provider health to capability availability, and the SQLite
audit log and policy table in front of capability execution.

Also done: Ollama behind a provider, with chat and embeddings through the broker.

Also done: the voice studio with Kokoro presets, blends, and saved voices; Chatterbox
and Qwen3-TTS as helper-process engines for cloning and voice design; a benchmark.

Also done: the spatial avatar, Phase 1 (placeholder overlay, scaling, boundaries,
pointing at made-up or user-chosen targets).

Also done: an avatar library, search and conversion of downloaded models, and the
chosen model's face on the desktop.

Not yet started: avatar Phases 2 to 5 (real screen awareness, whole-body 3D rendering, lip
synchronisation, other devices), each needing its own decision; the ElevenLabs engine; speech for devices other than this PC (needs
the authenticated gateway); routing the voice, image, and memory paths through providers and
the broker; authenticated sessions to replace the fixed `local-owner` principal; a
dedicated Windows service identity and ACLs for the control database (owner action); an
off-host checkpoint sink.
