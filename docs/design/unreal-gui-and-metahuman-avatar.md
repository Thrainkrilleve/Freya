# Design proposal: an Unreal Engine desktop GUI with a MetaHuman avatar

**Status: proposal, written 2026-10-08. Nothing here is implemented, installed, or
approved.** It does not approve any ADR, freeze the architecture, or assign
requirement IDs. New requirements carry provisional labels **NEW-AC to NEW-AJ**,
continuing from NEW-A to NEW-L (pending amendments) and NEW-M to NEW-AB (the
development design); no existing ID is reused. Baseline references (ARCH-, GUI-,
CAP-, SEC-, RES-, LIC-, EXT-, OPS-, PROV-) are to *Freya Foundation v1.1*.
ADR-017 (gateway authentication) is pending and this design depends on it.

Each statement below is marked **[Verified]** (checked today, with how),
**[Recommend]** (a design choice), or **[Experiment]** (unknown until tried).

> Completed from the Claude handoff by Codex on 2026-10-08. This remains a proposal.
> Current source and the relevant baseline contracts were read; Epic primary
> licensing sources and the Fab listing were accessible during this review.
> No implementation, installation, packaged-build test, or benchmark was performed.

## 1. Direction (from the owner)

1. Unreal Engine becomes the proposed main desktop GUI: one ordinary resizable
   window with a MetaHuman-style avatar beside chat and controls, portrait and
   full-body views, optional always-on-top. The floating transparent desktop
   avatar, click-through, desktop walking, and pointing outside the window are
   **deferred**.
2. The Python backend stays independent and owns reasoning, memory, models, tools,
   permissions, audit, images, and voice generation. Unreal presents, takes input,
   animates, and plays coordinated speech. The web GUI stays as a fallback. Freya
   must work with no Unreal and no avatar.
3. MetaHuman is the first avatar, not the only one. VRM and lightweight renderers
   are preserved; identity, asset format, renderer, and presentation settings are
   separated; renderers report their features.

This fits the baseline: "Freya has no inseparable canonical GUI" (section 14),
GUI-001 (several GUIs, no duplicated state), GUI-004 (GUIs are untrusted, limited
clients), ARCH-001 (every GUI is replaceable).

## 2. What was checked

| Fact | Status |
|---|---|
| The plugin repository `isundahl/LLM_Plugin` is Apache-2.0, created 2026-07-30, last pushed 2026-09-07, 14 commits, 5 stars, Win64 only, marked beta (0.2.0-beta). | **[Verified]** GitHub API and README |
| Its descriptors name **Unreal Engine 5.8**. | **[Verified]** `.uplugin` |
| The MetaHuman bridge is about 26 KB of C++ in one component. It contains **no characters, animations, maps, models, or voice weights**; the README says so, and the repository excludes "demo map and MetaHuman assets". | **[Verified]** source read |
| The bridge is a thin layer over **Epic's own** real-time audio solver: it subclasses `FMetaHumanAudioBaseLiveLinkSubject` and `FMetaHumanLocalLiveLinkSource` (modules `MetaHumanLocalLiveLinkSource`, `MetaHumanCoreTech`, `MetaHumanPipelineCore`; plugins `LiveLink`, `MetaHumanLiveLink`, `MetaHumanCoreTech`), pushes PCM with `AddAudioSample`, and publishes Live Link curves the MetaHuman face reads. | **[Verified]** source read |
| It feeds 20 ms frames, uses a solver lookahead of 80 to 240 ms, warms the solver with 1 s of silence at 24 kHz mono, and delays audible playback separately from the solver so lips and voice can be aligned. | **[Verified]** source read |
| The bridge depends on the plugin's own TTS component and audio types (`ULocalLLMTextToSpeechComponent`, `FLocalLLMAudioChunk`). It cannot be used without the main plugin as written. | **[Verified]** `Build.cs`, headers |
| The companion README instructs testing a cooked Win64 build with the intended versions. It is not proof of packaged compatibility, nor proof that packaging is unsupported. | **[Verified]** README; **[Experiment]** our exact build |
| Epic has published UE 5.8 toolchain documentation, and the listing targets 5.8.1. | **[Verified]** Epic documentation and Fab listing; exact installed version remains to be established |
| Epic documents real-time MetaHuman animation from audio through Live Link from UE 5.6, and says it is "computationally heavy" and needs the GPU. | **[Verified]** Epic documentation, via search |
| Kokoro, Freya's voice, produces 24 kHz mono float audio: the same form the bridge warms the solver with. | **[Verified]** `voice/tts.py` |
| On this PC: Epic Games Launcher is installed; **no Unreal Engine is installed**; Visual Studio 2026 Community with MSVC 14.51 is installed; 1.3 TB free; RTX 4090 with 24 GB, 3.6 GB in use when idle. | **[Verified]** looked today |
| The Fab listing and Epic's English Unreal EULA were read during Codex's completion. | **[Verified]** primary sources, linked in section 13 |
| Current EULA section 6(e) restricts MetaHuman characters, animation curves, and certain rendered output from AI database/training/testing uses. This is not a blanket prohibition on an AI assistant displaying a character. | **[Verified]** EULA; applicability to distribution must be reviewed |
| Licence cost depends on use: seat exceptions, royalty-free products, and royalty-bearing products have different conditions. Do not apply a blanket $1 million rule to all cases. | **[Verified]** EULA sections 3, 4, 6 and Royalty Addendum |


