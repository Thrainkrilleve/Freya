# Delegating work to cloud models

Status: design proposal for the owner's review, written 2026-10-09. Nothing here is
built. It records the owner's decisions of that day and what follows from them. It
adopts no requirement or ADR; the Foundation v1.1 baseline remains authoritative, and
sending anything to an outside model is escalation under ESC-001 to ESC-006.

## Owner decisions

1. Delegation means all available models, local and cloud.
2. Providers: OpenAI and Anthropic first. Any other provider must be connectable the
   same way, without changing Core.
3. Cloud work uses the owner's subscription plans. No money is spent outside them.
   Paid API routes are built but every money cap starts at zero, so no paid call can
   be made until the owner raises a cap.
4. API keys are kept in Windows Credential Manager.
5. What may leave the machine is set per kind of material, starting strict.
6. Approval is by standing rules: kinds of task approved in advance for a provider.
7. A spoken request may start cloud work, after a confirmation the owner clicks in
   the window.
8. Freya tracks each provider's own limits and reset times, weighs cloud use against
   the value of the task, paces weekly allowances across the days, and offers the
   choice to go over when a cap is near.

## Routes

A route is one way of reaching one model: a provider, a model, and how it is paid for.

| Route kind | How Freya connects | What is limited |
|---|---|---|
| Local | Ollama on this PC | VRAM and time |
| Plan | The vendor's own program (`claude`, `codex`), which holds the sign-in | Usage windows set by the vendor |
| API | HTTPS with a key from Credential Manager | Money and requests per minute |

Each cloud route is its own provider (`anthropic-api`, `anthropic-plan`, `openai-api`,
`openai-plan`) with its own capabilities, so policy allows or denies each one
separately and the audit log shows which route served each call. No cloud capability
is ever in a default policy seed: the owner adds the rule. A new vendor is a new
provider module plus a price table and a list of limits; nothing else changes.

## How a turn flows

The owner only ever addresses Freya's main local reasoning model. The model that did
delegated work, local or cloud, may answer the owner directly: its answer is shown
under its own name, and Freya's interpretation follows (owner decision, 2026-10-10;
NEW-LC11, `docs/design/attributed-chat-responses.md`). On a spoken turn Freya speaks
only her interpretation; the other model's answer stays in the window. This replaces
the earlier rule that no other model answers the owner directly.

1. The owner says something. The main model decides whether she can answer herself or
   whether part of the work should go to another model.
2. For each part she hands over, she writes the prompt: a self-contained brief made
   for that model and that job, as short as it can be while still complete.
3. The router chooses the route for each brief (local first, then plan, then paid API)
   under the limits, check boxes, rules and prompts described below, and sends it.
4. What comes back is untrusted text. It is shown to the owner under the name of the
   model that wrote it (the label comes from the route, never from the text), and it
   goes to the main model as labelled input, the same way tool results do.
5. The main model then gives her interpretation in her own words, without repeating
   the other model's answer.

This can happen inside an ordinary conversation turn; it does not need the owner to
declare a task. The existing `background` role (memory decisions, ranking, summaries)
stays local always, because it reads the personal store.

The main model always has her full context, including stored memories and past
conversations: she is the intelligence that decides Freya's behaviour (owner decision).
She uses all of it to decide whether to hand work over, what kind of task it is, how
much it is worth, and which material from the present turn goes with it.

She does not write the brief (owner decision of 2026-10-09, after Codex's review; the
"middle path"). Her decision is a plan in a fixed form: a kind of task and a value
chosen from fixed lists, an urgent flag, and the ids of inputs that Freya's code
offered her. The plan has no field for prose, and anything else in it is refused, so
nothing she knows can ride along in her own words.

The brief is then written by a separate model call, the brief writer, whose whole
context is fixed instructions plus the parts of the chosen material that the owner's
settings and approvals have cleared to leave. Material set to never is not shown to
it. What the writer cannot see, it cannot pass on. Freya's code, not a model,
assembles the final text from the writer's instructions and the cleared parts, each
still carrying where it came from.

The cost of this: a cloud brief cannot draw on what Freya knows about the owner unless
the owner changes the setting for that kind of material or attaches it.

Screening stays as a second safeguard, because the owner's own request and the cleared
material can themselves contain something private. Every cloud-bound brief passes
these guards in order:

1. **Instruction.** The brief writer is told to use only the material it is shown
   and to add no facts, names or background of its own.
2. **Local screening.** Before a brief is sent, Freya's code compares it with the
   personal store on this machine: names, keys and phrases from saved memories, and
   closeness in meaning using the existing embeddings. This runs locally and sends
   nothing.
