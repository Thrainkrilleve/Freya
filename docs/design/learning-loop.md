# Design proposal: a learning loop inside the governance

**Status: draft proposal, written 2026-10-21 at the owner's request. Nothing here is
built.** It approves no ADR, adopts no pending amendment, and assigns no requirement
ID. Requirements in this document carry provisional labels **NEW-BE to NEW-BL**,
continuing from NEW-AS to NEW-BD (`openrig-lessons.md`); they get final IDs only at
reconciliation. Baseline references (CTX-, MEM-, SEC-, CAP-, ARCH-) are to *Freya
Foundation v1.1*, which remains authoritative.

## 1. The gap this closes

Freya today is an assistant with tools: one model call, at most three tool rounds,
and a hand-over to a cloud model the owner approves. The baseline describes more
(section 11, goal orchestration: planning, authorized execution, verification,
replanning), and the cognitive principles in the owner's brief ask for more still:
"have I reliably solved this?", "reuse and disclose prior verified solutions",
"discover existing tools before building". None of that is built. Freya remembers
(memories, summaries, a model record, screening corrections) but does not learn:
nothing about the next similar task changes because of what happened on the last.

The temptation is to build learning beside the governance, as a separate loop with
its own store. This document builds it through the governance that exists —
capabilities, the broker, deny-by-default policy, the audit chain, trust labels on
everything retrieved — so that every new power is inside a boundary that already
holds. Nothing here adds a trust boundary; each requirement names the existing rule
it rides on.

## 2. Requirements

### NEW-BE — Experience is a store reached only as a capability

Freya's record of what she tried and how it went lives in a store of its own
(`data/freya_experience.db`, through `ControlStore`), reached by two capabilities on
a provider `experience-local`: `experience.record` and `experience.recall`. Both are
in their own policy seed (`alpha-experience`), go through the broker, and are
attributed to the turn's principal.

- Every consultation and every write is an audit row, as `conversation.recall` is.
- Denied or unavailable recall supplies no experience; it is not reported as "nothing
  learned" (same rule as CLAUDE.md's conversation recall).
- Records carry the source, trust and owner labels the Context Authority puts on
  every retrieved passage, and are supplied inside its existing budget (sources get
  at most one third of the input), labelled when truncated.
- Per-owner isolation comes with the gateway's authenticated sessions (ADR-017); until
  then the store is single-owner like the memory database.
- The store is a new store under `data/`, so it is added to `DATA_KINDS`
  (`storage/placement.py`; a test checks), copied by the backup on close
  (`storage/backup.py`), and its path comes from `data_dir()` once C-4 is merged.

### NEW-BF — A record points, it does not copy

An experience record holds: the kind of request (from the fixed list the delegation
plan already uses), the capabilities and tools used by name, whether the verify step
accepted the result, how the turn ended, the owner's reaction if any, timings, and the
id of the stored exchange it came from. It never holds conversation text, tool output,
memory text or a brief.

- How a turn ended is a code written by the kernel, never the model's account of
  itself: `accepted`, `verify-rejected`, `tool-denied`, `provider-unavailable`,
  `rounds-exhausted`, `brief-held`, `owner-rejected`. A failure caused by the machine
  (a dead provider) is thereby told apart from a failure of approach, so Freya cannot
  learn "I am bad at summarising" from an Ollama that was not running.
- A failure that touched personal material (a brief held by screening, a memory the
  owner corrected) is recorded by its code alone, never with what was held.
- Reading the words behind a record is a `conversation.recall` call, policy-checked
  and audited like any other.
- The record is bounded (one line, under 1,000 bytes) so it can never carry what it
  must not.

### NEW-BG — The loop is kernel code, not model prose

The turn engine (the part Phase 0 is splitting out of `main.py`) runs a fixed loop:
recall experience → act → verify → accept, or retry once → record. Each step is a
broker call under the turn's operation id, so the audit shows the loop, and its
limits are enforced by code the model cannot argue with.

- The verify step runs on turns that used a tool or a hand-over, not on plain
  conversation (owner decision, 2026-10-21): that is where a wrong answer comes from,
  a tool result misread or a cloud answer misused, and plain chat stays as fast as
  today. A `/verify` command for one reply is a later addition, not part of this.
- Limits for the alpha: one verify call, one retry, and the existing cap of three tool
  rounds and twelve tool attempts a turn, counted across the retry.
- The verify step is a fresh model call (the `background` role) given the request and
  the answer, never the memories, and asked a fixed question: does this answer the
  request, and what would show that it does? Its reply is parsed into a fixed form
  (`accept`, `retry` with one sentence of reason, or `unsure`), as `compose.parse_plan`
  does; any other field is refused.
- Unless `FREYA_BACKGROUND_MODEL` names another model, the `background` role is the
  same model that wrote the answer, so the verify step is that model checking itself,
  with a fresh context but the same blind spots. That is weaker than a second model
  and is said so: the *catch* scenario (NEW-BL) measures what it actually catches, and
  a second, smaller local model for the role is the first thing to try if it
  catches too little.
- A retry is told the verify reason and nothing else new. A second failure returns
  the better of the two answers with a note that Freya was not sure; it never loops.
- The model proposes; the kernel decides whether a step runs. This is the same shape
  as `work.delegate`.

### NEW-BH — Learned material informs and never authorizes

A record, a lesson or a verified solution is text with a trust label. It cannot name
a capability to be granted, change a setting, stand in for an approval, or be offered
as an input to a cloud brief.

- The capabilities a turn may use are decided by policy and the model-tool offer, as
  today. Experience can suggest "last time `files.search` found it"; it cannot make
  `files.search` available.