**Conclusion on the plugin. [Recommend]** Treat the MetaHuman bridge as
**reference code**, not a dependency. The solver-facing subject/source is only one part of the work. Transport,
bounds, thread ownership, playback timing, cancellation, lifecycle and packaging
need additional implementation and tests; no reliable line-count estimate exists.
Record a proportionate custom-build decision note (LIC-005). Write a narrow
Freya Unreal module that does this with
Freya's audio, keeping the Apache-2.0 notice for anything adapted (LIC-001,
LIC-004). Do not take the main plugin: it brings its own LLM, speech recognition,
speech synthesis, and 3 GB of models, all of which Freya already has.

## 3. Where the code is today

| Area | Today | For this design |
|---|---|---|
| `AvatarHost` | Starts an overlay process, talks JSON lines over pipes, survives its crash, kills it on stop. | **Reuse the pattern** for starting and supervising Unreal. The pipe protocol itself is not reused: Unreal is a full GUI client and goes through the gateway. |
| `AvatarService` | Three display capabilities plus library, search, prepare; every request is a broker call; settings per user; an off switch. | **Keep.** Unreal's avatar requests go through the same broker calls. |
| `AvatarModel` | Size limits, boundaries, placement beside a target, pointing geometry on the desktop. | **Mostly not needed** inside a window. Useful parts: expression state, speaking state, wave, the animator's priorities and interruption. Desktop placement and pointing stay for the deferred floating avatar. |
| Renderer selection | `RENDERERS`, `choose_renderer`, `on_request`; `tk-placeholder`, `null`, `web`. | **Extend** with a feature report (section 6). |
| Avatar profiles | `AvatarLibrary`: a profile is a name and the SHA-256 of a VRM file. | **Generalise** (section 6). VRM-specific today. |
| Speech events | `FreyaVoice` synthesises a sentence with Kokoro and plays it itself with `sounddevice`; a listener hears started, completed, interrupted, and a constant level. | **Change** (section 5): audio must be able to go to a client, and "speaking" must mean what is actually audible. |
| Gateway | Loopback HTTP and one WebSocket; Host, Origin, and cookie checks; a single-use key in the address; per-window queue; no version, no client identity, no scopes. | **Extend** into a versioned protocol with a handshake (section 4). |
| Shutdown | Ordered: gate closed, windows stopped, turn drained, then voice and control database. Console-free mode exits 8 s after the last window closes so the microphone is never left open unseen. | **Keep the order; generalise** "window" to "client". |
| GPU coordination | `GPUResourceManager` unloads Ollama models for ComfyUI and for a large speech engine. No accounting of anything else. | **Extend**: Unreal becomes a known consumer (section 8). |
| Review finding: bounded archive extraction | **Not fixed. [Verified]** `converter.unpack` checks declared sizes for `.zip` before extracting, but for `.7z` and `.rar` it runs `tar -xf` and checks size, count, and links only **afterwards**, limited by a 10-minute timeout alone. A hostile archive can fill the disk before the check. | Fix before accepting imported assets: use bounded extraction that enforces bytes and entries as written, validates members/links before writing, and cleans partial output; otherwise disable non-ZIP extraction. Listing and polling a folder alone do not enforce a hard limit. |
| Review finding: verified avatar content identities | **Not fixed. [Verified]** `AvatarLibrary.scan` takes a model's SHA-256 and licence from a cache in `avatar_library.json` when the file's name, size, and modified time match, without checking the cached entry's shape or re-hashing. `model_bytes` and `active_model_path` hand out the file without confirming it still has that hash. A file swapped in place, or an edited cache, gives one model another's identity and licence confirmation. | Fix by hashing at the point of use, or by keeping accepted models in a content-addressed, read-only store. Section 6 depends on this. |

## 4. Backend communication and lifecycle

**[Recommend]** Unreal is a client of the Client Gateway like the web page, not a
special path. One protocol, two clients (GUI-001, GUI-002).

**NEW-AC (proposed, MUST).** A GUI client presents to the user and collects input.
It holds no personal database, secret, model, or capability handler, and reaches
Freya only through the Client Gateway. Every request it makes that has an effect
is carried out by a broker invocation in the backend, attributed and audited
there (GUI-004, ARCH-019).

**Protocol.** Version 1 formalises what the window already does.