3. **Hold on a match.** A brief that matches is not sent. The owner is shown the
   passage and what it matched, and can remove it, send it once anyway, or cancel. A
   standing rule never covers a matched brief.
4. **A local record.** The text of every brief that leaves is kept on this machine for
   the owner to read, with the route and the time. It is personal data and is stored
   and protected like the conversation store, not in the audit log.

**Correcting a wrong label.** Screening will sometimes hold something that is not
private (a common word that happens to be in a memory, a public name, Freya's own
name) and will sometimes miss something that is. The owner corrects both, and the
corrections are kept:

- On a held brief the owner can also answer "this is not private". That adds the
  matched phrase, or the one memory it matched, to a not-private list, so it is not
  held again. The correction covers that phrase or that memory only, not its kind.
- From the local record of sent briefs, or at any time in the window, the owner can
  mark a phrase or a memory "always private". Such a mark always holds a brief, and
  a not-private entry never overrides it.
- Both lists are shown in the window and each entry can be removed. Entries are made
  only by the owner's click, never by speech, by Freya, or by a model, and each
  change is audited. The lists are personal data and stay on this machine.
- A correction changes what screening holds. It does not change a kind's setting:
  a stored memory still never leaves as a memory unless the owner changes that
  setting; marking it not private only stops its words from holding a brief that
  happens to contain them.

Screening and the two lists are built (`freya.delegation.screening`: `screen_brief`,
`Corrections`, kept in `data/screening_corrections.json`) and tested with made-up
memories. A brief is held when it contains a name, address or long number from a
memory, four words in a row copied from one, an always-private phrase, or, when the
caller supplies a closeness measure, a sentence close in meaning to one. Not yet
built: reading the real memory store and its embeddings into it, the hold prompt in
the window, and the local record of sent briefs. Four copied words in a row will
sometimes be an ordinary phrase; that is what "this is not private" is for.

This is a strong filter, not a proof. Screening catches copied and closely restated
material; a loose paraphrase or an inference can pass. The owner's prompts, the local
record and the per-provider switches are what bound the remainder.

The Claude plan route is built (`providers/model/claude_plan.py`, provider
`anthropic-plan`, capability `cloud.anthropic_plan.ask`) and tested with a fake
program. It is not registered in the running control plane and nothing calls it. One
real trial on 2026-10-09 with a made-up arithmetic brief returned the right answer in
3.3 seconds and reported 896 input and 7 output tokens, from the program's default
model (`claude-opus-5-5`). That trial was started from inside a Claude Code session,
so it does not show which sign-in a run started by Freya would use. Reading a "limit
reached" notice and its reset time from the program's output is not built: the
notice's exact form has not been seen.

Plan routes drive a program that can read files and run commands by itself. Freya
asks it for a worker with no tools and none of the owner's customisations, gives it
only an allowlisted environment and an empty folder, and treats its reply as
untrusted text. Those are requests to the vendor's program, not a containment
boundary.

## Choosing the model

Owner decision: one evaluation system decides every choice of model. The router picks
among a vendor's cloud models the same way it picks between a local model and the
cloud: from measured results, not from a fixed default and not from any model's
opinion of itself.

- **A record per model and kind of task.** For each model Freya can reach, local or
  cloud, and each kind of task (for example code review, summarising, extraction,
  multi-step reasoning), the record holds how often it passed the evaluation tasks of
  that kind, how long it took, and what it used: VRAM and time for a local model,
  plan allowance for a cloud one. Each entry names the exact model version it was
  measured on and when.
- **Where the record comes from.** The existing evaluation runner
  (`freya.evaluation.workflow`), extended with tasks per kind and able to run them
  against a cloud route. Tasks are synthetic and contain nothing personal. Later,
  verified outcomes of real delegated tasks can add to it; a model never grades
  itself.
- **How the router uses it.** Among the models allowed for the task (provider
  switched on, approval in place, limits not exceeded), it takes those whose measured
  pass rate for that kind meets the bar for the task's value, and of those the one
  that uses least. Low value accepts a lower bar than high value. If none meets the
  bar it says so instead of sending the task to a model that is likely to fail
  (ESC-002).
- **Unmeasured models.** A model with no record for a kind is not assumed good or
  bad. It is used only when no measured model qualifies, the least costly first, and
  the reply says the choice was unmeasured.
- **Measuring cloud models uses the plan.** An evaluation run against a cloud route
  draws on the allowance and sends the synthetic tasks to the vendor, so it needs the
  owner's approval like any other cloud work, is kept small, and is repeated only when
  the model version changes.

**Starting kinds of task and bars.**

