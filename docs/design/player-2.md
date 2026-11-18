# Design proposal: Player 2 (Freya in a game)

**Status: draft proposal, written 2026-11-18. Nothing here is built.** It records
a vision from the main developer, with constraints Thrain added the same day.
It approves no ADR, adopts no pending amendment, and assigns no requirement
ID. ADR-015 stays **Not approved**. Requirements carry the provisional labels
**NEW-EH to NEW-ES** (`docs/labels.md`). They get final IDs only at
reconciliation.

PHY-003 is a baseline reference to *Freya Foundation v1.1*. Worker rules
cited as NEW-DB to NEW-DM are the proposal in
`worker-nodes.md`, which is not adopted. Learned material informs and never
authorizes (NEW-BH, NEW-CL).

## 1. What Player 2 is

A scripted game character would be a second person. Player 2 is not that.
She is Freya, playing beside Thrain with the memory, personality,
relationships, and voice she already has. The same turn, the same store, the
same names she answers to.

Nothing in the tree plays a game today. The desktop avatar draws and points;
`spatial-avatar.md` keeps screen capture and input injection unbuilt, and a
test fails if those calls appear in that package. `gpu/resource_manager.py`
only unloads Ollama or asks ComfyUI to free memory. ADR-007 and ADR-016,
which would reserve the card, are not approved. `freya.federation` can record
an offer and Thrain's approval; it sends nothing, and Core does not import it.

So the first build is a contract and a mock, not a bot in a live game.

## 2. What stays, what this extends, what this does not replace

**Stays.** One principal (`local-owner`) until the signed-in gateway exists.
Deny-by-default policy, the broker, and the hash-chained audit log. Voice is
not authentication. `code.run` stays unregistered. Cloud sends stay behind
the three locks in `cloud-delegation.md`. Memories stay in the existing store
(`src/freya/memory/memory_manager.py`, conversations in the same database).
A child process is started with `child_environment` (`src/freya/child_environment.py`),
which drops credential-named variables. Graphics reads stay AMD, then Intel
Arc, then NVIDIA (`src/freya/gpu/status.py`). ADR-015 is unchanged: no new
database, no new migration, no change to key custody.

**Extends, once Thrain accepts this and a later change registers it.** A
game adapter behind the broker, with its own policy seed, on the same pattern
as the file tools (`alpha-file-read`) and the avatar (`alpha-avatar`). A
planner role in the model runtime, so the play model is named apart from the
conversation model and audited apart. Optional use of a worker's model, only
after the worker channel in `worker-nodes.md` exists.

**Does not replace.** The desktop avatar, world-view tools, federation, or
the conversation model. Phases 2 to 5 of `spatial-avatar.md` stay their own
decisions. This document does not fold screen reading or keyboard control
into the avatar package. PHY-003 still bars unrestricted desktop control.

## 3. Three layers

```
 Thrain
   voice / window          kill switch, allow-list, approvals
        |
        v
 Freya (the same assistant)
   memory, personality, voice, broker, policy, audit
        |
        |  short plan, on a slow clock
        v
 Hands: one action schema -------- Eyes: structured state
        |                               (screen and audio later, off)
        +-- Minecraft adapter (M1)      Mineflayer, private local world
        +-- a second game (M4)          not Elite, not EVE
        +-- Elite Dangerous (after M4)  read-only, then input only if approved
        +-- keyboard / controller       per operating system, late, off
```

**Hands.** Each game is an adapter behind one interface. A goal goes in as a
closed action schema. A structured observation comes out. The adapter is a
separate program. It does not import `freya`. Freya does not import it into
Core. The first adapter is Minecraft through Mineflayer (Node), in a private
local world. A later adapter may simulate a controller or a keyboard for a
single-player game that has no API. That adapter has one backend per
operating system. A missing backend reports unavailable.

The model does not write the program the adapter runs. Voyager did that
(section 7). Here the action list is fixed in the adapter Freya's developers
ship. A plan with an unknown name, an extra field, or a block of code is
refused. `code.run` is not how a character moves.