- Transport: the existing loopback HTTP and WebSocket (GUI-008). Text frames are
  JSON objects with `type`; binary frames are used only for audio and uploads.
- Handshake: the client's first frame is
  `hello {protocol: 1, client: {kind, name, version}, features: [...]}`. The
  server answers `welcome {protocol, server_version, features, limits, session}`
  or `refused {reason}` and closes. Unknown required features are refused, not
  ignored (GUI-012).
- Limits, stated in `welcome` and enforced by the server: frame size, message
  rate, upload size, audio buffer, text length. The existing limits (4,000
  characters of chat, the upload cap) become part of this.
- Events carry a sequence number; a reconnecting client sends the last one it saw
  and is told to resynchronise if it is too old (GUI-013).
- Current request types keep their meaning: `say`, `image`, `command`, `mute`,
  `volume`, `interrupt`, `status`, `voice`, `avatar`. Memory currently uses
  `command` (not a separate existing `memory` request); new structured memory
  APIs require independent schemas and authorization. New: `speech.*`
  (section 5), `client.state` (visible, minimised, hidden), `ping`/`pong`.
- The web page moves to the same handshake. There is one server-side
  implementation of business rules and canonical state. Each client owns its
  presentation and interaction state; contract tests detect client divergence.

**Authentication. [Recommend], subject to ADR-017.** Today's single-use key in the
address is acceptable for a browser Freya opens herself but conflicts with GUI-009
("never put bearer tokens in URLs") as a general mechanism.

- When Freya starts Unreal, she passes a single-use pairing secret **on the child
  process's standard input**, not on the command line or in the environment (both
  are visible to other programs of the same user). Unreal exchanges it once for a
  session token, held in memory only.
- A client Freya did not start must be approved by the owner in an already
  trusted place (GUI-009). Not in the first milestones.
- Each client kind gets a **scope**: the set of message types it may send. The
  Unreal client's maximum scope is explicitly enumerated, not inferred from its
  self-reported kind. Playback acknowledgments belong only to the current
  playback owner. It is not a way
  to name capabilities directly.
- This is still not user authentication: turns remain attributed to the fixed
  `local-owner` principal until authenticated sessions exist (GUI-014).
- Passing a secret through stdin reduces accidental disclosure; it is not
  protection against a compromised same-user process. The baseline requires
  authenticated owner confirmation of pairing and independently authenticated
  human sessions. Automatically launching a child cannot substitute for those
  requirements. A local-only experimental profile needs an explicit deviation
  record, restricted scope and disabled approval-capable/protected actions.
- Sessions need expiry, revocation, subscription filtering and replay checks.
  Changing GUI technology does not grant owner authority. Existing direct voice
  studio/image paths must be inventoried and migrated through the broker before
  claiming all GUI effects are broker-controlled.

**NEW-AD (proposed, MUST).** Exactly one party owns Freya's lifecycle. In the
alpha it is the Python backend: it starts first, starts the chosen GUI, and
decides when to exit.

- Setting `gui = unreal | web | none`. `unreal` falls back to `web` with a notice
  if Unreal is not installed or fails to start twice.
- Readiness: distinguish protocol-ready from UI/asset/solver-ready. The full GUI
  is ready only after required initialization, within configurable deadlines;
  the web window opens meanwhile only if the owner has chosen that.
- Heartbeat: ping every 5 s; a client silent for 15 s is treated as gone.
- Crash or disconnect: revoke the playback generation and transfer future audio
  after the bounded lease/watchdog rule (section 5). Restart crashes with backoff
  at most three times; do not restart an intentional user close. Offer the web
  fallback after the restart budget is exhausted.
- **Closing Unreal** follows one explicit policy, the owner's choice (decision
  U2): *exit with the window* (today's console-free behaviour, 8 s after the last
  client goes) or *keep running*. If Freya keeps running with no client
  connected, the microphone is **muted until a client returns**, so it is never
  open unseen. With a console attached, today's behaviour is unchanged.
- Shutdown order is unchanged, with "window" read as "every client"; Unreal is
  asked to close, given a few seconds, then ended with its process family.

**Threads. [Recommend]** In Unreal all network reading, JSON parsing, and audio
decoding happen off the game thread; the game thread receives small, already
parsed messages through a queue and never waits on the backend.

## 5. Speech and facial animation

**NEW-AE (proposed, MUST).** At any moment exactly one party plays Freya's voice
aloud: the backend, or one client that asked for it and was granted it.

- A client asks for playback with the `speech.playback` feature in its `hello`.
  The backend grants it to at most one client and records which. With no such
  client, the backend plays, as now. Owner transfer occurs between utterances
  after release acknowledgment, or after a bounded playback lease expires.