| Kind | What it covers |
|---|---|
| extraction | Pulling stated facts out of text into a fixed form |
| summarising | Shortening text within a limit, keeping what matters, adding nothing |
| classification | Sorting items into given labels |
| rewriting | Changing form, tone or units while keeping every fact |
| reasoning | Multi-step problems with one right answer |
| code-review | Finding a defect or saying what code does, by reading it |
| planning | Ordering or allocating things under stated rules |
| analysis | Analysing anything, in general: figures, records, statements, long documents, a body of code, images, and forms of it not yet thought of (owner decision) |

| Task value | Pass rate a model needs for that kind |
|---|---|
| low | 70% |
| normal | 85% |
| high | 95% |

The owner accepted these three bars as a start (2026-10-09) on condition that
they can be adjusted. They are therefore the owner's settings, not constants: kept
with the other cloud settings, changed only by the owner in the window, each change
audited, and able to differ by kind of task if the owner wants that later.

The kinds are an open list, and analysis most of all. Adding a kind, or a new area
inside one, means adding tasks for it; until a model has been measured on that area
it is unmeasured there and the unmeasured rule above applies, so new sorts of work
are never silently treated as something a model is known to be good at. Analysis is
measured today on small figures and statements, one long record, and a three-file
project. Not yet covered: long documents of real length, larger bodies of code, and
images, which also need a route that can send a picture and a model that can read
one.

A result belongs to a model together with its settings. The same small model scored
7, 21 and 26 out of 30 on one set of tasks depending only on whether it was allowed
to think and how much output it was allowed, so each combination of model, thinking
setting and allowances is measured and recorded as its own entry, and the router
chooses among those entries.

A model counts as measured for a kind once it has at least 15 attempts on the exact
version in use; an attempt that got no reply (a timeout, a failed route) counts, and
counts as a failure, because a worker that often fails to answer is not a reliable
one. Writing code is not a kind yet: grading it means running what a model wrote,
which must wait for the isolation the baseline requires (ARCH-017).

The tasks are built: `freya.evaluation.delegation_tasks` holds 91 synthetic tasks, 45 of them marked harder
and 28 of those the hardest (added 2026-10-10, three or four per kind, once the
strongest local model passed everything else), at least eight per kind, each with a deterministic check, a reply that must pass and
one that must fail. `python -m freya.evaluation.delegation` runs them against an
installed Ollama model, or against Claude on the plan with `--claude-plan`, which is
refused without `--owner-approved-cloud-run`. No reply is executed.

The record and the choice are built (`freya.delegation.record`): `ModelRecord`
takes in evaluation reports and keeps counts and timings per entry and kind in
`data/model_record.json`; `choose` applies the rules above to the candidates it is
given; the bars are in `CloudSettings` and validated when changed.
`python -m freya.evaluation.record_reports` puts finished reports in the record,
grading their stored replies again with the current checks first.

Freya can read the record (`freya.delegation.abilities`, capability
`model.record.read`, offered to the model): her own model's counts by kind, the same
for each cloud route, where `choose` would send a task of each kind and value among
this machine's models and the cloud routes whose provider is on, and the kinds of
work no task covers. This is how she answers what she is good at and whether another
model would do better: from measurement, with unmeasured reported as unknown. It is
advice only. The part that checks the ledger and sends the brief on that advice is
not built.

## What may leave the machine

Every delegated request is assembled from labelled parts, using the source labels the
context system already carries. Each kind has a setting: allowed, ask first, or never.

| Kind | Starting setting |
|---|---|
| The request itself | Allowed |
| Live conversation | Ask first |
| Stored memories | Never |
| Past conversations and summaries | Never |
| Tool results from this PC | Ask first |
| Files and media | Ask first |
| Freya's own code | Ask first |

A part marked never is left out before the request is built, not filtered afterwards.
This sorting and the description are built (`freya.delegation`, `CloudSettings`,
`review_brief`) and tested; the window controls and the sending are not.
Before sending, Freya shows a description (which kinds, how much, which route, the
estimated cost) with the full text one click away. Settings are changed by the owner
in the window, never by voice or by a model, and every change is audited. Only the
minimum the task needs is sent (ESC-005).

## Approval

The chat window has a check box per cloud provider for whether it may be used. While
a provider's box is clear, no route to it is used and no approval prompt for it
appears, whatever rules exist; with every box clear, Freya says the work stayed
local. Each starts clear. Only a click in the window changes one, never speech or a
model, and each change is audited.

The setting itself is kept by Freya (an on/off value per provider, in the owner's
settings) and the window changes it with one message type. The check boxes are only
how the page shows it, so they can later become a menu without changing anything
behind the page.

