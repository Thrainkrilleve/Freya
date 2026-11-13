# Design proposal: skills, checks and one gate — how Freya gets better

**Status: draft proposal, written 2026-10-22 at the owner's request. Nothing here is
built.** It approves no ADR, adopts no pending amendment, and assigns no requirement
ID. Requirements in this document carry provisional labels **NEW-BW to NEW-CG**
(`docs/labels.md`); they get final IDs only at reconciliation. Baseline references
(CTX-, MEM-, SEC-, CAP-, ARCH-) are to *Freya Foundation v1.1*, which remains
authoritative.

This document supersedes the mechanism in `learning-loop.md` (NEW-BE to NEW-BL) and
keeps its rules. Section 2 says exactly what carries over and what is replaced.

## 1. Why a second proposal

`learning-loop.md` was right about governance and thin about learning. Its unit of
learning is a one-line record ("`files.search` was used, the owner approved") which
can remind the model that something worked but cannot tell it how. Its only reward is
the owner's click, so learning runs at the speed of the owner's attention; unrated
observations expire in 30 days, which is most of them. Its verify step is a model
grading a model. And the decision router (`docs/Decision_Router_Design_Handoff.md`,
2026-10-22) adds a second thing that learns, a nightly retrain, with no gate at all.

This proposal keeps every boundary the first one drew and changes four things:

1. **The unit of learning is a skill**: a small, versioned, tested file that carries
   the method, not a pointer to the time it worked.
2. **Evidence comes first from checks**, then from the owner, and never from a model's
   opinion of itself. Weak signals (the owner re-asking, abandoning, editing) count
   too, but only ever against a skill.
3. **Candidates are drafted at night** on the idle GPU from the day's episodes, so
   learning does not wait on the owner's clicks.
4. **One promotion gate** for everything that learns: a skill, a router retrain, a
   prompt change, a model swap. Each must beat the system without it on held-out
   cases, and each can be rolled back in one step. The owner still approves every
   promotion, in a weekly batch instead of one click at a time.

What this does not do, said plainly: no weights change in the alpha. Freya gets
better the way a careful person does, by keeping tested methods, not by becoming a
different model. A larger model or a second GPU makes every part of this better and
is proved to by the same gate (NEW-CF).

## 2. What is kept, what is extended, what is replaced

Nothing from `learning-loop.md` is built, so nothing is thrown away in code. In
design terms:

**Kept unchanged (Freya today).** The deny-by-default policy engine, the broker, the
hash-chained audit log, `ControlStore` and its migrations, the Context Authority and
its budgets and trust labels, the conversation store, the model record and usage
ledger, brief screening, the sandbox contract (`code-sandbox.md`), and the decision
router's three-layer design. Every new thing below is reached through these; none is
changed to admit it.

**Extended.**

- The workflow-evaluation runner (`freya.evaluation.workflow`) becomes the promotion
  gate: it gains a baseline run (the same cases with the candidate switched off) and
  a held-out split. Its sandboxing, synthetic fixtures and report format stay.
- The turn engine from the Phase 0 split gains two steps: skill lookup before the
  model is called, and a verifier hook after a tool-using answer. Its limits (three
  tool rounds, twelve attempts) are unchanged.
- `ModelToolHost` offers a matched skill's text as a labelled source, as it offers a
  memory. The tools a skill names are still offered only if policy allows them today.
- `storage/placement.py` and `storage/backup.py` learn the two new stores.
- The window gains a feedback control on the answer card (as NEW-BJ asked) and one
  weekly review card.

**Replaced from `learning-loop.md`.**

| There | Here | Why |
| --- | --- | --- |
| NEW-BE/BF experience store, pointer records | NEW-BY episode log, plus NEW-BW skills | a record cannot carry a method; a skill can |
| NEW-BG verify as a model call | NEW-BX checks first, model last | a check is evidence, an opinion is not |
| NEW-BI stages on records, owner promotes each | NEW-CC skill states, weekly batch approval | same rule, less of the owner's time |
| NEW-BJ owner's reaction the only reward | NEW-BY/BZ graded signals; owner's reaction the only *promotion* | checks and weak signals add evidence without adding authority |
| NEW-BL three scenarios before switch-on | NEW-CB the gate, applied to every candidate | one mechanism instead of a scenario per feature |

