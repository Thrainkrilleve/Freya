---
name: design-proposal
description: Write a Freya design proposal under docs/design/ that follows the spec rules (provisional labels, honest status, baseline references). Use whenever a change needs a design before code, or the owner asks for a proposal.
---

# Freya design proposal

Read `CLAUDE.md` ("Architecture documents" and "Working rules") first. The
baseline, *Freya Foundation v1.1*, stays authoritative; a proposal only
proposes.

## Before writing

1. Search `docs/design/`, `docs/adr/`, `docs/plans/` and the open pull requests
   for a proposal that already covers this. Extend or supersede it explicitly;
   do not write a parallel one.
2. Find the requirements it builds on (baseline IDs like CTX-, MEM-, SEC-,
   CAP-, ARCH-, and provisional NEW-x labels) and read them.
3. Take a free label range from `docs/labels.md` and add a row for your
   document in the same PR. If the register is not on your base branch yet,
   check the open PRs for labels already claimed, take the next free range
   after all of them, and say so in the PR description. Never reuse a label
   or renumber one.

## The document

`docs/design/<topic>.md`, starting with:

```markdown
# Design proposal: <what it does>

**Status: draft proposal, written <date> at the owner's request. Nothing here is
built.** It approves no ADR, adopts no pending amendment, and assigns no
requirement ID. Requirements carry the provisional labels **NEW-x to NEW-y**
(`docs/labels.md`).
```

Then, in this order:

1. Why: the problem, with evidence from the code (file paths) or a measurement.
2. What is kept, extended and replaced, naming the documents and labels affected.
3. Requirements, one heading each: `### NEW-x — <rule in plain words>`.
4. Acceptance tests, provisional IDs, each one checkable by code.
5. What it costs and what it does not do.
6. Build order, smallest safe step first.
7. Open questions for the owner, each with a recommended answer.

## Rules

- Never describe an ADR as approved, the architecture as frozen, or a test as
  passed unless the owner said so. Owner decisions are quoted with their date.
- Learned material informs and never authorizes; no proposal may let a model,
  a skill or a decider widen what a turn may do (NEW-BH, NEW-CL).
- Say plainly which parts depend on proposals that are not adopted.
- Keep it in the repository's plain style: short sentences, file paths in
  backticks, no marketing words.