A standing rule names a kind of task, the routes it may use, the kinds of material it
may send, and its own usage limit. A task that matches a rule goes without asking. A
task that matches none is described to the owner as ESC-004 requires (why local is not
enough, what would leave, which route, the cost) and waits for a yes that covers that
task only.

The prompt offers four answers: allow once, allow for this conversation, allow
permanently, and deny. "This conversation" lasts for the current run of Freya and is
kept in memory only. "Permanently" creates a standing rule for exactly what was shown:
that kind of task, that route, and those kinds of material, with a plan limit of its
own. It is listed in the window, where the owner can remove it. No answer can send a
kind of material whose setting is never, and a later task that wants more than a rule
covers asks again.

Deciding which rule a task matches is done by Freya's code from the task's declared
kind and the labels on its parts, never from a model's say-so. A task that picked up
untrusted content (a web page, a file, a tool result) cannot widen what it sends.

Voice: a spoken request can propose cloud work, including work a standing rule covers.
It is held until the owner clicks the confirmation in the window. Speech never
confirms, and by itself never changes a rule, changes a setting, or raises a cap.

A spoken request may *ask* for a setting to be changed (owner decision, 2026-10-11).
A confirmation then appears on the device that recorded the request, and the change is
made only when the owner chooses it there. Today the only microphone is this PC's, so
the prompt appears in its window; when other devices can listen, the prompt must go to
the one that heard the request and to no other. Built so far: a spoken request that
says "urgent", "high priority" or "low priority" (or "value") offers that choice in the
confirmation prompt a spoken hand-over always shows.

Which settings a request may ask to change (owner decision, 2026-10-11): those that
only tighten. A request, spoken or typed, can ask for a provider to be switched off or
for a kind of material to be set to never. Freya's model passes the request on with a
tool of its own (`cloud.settings.tighten`), the owner is shown exactly what would
change, and only their click makes it. Everything that loosens (switching a provider
on, allowing a kind of material, raising an allowance, making a standing rule) is done
by the owner in the Cloud tab and cannot be asked for this way. A typed request is
asked in the same way as a spoken one, because the model that read it may have been
steered by something else it read.

## Limits and pacing

Freya keeps a ledger per route: each call's estimated and reported usage, each limit,
how much of it is used, and when it resets. It is built (`freya.core.usage`,
`UsageLedger`) and tested against made-up routes; nothing uses it yet. It has its own
database, `data/freya_usage.db`, so the control database's schema is untouched. It
holds amounts and times only, never prompts or replies.

- **API routes.** Usage and remaining rate limits are read from each reply. Cost comes
  from a price table per model; a model with no price is refused. Money caps exist
  per call, per task, per day, and per month, and all start at zero: a call whose
  estimated cost is above zero is refused, and near-cap offers to go over do not apply
  to a zero cap. Only the owner raises a cap, in the window. The month follows the
  provider's billing period, not the calendar.
- **Plan routes.** Vendors do not publish an exact meter. Freya counts what she sends,
  records every "limit reached, resets at" notice, and takes window lengths and reset
  times from the provider. Her figure is an estimate and is shown as one.
- **Pacing.** A weekly or monthly allowance is divided across the days left before its
  reset. Unused share carries forward; today's share cannot be borrowed from later
  days without the owner agreeing.
- **Value against cost.** Each delegated task carries a value (set by the standing
  rule, or by the owner when asked). Freya compares it with the share of allowance the
  task would use and how much is left until the reset. Low value near a limit: stay
  local or wait. High value: go. The comparison and its inputs are recorded.
- **Value levels.** Low: cloud only when allowance is plentiful for the period,
  otherwise local or wait. Normal: cloud within the day's paced share. High: may
  exceed the day's share, and the owner is told. The level comes from the owner when
  asking, or from the matching standing rule, or is normal. Freya and models never
  raise it.