**Eyes.** The first perception is structured state: position, health,
inventory, threats, and chat, for a game that has them. Fields a game does
not have are omitted, not invented. Screen and microphone perception of the
game are a later optional add-on. They are the unbuilt permissions
`screen.capture` and `screen.analyze` in `spatial-avatar.md`, and they stay
off through M4 and through the Elite Dangerous read-only phase. Audio she
already hears through the microphone path is the owner's speech to her, not
a feed taken from the game.

**Brain.** Freya produces a short plan. Fast reflexes stay in the adapter:
follow, path, stop, release keys. A local 4B-class model sharing a 16 GB
card with a game cannot close a frame loop, and this design does not ask it
to. When a plan is late, refused, or missing, the reflex is `stop`. The
adapter does not keep walking on a stale plan.

The brain is pluggable (NEW-EK). The adapter asks a role, `play`, through
the model runtime. It does not name a vendor library.

| Choice | Who sets it | What it costs on a shared card |
| --- | --- | --- |
| `qwen3.5-4b-mtp` (default) | The proposal, until Thrain changes it | The planner the main developer named for this role. Not listed in `local-models.md`. No VRAM or latency figure in this repository. Smallest of the options he named, if a later measurement shows it fits beside the game. |
| A larger local model | Thrain, an installed model he names | Stronger plans, more of the card, slower replies. The session does not start on the card when `status.py` cannot show a fit. |
| The conversation model (`qwen3.8:27b`) | Not the default | A 17 GB file in `local-models.md`. `larger-models.md` says whether that file fits the RX 9070 XT is unmeasured, and the older working-set note was a different card. Unsuitable as the planner that shares the card with a game. |
| A model on a LAN worker | Thrain, per session, after the worker channel exists | The game keeps the gaming PC's card. Plans pay the trip. Reflexes stay local. Section 6. |

Ollama reloads a model when the context size changes. One play session uses
one configured size. The observation is capped so the prompt stays inside
that size. Image generation and a large speech engine are not started during
a session; both already unload Ollama models (`gpu/resource_manager.py`).

## 4. Requirements

### NEW-EH — Player 2 is Freya

Player 2 is a capability of the assistant Thrain already talks to. She uses
the same memory, the same personality, the same relationships, and the same
voice. There is no second principal, no second name in policy, and no second
memory file. A plan prompt may be short. It uses her existing persona. Spoken
lines to Thrain use the conversation voice, not a character voice invented
for the game.

### NEW-EI — Hands share one schema

One schema serves every adapter. An action is a name from a closed list plus
declared arguments, each a bounded non-empty value, on the same rule
`ModelToolHost` already uses for tool arguments. The observation is a fixed
set of fields, encoded under a byte cap of the same kind as a tool result
(tool results are clipped at 2,400 bytes; the cap here stays under that so a
clipped observation cannot hide fields). Unknown actions, extra fields, and
a string the adapter would have to eval are refusals, and the refusal is
audited without the plan's text.

M1's list is only what that milestone needs: `follow`, `stop`, `look_at`,
`collect`, `chat`, `drop`. M2 may add `mine`, `craft`, and `place`. A game
that cannot perform one of those omits it and refuses it. The schema does
not grow a free-text command.

### NEW-EJ — Eyes are structured state first

The observation carries position, health, inventory, threats, and chat when
the game has them, plus a status of `idle`, `acting`, `stuck`, `paused`, or
`refused`. Chat lines are untrusted content. Pixels, a framebuffer, and a
capture of the owner's screen are out of the observation until a later
proposal builds `screen.capture` and `screen.analyze` as their own
capabilities. This document does not build them.

Elite Dangerous phase A is the structured-file case of the same rule: local
Journal and Status JSON the game already writes (section 5). The raw files
are not the observation. The adapter maps them onto the schema.

