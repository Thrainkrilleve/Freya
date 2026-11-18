# Freya roadmap

**Draft for Thrain to edit. Not a decision.** Written 2026-11-16 from the
repo. It does not approve an ADR, pass an acceptance test, or change any
status line. ADR-014 is the only accepted record. ADR-015 stays **Not
approved**.

**Size:** S is one small pull request. M is a few. L is a design plus
several pull requests.

**Holds for every item:** local-first; no plaintext fallback and no weaker
key stretch; a missing unlock refuses; `code.run` stays off until a tier
is probed and still refused on a weaker tier; voice is not authentication.

## 1. Where Freya is today

Working on one Windows 11 workstation, with Linux and macOS CI for a
subset. One owner, one principal (`local-owner`).

**Shipped and in use**

- Chat and embeddings through Ollama on `127.0.0.1`, behind the broker,
  deny-by-default policy, and a hash-chained audit log.
  [ADR-002](adr/002-capability-first-integration.md),
  [ADR-008](adr/008-model-runtime-independence.md),
  [ADR-013](adr/013-invocation-authorization-revocation.md).
- Window on loopback only: one-time key, cookie, Host and Origin checks.
  No signed-in user yet.
  [ADR-017](adr/017-gateway-authentication-compatibility.md).
- Memories, word-for-word conversations, and FTS5 with secure-delete.
  Startup opens memory in protected mode and replays the deletion ledger
  (`src/freya/main.py`). [ADR-006](adr/006-memory-vs-knowledge-vs-session.md).
- Live databases are SQLCipher. Backups are AES-256-GCM. A passphrase
  wrap can be added beside the OS wrap (`python -m freya.storage.keys
  add-passphrase`). Passphrase, recovery-key, and data-key rotation and
  revocation are built. The scratch restore drill runs in CI. This is
  not an acceptance result.
  [ADR-015](adr/015-persistence-recovery.md).
- Voice path goes through the broker. Image generation still calls
  ComfyUI directly. [ADR-002](adr/002-capability-first-integration.md).
- Avatar phase 1 (placeholder and a face tile). No screen capture and no
  input control. [spatial-avatar.md](design/spatial-avatar.md).
- Cloud send exists and stays locked until the owner opens three locks.
  [cloud-delegation.md](design/cloud-delegation.md).
- Read-only security self-check. [security-self-assessment.md](design/security-self-assessment.md).
- Promotion gate for a hint or a model change. It measures and switches
  nothing on. [skill-learning.md](design/skill-learning.md).

**Built, and off**

- `code.run` is not registered. ADR-014 is accepted; accepting it does
  not enable the capability.
  [ADR-014](adr/014-federated-isolation.md),
  [code-sandbox.md](design/code-sandbox.md).
- `decide` is not registered. [local-decider.md](design/local-decider.md).
- Task journal runs only when `FREYA_TASKS=1`. No policy seed.
  [ADR-012](adr/012-durable-workflow-abstraction.md).
- World view only when `FREYA_WORLD_VIEW=1`.
- Media tools stay denied until `python -m freya.media.activate`.
- Federation can record an offer and the owner's approval. It enrolls
  nothing and sends nothing. Core does not import it.
  [worker-nodes.md](design/worker-nodes.md).

**What pull requests #1 and #4–#21 shipped**

- **#1** brought the upstream branches (through 22 October) into this
  fork. Left out: `work/security-local-coding-alpha`.
- **#4** label register and security CI on `main`. **#9** stale label
  notes and usage totals.
- **#5–#6** ADR-014 proposed, then accepted. `code.run` stays off.
- **#7–#8** locked dependency upgrades (including Pillow).
- **#10** voice wake, turns, speech, and the Voice tab through the broker.
- **#11, #13** every registered capability declares itself; voice and
  task capabilities included.
- **#12** local task journal, off by default, audited transitions.
- **#14–#19** ADR-015 proposal, scratch restore drill, backup encryption,
  live SQLCipher, then key rotation and revocation. Status unchanged:
  not approved.