- **Priority by default (owner decision, 2026-10-11).** Everything is normal priority
  unless one action is required before another can complete; that dependency raises
  the urgency of the action being waited on. The raise comes from the dependency
  itself, established by Freya's code, never from a model's opinion of its own work.
  The owner saying so also raises it: the controls beside the chat box, or the words
  "urgent" or "a priority" in the message itself. Typed words count as they stand;
  spoken words count after the click every spoken hand-over already needs.
  What a dependency means (the owner's example): asked to design a system and then
  told it must include a particular capability, Freya works out that capability and
  how it would be done before she finishes the design, and she is to recognise
  similar situations herself. This is about the order of her own work. It is in her
  standing instructions, and a conversation scenario (`prerequisite-first`) checks
  that the added requirement is dealt with at the start of her reply.
  **Going past the day's share (owner decision, 2026-10-11).** Any use beyond the
  day's share of an allowance is approved by the owner, whatever the work's value and
  however urgent. Freya may suggest it and never decides it. The owner is asked in
  the limit prompt, which says when they marked the work urgent or a priority and,
  when Freya's model said other work is waiting on this hand-over, that she suggests
  going over and why. Her suggestion is a yes-or-no mark on the tool call, not prose,
  and changes no limit. This replaces the earlier rule that high-value and urgent
  work passed the day's share without asking. Value now changes two things only: low
  value waits once half the day's share is used (unless urgent), and it sets the bar a
  model must have measured at.
  *Not built:* ordering across several pieces of work running at once, which waits
  for Freya to be able to run more than one.
- **Urgent.** A separate flag the owner sets: the task does not wait for a reset.
  An urgent task may also use more than one cloud route at the same time. The usual
  way is splitting: independent parts go to different providers. Comparing, where two
  providers answer the same question and Freya shows where they agree and differ, is
  kept for major or complex work. Freya is expected to recognise such a task herself
  (from its size, the number of steps, how much depends on the result, and how local
  attempts went, as ESC-001 asks) and to propose a comparison, saying why. She never
  runs one without the owner's permission. Her recognition is a suggestion only: it
  does not raise the task's value, approve a route, or change what may be sent.
  Each route used must be covered by its own approval, each
  draws on its own allowance, and the prompt says that the material goes to more than
  one vendor. It names each vendor and, under each, describes
  what that vendor would receive: the part of the task, the kinds of material, and
  roughly how much of each, with the full text one click away. For each provider the
  prompt also shows Freya's estimate of how much of
  the remaining allowance for the period the task would use. Urgent does not widen what may be sent.
- **Order of routes.** Local first when a local model is good enough for that kind of
  task (ESC-001, judged from the evaluation results, not from the model's own
  confidence). Then plan allowance. Paid API calls last.
- **Near a cap.** At a warning level Freya says so. When a task would cross a cap she
  shows what is used, what the task needs, and when the limit resets, and offers: go
  over by a stated amount, wait for the reset, use another route, or stay local. Going
  over applies to that task only. Where a vendor's plan limit is a hard stop, going
  over is not possible and the offer is the other three.
- **Unknown outcomes.** A call that fails without reporting usage is counted at its
  full estimate.
- Every task also has a maximum number of calls and a time limit (ESC-006).

## Keys

API keys are read from Windows Credential Manager at the moment of a call, by the
provider only. They are never written to code, config, the audit log, `data/`, or a
child program's environment. The owner enters them; Freya has no code path that
creates or prints one. Plan routes use the vendor program's own sign-in, which Freya
never reads.

## Where long-term data is kept

Owner decision: the record of sent briefs, and all other long-term data, stays on this
machine, and where each kind lives follows from what the drive is good at. This is
wider than cloud delegation and is its own piece of work; its first step, a plan that
moves nothing, is in `docs/design/storage-placement.md`. This workstation has one NVMe SSD (C:, 2 TB) and one hard disk (D:, 8 TB).

| Drive | Good at | Suited to |
|---|---|---|
| SSD | Fast random reads and writes | Databases and their indexes, anything read during a turn, model files that are loaded often |
| Hard disk | Large capacity, sequential access | Backups, archives, old media and generated images, logs, model files kept but rarely loaded |

Rules for that work: each kind of data declares what it needs (fast lookup or bulk
keeping) and Freya places it by drive type, found from Windows, not by drive letter.
Nothing existing is moved without the owner's say, and the memory database is backed
up before any move. A backup belongs on a different drive from the data it protects.
The same sensitivity rules apply wherever the data lives.

## Vendor terms

Read on 2026-10-09 from the vendors' own pages. This is a reading of published
terms, not legal advice, and both vendors change these pages often: read them again
before each plan route is switched on.

**Anthropic.** The Claude Code legal page says subscription sign-in "is designed to
support ordinary use of Claude Code and other native Anthropic applications", and
that nothing prevents "an end user from signing in to the unmodified Claude Code
binary with their own Claude subscription". Anthropic's support page (update of 7
October 2026) says the Agent SDK and `claude -p` may still be used with subscription
limits and draw from them. What is not permitted: offering Claude sign-in inside
another application, routing requests through plan credentials on behalf of other
users, and collecting or storing Claude credentials or session tokens. Advertised
limits "assume ordinary, individual usage".