### NEW-EK — The planner is pluggable; reflexes stay in the adapter

The `play` role is the only model call the adapter's plan step uses. The
default model for that role is `qwen3.5-4b-mtp`. Thrain may point the role
at another installed local model. An unknown name refuses the session. The
role does not silently substitute `qwen3.8:27b` or any other model.

A larger model is an owner choice, made before the session, with the fit
check in section 6. He trades VRAM and latency for plan quality. The adapter
does not make that trade.

The model is asked when the adapter is stuck, when a plan finishes, or a
few seconds have passed. It is not asked per tick. A late, refused, or
missing plan leaves the adapter on `stop`. Follow continues only while the
current plan still says follow and the planner is still the one that issued
it.

Graphics for a local planner are read AMD first, then Intel Arc, then
NVIDIA. A runtime that requires CUDA is not eligible on the RX 9070 XT.

### NEW-EL — She plays only where a bot is allowed

An adapter attaches only to a target on an allow-list Thrain wrote. A
missing or unreadable list means no. M1's only legal target is a private
Minecraft world served on the same machine, reached on loopback. A public
address is refused. Another machine is refused in M1.

EVE Online is out of scope. Bots are prohibited there, and Player 2 collects
no EVE training data: no logs, no screenshots, no corpus, no evaluation
tasks, no adapter. A request that names EVE is a refusal.

Elite Dangerous is not M1 and not the M4 proof. It is a stretch after M4
(section 5). Phase A is read-only. Phase B drives input, and it runs only
with the approval and the disposable account that section names. A
competitive game, a server whose rules forbid bots, and any path that tries
to evade anti-cheat are refusals. This is a product rule, not legal advice.
A ban is Thrain's to weigh before he approves a target.

### NEW-EM — He can stop her at once, and he can see that she has control

`play.stop` ends control without a second approval. Removing authority stays
easier than giving it. The spoken wake phrase for stop runs this too. A
spoken phrase does not start a session and does not approve a target.

While she can act, or while a simulated key can still be down, the window
status strip (`src/freya/window.py`) shows the fixed text `Freya in control`.
When the brain is gone and inputs are released, it shows the fixed text
`Freya paused`. The text is fixed so a model string cannot be written there.
The same flag is in the observation, so a test can see it without a display.

The adapter is a child process. `play.stop`, and Freya exiting, terminate
it. The speech helper (`src/freya/speech/helper.py`) is the pattern for
stopping a child. On an operating system where that pattern is missing, the
adapter does not start.

### NEW-EN — She does not play on her own unless the project was approved

Independent or offline activity runs only for a project Thrain approved: a
goal, an allow-listed target, and an expiry. The environment still has to
pass NEW-EL. A spoken request does not approve a project. When the project
expires, the kill switch runs, or the last window closes, the adapter
pauses. The indicator has to be on a surface he can see, so a project does
not continue with no window.

### NEW-EO — Permissions are owner-only, and there is no new secret

Each effect is a capability with its own policy seed, deny by default,
audited, and not registered by this proposal. None of them is a model tool
until a later change Thrain accepts. The plan step parses a fixed form, the
way `compose.parse_plan` refuses an unknown field, and code then calls the
broker. The model is not handed a tool that types keys.

| Capability | When it could exist | What it allows |
| --- | --- | --- |
| `play.observe` | M1 | Read one bounded observation from an allowed session |
| `play.act` | M1 | Send one schema action |
| `play.stop` | M0, owner and the stop phrase | End control and release input |
| `play.project` | With NEW-EN | Record an approved project. Not a model tool |

`play.act` for Elite phase B is a separate seed from Minecraft, so turning
on a local voxel bot does not turn on keyboard injection.

No new secret store. The adapter's environment is `child_environment`. A
game password is not written into settings, logs, the audit message, or the
observation. M1 assumes a private world that does not need one. A later game
that cannot be played without Freya holding a password waits for its own
decision. Elite phase B does not log in. Thrain logs in on the account he
chose. Freya never reads the game's credential files.