- **#16** workflow tokens are `contents: read`; vendored browser files
  are out of the advanced CodeQL config.
- **#20** Unreal Build Tool scripts (`*.Build.cs`, `*.Target.cs`) ignored
  by the advanced CodeQL config. GitHub's default setup still scans them
  until C# is unchecked there.
- **#21** Discord coordinator CI installs `cryptography` and `argon2-cffi`.

**CI today**

- Windows runs the full pytest suite (`tests.yml`).
- Ubuntu, Windows, and macOS run a foundation subset
  (`core-foundation-ci.yml`), including encryption, custody, and rotation.
- Python CodeQL, dependency audit, and Discord coordinator tests run.
- No first-party `TODO` or `FIXME` in `src/freya`. The markers in
  `gateway/static/vendor/` belong to three.js.

**CI gaps**

- The full suite is Windows-only. Ubuntu and macOS do not run it.
- Default CodeQL setup still extracts the Unreal C# scripts with
  `build-mode: none`. This fork is user-owned, so it cannot set the
  config-file property. [codeql-config.yml](../.github/codeql/codeql-config.yml).
- Unreal C++ is not built in CI.

## 2. Now

Next few weeks. Each one can start without a new architecture.

### 1. Correct the runbook

Done. [README.md](../README.md) matches startup: `ensure_memory_protection()`
runs before the assistant is built, and the ledger stays outside `data/`.

### 2. Skill files, still off

Done as a loader. [skill-learning.md](design/skill-learning.md). Three empty
templates wait for Thrain. Lookup runs only when `FREYA_LEARNING=1` and
matches an approved skill's example exactly. A template is not matched
and is not promoted.

### 3. Image and memory through the broker

Done. An image request and a memory read for a reply or for screening go
through the existing broker capabilities. The handler is the only code
that talks to the image server. A missing broker or a refusal reads
nothing and generates nothing. Owner memory commands stay direct.

- **Goal:** image generation and memory recall use the same broker path voice already uses.
- **Why:** a path around the broker is not audited and not deny-by-default.
- **Depends:** nothing new. Do not add a write tool in the same change.
- **Size:** M
- **Where:** [ADR-002](adr/002-capability-first-integration.md),
  `src/freya/image/request.py`, `src/freya/core/conversation_memory.py`.

### 4. Sandbox probes, capability stays off

Done as a survey. From the repository root, with Freya's virtual environment:

```
python -m freya.sandbox probe
```

Needs Python 3.12 and this repository's environment. WSL, Docker, and an
administrator are not required. The command does not install them and does
not change a system setting. It writes `docs/results/sandbox-probes.md`
(uncommitted; the empty shape is
[sandbox-probes.template.md](results/sandbox-probes.template.md)). The
verified tier in that file stays T0. `code.run` stays unregistered.

- **Goal:** Thrain runs the sandbox probes on this PC and the result is recorded. `code.run` stays unregistered either way.
- **Why:** ADR-014 is accepted and unvalidated. Windows 11 Home has no Hyper-V. The WSL2 profile holds mistakes, not a determined attack.
- **Depends:** the owner runs the probes. A failed probe does not enable the capability.
- **Size:** M
- **Where:** [ADR-014](adr/014-federated-isolation.md),
  [code-sandbox.md](design/code-sandbox.md), `python -m freya.sandbox`.

### 5. Close the CI holes that mislead

- **Goal:** uncheck C# in GitHub's default CodeQL setup, and decide whether Ubuntu and macOS should run the full suite.
- **Why:** the low-quality C# warning is the engine's build scripts, not a .NET app. A green Windows run is not a green Linux run.
- **Depends:** a GitHub setting. A user-owned repository cannot point default setup at the config file.
- **Size:** S
- **Where:** [codeql-config.yml](../.github/codeql/codeql-config.yml),
  [tests.yml](../.github/workflows/tests.yml),
  [core-foundation-ci.yml](../.github/workflows/core-foundation-ci.yml).

