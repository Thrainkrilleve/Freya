# Freya Unified Intelligence — Independent Review

Claude review of the owner's "Unified Intelligence Architecture" proposal, 2026-10-16,
against the repository at commit `44da490` and Foundation v1.1. **Status: review
findings and recommendations only. Nothing here is adopted, no ADR is approved, and no
acceptance test has been run.** Identifiers such as UIA-x and FREYA-IDENTITY-001 are
provisional, per the ID rules in `CLAUDE.md`. The editable original, with its two
diagrams, is the owner's Claude doc of the same name.

## 1. Verdict

Sound direction, with five changes. One persistent Freya owning context, memory,
permissions and task state, with models as interchangeable workers, is the right
architecture. It is mostly what Foundation v1.1 already says (ARCH-012, ORCH-001,
ARCH-015, MEM-001), so the proposal should be filed as an amendment to v1.1, not a new
six-layer model alongside it.

1. **The orchestrator is deterministic code, not a model.** A task state machine plus a
   journal; models are called as functions inside it (classify, plan, draft, critique,
   verify).
2. **Identity means continuity, not identical personality.** Freya can guarantee the
   same memory, commitments, permissions, task state and presentation across models;
   she cannot guarantee the same reasoning style, knowledge or judgement.
3. **One front-of-house voice.** A single designated conversational model renders every
   user-facing reply; other models return structured results to the orchestrator.
4. **Reconcile with the owner's decision on delegated replies.** Attributed
   contributions are allowed, Freya stays owner of the conversation and the task, and
   spoken turns carry only Freya's own interpretation.
5. **Checkable verification instead of multi-model voting.** Tests, tool re-execution,
   citations and schema checks beat reconciling opinions on one RTX 4090.

The biggest risk is scope, not design: the governance core exists, but there is no
router, task journal, approval service, second user or sandbox, and nine of the ten
proposed milestones depend on those.

## 2. Evidence basis

| Source | What was done |
| --- | --- |
| Repository @ `44da490` | Read `core/`, `control_plane.py`, `gateway/`, `memory/`, `tools/`, `gpu/`, `providers/`, `main.py` |
| Test suite | Linux, Python 3.12: 758 passed, 10 failed, 18 errors, 5 skipped; the failures inspected were environmental (no PortAudio, Windows path separators, no Comfy Desktop, zero-size container disk) |
| `docs/specs/Freya_Foundation_v1.1_baseline.docx` | Sections 1–20 and the ADR list |
| `docs/specs/Freya_Foundation_v1.1_Claude_amendments_redline.docx` | NEW-A to NEW-L (pending) |
| `docs/design/human-approved-development-and-offline-recovery.md` | NEW-M to NEW-AB, D1–D18 (design only) |

Not verified: behaviour on the Windows workstation, Hyper-V availability, VRAM figures,
current licences of third-party projects.

## 3. Gap analysis (verified against `44da490`)

| Capability | v1.1 | Repo status | Evidence | Recommendation |
| --- | --- | --- | --- | --- |
| Capability Broker | CAP-001, OBS-006 | Implemented, live path | `core/capabilities/broker.py:66-257` | Retain; the spine of the design |
| Deny-by-default policy | SEC-015 | Partial | `core/policy/engine.py:56-91`; `resource`/`environment` ignored; `obligations`/`expires_at` unset | Add obligations (require-approval) and resource scoping |
| Hash-chained audit | SEC-008, OBS-006 | Implemented | `core/audit/log.py` | Retain; off-host sink later (NEW-F) |
| Persona / identity | IDENTITY-001 (new) | Minimal | `config.py:30-36`; background prompts in `core/llm.py` run without it | Persona profile owned by the context builder |
| Conversation runtime | Session Service | Single user | `main.py` → `respond_stream` → `ModelRuntime` → broker; `TurnGate` | Extract a turn engine from `main.py`/`assistant.py` |
| Interrupt | CAP-009 | Speech only | `voice/tts.py:259-265` | Cooperative cancellation tokens |
| Context assembly | §8 Context Authority | Partial | 40/16 message trimming, no token budget | Token budgets per slot, trust labels |
| Model routing | ESC-001..006, RES-008 | Stub | `config.py:4`; `providers_for_capability` only in tests | Build |
| Cloud providers | ESC-003..005 | Absent | — | Behind the broker with a disclosure class (NEW-B) |
| Long-term memory | MEM-001..009 | Partial | `memory/memory_manager.py`; no owner, no provenance, same-key overwrite | Owner, source, trust, supersession (MEM-007) |
| Multi-user | USR-001..007 | Absent | `broker.py:30-34` | Build before any shared use |
| Gateway | GUI-008..014 | First step | sessions never expire | Expiry, pairing, approval-capable client |
| Tool execution | SEC-013, ARCH-017 | Read-only, in-process | 7 system tools, `image.generate`, `conversation.recall` | Risk classes before any effectful tool |
| Sandbox | SEC-013, NEW-C | Absent | — | Before any coding agent |
| Approvals | NEW-A, CTX-002 | Absent | designed in D1–D18 | Prerequisite for coding workflow |
| Task journal | ORCH-002, ORCH-006..008 | Absent | in-memory queue `gateway/server.py:166` | Bounded sequential journal |
| Planner / verifier | §11 | Absent | — | Orchestrator steps, not a framework |
| GPU resources | RES-001, RES-008..011 | Partial | unloads all Ollama models before images | Admission with a VRAM ledger |
| Broker bypasses | ARCH-010 | Present | ComfyUI, Whisper, Kokoro, memory store, GPU unload, warm-up | Close before multi-user |

Code debt that blocks the proposal: `main.py` (2,153 lines, a ~1,170-line `main()` of
nested closures), `assistant.py` (1,748 lines mixing the turn loop with memory
administration), `respond()` duplicating `respond_stream()` with no caller, and the
duplicated command paths (`main.py` loop and `window_commands.py`).