- Consistent with NEW-D (no self-modification as a side effect of a task) and NEW-BB
  (shared packages cannot raise privilege). A poisoned or mistaken record can at
  worst make a bad suggestion, which the verify step and the owner catch.

### NEW-BI — Stages, and only the owner promotes

Every record has a stage: `observation` (it happened), `insight` (it happened and the
owner reacted well), `established` (the owner marked it as a way to do that kind of
thing). Promotion to `established` is an owner action through a capability
(`experience.promote`, seed `alpha-experience-owner`, reached from the window like
`cloud.settings.change`), never a side effect of a turn.

- At read time an `established` record last confirmed more than 30 days ago (owner
  decision, 2026-10-21; the same window answers NEW-AV's open question, to be widened
  once the tools stop changing weekly) is treated as `insight`; the downgrade is never
  stored, and the owner re-confirms from the window.
- `observation` records expire under the existing retention targets (30 days to
  start). `insight` and `established` records are kept until forgotten.
- The stage travels in the trust label, so the model is told how far to trust what it
  is shown.
- Failed observations are kept and are the other half of learning: a lesson (NEW-AW)
  is a failed observation the owner reacted to, and it is recalled for the same kind
  of request as "this was tried and did not work, because". A failure never
  generalises on its own: one failed observation stays one observation, and no number
  of them becomes a rule without the owner, so three unlucky timeouts cannot teach
  "never use the file tools" and a model shown many failures does not turn timid.
- A failure can flag an `established` record but not demote it: two owner rejections
  of the same established approach (owner decision, 2026-10-21: one is noise, two is a
  pattern) put it in front of the owner ("this stopped working?") in the window; the
  owner decides. Automatic demotion is the same
  self-modification risk as automatic promotion.

### NEW-BJ — The owner's reaction is the only reward

What moves a record's stage is the owner: a thumbs-up or thumbs-down on the answer
card (a new `feedback` window message, deliberately added), a correction in the next
turn, or a promotion. The verify step's own verdict is recorded, labelled as the
model's opinion, and never counts toward "this works".

- Same rule as the model record: measured results, never a model's claim.
- A reaction is one message, for one exchange, from a window, for typed and spoken
  replies alike (owner decision, 2026-10-21). Nothing said aloud moves a record: voice
  is not authentication, and more than one person is in the room.

### NEW-BK — Experience is personal data

Experience records never leave the machine, by kind, not by content scanning
(consistent with NEW-BQ, `freya-to-freya-cooperation.md`). They are never among the inputs Freya's code offers to
`work.delegate`. Forgetting a memory or deleting an exchange removes the experience
records that cite it, in the same transaction, and clears nothing else.

### NEW-BL — Measured before trusted

Three workflow-evaluation scenarios (`docs/design/workflow-evaluation.md`), run with a
synthetic experience record and made-up memories only:

1. **Reuse.** The record holds an `established` way to answer a kind of request; the
   scenario grades whether Freya uses it rather than starting from nothing, and
   whether she discloses that she did (the cognitive principle "reuse and disclose").
2. **Catch.** A tool result is planted wrong; the scenario grades whether the verify
   step retries and whether the final answer says Freya was unsure.
3. **Avoid.** The record holds a lesson that one approach fails for a kind of request;
   the scenario grades whether Freya avoids it, says why, and still answers rather
   than refusing.

The loop is switched on for ordinary turns (`FREYA_LEARNING=1` to start, then the
default) only when all three scenarios pass on the main model at the owner's normal bar
(85%). If the record does not improve the scores, the loop is not learning, and
that is known before anyone relies on it.

## 3. What this costs, and what it does not

Built: one store with two capabilities and a seed (a day), the loop in the turn engine
once Phase 0 has made a turn engine (two days), the feedback message and card control
(half a day), the three scenarios (a day). Each piece is a pull request of its own and
each runs on the workstation before the next starts.

Not added: any new trust boundary, any new principal, any new way for text to leave
the machine, any way for a model to change what Freya may do. Every capability here
is deny-by-default, audited and attributed, like the ones before it.

## 4. Build order

1. NEW-BL first, as `openrig-lessons.md` says of NEW-AY: the scenarios exist before
   the loop, so the loop is measured from its first run.
2. NEW-BE and NEW-BF, the store and its two capabilities, with the audit test
   extended to cover them (the pattern of `tests/test_audit_completeness.py`).
3. NEW-BG, the loop, behind `FREYA_LEARNING=1`, after Phase 0 of the command-path and
   turn-engine split has merged.
4. NEW-BJ and NEW-BI, the feedback message and promotion, with the window's message
   list in CLAUDE.md updated in the same pull request.
5. NEW-BH and NEW-BK are tests, not features: a record that names a capability is
   refused; a brief's offered inputs never include experience; forgetting cascades.

This comes after the reconciliation (three weeks from 2026-10-21) and before the
authenticated gateway, which then has one more capability to scope.

## 5. Owner decisions, 2026-10-21

1. **Trust window: 30 days.** An `established` record is read as `insight` once its
   last confirmation is older than that; widen when the tools stop changing weekly.
2. **Verify on tool and hand-over turns only.** Plain conversation is not verified.
3. **Feedback from the window only.** Thumbs on the card for typed and spoken replies;
   nothing spoken counts.
4. **Failed observations are kept**, with a kernel-written cause code, and become
   lessons only through the owner; **two** rejections flag an established record for
   the owner's decision.

No question is open. The next ones belong to the build: the exact fields of the record
(NEW-BF) and the wording of the verify prompt, decided in their pull requests.