- If the playing client vanishes mid-sentence, that sentence is abandoned and the
  backend may speak unsent sentences after revoking the client playback lease.
  A disconnect cannot reveal exactly which samples were audible. Do not replay
  an ambiguous utterance automatically. Use ownership generations, deadlines,
  and a client watchdog that silences itself on lease expiry; this bounds stale
  playback but cannot promise instantaneous or sample-perfect failover.

**Format and transport.**

- Kokoro synthesis currently produces 24 kHz mono floating-point samples.
  Proposed transport: explicit little-endian signed 16-bit PCM after bounded,
  validated conversion. The native solver input may be converted back to float.
  Other studio engines report their own rates; any conversion is explicit and
  tested, rather than assuming all Freya audio is 24 kHz.
- Per sentence: `speech.start {utterance, sample_rate, channels, text}`; then
  binary frames, each with an 8-byte header (utterance number, sequence number)
  and at most 100 ms of audio; then `speech.end {utterance, samples}`.
- The client answers `speech.started`, `speech.finished {utterance, samples}`, or
  `speech.interrupted`.
- Bounds: at most 30 s of audio per sentence and 60 s queued in total; beyond that
  the backend waits with a cancellation-aware deadline. Longer sentences must
  be segmented or rejected explicitly. Do not wait for playback of a sentence
  whose complete buffer can never fit. A frame out of sequence ends the utterance
  with an error. Binary envelopes also declare payload kind, format and protocol
  version, so uploads cannot be confused with speech.
- `speech.cancel` empties the client's queue, resets the solver, and returns the
  face to neutral.

**NEW-AF (proposed, MUST).** Freya counts herself as speaking from the first
audio sent until the playing client reports the sentence finished or interrupted,
or a bounded lease/watchdog establishes that playback has stopped. Separate
`queued`, `playing`, `draining`, `cancelled`, and `unknown` states. Conservatively
suppress listening while playback could be active. A timed-out report alone must
not unmute the microphone while a disconnected client might still be speaking.

Today `FreyaVoice.is_busy()` is true while the backend's own player runs; with
Unreal playing it must follow the client's reports instead, or Freya would hear
herself.

**NEW-AG (proposed, MUST).** Audio sent for facial analysis is at full level.
Volume is applied only where sound is made audible.

Today `FreyaVoice` scales the samples by the volume before playing. For a client,
the unscaled audio and the volume are sent separately, so a quiet voice still
articulates fully.

**In Unreal. [Recommend]** A small Freya module, modelled on the reference bridge:
a Live Link subject fed from a queue at real-time pace, the solver warmed with
silence at start, and audible playback delayed by a calibrated amount so the
voice does not lead the lips.

**[Experiment]**

1. Does Epic's audio solver run in our exact **packaged** build, not only the Editor?
   The reference asks developers to test this configuration. This is a gate and is
   the first thing milestone 1 must prove.
2. Whether the solver accepts 24 kHz directly in 5.8 (the reference feeds it so).
3. The lookahead and playback delay that look right with Kokoro's voice, measured
   against audible playback; compare relative timing rather than assuming a
   particular direction of delay fixes every configuration.
4. Blending: the solver drives jaw, lips, and tongue; expressions are layered on
   the upper face, and on the mouth corners at reduced strength, so a smile does
   not fight the lip shapes. Whether this looks right needs trying.
5. Neutral recovery: after `speech.finished` or a cancel, ease all speech curves
   to zero over about a quarter of a second.
6. The solver's GPU cost beside Ollama.

## 6. Avatars: identity, asset, renderer, presentation

**NEW-AH (proposed, MUST).** Four things are recorded separately:

1. **Identity**: who the avatar is (a name, an id). One identity may have several
   assets.
2. **Asset**: one concrete form of it, with a `format` (`vrm`, `metahuman`,
   `placeholder`), where it is, a content digest, and its licence record.
3. **Renderer**: what draws it (`tk-placeholder`, `web`, `unreal`), chosen by
   what is installed and what the asset's format needs.
4. **Presentation**: view (portrait, full body), always-on-top, quality, size.

A MetaHuman asset is **not** squeezed into the VRM fields: it has no single file,
no VRM licence block, and no VRM expression list.

**NEW-AI (proposed, MUST).** A renderer reports its features and the GUI shows
only controls for those: expressions, lip sync from audio, portrait and
full-body views, gestures, appearances, pointing on the desktop, click-through.

**How MetaHuman differs from VRM. [Recommend, based on the cooked-build approach]**

- A VRM is one file the owner drops in a folder; Freya reads its description and
  shows it immediately.
- A MetaHuman is made in MetaHuman Creator inside the Unreal Editor, assembled,
  and **cooked into the Unreal build**. Adding or changing one means editor work
  and a rebuild, or a separately built content pack. This proposal does not implement raw MetaHuman import at
  run time. Compatible cooked content packs are possible to investigate; arbitrary
  source-asset import is separate later work, not categorically impossible.