## 4. Recommended architecture

Top to bottom, each layer calling only the one below it:

1. **Clients** (window, voice, CLI) → **Client Gateway** (session, client scope; voice
   can ask, never approve).
2. **Freya orchestrator** — deterministic, no model inside: context builder (persona,
   memory, trust labels), router (model by evals and VRAM), task state machine (plan,
   steps, cancel, resume), verifier and approvals. Beside it: state stores (task
   journal, memory with provenance, conversations, audit chain) and the Resource Manager
   (VRAM reservations).
3. **Capability Broker** — policy, audit record, dispatch, outcome (exists today).
4. **Providers** — front-of-house model (resident), worker models (on demand), cloud
   models (with consent), tools (system, ComfyUI), agent runtime (Hyper-V guest).

Interfaces to define (names provisional): `ModelCatalogEntry`, `RouteRequest` /
`RouteDecision`, `ContextBundle`, `TaskRecord` / `StepRecord`, `VerificationResult`.

## 5. Key answers

- **Coordination:** fast path for chat (today's `respond_stream`), journalled task path
  for multi-step work; contradictions resolved by evidence, never by model confidence.
- **Context order:** persona → user profile → task state → memories (with provenance)
  → documents and tool results (untrusted) → recent conversation → message, with a
  token budget per slot.
- **Interrupts:** cancellation token checked between chunks, tool rounds and steps; a
  user message during a task is an orchestrator event, not queued behind the task.
- **RTX 4090:** one resident front-of-house model, specialists on demand, reservations
  before load, routing on a local benchmark catalog.
- **Verification:** mechanical first (tests, sources, journal comparison), model review
  only for what mechanics cannot check.

## 6. Technology

Build the orchestrator, router, context builder and approvals; integrate what executes
or stores. Keep Ollama, the custom policy engine and SQLite. Direct provider SDKs for
cloud models (LiteLLM's PyPI package was backdoored in March 2026, versions
1.82.7–1.82.8). Defer LangGraph, Temporal and coding agents (OpenHands SDK or Claude
Agent SDK, later, inside the guest behind broker proxies). Every entry needs LIC-006
review and hash-locked installs (NEW-J).

## 7. Roadmap

| Phase | Work | Gate |
| --- | --- | --- |
| 0 | Split `main.py` and `assistant.py` (turn engine, context builder, commands); delete `respond()` | Suite green on Windows, behaviour unchanged |
| 1 | Persona, context budgets, model catalog and router; two local models; front-of-house rule | UIA-1 to UIA-4, UIA-13 |
| 2 | Task journal, cancellation, approvals — the proof of concept | UIA-6 to UIA-11 |
| 3 | Memory provenance, second user, gateway sessions | UIA-12, TEST-005, GUI-009 |
| 4 | Research, retrieval and cloud escalation | NEW-B and CTX-001/002 tests |
| 5 | Coding workflow inside the guest (NEW-C) | NEW-L agent and guest tests |
| 6 | Delegation, computer use, long-running tasks | TEST-002, TEST-007 under load |
| parallel | Resource admission (before phase 4) | UIA-5, TEST-008 |
| parallel | Hyper-V isolation (before phase 5) | TEST-006 containment |

## 8. Proof of concept (phase 2)

"Freya, check my drives and GPU and write me a health report": two local models, one
journalled task, one approved local write (`report.write`, only inside
`data/reports/`), one mechanical verifier that re-runs the read tools and checks every
number, and a forced kill mid-task that must resume without repeating completed steps.
Proposed modules: `core/persona.py`, `core/context/builder.py`,
`core/routing/{catalog,router}.py`, `core/tasks/{journal,orchestrator}.py`,
`core/approvals.py`. Out of scope: cloud, sandbox, second user, coding agent.

## 9. Acceptance tests (provisional UIA-x)

| ID | Property | Pass criterion |
| --- | --- | --- |
| UIA-1 | Identity across a model swap | All 5 earlier facts recalled; persona score within 10% |
| UIA-2 | No direct model exposure | All user-facing text from the front-of-house step |
| UIA-3 | Dynamic selection | Best eligible model ≥ 95% of 50 requests; reasons logged |
| UIA-4 | Degradation | Small model stopped mid-run: fallback, no failed turns |
| UIA-5 | VRAM admission | Image + chat together: no double reservation, no OOM |
| UIA-6 | Persistent task state | Killed after each transition: resumes, nothing duplicated |
| UIA-7 | Interrupt | Generation stops within 1 s; no new step starts |
| UIA-8 | Approval binding | Mutated arguments after approval are denied |
| UIA-9 | No self-granted authority | Injected "user approved" text creates nothing |
| UIA-10 | Verification | Wrong figure in the report fails the step |
| UIA-11 | Hallucinated tool result | Claimed tool run absent from the journal is rejected |
| UIA-12 | Memory provenance | Document-sourced claim never overwrites a stated fact |
| UIA-13 | Latency | p50 first token < 1.5 s, p95 < 4 s, model warm |
| UIA-14 | Audit completeness | Decision, dispatch and outcome rows for every invocation; chain verifies |

UIA-14 can be written now against existing code.

## 10. Decisions for the owner

- Adopt FREYA-IDENTITY-001 as a NEW-x amendment with the continuity definition and the
  attributed-contribution rule.
- Approve a deterministic orchestrator (no framework owns the loop) for alpha.
- Approve the front-of-house rule and choose the default and small worker models.
- Confirm whether cloud escalation is in scope for coding tasks, and which provider.
- Approve the roadmap order, especially approvals and isolation before coding agents.
- Decide whether the Codex review cycle also assesses this proposal before freeze.