**Carried over as rules, by reference.** NEW-BH (learned material informs and never
authorizes) and NEW-BK (experience is personal data, never leaves, forgotten with
what it cites) apply to skills, episodes and candidates exactly as written. The
owner's four decisions of 2026-10-21 stand where they still apply: feedback from the
window only; verifier on tool and hand-over turns only; failed episodes kept with a
kernel-written cause code; two failures flag, never demote on their own (now NEW-BZ).

## 3. Requirements

### NEW-BW — A skill is the unit of learning

A skill is one Markdown file under `data/skills/<name>/v<n>.md`, read by the Context
Authority as an addressable record (NEW-AS), with a fixed front matter and no other
fields:

- `applies_to`: the request kinds (the router's fixed list) and a few example
  requests, from which the embedding used for matching is computed.
- `tools`: the capabilities the method uses, by name. This is a declaration for the
  reader, not a grant: a turn may use them only if policy allows them that turn
  (NEW-BH). A skill naming a capability that policy denies is still shown; the
  denial is reported as today.
- `steps`: the method, in the model's words, bounded (2,000 bytes).
- `check`: how success is shown, as a verifier name from the fixed list (NEW-BX) and
  its arguments. A skill with no check is accepted but can never be promoted above
  `candidate`.
- `cases`: three to ten test cases taken from real episodes, each a request and an
  expected outcome the check can grade. Cases are made of synthetic or owner-cleared
  text only: a case drafted from an episode is shown to the owner at approval, and a
  case the owner has not seen is not run outside the sandbox.

Skills are personal data (NEW-BK): kept under `data/`, never committed, never offered
to `work.delegate`, never sent to another Freya, removed when an episode they were
drafted from is forgotten. A skill is text with a trust label; it cannot change a
setting, answer a prompt or widen what a turn may do.

The three first skills are written by hand by the owner (section 5), so the loader,
matcher and gate are exercised before anything is drafted by a model.

### NEW-BX — Checks before opinions

After a tool or hand-over turn the kernel runs the matched skill's check, or, with
no skill, the default check for the request kind. Checks are code from a fixed list
in `freya.learning.checks`, each returning `pass`, `fail` or `not_applicable` and a
one-line reason: a named file now exists and is non-empty; a number in the answer
equals one computed from the tool results; a tool returned a non-error result; the
answer cites every tool result it relies on; a JSON reply parses to the declared
form. A model may not add a check; adding one is a pull request.

Only when every applicable check says `not_applicable` does the kernel ask the
`background` model the fixed verify question of NEW-BG, parsed to `accept`, `retry`
or `unsure`. Its verdict is recorded as the model's opinion and drives one retry at
most; it never counts as evidence (NEW-BY). A failed check also drives one retry,
told the check's reason and nothing else. The loop's shape, limits and audit rows
are NEW-BG's.

### NEW-BY — The episode log and graded signals

Every tool or hand-over turn writes one episode to `data/freya_episodes.db`
(through `ControlStore`, in `DATA_KINDS`, backed up with the rest), reached only by
the capabilities `episode.record` and `episode.recall` on provider `learning-local`,
policy seed `alpha-learning`, audited and attributed like `conversation.recall`.

An episode holds what NEW-BF allowed and nothing more: request kind, the router's
embedding of the request, the skill used (name and version) if any, tools by name,
how the turn ended (NEW-BF's kernel-written codes), timings, the id of the stored
exchange, and its signals. It never holds conversation text, tool output or memory
text.

Signals have three strengths, written by code, never by the model:

- **Strong.** A check passed or failed (NEW-BX); the owner's thumbs on the card; a
  correction in the next turn that names the answer; a skill the owner approved.
- **Weak.** The owner asked the same thing again within ten minutes (by embedding
  distance, not by words); the owner abandoned the exchange (closed the window or
  moved on without reply within the turn); the owner edited the answer before using
  it where the window can see that.
- **Model.** The verify step's verdict. Recorded, never counted.

The owner's reaction stays the only thing that promotes (NEW-CC). Checks and weak
signals add evidence the gate reads; they never move a skill up.

### NEW-BZ — Weak signals only demote

A weak signal can lower a skill's score, flag it, or retire it; it can never raise
one. Two owner rejections of the same approved skill (owner decision, 2026-10-21)
put it in the weekly review as "this stopped working?"; three consecutive check
failures on the same skill version take it out of lookup until the owner sees it,
and the previous version, if any, is restored in the same step (rollback is one
version back, never further on its own). Nothing becomes a rule from failures alone
(NEW-BI's reasoning): three timeouts cannot teach "never use the file tools".

### NEW-CA — Nightly reflection drafts candidates

A scheduled job (`python -m freya.learning.reflect`, run by Windows Task Scheduler
under the owner's account only, at a time the owner sets, 02:00 to start) reads the
day's episodes through `episode.recall`, groups them by request embedding, and where
the same kind of request succeeded twice or more with the same tools, asks a local
model to draft a skill file for the group. The draft is a **candidate**: kept under
`data/skills/_candidates/`, never matched by lookup, and reaching a turn only after
NEW-CB and NEW-CC.

Keep these rules: the job runs only when no other Freya session is active on the
machine (`single_instance.py`'s mutex) and no other user is signed in to the console;
it checks free video memory before loading any model and skips the night rather than
contend; it unloads what it loaded and reloads Freya's conversation model before it
exits, so she is ready in the morning; it reads episodes and stored exchanges only
through the broker, so each night is audited; the model it asks is given the
episodes' request texts and the tool names, never memories; it drafts at most ten
candidates a night and never edits an approved skill in place (it drafts a new
version). The job's own output is a one-page morning note the owner can read in the
window.

### NEW-CB — One promotion gate for everything that learns

Anything that would change Freya's behaviour on later turns goes through the same
gate before it can take effect: a candidate skill, a new version of an approved
skill, a router retrain, a change to a system prompt, a change of local model, and
later a fine-tune. The gate is `freya.evaluation.workflow` extended with:

- **A baseline run.** The candidate's cases and a fixed regression suite are run
  twice in the sandbox, once with the candidate and once without, same model, same
  fixtures, same seed. The candidate must do at least as well on the regression
  suite and better on its own cases.
- **A held-out split.** A third of every skill's cases, chosen at creation and never
  shown to the drafting model, are the ones that decide; the rest are for the
  drafter's use.
- **Fixed thresholds.** The owner's bars (`CloudSettings.bars`, 70/85/95% by value)
  are the pass marks; fewer than 15 trials is unmeasured, as for the model record.
- **A report the owner can read**, with before and after scores, kept with the
  candidate and attached to the review card.
- **Rollback.** Every promotion records what it replaced; `skill.rollback`,
  `router.rollback` and the model setting each restore the previous version in one
  owner action.

Gate runs use synthetic and owner-cleared cases only, never the owner's data or
live tools (`workflow-evaluation.md`'s rules). A thing that has not passed the gate
cannot be switched on by any setting; `FREYA_LEARNING=1` turns on lookup of
*approved* skills, not of candidates.

### NEW-CC — The owner approves in batches; the system only demotes

Skill states are `candidate`, `approved`, `retired`. Only the owner moves a skill to
`approved`, through `skill.approve` (seed `alpha-learning-owner`, reached from the
window like `cloud.settings.change`, one action per skill version, audited). The
window shows a weekly review card listing only candidates that passed the gate, each
with its name, its steps, its cases, the gate report and the episodes it came from;
the owner approves, rejects or defers each. A candidate not approved within four
weeks is dropped. The card is sized for ten minutes; if there are more candidates
than that, the oldest wait.

The system moves skills down without the owner (NEW-BZ) and retires an approved
skill unused for 90 days or superseded by an approved later version; a retired skill
is kept on disk, out of lookup, until the owner deletes it.

### NEW-CD — New tools are learned, not assumed

When a new model tool is registered (today, a new capability with `model_tool: True`
in `control_plane.py`), it is offered to ordinary turns only on the owner's explicit
request in that turn until a skill naming it is approved. In between, the nightly
job may run one **exploration session** for it in the sandbox: a bounded set of
calls (twenty at most) with synthetic arguments, graded by the tool's own declared
result form, from which it drafts a candidate skill "how to use `<tool>`". The
exploration is audited like any run, uses no owner data, and can neither grant the
tool nor change its policy seed. A tool with consequential effects (CLAUDE.md's
rule on adding one) gets no exploration at all; the owner writes its skill.

### NEW-CE — Assisted self-coding, with a protected core

Freya may propose changes to her own repository, as she may to any folder the owner
opens, under these rules, which extend NEW-D and ARCH-017 and are tests, not
options:

- A change is a branch and a pull request, made by the owner's own signed-in
  program from a git worktree outside the running checkout (the Codex pattern in
  CLAUDE.md), never an edit to the running instance.
- The protected set — the kernel's turn loop, `core/policy`, `core/audit`,
  `core/capabilities`, `delegation/dispatch.py`, `freya.learning.checks`, the gate,
  `control_plane.py`'s seeds and `MIGRATIONS` — cannot be the target of a proposal
  at all; a proposal touching a path in it is refused before any model is called,
  by path match, and the refusal is audited.
- Code a model wrote is untrusted content (strict mode, owner decision 2026-10-07):
  it is run only in the sandbox, its tests run there too, and the pull request says
  what the model wrote.
- The owner merges. Nothing Freya does can push to `main` or merge, which the
  guard hook and branch protection already enforce for Claude Code.
- Review notes the owner leaves on such a pull request are strong signals
  (NEW-BY) for a `coding` skill, so what she learns from coding is the owner's
  taste, through the same gate as everything else.

### NEW-CF — Better hardware and models make it smarter, and the gate proves it

Changing the conversation, background or reflection model, or a model's context
size, is a candidate under NEW-CB: the regression suite and every approved skill's
held-out cases are run on the new model and compared with the current one, and the
owner sees the report before switching. The skill library and the router's labelled
data are model-independent and carry over whole. The reflection job and the gate
run on whatever GPU is free and may be pointed at a second machine later through
Freya-to-Freya cooperation (NEW-BM to NEW-BV) without change to this design.

### NEW-CG — What may train, by provenance

Any data that trains or retrains anything (the router's classifier, a future
fine-tune) is selected by provenance, not by content: the owner's own typed or
spoken turns and their labels, approved skills' cases, and gate reports. Text that
arrived through a tool (a web page, a file, another model's answer, a cloud brief's
reply) is never training data, whatever it says. A training set lists its sources
by episode id, and the gate report names the set it was trained on.

## 4. What this costs, and what it does not

Built, in order (section 5): the gate's baseline and held-out additions (two days),
the skill format, loader and matcher with three hand-written skills (two days), the
checks module and the verifier hook in the turn engine (two days, after Phase 0), the
episode log and signals (two days), the review card and `skill.approve` (one day),
the nightly job (two days), tool exploration (one day), the self-coding guards (one
day, mostly tests). Each is a pull request of its own and runs on the workstation
before the next starts.

Not added: any new trust boundary, any new principal, any way for text to leave the
machine, any way for a model to change what Freya may do, any change of weights.
Removed from the plan: `experience.promote`, the record stages, and the per-feature
scenarios of NEW-BL, all folded into the gate.

## 5. Build order

1. **NEW-CB first**, the gate, because nothing else can be measured without it. The
   regression suite is the existing workflow scenarios plus NEW-AY's reuse scenario.
2. **NEW-BW**, with three skills the owner writes by hand: reading a file the owner
   opened and answering from it; reporting this PC's temperatures; one of the
   owner's own recurring tasks. Lookup behind `FREYA_LEARNING=1`.
3. **NEW-BX**, the checks module and the verifier hook, once Phase 0's turn engine
   has merged. Its first scenario is NEW-BL's *catch*, now a gate case.
4. **NEW-BY and NEW-BZ**, the episode log, signals and demotion; the audit test
   extended as for every new capability.
5. **NEW-CC**, the review card and approval; CLAUDE.md's window message list updated
   in the same pull request.
6. **NEW-CA**, the nightly job, first run by hand in the day with the owner watching.
7. **NEW-CD** and **NEW-CE**, each small, each mostly tests.
8. **NEW-CF** and **NEW-CG** are tests on the gate, written with it in step 1 and
   kept green.

This comes after the three-week reconciliation (from 2026-10-21) and before the
authenticated gateway, which then has two more capabilities to scope
(`alpha-learning`, `alpha-learning-owner`). The router handoff is unchanged by this
document except that its nightly retrain now goes through NEW-CB and NEW-CG.

## 6. Open questions for the owner

1. The nightly time and whether the job may also run in a daytime idle window
   (screen locked for an hour, GPU free).
2. Which recurring task of the owner's is the third hand-written skill.
3. Whether the weekly card is a fixed day or shown when ten candidates are waiting.
4. Whether weak signals from the *window* only (as feedback is) or also from the
   console count; nothing spoken counts either way.