- So "switching character" means choosing among characters in the installed
  pack, each listed in an asset manifest with a digest (LIC-006, EXT-006).

**NEW-AJ (proposed, MUST).** An asset's identity is the digest of its content,
checked when it is accepted and again when it is used. A cached record is never
enough on its own. (This is the open review finding, made a requirement.)

## 7. Avatar assets and animation

**[Recommend]** Start with one owner-directed character, hairstyle and outfit,
using UE Optimized assembly. Make a portrait and full-body camera preset, a
simple backdrop and consistent lighting. Record a visual reference approved by
the owner before expanding the wardrobe. Hair cards/strands, textures, LODs and
material cost are quality choices to benchmark, not fixed assumptions.

An asset manifest records character identity, build/engine version, content
bundle digest, dependencies, provenance, allowed uses, redistribution terms and
review status. Verify the complete cooked bundle before mounting it. A verified
digest establishes identity; it does not make executable content trustworthy.
Only vetted cooked packs are loaded. Arbitrary downloaded Unreal plugins,
Blueprint code, scripts and import hooks are not runtime avatar assets.

**Animation implementation. [Recommend]**

- A backend semantic state drives listening, thinking, speaking, idle and error.
  These describe activity, not inferred psychological states or authentication.
- An Animation Blueprint blends idle breathing/weight shifts, blink, head/gaze,
  expressions and upper-body gestures. The speech solver owns articulation;
  expression masks and strengths must preserve speech intelligibility.
- Wave and pointing have cancellation, priority, completion and neutral recovery.
  Targets are local UI rectangles resolved from the current layout; offscreen or
  stale controls invalidate gestures. They do not read other applications.
- Reduced-motion mode suppresses unnecessary motion; an avatar-off state retains
  the GUI and text controls while releasing character/solver resources where
  possible. It must not inadvertently close the entire Unreal GUI.
- Source animations need explicit licence records and retargeting to the chosen
  skeleton. The reference companion includes none. Do not assume every Fab
  animation or accessory permits redistribution with Freya.

**Acceptance. [Experiment]** Owner visually reviews portrait/full-body views,
idle loops, hair, outfit, eye contact and speech. Test interrupt during each
animation; mouth and body return to a neutral state without snapping or continuing
stale gestures. More characters, runtime wardrobe tools and arbitrary import
remain separate later milestones.

## 8. Performance, operating modes and distribution

**[Recommend]** Resource Manager remains the single admission authority
(RES-008–011). Add Unreal/MetaHuman as an observed workload with a reservation,
quality profile and drain/release path. A separate renderer process does not
prevent it from competing with Ollama, ComfyUI, Whisper or speech helpers.

Measure with the actual configured model, context size, character and drivers:

| Scenario | Record |
|---|---|
| Backend without Unreal | RAM/VRAM baseline, chat and voice latency |
| GUI without avatar; idle portrait/full-body | Process RAM, incremental VRAM, CPU/GPU use, frame time, power |
| Speech solver and gestures | First audio delay, audible-to-visible alignment, p95 frame time, backlog |
| Concurrent chat/STT/TTS | Change in inference throughput/latency and failed admissions |
| Image generation / large voice helper | Drain duration, observed released capacity, operation success, recovery |
| Minimize, sleep/resume, display/device changes | Playback/listening correctness, device recovery, allocation changes |

Use repeatable scratch prompts and audio. Save versions, settings, repeated
measurements and raw results. No numeric performance claims exist yet. Provisional
quality targets (for example 30 fps during interaction, lower idle rate) are
configurable targets, not acceptance evidence. Budget RAM, GPU workspaces and
non-Freya usage as well as model weights; telemetry alone is not a reservation.

**Release. [Recommend]** First reduce quality or unload avatar assets while keeping
the GUI. Stopping frames does not prove memory is released. Measure retained
allocation after each step; complete Unreal termination is the most dependable
fallback for reclaiming its process resources. When termination is necessary,
transfer future audio safely, mute unseen listening and open the authorized web
fallback before stopping the GUI. Do not kill unrelated Unreal or host processes.

**Operating modes. [Recommend]**

1. Desktop: Python backend plus visible packaged Unreal GUI.
2. Backend only: Unreal stopped. In unattended operation microphone is off by
   default; any voice-only operation needs an explicit configured lifecycle and
   visible/accessible mute mechanism.
3. Offscreen/remote: a future deployment profile. Epic documents `-RenderOffScreen`
   and Pixel Streaming, which still consume GPU/encoding resources. Add TLS,
   authenticated users, scoped subscriptions and microphone consent before remote
   access; loopback pairing cannot be reused as remote security.
4. Unreal `-nullrhi`: no graphical rendering, useful for selected automation tests.
   It does not establish that the MetaHuman solver or audio path works, and is not
   needed just to run Freya's Python backend. Dedicated-server builds are a
   separate Unreal target, not an assumed GUI deployment requirement.