**OpenAI.** The Codex documentation presents `codex exec` for scripts and scheduled
jobs, says it "reuses saved CLI authentication by default", and runs it in a
read-only sandbox by default. It recommends API keys for automation and describes
running under a ChatGPT sign-in as an advanced path for "users who need ChatGPT/Codex
rate limits instead of API key usage", on trusted machines. The Terms of Use forbid
extracting output "automatically or programmatically"; no OpenAI page was found that
says how this applies to `codex exec` under a plan, and the Terms page itself could
not be opened during this check. A request for third-party "Sign in with ChatGPT"
was closed as not planned.

What this means for Freya's plan routes:

- She runs the vendor's own unmodified program (`claude -p`, `codex exec`) on this
  PC, as the owner, for the owner's own work. That is the case both vendors describe.
- She never reads, copies, stores or forwards either program's sign-in. The owner
  signs in through the vendor's own flow.
- Plan routes serve the owner only. When Freya has more than one user, each needs
  their own sign-in or an API key; one person's plan never serves another.
- Anthropic's position is stated plainly. OpenAI's is documented practice and not an
  explicit grant, so `openai-plan` carries more risk of a later change or a refusal.

**Included API credits.** Anthropic's Max and Team plans now include monthly API
credits ($100 on Max 5x, $200 on Max 20x; Pro has none), claimed by linking a
Console organization in the plan's billing settings. They cover the Claude API and
the Agent SDK. Usage is never charged to the plan, and with no purchased credits and
auto-reload off, requests stop when the credits run out. So an `anthropic-api` route
can run at no cost beyond the plan: the ledger would hold the credit as a hard
monthly allowance that resets on the billing date, with the money cap still zero.
Claiming the credits and creating the key are owner actions. The owner is on Pro
(2026-10-09), which has no included credits, so `anthropic-api` stays at a zero cap
and unused; this becomes an option only if the plan changes.

## Open before building

1. **Vendor terms for plan routes.** Checked on 2026-10-09; see "Vendor terms" below.
2. **Figures.** Money caps are zero until the owner sets them. The warning level and
   the per-rule plan limits are the owner's to set.
3. **When parallel use "makes sense".** The test for splitting an urgent task across
   providers, or asking two for the same answer, is not yet defined. (Decided: Freya
   starts with no standing rules; value is low, normal or high, plus urgent.)
4. **Authorization of the confirmation click.** The window has no pairing or per-client
   scope yet, so a click is attributed to the fixed `local-owner`. This is acceptable
   on a loopback-only alpha and must be revisited with the authenticated gateway.
5. **Codex's ledger work** (`feat/synthetic-self-design`) holds reservation and
   accounting ideas that may be reusable for the usage ledger. It has not been reviewed.
6. **The kinds of task and the bar for each value level** are a draft awaiting the
   owner. Sixty-three tasks are few: they can rank models but cannot show that a model
   is reliable at a 95% bar, and they will need to grow and be kept unseen.
7. A second opinion from Codex on this design, as the project rules ask before an
   architecture decision, once its allowance resets.

## The OpenAI plan route is not built, and why

Looked at on 2026-10-09 with Codex 0.162 as installed here. `codex exec` can be driven
the same way as the Claude program: the brief on standard input, an empty working
folder, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `--json`, and
`codex login status` reports "Logged in using ChatGPT" without Freya reading any
sign-in file.

The difference that stops it: the Claude program can be asked for a worker with no
tools at all (`--tools ""`), and in the trial it reported none. Codex has no such
switch. Its worker is an agent that can run commands, and its tightest sandbox,
read-only, still lets those commands read files anywhere on this PC, which is what the
project's own rules already say about it. A brief can carry material from a web page
or a file, and that material can contain instructions. A Codex worker that follows
them could read the owner's files, and whatever it reads is sent to OpenAI as part of
its own working. Screening checks what Freya sends; it cannot see what a worker reads
for itself.

Codex does have feature switches (`--disable shell_tool` and others), but this
version lists well over a hundred features, several of them other ways to act
(a newer command runner, browser and computer use, apps, hooks, image generation),
and which of them a non-interactive run can use is not documented. Proving a
no-tools worker would take real runs against the owner's plan.

**Tried, at the owner's request (2026-10-09).** Three real `codex exec` runs were
made with a made-up canary file outside the working folder, each asked to list its
tools and to read the canary.

- *As supplied, read-only sandbox.* The worker listed dozens of tools: command
  execution, patching, image generation, agent spawning, and tools for the apps
  connected to the owner's ChatGPT account (the list included Atlassian tools). It
  tried to read the canary with a shell command, which Codex rejected as "blocked by
  policy". The run took 90 seconds and about 34,000 input tokens, most of it the tool
  list.
