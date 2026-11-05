# Design proposal: model tiers — the strongest model plans, lighter models follow, checks decide

**Status: draft proposal, written 2026-11-05 at the owner's request. Nothing here is
built.** It approves no ADR, adopts no pending amendment, and assigns no requirement
ID. Requirements carry the provisional labels **NEW-CV to NEW-DA**
(`docs/labels.md`); acceptance tests carry **TIER-1 to TIER-8**. It builds on
`skill-learning.md` (NEW-BW to NEW-CG), which is itself a proposal, and refers to
`local-decider.md` (NEW-CI to NEW-CU, open PR #47). Nothing here works without the
NEW-BW skill files and the NEW-CB gate.

## 1. Why

The owner set the direction on 2026-10-23: *the most capable model should do the
plan and skill development and hand off to lighter-weight models that follow the plan
at turn time.* That is the right shape: a plan written once by a strong model and
followed many times by a cheap one spends the strong model's cost once.

As stated, it runs one way only, and that leaves four gaps. On 2026-11-05 the owner
agreed to close them:

1. **No way back up.** When the lighter model meets a case the plan did not foresee,
   nothing says what it does. Light models guess there, and guess wrong.
2. **Plans have no edges.** A skill (NEW-BW) says when it applies (`applies_to`) but
   not when it stops applying, so a near match is followed as if it were a match.
3. **The strong model only writes.** It never sees how its plans did, so they improve
   only when the owner notices a problem.
4. **Nothing says which decisions belong to no model.** NEW-BH and NEW-CL say learned
   material and decisions never authorize; tiers need the same rule said for them,
   because escalating to a stronger model must not feel like asking a higher
   authority.

The rule this document proposes, in one line: **the strongest model writes and
reviews plans, lighter models follow them, checks decide pass or fail, and anything
outside a plan escalates.**

## 2. What is kept, extended and replaced

**Kept unchanged.** The policy engine, broker and audit log; the model record and
`choose()` in `delegation/record.py` (least costly route measured good enough for the
kind and value; nothing a model says about itself counts); `CloudSettings` and its
bars; the turn engine's limits (three tool rounds, twelve attempts); NEW-BX's rule
that a check is evidence and a model's verdict is not; NEW-CB as the one gate;
NEW-CC as the only way anything is promoted.

**Extended.**

- NEW-BW's skill front matter gains one field, `stops_when` (NEW-CX). This changes
  NEW-BW's "no other fields" by exactly that field.
- The turn engine's verifier hook (NEW-BX) gains one outcome, *escalate* (NEW-CY).
- The nightly job (NEW-CA) gains a review pass by the author tier (NEW-CZ).
- The episode log (NEW-BY) records escalations with a kernel-written cause code.
- The gate's report (NEW-CB) shows escalation rate per request kind (NEW-DA).

**Replaced.** Nothing.

## 3. Requirements

### NEW-CV — Tiers are roles, not model names

Freya has three tiers, each a setting that names a route, never a model hard-coded
in code:

- **author**: writes and revises skills (NEW-CA), reviews episodes (NEW-CZ), and
  takes escalated turns. The most capable route the owner allows: the largest local
  model, or a cloud route where `CloudSettings` permits it for that material.
- **follower**: answers ordinary turns by following an approved skill. The
  conversation model. For delegated work, `choose()` already picks the least costly
  route measured good enough, which is this tier.
- **decider**: typed decisions with a calibrated probability (NEW-CI). Never writes
  prose, never plans.

Which route fills each tier is one owner setting per tier. Changing it is a NEW-CB
candidate, measured as NEW-CF requires; the tiers and their rules do not change when
a model does.

### NEW-CW — The author writes plans for the follower, not for itself

A skill drafted for the follower is written so it can be followed without judgment
calls: numbered steps, each naming the tool and what a good result looks like, and
the check (NEW-BX) that shows it worked. The gate (NEW-CB) runs a candidate's cases
**on the follower route**, not the author's, because that is who will follow it. A
skill that passes only on the author route is not promoted for follower use.

### NEW-CX — Every skill says where it stops

NEW-BW's front matter gains `stops_when`: a short list of conditions, in plain
words with one or two example requests each, under which the skill does not apply
even though `applies_to` matched. Examples: "the file is not one the owner opened",
"the request asks to change something, not read it", "more than one PC is named".
Matching reads both fields; an embedding close to a `stops_when` example counts as
no match. A skill with an empty `stops_when` can be approved but the review card
(NEW-CC) shows the gap.

### NEW-CY — Outside the plan, escalate; do not improvise

The follower hands the turn up one tier, instead of answering, when any of these
holds. The kernel decides; the follower model is never asked whether to escalate:

1. the matched skill's `stops_when` applies;
2. the skill's check failed, and so did the one retry NEW-BX allows;
3. no approved skill matches and the request kind is on the owner's list of kinds
   the follower may not answer unaided (empty at first);
4. a decider point with an escalation path abstained (NEW-CN).

Escalation goes one tier up and happens at most once per turn. The author tier gets
exactly the tools that policy allows that turn, no more (NEW-DA). If the author route is
not available, because it is offline, unmeasured, or not permitted for this material
by `CloudSettings`, Freya says she cannot do this reliably now and why, rather than
letting the follower guess. The reply is shown under the name of the model that
wrote it (owner decision, October 2026).

Every escalation is an episode (NEW-BY) with a cause code from a fixed list:
`stops_when`, `check_failed`, `no_skill`, `decider_abstained`, `author_unavailable`.
Escalations are the nightly job's best evidence of where a skill is missing or
wrong.

### NEW-CZ — The author reviews, and review produces candidates, not verdicts

Each night (NEW-CA), the author tier reviews:

- every escalated episode;
- every episode whose check failed;
- a random sample of passed episodes, one in twenty to start (owner setting).

From each it may draft a new skill, a new version of an existing one with a sharper
`stops_when`, or a note that a check is missing (adding a check stays a pull request,
NEW-BX). Its findings are candidates and notes. They go through NEW-CB and NEW-CC
like anything else; the author's opinion of an episode is recorded as an opinion and
never counts as evidence (NEW-BY). Review reads only what NEW-CA's rules let the
drafting model read: request texts and tool names, never memories.

### NEW-DA — Some decisions belong to no tier

No tier, and no escalation, decides:

- whether an action is allowed (the policy engine, deny by default);
- anything destructive or irreversible, or any external effect, beyond what an
  owner-approved rule already allows;
- what may leave the machine (brief screening, `CloudSettings`);
- adopting a spec amendment, approving an ADR, promoting a skill, or changing which
  route fills a tier (owner actions).

Escalating a turn moves it to a more capable model; it never moves it to a more
permitted one. Learned material informs and never authorizes (NEW-BH); decisions
choose and never authorize (NEW-CL); the same holds for tiers.

### Measuring the tiers (an extension of NEW-CB, no new label)

The gate's report (NEW-CB) shows, per request kind, the follower's pass rate on
checks and its escalation rate over the last 15 trials or more. A kind whose
escalation rate rises is put first in that night's drafting. A kind the follower
handles with no escalations and passing checks over the measured window is the
evidence for moving it off the owner's "may not answer unaided" list, which only the
owner does.

## 4. How a turn runs

1. The decider and rules classify the request (router, NEW-CI). Policy decides what
   this turn may do.
2. Skill lookup: match `applies_to`, then rule out with `stops_when` (NEW-CX).
3. Follower answers by the skill, through the broker.
4. The kernel runs the skill's check (NEW-BX). Pass: done. Fail: one retry with the
   check's reason. Fail again: escalate (NEW-CY).
5. Author answers with the same tools policy allowed, or Freya says she cannot do it
   reliably now. The episode records the cause.
6. At night, the author reviews escalations, failures and a sample of passes
   (NEW-CZ) and drafts candidates; the gate measures them on the follower route
   (NEW-CW); the owner approves in the weekly batch (NEW-CC).

## 5. Acceptance tests (provisional)

| ID | Test |
| --- | --- |
| TIER-1 | A skill whose `stops_when` matches the request is not used; the turn escalates with cause `stops_when` |
| TIER-2 | Two failed checks on one turn escalate once, never twice; the episode records `check_failed` |
| TIER-3 | With the author route unavailable, the reply says so; the follower's unchecked answer is not shown as the answer |
| TIER-4 | An escalated turn is offered exactly the tools policy allowed the original turn |
| TIER-5 | A candidate that passes its cases on the author route but not the follower route is not promoted for follower use |
| TIER-6 | The nightly review never reads memory text; its drafts are candidates only and its verdicts are not counted as evidence |
| TIER-7 | Changing the route of a tier without a passed NEW-CB run is refused |
| TIER-8 | No code path lets a tier, an escalation or a review change policy rows, `CloudSettings`, or a skill's state |

## 6. What it costs, and what it does not do

Cost: one field in the skill format, one outcome in the verifier hook, five cause
codes, one review pass in the nightly job, one column in the gate report. No new
capability, store or window message beyond what `skill-learning.md` already adds.

It does not change weights, add a cloud dependency, or make any model a judge of
its own work. It does not let the follower decide when to escalate, and it does not
make the author tier a source of authority.

## 7. Build order

1. `stops_when` with NEW-BW's loader and matcher (skill-learning step 2), so the
   three hand-written skills carry it from the start.
2. The escalate outcome and cause codes with NEW-BX's verifier hook (step 3), behind
   the same `FREYA_LEARNING=1` flag. TIER-1 to TIER-4 written with it.
3. Follower-route gate runs and the escalation column with NEW-CB (built in PR #46
   for hints and models; this adds the route). TIER-5, TIER-7.
4. The review pass with NEW-CA (step 6), first run by hand with the owner watching.
   TIER-6.
5. TIER-8 as a test on the policy and settings boundaries, kept green throughout.

## 8. Open questions for the owner

1. **May the author tier be a cloud route at turn time, or only at night?**
   Recommended: night only to start; at turn time only for material `CloudSettings`
   already lets leave the machine, and with the reply marked as cloud-written.
2. **The review sample rate.** Recommended: one in twenty, raised for a kind whose
   escalation rate is rising.
3. **Which request kinds the follower may not answer unaided at first.** Recommended:
   none, and let the measured escalation rates say which to add.
4. **Whether an escalation is visible in the reply.** Recommended: yes, as the
   author model's name on the reply, in line with the October decision that each
   model answers under its own name.
