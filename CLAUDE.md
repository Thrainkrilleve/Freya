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
  owner says so. `docs/adr/` records, for each, what the code has already decided and
  what validation is owed ("recorded from implementation"); recording is not approval.
  Change a file there in the same pull request as code that changes its decision.
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
  `llm.chat`, `llm.chat_stream`, and `llm.embed` capabilities. Every chat request is
  given the configured context size unless it names its own, because Ollama reloads a
  model (several seconds) whenever the size differs from the request before. Its client is pinned to
  `127.0.0.1` and ignores `OLLAMA_HOST` and proxies, and a model that Ollama would run
  on its own servers makes the provider unhealthy: full context goes here, so "local"
  must be true.
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
- `src/freya/host_checks.py` — read-only checks of this PC whose result is told to the
  owner at startup. Today: Windows Smart App Control, which when on blocks unsigned
  libraries (ComfyUI's, PyTorch's, the voice engines') with no exception possible.
  It only reads the registry; never add code that changes a Windows security setting.
- `src/freya/storage/placement.py` — lists every store under `data/` with what it
  needs from a drive and reports where each would best live (SSD or hard disk, by
  type, not letter). It reads sizes only and moves nothing; see
  `docs/design/storage-placement.md`. A store added under `data/` must be added to
  `DATA_KINDS`; a test checks. `src/freya/storage/backup.py` copies the databases
  (with SQLite's backup) and settings to a dated folder on another drive and refuses
  the same drive; it never moves or deletes. Freya runs it as she closes, beside the rest of
  closing and for at most 20 seconds, unless one was made in the last hour
  (`FREYA_BACKUP=0` turns it off). `src/freya/storage/locations.py` says where a
  store lives: the six in `BULK_STORES` follow the owner's setting in
  `data/storage_locations.json`, everything else stays in `data/`. Define a bulk
  store's folder with `store_path(name)`, never `DATA_DIR / name`. It imports
  nothing from Freya. `src/freya/storage/move.py` copies the bulk stores to another
  drive, checks every file, then switches over; it deletes nothing.
- `src/freya/media/` — the media library, Jellyfin playback and transcripts
  (`docs/design/media-tools.md`). `src/freya/kernel/context/authority.py` — the
  context budget and labelled sources every reply is built from
  (`docs/design/context-system.md`). `src/freya/child_environment.py` — the
  environment given to programs Freya starts, with credential-named variables removed.
  `src/freya/unreal_client.py` — pairing and speech relay for the paused Unreal window.
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
  `comfyui-local` provider. As Freya comes online (unless `FREYA_COMFYUI_AT_START=0`),
  and again whenever an image is requested and ComfyUI is not answering,
  Freya runs the ComfyUI server that Comfy Desktop installed, directly and with no
  window (the Desktop app waits for a click before starting its server), using the
  app's own model-path config and shared input/output folders. Output goes to
  `data/comfyui_server.log`. Freya stops that server when she exits, and a Windows job
  object ends it if her process dies without a normal exit; she never stops a ComfyUI
  the owner started. `FREYA_COMFYUI_COMMAND` overrides how it is started. Starting it loads no image model; ComfyUI loads one at the first picture. Both
  starts take `ControlPlane.image_start_lock`, so two servers are never launched. A
  failed start says why when the server's log does (`failure_reason`), for example
  when Windows Smart App Control blocks one of ComfyUI's files. The
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

Model roles: a caller of `ModelRuntime` names a role, not a model. `conversation` is
Freya's own replies; `background` is the unseen work around a turn (memory decisions,
comparing and ranking memories, summaries, media summaries). Both use `MODEL` unless
`FREYA_BACKGROUND_MODEL` names another installed Ollama model. Only then are
`llm.background.chat` and `llm.background.chat_stream` registered and allowed (policy
seed `alpha-model-background`), so each model's use is separately audited, and both
models are checked for health and warmed. Cloud routes are separate: sending anything
to an outside model is escalation under ESC-001 to ESC-006 and goes only through the
dispatcher described below.

Usage ledger: `freya.core.usage.UsageLedger` records what each route (one way of
reaching one model) has used and decides whether more fits: go, ask the owner, wait
for a reset, or refuse. Going past the day's share of a paced allowance always asks
the owner, whatever the value or urgency (owner decision, 2026-10-11): never make a
value, an urgent flag or a model's mark pass it. Freya's model may mark a hand-over as
waited on (`waited_on`), which only adds her suggestion to that question. Limits are per call, per task, or per period; a period longer
than a day is paced across the days left, counted back from the provider's reset. A
route with no limit recorded is refused, and a zero cap cannot be overridden. It lives
in its own database, `data/freya_usage.db`, through `ControlStore(path,
USAGE_MIGRATIONS)`. The dispatcher reserves and settles against it; the design it serves is
`docs/design/cloud-delegation.md`, a proposal that is not adopted.

Cloud material settings: `freya.delegation` holds the owner's choices about cloud
models in `data/cloud_settings.json` (`CloudSettings`): which providers are switched
on (all off to start) and, for each kind of material, allowed, ask first, or never.
`review_brief` sorts a brief's labelled parts by those settings and describes what a
provider would receive without showing the text. Keep these properties: a damaged
file means the strict defaults; a part of unknown kind never leaves; an approval
cannot release a kind set to never; nothing is sendable to a provider that is off.
Briefs are sent only by the dispatcher, below.

Composing and sending briefs: the main model, which sees memories, never writes a
cloud brief. It returns a plan in a fixed form (`compose.parse_plan`: a kind and value
from fixed lists, an urgent flag, ids of inputs Freya's code offered); a plan with any
other field is refused, so there is no room for prose. `compose_brief` sorts the
chosen inputs by the owner's settings and shows only the cleared parts to the brief
writer, a fresh model call; code assembles the final text. `dispatch.Dispatcher` is
the only place a brief may leave from: `authorize` checks the provider switch, the
material sorting, screening and the ledger and reserves usage; `send` takes only an
authorization it issued, for that exact text (by hash), once, within two minutes,
with unchanged settings, then settles the ledger and appends to the owner's record
`data/sent_briefs.jsonl` (personal data, never the audit log). `authorize` trusts nothing a composed brief says about itself: it sorts the
parts again and requires the text to equal what they assemble to. The plan's value
and urgent flag are suggestions; the caller passes the owner's. Do not add a prose
field to the plan, do not show the writer anything not cleared to leave, and do not
call a route's `ask` from anywhere but the dispatcher (the Claude capability must take
the send's one permit, `Dispatcher.take_permit`). `service.build_cloud_delegation` assembles
them on a control plane and registers `anthropic-plan`; a brief then goes dispatcher,
broker, route. Three locks only the owner opens each stop a brief alone: the
provider's switch, a policy rule for `cloud.anthropic_plan.ask` (never put it in a
seed), and a usage limit for the route. Freya's startup assembles it so the
Cloud tab works; a turn reaches it only through `work.delegate` (see
"Delegation inside a turn"). The worker model is `sonnet` unless `FREYA_CLAUDE_MODEL` names another. The tab (`gateway/static/cloud.js`, message type
`cloud`, `delegation/window_requests.py`) changes the owner's cloud settings through
the `cloud.settings.change` capability (seed `alpha-cloud-settings`), so each change
is audited without its content; its answers go only to the asking window. Switching a
provider on in the tab unlocks it (owner decision): the switch, the policy rule for
its route (reason `SWITCH_REASON`, removed again when switched off) and a starting
weekly allowance. Standing rules: answering an approval prompt with "Always allow" records a rule in the
cloud settings (`CloudSettings.add_rule`: one provider, one kind of task, the ask-first
kinds of material shown), and later tasks of that kind send that material without
asking. Keep these rules: a rule is made only by that answer (the `rule` action of
`cloud.settings.change`, which the tab's requests cannot reach; the tab lists and
removes); it holds only kinds set to ask first and never brings back a kind set to
never; it does not cover a held brief or a spoken turn's click; a stored rule that is
not understood is dropped. A rule has no usage limit of its own yet.
How much a hand-over matters (low, normal, high, and urgent) is the owner's to say
and never a model's. In the window it is set beside the chat box and sent just ahead of
the message (`cloud` request `next`, `TurnDelegation.set_next`); it applies to that one
message and is not a setting. A spoken request may ask for a setting change, but the
change is made only by a click on a confirmation shown on the device that recorded the
request (owner decision, 2026-10-11). The owner saying "urgent" or "a priority" in a
message also raises it (`said_value`, code not a model): typed words count as they
stand, read from the message's opening only; spoken words add a choice to the prompt a
spoken hand-over always shows and count after that click. Everything else is normal. The model can also ask, with
`cloud.settings.tighten` (`delegation/tighten.py`, seed `alpha-cloud-tighten`), for a
provider to be switched off or a kind of material to be set to never; the owner's
click makes the change through `cloud.settings.change`. That tool must only ever
tighten: anything that switches on, allows, raises a limit or makes a rule stays in
the Cloud tab. Do not let spoken words change anything
without that click, and send the prompt only to the device that heard them once more
than one can listen.
The tab must never be able to send a brief or write any other
policy rule.

Delegation inside a turn: `delegation/turn.py` (`TurnDelegation`, built by
`service.build_turn_delegation`) registers the model tool `work.delegate` (policy seed
`alpha-work-delegate`, which lets the model *ask*; it allows no cloud call). Arguments are
a `kind` (at most 20 characters, from the fixed list) and `inputs` (at most 400, ids
separated by commas, may be empty); there is no prose field. Freya's code offers the ids
before the model is called: `request`, `turn-1` to `turn-3` (what the owner said in the last exchanges, never
Freya's replies, which can restate memories) and
`tool-N` (the Nth tool result of the turn, only from tools that read this PC). Memories,
summaries, recalled passages and media are never offered, whatever the settings say; do
not add a source without deciding that first. The tool is offered only while a turn is
under way, a provider is switched on and a window is connected. One delegation a turn;
`Dispatcher.authorize` gets value `normal` and urgent `False`, never the model's. The
answer is an untrusted tool result kept under 2,000 bytes so it survives the context's
clip as valid JSON (`"shortened": true` when cut); the owner sees the whole answer in a
card published by the dispatcher's `on_reply`, labelled from the route and never from the
answer's text. What the model record says about the kind of work is added to the prompts
and the result (`abilities.delegation_notes`); it informs and never refuses. Every question to the owner
(approval for ask-first material, confirmation for a spoken turn, screening hold, usage
limit) goes through `gateway/prompts.py` (`OwnerPrompts`), never the event bus, whose
history is replayed to new windows; a question is shown only to windows connected at the
time, no answer in 120 seconds, when Freya closes, or when every window it was shown in
has disconnected is a denial, and an answer counts
once and only if it is one the question offered. A refusal's wording is fixed: it reaches
the model and the audit log, so it must never hold a brief, a match or the vendor's text.
Out of this version: other providers, several delegations in a turn, file or code inputs,
"Allow permanently", removing a passage and sending.

Brief screening: `freya.delegation.screening.screen_brief` compares a brief bound for
a cloud model with (id, text) pairs from the personal store and returns what matched;
any match holds the brief. `Corrections` (`data/screening_corrections.json`) holds the
owner's "not private" and "always private" phrases and memories. Keep these rules:
always-private wins over not-private; a not-private entry clears one phrase or one
memory, never a kind; a damaged file means no corrections; entries are made only by
the owner. It is a filter, not a guarantee, and a test records a paraphrase that
passes so nobody claims otherwise. Tests use made-up memories only. The dispatcher
screens every brief when it is authorized and again when it is sent.

Model record: `freya.delegation.record.ModelRecord` keeps, in
`data/model_record.json`, what evaluation reports measured for each entry (a route, a
model and its settings together) by kind of task: counts and timings only. `choose`
picks the least costly allowed entry whose measured pass rate reaches the owner's bar
for the task's value (`CloudSettings.bars`, 70/85/95% to start). Keep these rules:
only measured results count, never a model's own claim; fewer than 15 attempts
(graded replies and attempts that got no reply together, since a worker that often
fails to answer is not a reliable one) is unmeasured; an unmeasured entry is used only when no measured one qualifies and is
labelled so; when every candidate is measured and none qualifies, nothing is chosen.
`python -m freya.evaluation.record_reports <reports>` is what fills the record: it
grades each report's stored replies again with today's checks (reading them, never
running them) before recording the counts.

What Freya knows about her own ability: `freya.delegation.abilities` reads the record
for the model she answers with and for each cloud route, and `model.record.read`
(provider `model-record-local`, policy seed `alpha-model-record`, a model tool with
no arguments) gives it to her: passes out of attempts by kind, the value of work each
is good enough for under the owner's bars, where `choose` would send a task of each
kind and value, and `NOT_MEASURED`, the work no task covers. Keep these rules: it
reports measurements only, never an opinion of a model, Claude's included; an
unmeasured model is unknown, not better or worse; a cloud route is advised only
while its provider is switched on; a line leaves `NOT_MEASURED` only when tasks for
it exist and have been run. The model is given the short form (`abilities_for_model`, kept under
2,200 bytes because the context clips a tool result at 2,400); keep it under that
and keep the test that passes it through `fit_tools`. It reads and advises; it sends nothing and no turn
delegates on it yet. Her replies name no thinking setting, which the record holds as
`default`; figures for other settings are labelled as such. A figure measured on
another set of tasks than today's (`suite_version`), or on another build of a local
model than the one installed (its digest, asked of Ollama directly when she reads),
is shown but does not count (`CurrentFigures`): changing the tasks makes every model
unmeasured until it is run again, which is intended.

Claude plan route: `providers/model/claude_plan.py` sends one brief to Claude by
running the owner's own signed-in Claude Code program (`claude.exe` from the desktop
app, found directly, never through the `.cmd` launcher; `FREYA_CLAUDE` overrides) and
returns the reply as untrusted text with its token counts. Keep these rules: the brief
goes in on standard input and never as an argument; the fixed arguments ask for a
worker with no tools, none of the owner's customisations, and no saved session (these
are requests to the vendor's program, not containment); it runs in an empty temporary
folder and is given only the variables in `ENVIRONMENT_ALLOWLIST`, never Freya's whole
environment; before sending it asks the program how it is signed in and refuses
anything but a Claude subscription served by Anthropic; Freya never reads the
program's sign-in files; the vendor's error text goes in `PlanRouteError.detail`,
never in the message, because the message reaches the audit log; never add a
`--dangerously-` flag. When the program's error says the plan's limit is reached
(`read_limit_notice`; the vendor's wording is undocumented, so it reads only the forms
seen so far), `CloudDelegation.note_limit` stops further briefs to that provider until
the stated reset, or for an hour when no time could be read; the hold is kept in
memory only, and switching the provider off clears it. A weekly notice also moves the
ledger's week to the provider's reset. `ask_claude_plan` sends whatever it is given: do not register
the route in the control plane until one sending boundary checks every brief. The provider
`anthropic-plan` and capability `cloud.anthropic_plan.ask` exist but are not
registered by `build_local_control_plane`, are in no policy seed, and are not model
tools. Only the dispatcher calls the route. Tests use a fake program.

Model tool calling: `FreyaAssistant.respond_stream` offers the model the capabilities
whose metadata has `model_tool: True` (never destructive ones) using the model's native
tool calling. Requested tools run through the broker, their JSON results go back to
the model, and it answers in its own words; at most three tool rounds and twelve
tool attempts per streamed turn, including refused attempts. Requests on the final
answer round are never dispatched. The
offered tools mostly take no arguments, and arguments the model invents are discarded. Tool
results are untrusted content from the model's point of view. The tool exchange is not
kept in conversation history. A tool that takes arguments declares them in its metadata
(`tool_parameters`); `ModelToolHost` passes only declared arguments and requires each
to be non-empty text within its declared length (unless declared `optional`).
`image.generate` and `work.delegate` take arguments. `image.generate` is provided by Freya's in-process image service (`image-service-local`), which starts
ComfyUI on demand and unloads the Ollama models while it runs, so the reply after an
image waits for the model to reload. It has a local, reversible effect; before adding a
tool with a consequential effect, decide how it is authorized, including whether a
spoken request may trigger it.

Reading files: `tools/file_tools.py` (`FileReader`) gives the model three read-only
tools, `files.list`, `files.read` and `files.search` (provider `file-reader-local`,
policy seed `alpha-file-read`), over the folders the owner has opened and nowhere
else. No folder is open to start, and until one is the provider is unhealthy, so the
tools are not offered at all. Only the owner's `/folders`, `/folder open <path>` and
`/folder close <path>` (`file_access.py`, in both command paths) change the list,
kept as `readable_folders` in `data/freya_settings.json`; nothing the model can call
does. Keep these rules: a path is resolved to where it really is before it is
checked, so a link or junction cannot lead out; a refusal uses the same words
whether or not anything is there and never names a path, because the message
reaches the audit log; key and sign-in files and folders, `.git` among them (`SECRET_FILE_PATTERNS`,
`CLOSED_FOLDER_NAMES`) and Freya's own `data/` stay shut even inside an opened
folder; a whole drive, the whole user folder and `AppData` cannot be opened; every
call returns a bounded amount, kept under `MAX_RESULT_BYTES` (2,200) as encoded
because the context clips a tool result at 2,400 bytes and a clipped result would
carry line numbers and counts for text the model never sees (a test passes each
tool's largest result through `fit_tools`); only an ordinary file is read, never a
pipe or a device. There is no tool that writes, and adding one is a
separate decision. A spoken request can read an opened file aloud like a typed one;
voice is not authentication, so open only folders that may be heard.

Live world data: `tools/world_view.py` (`WorldView`) gives the model eleven tools,
`world.overview`, `world.aircraft`, `world.ships`, `world.satellite_pass`,
`world.earthquakes`, `world.fires`, `world.storms`, `world.launches`,
`world.cameras`, `world.military` and `world.show` (provider `world-view-local`,
policy seed `alpha-world-view`). They ask a God's Eye View program
(github.com/bilawalsidhu/gods-eye-view, MIT, installed and started by the owner, not
bundled) on its local endpoint; that program fetches the public feeds. These are
the first tools whose answers come from the internet (owner decision 2026-10-11:
public feeds only, every layer, spoken requests allowed), so they are registered
only when `FREYA_WORLD_VIEW=1` and offered only while the program answers, asked
again every few seconds. Keep these rules: the address (`FREYA_WORLD_VIEW_URL`,
default `http://localhost:4173`) must be on this machine and is refused otherwise;
only the few words a tool was given are sent, never memory or conversation text;
every answer is bounded and carries a note that feeds may be late; a missing key is
reported without naming it; `world.show` opens only an address of the program
itself in the browser and changes nothing else; `world.cameras` reports where
cameras are and never fetches a picture. Freya does not start or stop the program,
does not handle its keys, and must never turn on its ChatGPT sign-in option, which
reads another program's sign-in file. Adding a tool that fetches camera pictures,
or that contacts a feed directly, is a separate decision.

Conversation memory: one run of Freya is one conversation. Each exchange is stored
as it happens (`conversation`, `conversation_turn`, and the `conversation_search`
index in `data/freya_memory.db`), with how it arrived (typed, voice, window).
Commands are not stored. A summary is written when Freya closes; missed summaries
of earlier conversations are caught up by the next authorized recall. When the live
conversation passes 40 messages, its older part is folded into a summary and only
the last 16 are sent to the model; the words stay stored.

Stored memories, recent summaries and passages supplied for a reply go through one
`conversation.recall` broker call per turn (provider `conversation-memory-local`,
existing policy seed `alpha-conversation-recall`). The model's own recall tool uses
the same capability. A question about the past (`ASKS_ABOUT_THE_PAST`) gets a wider
bounded search. Retrieved records retain source/trust labels and are not cached in
live messages. Denied or unavailable recall supplies no stored records; it is not
reported as an empty search. Unsupported owners, domains and purposes fail closed;
the current storage remains single-owner, without authenticated multi-user isolation.
Pass both `tool_host=control_plane.model_tools` and `broker=control_plane.broker`
to `FreyaAssistant`; an assistant without a broker cannot retrieve stored context.

The Memory tab and `/find`, `/history`, and `/memories` remain direct owner operations.
Live dialogue, its current-run summary, and explicit maintenance/diagnostic model
calls retain their existing behavior and model-capability checks. Missing embeddings
do not block startup or saving: text search remains available, and a successful
semantic search or next start retries unembedded memories.

Explicit forgetting removes a saved memory and its embedding, retained history and
migration-plan copies in one transaction instead of archiving it. Edit and duplicate
history remain supported. Exchange/conversation deletion removes text and search
entries atomically and clears the affected summary. Current live context is rebuilt;
earlier summaries wait for authorized recall. These operations do not erase separate
conversation records that repeat a saved fact, backups, exports or every physical
disk copy, and do not implement restore-safe tombstones or full MEM-008 compliance.
Memory tab answers go only to the requesting window. The first creation of conversation
tables copies the database to `data/backups/`. Never print, commit or transmit its
contents. Never access personal data as part of development tests.

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
the same fixed `local-owner` principal. The page builds everything it shows with
`textContent` and a strict Content-Security-Policy, because replies and tool results
are untrusted; the one exception is `rich-chat.js`, a small display-only Markdown
subset (headings, bullets, bold, code, links and media cards that load only after a
click) that never parses text as HTML and refuses every URL that is not public
`https:`. The window can only send `client.hello`, a chat turn (`say`), an image,
mute/unmute, stopping speech (`interrupt`), a voice volume, `status`, a Memory tab
request, a command, a Voice tab request (with a recording sent as binary messages)
and the `speech.*` playback notices, a Media tab request, an Avatar tab request, a
Cloud tab request, or an answer to a question Freya put (`cloud.answer`; Freya sends
`cloud.prompt` and `cloud.prompt.closed` to the windows, never through the bus);
add new message types deliberately, in `Gateway._handle_message`. `main.py` publishes conversation and image events
on a module-level `EVENTS` bus; the console still prints as before. The bus calls its
subscribers in publish order while it is locked, so a subscriber must never block: the
gateway's only puts the event on that window's queue, and a thread per window does the
sending. A window joins with `subscribe_with_history`, which is what guarantees it
gets every event exactly once. The history is bounded by entries (500), and a reply
arrives one chunk per token, so the bus keeps the chunks of one reply as a single
entry; the live events are delivered one by one as they came. The hello's console lines are taken
in the same step as the subscription (`subscribe_with_history(..., snapshot=...)`), and
the tee records and announces a line under the bus's lock, so a line arrives once. Memory commands (`/memories`, `/forget`, `/memory-help`
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
`audit_event` table. It is verified at startup; if verification fails, or the log
cannot be read, the broker refuses every invocation. The dispatch record must be
written before a capability runs. A denial, unavailability decision, or outcome that
cannot be written (the decision is made or the action has happened) is never dropped:
it is logged, the result has `audit_recorded=False`, and the broker keeps it and
refuses every new dispatch until it is written (at most 256 are kept; beyond that each
loss is logged as critical and counted). Checkpoints are best effort and never decide
whether a capability runs. `ControlStore.execute` returns rows already read under its
lock, because the connection is shared between threads. Checkpoints of the chain head go to the file named by
`FREYA_AUDIT_CHECKPOINT_PATH` when set, which only adds tamper evidence if that file is
somewhere this machine cannot rewrite. This does not protect against a host
administrator. SQLite durability settings are provisional pending ADR-015; each audited
invocation currently costs roughly 30–60 ms in synchronous writes. Default policy rules
are applied once per named seed (`policy_seed` table), so removed rules stay removed.

Code sandbox: `freya.sandbox` is the contract for running code a model wrote
(`contract.py`) and the capability `code.run` on provider `code-sandbox-local`
(`service.py`). There is no real sandbox yet, only a stand-in in the tests, and
`build_local_control_plane` does not register it; a test checks that. The design,
`docs/design/code-sandbox.md`, is a proposal that is not adopted: a Linux container
in WSL2, because this PC runs Windows 11 Home, which has no Hyper-V. Keep these
rules: a sandbox must state no network, none of this machine's files, no
credentials, and nothing kept between runs, or it cannot be registered; a run is
bounded text files and a command from a fixed list, never a command line; only
made-up material goes in, never anything from `data/`; output is untrusted text;
`SandboxError.detail` never goes in the message, because the message reaches the
audit log; `code.run` is in no policy seed and is not a model tool. Describe the
container profile as holding mistakes, not a determined attack.

`python -m freya.evaluation.delegation` measures a model on 91 synthetic delegated
tasks in eight kinds (`evaluation/delegation_tasks.py`), each with a deterministic
check. Twenty-eight of them (`STRETCH_TASKS`) are the hardest, added when the best
local model passed all the rest; their expected answers are worked out by code from
the data the brief is written from, and a test solves the puzzles again by trying
every case. Add a task that way, not with an answer typed in by hand. It runs an installed Ollama model, or Claude on the owner's plan with
`--claude-plan`, which is refused without `--owner-approved-cloud-run` because it
sends the tasks to the vendor and uses allowance. Replies are read, never executed;
do not add a task that is graded by running what a model wrote. Every task carries a
reply that must pass and one that must fail, and a test checks both.

`python -m freya.evaluation.workflow` runs opt-in local-model evaluations with
synthetic memory and simulated tools; see `docs/design/workflow-evaluation.md`.
It never uses the owner's data or live hardware tools. `python -m freya.evaluation.gate`
is the promotion gate (NEW-CB) built on it: a hint or model change is run without and
with the change on the same cases, model, fixtures and seed; only held-out cases
decide, against the owner's bars, and fewer than 15 attempts is unmeasured. It
measures and switches nothing on. Keep these rules: a candidate file has no field
beyond the documented ones; training sources are accepted by provenance only, never
text that came through a tool (NEW-CG); a partial regression run can never pass.
The workflow runner's four `ability-` scenarios
give her a made-up model record and check that she reads it before speaking of her
own ability, calls unmeasured work unmeasured, and never claims to have sent work
away.

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
- Requires Ollama on `127.0.0.1:11434` with `qwen3.8:27b` and `nomic-embed-text`.
  `FREYA_MODEL` names another installed model (`gpt-oss:20b` was the main model until
  2026-10-09; see `docs/design/model-benchmarks-2026-10-09.md`). `qwen3.8:27b` uses
  about 19 GB of video memory at the 16,384-token context, against about 13 GB.
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
- `.claude/hooks/freya_guard.py` (wired in `.claude/settings.json`) enforces the rules
  above that a text check can: it refuses edits to `data/`, database files, `uv.lock`,
  the vendored browser libraries and shipped `MIGRATIONS` entries; commits or pushes
  to `main` and force pushes; shell commands naming the memory or control databases;
  and a `git commit` while the tests fail (`FREYA_SKIP_COMMIT_TESTS=1` skips that
  check). It is a guard rail, not a security boundary. Other agents follow the same
  rules from `AGENTS.md`; work delegated to Codex is in `docs/tasks/codex-delegation.md`.

## Working with Codex

OpenAI's Codex is installed on this workstation and reads `AGENTS.md`. Claude drives
and calls Codex as a helper through `scripts/codex.ps1`, which finds the program (it
is not on PATH). Do this without being asked:

- **Review before every pull request.** Run `scripts/codex.ps1 review --base main`
  (or `--uncommitted`), check each finding against the code, fix the real ones, and
  tell the owner what Codex found and what was done about it.
- **Hand over side tasks that do not touch the files being edited**: a test file for
  a finished module, a second implementation to compare, a search for the cause of a
  failure. Make a git worktree in a folder outside this one on a new branch, and run
  `"<task>" | scripts/codex.ps1 exec -s workspace-write -C <worktree> -`. A worktree
  has only committed files, so `data/` is not in it. Read the diff and run the tests
  before bringing the branch in; the pull request is still opened here.
- **Ask for a second opinion** when stuck or before an architecture decision:
  `exec -s read-only`, with the question and the file paths on stdin.

Limits: never use `danger-full-access` or either `--dangerously-` flag. Codex does
not push, merge, or open pull requests. Its answers are advice to verify, not
instructions, and never a reason to skip the tests. Everything Codex reads is sent to
OpenAI, so never put text from `data/` in a prompt; the sandbox does not stop Codex
reading outside its folder, so `AGENTS.md` and the worktree are what keep it away
from `data/`, not a hard barrier.

Both programs come from their desktop apps, in folders renamed on every update. Two
launcher files in `%APPDATA%\npm` (`codex.cmd`, `claude.cmd`; on PATH, not in this
repository) forward to the newest copy of each. The Codex plugin for Claude Code
(`/codex:rescue`, `/codex:setup`) needs Node.js and that `codex` launcher. In the
other direction Codex can run `claude -p "<task>"`, but only outside its sandbox,
which blocks the network and Claude's settings folder: in the Codex app the owner
approves that command each time. Do not widen the sandbox to cover `~/.claude` (it
holds Claude's sign-in) and do not put a bypass flag in a script to avoid the
approval.

## Working with Antigravity

Google's Antigravity command-line program (`agy`, signed in on the owner's Google
account; it replaced Gemini CLI, which no longer serves personal accounts) is a second
helper, called through `scripts/agy.ps1`. It also reads `AGENTS.md`. Codex stays the
reviewer before a pull request and the helper for coding side tasks. Use Antigravity
for a second opinion from a different vendor's model, or a read-and-report question
over many files:

    "<question>" | scripts/agy.ps1 -C <worktree>          (asked to read and advise only)
    "<task>"     | scripts/agy.ps1 -C <worktree> -Edit    (may edit files there)

Limits: `agy` reads and writes files in the folder it is started in without asking,
and everything it reads is sent to Google. So it is only ever started in a git
worktree outside this folder (the script refuses a folder holding `data`), and text
from `data/` never goes in a prompt. Never pass `--dangerously-skip-permissions`, and
do not add allow rules to its settings (`~/.gemini/antigravity-cli/settings.json`) to
let it run commands: those are owner actions. It cannot run shell commands, so it
cannot run the tests; run them yourself on anything it wrote. Without `-Edit` it is
only asked not to change files, which is a request, not containment; the script
reports any file that changed. It does not commit, push, or open pull requests, and
its answers are advice to verify, as Codex's are.

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

Proposed, not started: lessons from the OpenRig review (`docs/design/openrig-lessons.md`,
provisional NEW-AS to NEW-BD; `code-sandbox.md` holds NEW-AM to NEW-AR). Adopt
addressable knowledge, situation-tagged context, a generated map of existing
capabilities, read-time trust downgrading, lessons, and an evaluation of whether models
reuse what exists. Rule out reachability as authorization, caller-asserted identity, and
shared packages that raise privilege; NEW-AZ, NEW-BA and NEW-BC become acceptance
criteria for the authenticated gateway. First step: the evaluation scenarios (NEW-AY).

Proposed, not started: a learning loop inside the governance (`docs/design/learning-loop.md`,
provisional NEW-BE to NEW-BL): an experience store reached only as a capability, a
plan/verify/retry loop in kernel code with fixed limits, learned material that informs
and never authorizes, stages promoted only by the owner, the owner's reaction as the
only reward, experience that never leaves the machine, and three evaluation scenarios
(reuse, catch, avoid) that must pass before the loop is switched on. Superseded in mechanism, kept in rules, by `skill-learning.md`.

Proposed, not started: skills, checks and one gate (`docs/design/skill-learning.md`,
provisional NEW-BW to NEW-CG): a tested, versioned skill file as the unit of learning;
deterministic checks before any model verdict; an episode log with graded signals
where weak signals only demote; candidates drafted nightly on the idle GPU; one
promotion gate (baseline, held-out cases, rollback) for skills, router retrains, prompt
changes and model swaps; weekly batch approval by the owner; new tools learned in the
sandbox before ordinary use; self-coding only as pull requests with a protected core.
Build step 1 is done: the gate (`freya.evaluation.gate`) for hint and model changes.
Next: step 2, the skill format, loader and matcher with three skills the owner writes.
The next proposal starts
at NEW-CH; `docs/labels.md` is the register, and `tests/test_labels.py` fails on a
label defined twice or not registered.