- *With every acting feature switched off* (26 `--disable` flags: the shell and
  command tools, apps, browser and computer use, hooks, images, memories, agents,
  plugins, skills). The worker still listed `functions.exec`, `apply_patch`, a web
  tool and the agent-spawning tools. It answered that it could not read the canary
  and made no attempt. About 11,500 input tokens, 9 seconds.
- *The same, told to try every tool.* The same tool list and the same answer.

The canary did not leak in any run. But the question was whether a worker with no
tools can be had, and the answer on this version is no: with everything that can be
switched off switched off, it still reports command, patch, web and agent tools. One
blocked read in one run is not a boundary to rely on, and the supplied configuration
puts the owner's connected apps within a worker's reach.

So `openai-plan` stays unbuilt. What would change this: a documented no-tools mode in
Codex, running the worker inside the isolation the baseline requires for untrusted
execution, or reaching OpenAI by API, where a request simply has no tools unless they
are given.
The Cloud tab shows the OpenAI box as "not set up yet"; ticking it does nothing
because no route exists.

## The Cloud tab

Built: a Cloud tab in the window (`gateway/static/cloud.js`, message type `cloud`,
`freya.delegation.window_requests`). It shows and changes the provider check boxes,
the setting for each kind of material, the three bars, and the two screening lists,
and shows the owner's record of sent briefs. Its answers go only to the window that
asked. A change goes through the broker as `cloud.settings.change`, so the audit log
records that a change was made, never what it was. That capability is allowed by a
new default seed, `alpha-cloud-settings` (approved by the owner on 2026-10-09): it lets
the owner change their own choices, sends nothing, and is not offered to the model.
The check boxes are only how the page shows an on/off value kept by Freya, so they can
become a menu later.

**Switching a provider on unlocks it** (owner decision, 2026-10-09: using cloud should
unlock any cloud system presented, each chosen individually). A provider's switch now
opens all three locks for that provider and no other: the switch itself, the policy
rule for its route, and, if the route has none, a weekly allowance of 500,000 tokens
(the owner's agreed starting figure, Freya's own estimate). Switching it off removes
the rule the switch added and leaves the allowance and its usage for next time. A
rule the owner added by hand is left alone. The tab also has a control for each
provider's weekly allowance.

What this gives up: the policy rule used to be a second, separate act that the Cloud
tab could not perform. Now whatever can make a settings change through the broker can
also allow a cloud route. Only the window does that today, it is not offered to the
model, and each change is audited; it should be looked at again when sessions are
authenticated.

The tab cannot send a brief or write any other policy rule. Not built: the approval
prompt with its four answers, and the hold prompt.

## Codex's second opinion (2026-10-09)

Codex read this note and the code read-only and reported fifteen points. Each was
checked against the code.

Fixed:

- The Claude worker inherited Freya's environment minus credential-named variables,
  which still let through settings that point Claude at another server, bill another
  account, or send telemetry. It now gets a short allowlist only.
- "Local Ollama" was not enforced: the client followed `OLLAMA_HOST`, and Ollama can
  serve models that run on its own servers. The client is now pinned to this machine,
  and a model that runs elsewhere makes the provider unhealthy.
- The route assumed a subscription. It now asks the program how it is signed in and
  refuses unless that is a Claude subscription served by Anthropic.
- The vendor's error text, which can repeat a brief, was part of the error message
  and so could reach the audit log. It is now kept apart from the message.
- The program's output was read without limit. It now goes to files and a bounded
  amount is read.
- Ledger: a zero cap let through a call estimated at nothing; work larger than a hard
  allowance was told to wait for a reset that could never help; a call reserved before
  a reset stopped counting after it while still in flight.
- Record: two different sets of settings could produce the same entry; impossible
  counts were accepted; attempts that got no reply were left out, so an unreliable
  model could look perfect; high-value work could fall back to an unmeasured model
  without the owner's say.

Accepted, not yet done:

- **One sending boundary.** Built after the review (`freya.delegation.dispatch`).
  `Dispatcher.authorize` checks a composed brief against the provider's switch, the
  material sorting, screening and the usage ledger, reserves its usage, and returns
  an authorization naming the brief by its hash. `Dispatcher.send` accepts only an
  authorization it issued, for that exact text, once, within two minutes, and only
  if the owner's settings have not changed since. It settles the ledger and writes
  the owner's local record of sent briefs (`data/sent_briefs.jsonl`). A match on
  something marked always private cannot be released with one click.
  `freya.delegation.service.build_cloud_delegation` assembles all of this on a control
  plane and registers the Claude route so that a brief goes dispatcher, broker, route.
  Assembling it allows nothing: the provider's switch, a policy rule for the route's
  capability, and a usage limit for the route are three separate locks, each of which
  stops a brief by itself, and each of which only the owner opens. Freya's startup
  now assembles it, so that the Cloud tab works; no turn uses it. Still to do: the
  approval and hold prompts, and the turn that uses it.