Approvals are owner-only, at the keyboard or by a click in the window.
Voice is not authentication.

### NEW-EP — Game memory follows the memory rules that already exist

A fact worth keeping ("the bridge is north of the camp") is an ordinary
memory through `MemoryManager.add_memory`. `/forget` removes it, its
embedding, and its history in the transaction that command already uses.
Session talk is the conversation store, not a second log. There is no
`data/` database for games. Restore and the deletion ledger therefore treat
a forgotten game fact the way they treat any other forgotten fact
(ADR-006). ADR-015 is not modified to do this.

Game text is personal data. It is not offered as a cloud-brief input.
Delegation already keeps memories out of briefs; game observations and
journal text join that list. Player 2 does not train a model on what it
saw. It does not collect EVE data for any purpose (NEW-EL).

Playstyle notes in M3 are memories he can read and delete. They do not add
an action, a policy rule, or a permission. They inform. They do not
authorize.

### NEW-EQ — The contract is cross-platform

Freya's alpha runs on Windows, Linux, and macOS. The schema, the mock, and
the tests do not use a Windows-only type. M0 runs in CI without Node, without
a game, and without a GPU.

Mineflayer is a Node program the owner installs, the way Blender is
installed for avatar conversion. It is not vendored and not downloaded by
Freya. The Python side speaks JSON lines on the child's pipes.

Keyboard and controller simulation, when a milestone allows it, is a backend
per operating system behind the same schema. Elite Dangerous itself may
exist on only some of those systems. Where it is not installed, that
adapter is unavailable. The journal folder is a path Thrain names. Freya
does not search the disk for it. The path is resolved before use, and a
link that leaves the folder is refused, on the same rule as `files.read`.

### NEW-ER — Embodiment is last, and an unvetted runtime is not chosen

M5 is the first milestone that may present a body for play. It does not
start early to unblock M1. The desktop overlay's rules stay in force: no
screen capture and no input injection in `src/freya/spatial_avatar/`.

Four names were suggested for later embodiment work: OpenJutsu, Dramaturg,
NVIDIA Kimodo, and ARDY. They are to investigate. They are unverified as
fits for Player 2. This proposal does not describe how to run them and does
not depend on them.

Checked only far enough, on 2026-11-18, to see a vendor lock:

- Public pages under the name OpenJutsu describe a video restyle tool and
  say generation needs an NVIDIA GPU. It is not established that those pages
  are the project the main developer meant. If they are, an NVIDIA GPU
  requirement conflicts with AMD first, then Intel Arc, then NVIDIA. An AMD
  or vendor-neutral path would be required before that tool could be chosen.
- NVIDIA Kimodo is NVIDIA's kinematic motion work. A published model card
  for it says the models are designed for NVIDIA GPU-accelerated systems.
  That conflicts with the vendor order. It needs an AMD or vendor-neutral
  alternative before it can be chosen.
- ARDY is NVIDIA's interactive motion work in the same line, described by
  NVIDIA as building on Kimodo. Treat it as NVIDIA technology until an AMD
  or vendor-neutral path is shown. This note does not claim a separate CUDA
  sentence was read on an ARDY-only page.
- No description of Dramaturg was confirmed. It is a name only. It is not
  assumed to belong to any GPU vendor.

### NEW-ES — A remote brain plans, and only plans

The gaming PC runs the game, the adapter, and the reflex code. The kill
switch, the indicator, the allow-list, and `play.act` stay on that PC. A
planning model may run on a worker GPU on the home network.

This depends on `worker-nodes.md`, which is not adopted. The registry can
record an enrollment. The worker channel, mutual TLS, and remote providers
are not built. Nothing is sent today. Remote brain is not available for M0
or M1, and those milestones do not wait for it.

