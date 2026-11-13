# Shared agent setup

One set of development skills for every coding agent that works on Freya:
Claude Code, Codex, Antigravity, and any added later (Gemini CLI, Freya's own
local coding agent). These are instructions for agents building Freya. They
are not Freya's runtime skills, which are personal data under `data/skills/`
and never committed (NEW-BW).

| Path | What it is |
| --- | --- |
| `CLAUDE.md` | The project guide for every agent. |
| `AGENTS.md` | Hard rules and how work is shared, for agents other than Claude Code. |
| `agents.toml` | Which agents are on, what each reads, and its roles. |
| `.agents/skills/<name>/SKILL.md` | Shared skills: the maintained source. |
| `.claude/skills/` | Byte-exact copies for Claude Code. Never edit these. |
| `.agents/handoffs/` | Session handoffs, local only (ignored by git). |
| `scripts/sync_agents.py` | Writes the copies and pointers; `--check` reports drift. |

After editing a skill or `agents.toml`, run:

    python scripts/sync_agents.py

`tests/test_sync_agents.py` fails in CI if the copies are out of date.

## Skills

| Skill | Use it for |
| --- | --- |
| `plan` | A short plan built around deliverables and the checks that prove them. |
| `feature-handoff` | Passing work to another agent, keeping proposed, built, verified and accepted apart. |
| `session-handoff` | Saving a long session so a fresh chat or another agent can continue. |
| `delegate-work` | Choosing the agent, model and effort for a job by role. |
| `design-proposal` | A design document that follows the spec and label rules. |

## Adding an agent

Add a table to `agents.toml` with `enabled = true` and its roles. If it reads
`AGENTS.md` and `.agents/skills/` itself, that is all. If it reads a different
rules file, set `rules_file`; if it needs its own skills folder, set
`skills_dir`. Then run the sync script and commit the result. Decide how it is
started and sandboxed in `CLAUDE.md` first: what it reads is sent to its vendor.

The layout follows the workflow of
[Mr. Mak Workspace](https://github.com/witnesstodark/mr-mak-workspace) (MIT):
one maintained skill source with checked copies, and handoffs that keep
proposals, implementation, verification and acceptance separate. The skills
here were written for this repository.