**Build and release. [Recommend]** Keep C++ project, schemas and reproducible build
instructions in source control. Put large reviewed source assets in a dedicated
asset store or approved LFS arrangement; never commit generated caches,
Intermediate/Saved directories, credentials or personal model recordings. Cook a
small Win64 runtime using pinned Unreal, plugins, compiler and SDK versions.
Identify runtime-only dependencies and test with no Editor installed/running.

The optional Unreal component has a versioned manifest, code/content checksums,
protocol compatibility range, asset licences and third-party notices. Stage and
health-check updates before atomic activation; preserve a validated previous
release. Schema migrations need restore or compatible rollback, not just an old
executable. Test missing prerequisites, corrupt bundles, incompatible versions and
interrupted updates. Commercial redistribution requires a fresh licence review.

**Offline gate. [Experiment]** After lawful setup and all required assets are
present, test a packaged build with external network blocked. Check cold startup,
shader availability, chat/voice/image providers, avatar solver, save/load and
shutdown. Report any connection attempt; local operation must not silently depend
on a model hub or Epic service. Initial character creation/download requirements
are distinct from everyday runtime operation.

Initial platform promise is Win64 only. New devices require separate packaged
builds, tested graphics/audio dependencies and lower-resource options. Federation
remains deferred and preserves domain sovereignty (SEC-035).

## 9. GUI inventory and migration

**Current inventory [Verified]** was checked against `gateway/server.py`,
`gateway/static/index.html`, `app.js`, `window_commands.py` and `speech/studio.py`.
The backend owns canonical state. Clients may expose different subsets; matching
contracts do not guarantee matching layout or bug-free behavior.

| Function | Native Unreal implementation / migration requirement |
|---|---|
| Chat and history | Transcript, streamed start/chunk/end events, multiline composer, turn IDs, explicit errors, reconnect snapshot/replay; virtualize long history |
| Commands and memory | Preserve slash commands, bare exit/quit, help, names and memory commands through the shared backend handler; structured memory views are later APIs |
| Images | Prompt, pending/error status, bounded gallery thumbnails, full image view and native save dialog; authorized HTTP resources, bounded texture decoding/cache |
| Live voice | Mic mute, voice volume and stop speech; authoritative backend state, playback-owner protocol, accessible device-error notices |
| Voice presets/blends | Engine/language/voice/speed/mix choices, preview, naming and saving; preserve the experimental blend description |
| Clone/reference recordings | Native picker and recording UI, explicit rights confirmation, transcript, bounded local decoding/resampling into validated PCM; browser decoding cannot simply be assumed in Unreal |
| Voice design | Engine-reported options, preview and saving the generated reference; engine availability/errors from backend |
| Saved voices and clips | List/delete/use supported profiles, speak preview, download generated clips, free helper memory; retain existing live-voice restrictions |
| Console | Bounded log view, select/copy, command composer, show/hide; filter sensitive output before protected delivery |
| Status and settings | Model/image availability, listening state, wake names and persistent volume; add GUI/view/quality settings with documented owner and validation |
| Avatar selection | Identity/assets, licence confirmation, installed renderers, missing dependencies, preview and active choice; compatible migration of existing VRM profiles |
| Avatar search/prepare | Existing authorized search, bounded import/conversion progress and errors; review fixes before accepting assets |
| Avatar presentation | Avatar enable/disable inside GUI, portrait/full body, expression tests, quality, reduced motion and optional window always-on-top |
| Existing desktop controls | Preserve only for an explicitly selected legacy floating renderer; do not show desktop boundary/click-through/map controls in the in-window MetaHuman presentation |
| Connection and errors | Ready/reconnecting/refused states, session expiry, resynchronization, request correlation and per-function progress |
| Exit and cancellation | Separate stop voice, cancel supported jobs, close GUI and exit backend. Existing interrupt stops speech; full model/tool cancellation is new work, not an existing guarantee |

**UI approach. [Recommend]** Native UMG/Slate for the main application, subject to
a small text/accessibility prototype. Use an ordinary external browser fallback
for screens not migrated. Embedding a browser is an optional experiment, not an
automatic requirement: it adds browser compatibility, authentication, media/file
handling and packaging work. Avoid maintaining two sets of business rules.

**Accessibility/desktop gate. [Experiment]** Verify tab order, focus indication,
Enter versus Shift+Enter, IME/Unicode, text selection/copy, long text wrapping,
scroll retention during streaming, scalable fonts, high contrast, screen-reader
labels/status and 100/150/200/300 percent DPI. Test a resizable window at small
sizes. Provide textual equivalents to expressions; do not rely on the avatar to
convey an error or require it for navigation. No claim of complete UMG screen-reader
support is made until the actual widgets are tested.