When the channel exists, a remote planner still has to pass that proposal:
owner enrollment with the join code, a role on the enrollment, mutual TLS,
`may_assign` failing closed, an address on the home network or the tailnet,
and no public address. Voice cannot enroll the worker. Running a model on a
worker is inference. The worker does not run the game and does not send
input, so this use does not need the sandbox-host exception in that
document.

On 2026-11-10 Thrain decided that background work may go to a worker and
that Freya's replies stay on the main PC (W1 in `worker-nodes.md`). A play
plan is neither the background role nor a reply. Remote brain stays off
until he allows play plans on a named worker for a session. Spoken replies
still come from the gaming PC, or from no model if the conversation model
was unloaded to free the card.

The assignment carries the bounded observation and the closed schema. It
does not carry the raw journal, memories, the conversation, or credentials.
The worker stores no prompt or reply after answering (NEW-DJ). The worker
cannot call `play.act`.

Thrain's example of the worker card is an RX 7800 XT. `worker-nodes.md`
already records an Ubuntu server with a Radeon RX 7800 XT (16 GB), beside
this PC's RX 9070 XT (16 GB). Whether a given planner fits that 16 GB card
is a measurement on that machine, not a claim here. A 17 GB weight file is
not assumed to fit. Vendor order on the worker is the same: AMD, then Intel
Arc, then NVIDIA.

The benefit is that the game and the planner do not share the gaming PC's
card. The cost is the round trip plus the model's own time. That is
acceptable for a plan and fatal for a reflex, which is why reflexes never
move to the worker. The split-inference figure already in `worker-nodes.md`
(about 15–18 tokens a second for a 27B Q4_K_M across the 9070 XT and the
7800 XT) is a warning about large-model time, not a measurement of
`qwen3.5-4b-mtp`.

If the worker stops answering, the adapter does one of the two things the
session was given. He has asked for both. This proposal recommends that a
new session pause unless he has armed fallback.

1. **Pause.** `stop`, release any simulated input, show `Freya paused`,
   send no further actions. A stale plan does not keep moving her.
2. **Local fallback**, only if he armed it and `status.py` shows that
   `qwen3.5-4b-mtp` fits beside the game. The session switches to that
   model and tells him which model is planning. If the fit is unknown or
   short, fallback refuses and the session pauses instead.

## 5. Games

| Game | Place | Rule |
| --- | --- | --- |
| Minecraft Java, via Mineflayer | M1 | Private local world on this machine, loopback only. Follow, talk, fetch. |
| A second game, unnamed | M4 | Proves the schema. Not Elite. Not EVE. Chosen later. |
| Elite Dangerous, phase A | After M4 | Read-only co-pilot. Local Journal and Status JSON in a folder Thrain names. No input. |
| Elite Dangerous, phase B | After phase A, and only if he approves it | Input simulation. ToS and ban risk. Solo still uses Frontier's online servers and the account. Disposable account he chose. Owner approval each time it is armed. |
| EVE Online | Nowhere | Out of scope. Bots prohibited. No EVE training data. |

Phase A is the safe Elite test. The game writes those JSON files locally.
The adapter reads them and talks to Thrain through the existing voice path.
It does not press keys. It does not connect to Frontier. Made-up fixture
files are what tests use. A real journal is personal data and stays out of
the repository and out of prompts sent to a vendor.

Phase B is the dangerous Elite test. Solo mode does not make it a local
game: Thrain's constraint is that Solo still uses Frontier's online servers
and the account, so simulated input can put that account under the game's
terms and its ban rules. Phase B refuses unless the session record says he
approved input for Elite and names the account as disposable. Freya does
not check with Frontier. She enforces his flag. The indicator stays on, and
`play.stop` releases keys. Phase B is not required for M5.

The M4 game is a second adapter on the same schema, so the interface is not
Minecraft-shaped in a way that blocks every other game. A game with a local
API is a better proof than a keyboard-only title. Elite is a poor proof,
because phase A is files and phase B is input injection. It stays after the
proof.

## 6. The card

