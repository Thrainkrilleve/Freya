---
name: plan
description: Turn a broad request into a short plan built around deliverables, each with the check that shows it works. Use before any task that spans several files, sessions or agents.
---

# Plan useful work

1. Read `AGENTS.md`, the files the request names, and any earlier handoff in
   `.agents/handoffs/` on the same topic. Use what is on disk now, not what an
   earlier chat said was done.
2. State the outcome in one sentence, in the owner's words where possible.
3. Say what already exists and what has to change. Search the repository for an
   existing design, decision, script or skill before proposing a new one.
4. Write the plan as deliverables, not tool calls. For each one give:
   - the output (a file, a PR, a passing test, a decision for the owner);
   - the observation that will show it works (a test, a command and its result,
     a screenshot of the running thing);
   - who does it, by role from `agents.toml` (see the `delegate-work` skill).
5. Mark what is required and what is optional. Keep the owner's scope, budget,
   chosen tools and any "ask before" rules exactly as given.
6. Ask only questions whose answer changes the plan. Carry on with the
   independent parts while an answer is pending.

Keep the plan where the work lives: the PR description, a design document, or
a handoff file. Update it when evidence changes the approach, and say what
changed. Never call a step done until its check has been run and passed.

To pass a step to another agent or a later session, use `feature-handoff` or
`session-handoff`.