**Migration strategy. [Recommend]** Extract shared schemas and backend dispatch
first, then migrate chat/status/voice control, images, voice studio, avatar library
and console in small increments. Feature negotiation exposes completed functions;
missing screens link to the web fallback. Test both clients against identical
backend contract fixtures, with browser/Unreal-specific UI tests for interaction.
Do not automatically retry requests with effects after an ambiguous disconnect;
use operation IDs/status reconciliation. Replay scopes must be rechecked.

## 10. Milestones

Effort is in working days and is rough. Owner time for installs and character
creation is separate.

| | 1. Packaged feasibility prototype | 2. Integration | 3. GUI migration | 4. Animation and release |
|---|---|---|---|---|
| Contains | One MetaHuman, a transcript, a text box, connected to the real backend; Freya's own speech moving the face, in a **packaged** build | Streaming, cancel, reconnect, renderer choice, lifecycle, GPU release | Functions moved one at a time; web fallback kept | Expressions, gestures, polish, offline check, install, rollback |
| Depends on | UE/toolchain installed and reviewed character present; solver-only packaged test first, then protocol/authentication profile and `speech.*` | Milestone 1 passing; the two review fixes | Milestone 2; UMG experiment | Milestone 3 |
| Accept when | Typed text gets a real reply; lips match audio within a judged tolerance; mic ignores her own voice; works with the Editor closed | Kill Unreal mid-sentence: abandon ambiguous utterance, safely transfer future audio, mic state right, bounded restart; image generation receives verified resource capacity | Each function works in both GUIs from one backend implementation | Disconnected run works; install and rollback rehearsed |
| Tests | Protocol tests with a scripted client; audio framing and bounds; playback-owner hand-over | Crash, disconnect, slow client, refused version | Per-function contract tests run against both clients | Offline run; version mismatch; rollback |
| Main risk | Solver does not run packaged | VRAM does not fit | UMG text and accessibility fall short | Animation quality |
| Effort | 6 to 12 | 8 to 15 | 20 to 40 | 15 to 30 |

**If the solver does not run in a packaged build**, the choices are: investigate missing runtime modules/configuration, choose another compatible
lip-sync source, or retain a simpler renderer. An Editor-only demonstration is
not acceptance evidence and is not the proposed distributable runtime. That is why it is tested first.

The estimates total **49–97 developer working days**, not a calendar commitment.
They supersede the earlier conversational 4–8-week estimate for the narrower
avatar-only integration: migrating the entire GUI substantially increases scope.
Dependencies, asset work, licensing/authentication decisions and failed
experiments may extend them. Re-estimate after milestone 1.

## 11. Installs and downloads

| Needed | Have it? | Who |
|---|---|---|
| Unreal Engine 5.8 | 5.8.3 at D:/UE_5.8, verified from Build.version | Installed |
| MetaHuman plugins and Creator content | Runtime plugins, Optional texture model and body textures verified present | Verify Creator in the prototype project |
| Visual Studio with C++ game workload | VS 2026 with MSVC present; workload not checked | Owner if missing |
| Epic account, EULA acceptance | Owner's | Owner only |
| The Fab plugin | Not needed under this recommendation | None |

## 12. Decisions that need the owner

| # | Decision | Recommendation |
|---|---|---|
| U1 | Accept the Unreal Engine EULA and confirm its MetaHuman and AI-training terms by reading them | Owner only; primary terms now linked in section 13 |
| U2 | When Unreal closes | **Owner chose: keep backend running with microphone muted** |
| U3 | Use the bridge as reference code, not as a dependency | Yes |
| U4 | Fix the two open review findings before milestone 1 | Yes |
| U5 | Install Unreal Engine 5.8 and make the first character | Needed before milestone 1 |
| U6 | Commercial distribution intent | **Owner chose: preserve commercial distribution options**; record appropriate rights before asset use |

## 13. Primary sources and licence interpretation

Sources checked on 2026-10-08; record exact accepted terms and component versions
before integration. The EULA was read, not accepted on the owner's behalf.