This workstation is a Ryzen 7 9800X3D and an AMD Radeon RX 9070 XT (16 GB).
That 16 GB figure is this PC, not the contract. Another machine uses the
free memory `status.py` reads there. A missing tool leaves the field
unknown, and unknown refuses a local load.

ADR-016 is not approved, and the code has no GPU reservation. Until a
reservation exists, a play session uses a conservative stand-in: read free
memory, refuse to load a local planner when the free figure is unknown or
below the estimate for the chosen model plus a reserve Thrain sets for the
game. The estimate is measured (`python -m freya.gpu fit` or the equivalent
on the worker), not taken from a table in this document. There is no
measured size here for `qwen3.5-4b-mtp`. The 4.5 GB disk size of `tev1:4b`
in `local-models.md` is a different model and is not a VRAM budget.

Sharing the gaming PC's card, in order of pressure:

1. Default small planner, if it fits beside the game. Least pressure of the
   options Thrain named. Still contention. Still too slow for per-frame
   control.
2. A larger local planner. More pressure, slower plans, better plans when
   it fits. Refused when it does not.
3. Remote planner (NEW-ES). The gaming PC's card is left to the game. This
   is the deployment that removes contention. It adds latency, so only the
   plan moves.

The conversation model is unloaded before a local planner loads, the way an
image request unloads Ollama. It is not unloaded merely because a remote
planner is in use, unless Thrain also wants the card free for the game,
which is the point of the remote deployment and the recommended default
when the game is full screen.

## 7. Prior art