- **The program's flags are requests, not containment.** Safe mode leaves sign-in,
  model choice and administrator-managed settings in force, and a temporary folder
  does not confine a program. The route's wording now says so. What the worker can
  really reach needs testing against the installed version.
- **Periods.** Fixed after the review: a limit now records when its present period
  began as well as when it resets, so a billing month need not be a fixed number of
  days and a changed reset time no longer moves what was already counted. Later
  periods are assumed to follow at the stated length until the provider's dates are
  given (`set_period`).
- **Exact identity of what was measured.** Fixed: a report now records the digest of
  the local model, or the model a cloud route served, says when more than one model
  answered (the record then refuses it), records only settings that were applied, and
  names the exact set of tasks it was measured on. The runner also refuses to measure
  a cloud-backed Ollama model as a local one, which Codex's branch review found.
- **Grading.** Tightened: the checks Codex showed could be passed by a reply with the
  right words and the wrong meaning now refuse those replies, and the two that
  refused valid replies accept them. They remain checks of form and stated facts. A
  reply can still be wrong in a way no pattern catches, so a pass means the automatic
  checks passed, not that a person would agree.

Decided by the owner after the review:

- **Private context and briefs.** Codex's most serious design point is the one raised
  earlier: because the main model writes cloud briefs with memories in view, a fact it
  restates as its own words is labelled as part of the request and passes the material
  sorting, and screening can miss it. Codex recommends building briefs only from
  inputs that were explicitly authorised, keeping track of where each part came from,
  with screening as a second safeguard. The owner chose the middle path: the main
  model keeps full context for deciding, and the brief is written from cleared
  inputs only. It is built (`freya.delegation.compose`) and described under "How a
  turn flows".

### Re-evaluation after the changes (2026-10-09)

Codex read the changed design and code again. It found no way to alter or replay an
authorization, and confirmed the environment, error-text, zero-cap and hard-cap fixes.
It found these, all then fixed:

- Authorization trusted the composed brief's own account of itself. A brief composed
  before a setting was tightened, or one whose text had been swapped, could be
  authorized. Authorization now sorts the parts again under the present settings and
  requires the text to be exactly what the cleared parts assemble to.
- A match against a memory marked always private was reported as an ordinary match,
  so "send it once anyway" released it. It is now reported, and held, as always
  private.
- Nothing was screened again at the moment of sending. It now is, so a memory or an
  always-private mark added after authorizing still stops the brief.
- The plan's value and urgent flag went straight to the usage ledger, letting the
  model raise a task's value against the owner-only rule above. They are now
  suggestions only; the value and urgency a task is sent with come from the caller
  acting for the owner.
- A model whose location could not be established was treated as local.
- A wrong copy of an authorization cancelled the real one; unused authorizations kept
  their reserved usage for ever; a reply with no usage figure was counted as zero;
  stored record counts were not checked when read; results stayed attached to a name
  after the exact model behind it changed.
- The Claude route's capability accepted any caller that policy allowed. It now
  refuses unless the dispatcher confirms it is sending that exact text.
- The worker was given the Windows user, domain and computer names, which it does not
  need. The sign-in is now asked before every brief instead of being trusted for five
  minutes.

Left as they are, and why:

- The owner's request always goes in a brief, and can itself contain something private
  that is in no memory. That is the owner's own text and the owner's choice; screening
  catches what matches the store.
- Which material the main model picks, and in what order, can reflect what it knows.
  The plan carries ids and fixed choices only, so this is a very narrow channel.
- The brief writer is told to add nothing but cannot be made to. It has no private
  material in view, and what it writes is screened.
- The last settings check and the send are made together inside Freya, but a change
  made in the instant between them is not excluded.
- The program's output files can grow until the time limit.
- A second real trial was made under the allowlisted environment (2026-10-09, one
  made-up brief). With none of the calling session's variables passed on, the
  program reported a Claude subscription served by Anthropic, answered correctly,
  and, asked which tools it could call, answered none. That is the worker's own
  account, which is evidence and not proof of containment.

## Suggested build order

1. Usage ledger and limits, with no cloud provider (tested with a fake route).
2. Material settings and the request description, using the existing source labels.
3. Plan routes, if the terms check in the open items allows.
4. Standing rules, the approval prompt, and the voice confirmation.
5. Pacing and the value comparison.
6. `anthropic-api` and `openai-api` providers behind deny-by-default capabilities,
   with zero caps.
