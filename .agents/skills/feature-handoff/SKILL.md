---
name: feature-handoff
description: Write a precise handoff for another agent or person who will implement, continue or review a feature. Use whenever work moves between agents, including between models of different strength.
---

# Feature handoff

Write it from the files and test results on disk now, not from an earlier
progress message. Re-read anything another agent may have changed.

## Keep five states apart

Every handoff says which of these each part of the work is in. They are
different things and must never be merged into one word like "done":

1. **Baseline**: the branch and commit the work starts from.
2. **Proposed**: what was chosen (a design, a mockup, a plan). A chosen design
   is not a built feature.
3. **Implemented**: what the code now does, with file paths.
4. **Verified by an agent**: which checks were run and their actual results.
   Name the command. A check that was not run is listed as not run.
5. **Accepted by the owner**: only what the owner has said they accept.

## Contents

- Goal: the behaviour wanted, in one or two sentences.
- Baseline: branch, base commit, and the PR it belongs to if any.
- Starting points: the files to read first, and exact paths of the skills the
  receiver needs (`.agents/skills/<name>/SKILL.md`).
- Constraints: the rules from `AGENTS.md` that bite here, and the owner's
  decisions that must not be reopened.
- Ownership: files the receiver owns, files it must not touch because another
  agent is working in them, and any process that must not be stopped.
- Acceptance: the checks that will show the work is right, written so the
  receiver can run them itself.
- Open questions: what is still undecided. Leave unknown values unknown; do not
  turn an example's numbers into requirements.

## For a receiver that is a lighter model

Write the steps so they can be followed without judgment calls. Add a
**Stop and hand back when** list: the situations the plan does not cover. The
receiver stops and reports there instead of improvising.

## Rules

- No secrets, sign-in material or private data in a handoff.
- Do not give the owner work the receiver can do itself.
- For finished work, lead with the practical result, then the change, then the
  verification, and link the PR.