- [Fab listing](https://www.fab.com/listings/1efdd798-6cb0-4df0-b795-b19c995e79fe):
  advertised Win64/UE 5.8.1 scope and excluded demo characters/assets.
- [Plugin repository](https://github.com/isundahl/LLM_Plugin) and
  [MetaHuman companion](https://github.com/isundahl/LLM_Plugin/tree/main/Plugins/LocalMultimodalLLMMetaHuman):
  original code licensing, dependencies and cooked-build test instruction.
- [Unreal EULA](https://www.unrealengine.com/eula/unreal): sections 3/4/6 distinguish
  seat exceptions and distribution/royalties; section 6(e) sets AI-use restrictions.
  The default royalty rate and exemptions require product-specific review; do not
  describe all uses as free below a single threshold. Epic/marketplace content and
  third-party assets need their own records; keep Unreal Engine Code out of public
  source distribution except as permitted by the applicable terms.
- [MetaHuman licence overview](https://www.metahuman.com/license?lang=en-US): permits
  other engines/creative software; it does not waive asset-specific obligations.
- [Epic toolchain table](https://dev.epicgames.com/documentation/en-us/unreal-engine/setting-up-visual-studio-development-environment-for-cplusplus-projects-in-unreal-engine):
  UE 5.8, VS/compiler/SDK compatibility. Validate installed workloads.
- [MetaHuman assembly](https://dev.epicgames.com/documentation/metahuman/assembly?lang=en-US)
  and [hardware recommendations](https://dev.epicgames.com/documentation/metahuman/metahuman-hardware-requirements-in-unreal-engine?lang=en-US):
  authoring/optimization guidance, not measured Freya runtime requirements.
- [Command-line modes](https://dev.epicgames.com/documentation/unreal-engine/unreal-engine-command-line-arguments-reference?lang=en-US)
  and [Pixel Streaming setup](https://dev.epicgames.com/documentation/unreal-engine/getting-started-with-pixel-streaming-in-unreal-engine?lang=en-US):
  offscreen rendering differs from disabling rendering entirely.

## 14. Open items and handoff

- No code, policy, architecture approval or system installation was changed.
- No solver packaged-build experiment, visual acceptance or performance benchmark
  has been performed. Local hardware numbers above are Claude's reported inspection,
  not independently repeated benchmarks. Unreal was absent from the standard
  installation folder during Codex's check; custom locations were not exhaustively
  searched.
- Owner EULA acceptance is not independently recorded here. U2 and U6 were answered
  in this chat; Unreal and Core Data are installed, while the first character
  remains to be made. U3 is an implementation recommendation;
  U4 is a security prerequisite for accepting imported assets. Neither requires a
  new permission question merely to write this proposal. Required authenticated
  pairing/session work must be implemented or an explicit prototype deviation
  approved before claiming baseline compliance.
- Memory PR #16 is separate work. This document does not change or merge it; its
  current contents/status were not reviewed during this completion.
- Next: decide exit policy and distribution intent, record licence acceptance,
  verify/install the toolchain and prepare one character; then perform the smallest
  solver-only packaged experiment before expanding the gateway or GUI.


## 15. Owner decisions and environment update — 2026-10-08

- Closing the Unreal GUI leaves the Python backend running **with its microphone
  muted**. Reconnecting must not silently unmute it; the owner explicitly unmutes.
  This changes the current console-free exit policy only when implemented/tested.
- Preserve options for commercial distribution when selecting code and assets.
  This is an intended-use constraint, not a licence determination.
- The owner approved a small virtual environment: one room, a conversation camera,
  and a display for images, with readable chat/essential controls as screen overlays.
  Scene activity reflects real backend state. A solver-only test precedes room
  construction; environment polish does not delay the runtime feasibility gate.
- Unreal 5.8.3, compiler 14.51.36231 and Windows SDK 10.0.26100.0 are installed.
  Core Data texture-synthesis .ar and body .uasset files were found using the same
  checks as the installed Creator module. That does not prove character creation
  or packaged facial animation works.
- A separate FreyaUnrealProbe project is staged in this chat's outputs. It has no
  personal-data access, live microphone, gateway connection or automatic speech.
  It is experimental source, not an implemented replacement for Freya's GUI.

## 16. Feasibility result — 2026-10-08

The first gate of milestone 1, "does Epic's audio solver run in a packaged build",
has been tried in the separate `FreyaUnrealProbe` project (outside this
repository). **[Verified]** by running it:

- A MetaHuman character was rigged, textured, and assembled (UE Optimized) by
  script through `MetaHumanCharacterEditorSubsystem`, with the owner's sign-in
  to Epic's service for the rig and textures.
- A 10.5 s Kokoro clip (24 kHz mono, synthetic) fed to the solver produced 525
  result frames over 260 curves, in an uncooked game run **and in a packaged
  Win64 build with the Editor not running**. Screenshots from both show the
  mouth in different open shapes, and a blink.
- `MetaHumanPipelineCore` being typed `Editor` in its descriptor did not prevent
  a Game target from linking or the solver from running when packaged.
- The solver's model lives in Epic's `StreamingADA` plugin and is loaded by
  path; `/StreamingADA` had to be added to the directories always cooked.
- The assembled character's face did not follow Live Link from its blueprint
  settings alone. The probe sets the face's animation class to Epic's
  `ABP_MH_LiveLink` and writes the subject name into it. Section 5's Unreal
  module needs the same step.
- Packaging took about six minutes; the package is 2.3 GB.

**Still [Experiment]:** lip-to-voice timing by ear, cancellation in a real run,
performance and VRAM beside Freya's other workloads, and offline operation. No
backend connection exists yet; the probe plays a file.

This retires the "solver does not run packaged" risk in section 10. It approves
nothing and changes no other statement in this document.