## 3. Next

After the skill file exists, or sooner if Thrain reorders section 5.

### Signed-in gateway

- **Goal:** replace the fixed `local-owner` principal with a paired session. Reachability is not authorization.
- **Why:** the recorded order names this the next architecture piece after the learning step. No tool with new reach before it.
- **Depends:** Thrain's order in section 5. Loopback stays the alpha profile.
- **Size:** L
- **Where:** [ADR-017](adr/017-gateway-authentication-compatibility.md),
  [openrig-lessons.md](design/openrig-lessons.md).

### Checks, episode log, weekly approval

- **Goal:** deterministic checks before any model verdict; an episode log; the owner's approval as the only promotion.
- **Why:** a skill that cannot be checked or rolled back is not a skill.
- **Depends:** the skill file in section 2. Stays off until Thrain turns it on.
- **Size:** L
- **Where:** [skill-learning.md](design/skill-learning.md).

### Off-host audit checkpoint

- **Goal:** the chain head is copied somewhere this machine cannot rewrite.
- **Why:** a local checkpoint does not show tampering by someone who can rewrite the disk.
- **Depends:** Thrain chooses the place. It does not protect against a host administrator.
- **Size:** M
- **Where:** [ADR-015](adr/015-persistence-recovery.md), `FREYA_AUDIT_CHECKPOINT_PATH`.

### GPU admission

- **Goal:** a reservation for the card, not only evict-on-demand.
- **Why:** image, voice, and a larger model fight over one graphics card. The owner's
  card is an AMD Radeon RX 9070 XT (16 GB); live reads ask AMD first, then Intel Arc,
  then NVIDIA.
- **Depends:** [ADR-016](adr/016-resource-admission-ownership.md). Cloud usage limits already exist.
- **Size:** M
- **Where:** [ADR-007](adr/007-resource-intelligence-vram-management.md),
  `src/freya/gpu/`.

## 4. Later / ideas

Not scheduled. Each needs its own decision. None of these ship inside another task.

- Avatar phases 2–5: screen awareness, whole-body 3D, lip sync, other devices. No screen reading and no input control until a separate permission exists. [spatial-avatar.md](design/spatial-avatar.md). **L**
- ElevenLabs, and speech on another device. The second one waits on the signed-in gateway. **M**
- A knowledge store. Memory and session exist; knowledge does not. [ADR-006](adr/006-memory-vs-knowledge-vs-session.md). **L**
- Worker channel. Offers never enroll. Nothing is sent today. [worker-nodes.md](design/worker-nodes.md). **L**
- Durable workflows (Temporal or otherwise). A turn is still lost if Freya stops mid-turn. The task journal is the bounded local stand-in. [ADR-012](adr/012-durable-workflow-abstraction.md). **L**
- Hardware VM isolation (T1). Windows 11 Home cannot do Hyper-V. T2 is WSL2 and is the weaker profile. [ADR-014](adr/014-federated-isolation.md). **L**
- More cloud providers, file or code inputs to a brief, "allow permanently". [cloud-delegation.md](design/cloud-delegation.md). **L**
- Freya-to-Freya cooperation. Proposal only. [freya-to-freya-cooperation.md](design/freya-to-freya-cooperation.md). **L**

## 5. Open questions for Thrain

1. Approve ADR-015, or leave it recorded and not approved now that encryption and rotation are built?
2. Are the next build weeks the skill file, or the signed-in gateway?
3. Which three skills do you want written by hand?
4. Does this alpha need durable workflows at all?
5. What is the knowledge store, if anything, before a second node?
6. Uncheck C# in the default CodeQL setup?
7. Should Ubuntu and macOS run the full suite, or is Windows enough for the alpha?
8. After the probes: is WSL2 on this Home PC an acceptable tier for any future `code.run`, or does untrusted execution stay off until a VM exists?
