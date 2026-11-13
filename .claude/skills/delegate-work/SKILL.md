---
name: delegate-work
description: Choose which agent and model does a piece of work, and at what effort, using the roles in agents.toml. Use before handing work to another agent or starting a side task.
---

# Delegate work

The agents that may work in this repository are listed in `agents.toml`, each
with roles. Run `python scripts/sync_agents.py --list` to see them. Pick by
role, never by habit, and only from agents marked on.

## Roles

- **author**: writes plans, designs, skills and handoffs; reviews finished work.
  Use the most capable model available.
- **implement**: carries out a written plan or handoff.
- **review**: checks a change before a pull request is opened or merged. A
  reviewer from a different vendor than the author catches different mistakes.
- **second-opinion**: answers a read-only question from a different model.
- **local**: runs on the owner's machine with nothing sent to a vendor. Use it
  for anything private.

## The rule

The strongest model writes and reviews plans; lighter models follow them;
checks (tests, scripts, a measured result) decide whether work passed, never a
model's opinion of its own work; anything outside the plan goes back up.

So:

1. Author the plan or handoff first (`plan`, `feature-handoff`), with a
   **Stop and hand back when** list.
2. Give it to an `implement` agent with the files it may touch.
3. Run the checks yourself on what comes back. Do not take the report's word.
4. If the receiver stopped at an edge of the plan, or a check failed twice,
   take the work back to the author role. Do not ask the receiver to guess.
5. Have a `review` agent look before the PR, and fix the real findings.

## Effort

Set effort to `medium` for simple, well-specified work, `high` for substantial
implementation, and `xhigh` for research, design and hard debugging. Never go
below `medium`. Use the maximum only when the owner asks for it.

## Never delegate

Authorization, security rules, policy, anything that cannot be undone, merging,
and choices the owner has reserved. These are decided by fixed rules in code or
by the owner. Nothing private goes to an agent that sends text to a vendor
unless the owner has said it may.