Voyager (Wang and others, "Voyager: An Open-Ended Embodied Agent with Large
Language Models", 2023, arXiv:2305.16291) is a research agent in Minecraft.
It has three parts: an automatic curriculum, a library of executable
programs, and a prompt loop that revises a program from environment
feedback, execution errors, and the model's own check. It calls GPT-4. Its
action space is code, sent through Mineflayer's JavaScript API, rather than
low-level controls. It has no owner in the loop.

The useful result is that structured state and a high-level action
interface are enough to play a first Minecraft agent. Pixels are not
required for M1. Player 2 does not take the rest. The model here does not
emit a program. The curriculum does not explore without Thrain. The brain
is the local role in NEW-EK, or a worker model he enrolled, not a cloud
call. A cloud model would have to go through the dispatcher, and game
observations are not brief inputs (NEW-EP).

## 8. Milestones

Each one can fail without enabling the next. None of them is started by
this document.

| Milestone | Exit | Tests that must already pass |
| --- | --- | --- |
| M0. Schema and a mock game | A Python mock speaks the schema. No Node, no network, no capability registered, Core does not import the package. | P2-1, P2-2, P2-8, P2-13, P2-14 |
| M1. Minecraft companion | Mineflayer, owner-installed, joins a private local world on loopback, follows, speaks with Thrain through the existing voice path, and fetches an item he asked for. | M0, plus P2-3, P2-4, P2-5, P2-7, P2-9 |
| M2. Tasks and memory | A schema task (mine, craft, or place) finishes in the mock and in a local world, and a build fact is recalled after a restart and removed by `/forget`. | P2-6 |
| M3. Playstyle | Suggestions cite memories of sessions he agreed to keep. They add no action and no rule. Nothing is trained. | P2-6, and a test that a suggestion cannot widen the schema |
| M4. Second game | A second adapter, not Elite and not EVE, passes the same schema tests as the mock. | P2-1, P2-3 |
| Stretch. Elite phase A | After M4. Read-only Journal and Status fixtures. A briefing. Zero input events. | P2-11 |
| Stretch. Elite phase B | After phase A. Refuses without the disposable-account approval. With it, the indicator is on and stop releases keys. | P2-12 |
| M5. Embodiment | Last. A body presentation whose runtime is AMD or vendor-neutral, or has such a path. NVIDIA-only runtimes do not pass. Avatar capture and input stay absent. | P2-15 |

## 9. Acceptance tests (provisional)

All of these are code. None of them is claimed to pass now.

| ID | Pass |
| --- | --- |
| P2-1 | The mock refuses an unknown action and an oversized observation. `follow` and `collect` complete against the mock. |
| P2-2 | A plan with an extra field or a program string is refused. No process is spawned from the plan. |
| P2-3 | A target off the allow-list, a non-loopback Minecraft address, and any EVE target are refused. |
| P2-4 | `play.stop` mid-action sends no further actions, terminates the child, and clears `Freya in control`. |
| P2-5 | A spoken start and a spoken approval are refused. A spoken stop runs `play.stop`. |
| P2-6 | A stored build fact is returned by the existing recall path and removed by the existing forget path, embedding included. There is no second game database. |
| P2-7 | Unknown free graphics memory refuses a local planner. The status order used is AMD, then Intel Arc, then NVIDIA. |
| P2-8 | With the mock brain delayed for several seconds, the adapter issues `stop` rather than a per-tick model call, and it does not continue a move from a plan whose issuer has gone. |
| P2-9 | The default `play` model name is `qwen3.5-4b-mtp`. An owner-chosen installed name is the one used. An unknown name refuses. Unset does not select `qwen3.8:27b`. |
| P2-10 | A scripted worker receives the bounded observation only. A dead worker pauses, unless local fallback was armed and the small model fits, in which case the planner becomes `qwen3.5-4b-mtp` and the observation says so. Reflex actions are not in the assignment. |
| P2-11 | Fixture Journal and Status JSON yield a briefing and an empty input log. |
| P2-12 | Elite input simulation refuses when the disposable-account flag is off. When it is on, `Freya in control` is set, and stop releases every key the fixture held. |
| P2-13 | The M0 tests run without Windows-only calls. A missing input backend returns unavailable and does not pretend to send keys. |
| P2-14 | Core does not import the game package. `screen.capture`, `screen.analyze`, `input.keyboard`, and `input.mouse` stay unregistered in the avatar package. |
| P2-15 | The embodiment names in NEW-ER are not imported. A NVIDIA-only runtime does not satisfy the M5 exit. |

## 10. What it costs, and what it does not do

M0 is a Python package and tests. M1 adds a Node program the owner
installs, and GPU time only if the planner is local. Remote planning waits
on the worker channel and on an explicit allow for play plans. There is no
new secret, no new database, and no edit to ADR-015.

It does not play EVE. It does not join a public Minecraft server. It does
not turn Elite phase B on with phase A. It does not capture the screen. It
does not put input code in the avatar package. It does not run model-written
JavaScript. It does not send a journal or an observation to a cloud model.
It does not approve ADR-007, ADR-015, or ADR-016. It does not adopt
`worker-nodes.md`. It does not schedule the work.

## 11. Open questions for Thrain

| # | Question | Recommended answer |
| --- | --- | --- |
| Q1 | If Minecraft's own "open to LAN" cannot bind loopback, how does M1 attach? | He starts a private server bound to loopback. Freya does not change the game's network. If that cannot be done, M1 waits. |
| Q2 | Which game is the M4 proof? | Decide after M2 has used the schema in Minecraft without a breaking change. A second game with a local API, not Elite and not EVE. |
| Q3 | Default when the worker is unreachable? | Pause. Local `qwen3.5-4b-mtp` only when he armed fallback and the card reports a fit. Both behaviours are in NEW-ES. |
| Q4 | May play plans leave the gaming PC, given W1? | Only after he allows them on a named enrolled worker, per session. Replies to him stay on the gaming PC. The raw journal never leaves. |
| Q5 | May a spoken "follow" during an approved session send `follow`? | Yes, inside a session he already approved. Start, allow-list, project, Elite phase B, and a larger model still need the keyboard or a click. Stop always works. |
| Q6 | Does M1 wait for signed-in sessions (ADR-017)? | No. M0 and M1 stay on this machine and `local-owner`. A second device does not get `play.act`. |
