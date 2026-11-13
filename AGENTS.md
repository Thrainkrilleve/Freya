# Freya — instructions for Codex, Antigravity and other coding agents

Read `CLAUDE.md` in full before changing anything. Despite the name, it is the project
guide for every agent: architecture documents, repository layout, known state, and the
working rules. Everything in it applies to you as it does to Claude. This file adds
what is specific to helpers other than Claude Code (OpenAI's Codex, Google's
Antigravity `agy`), and how work is shared between agents.

## Hard rules

Claude Code has hooks (`.claude/hooks/freya_guard.py`) that block the mistakes below.
You do not run those hooks, so you must follow these rules yourself:

- Never open, list, read, print, copy, edit or delete anything under `data/`, or any
  `*.db` file. `data/` holds the owner's memories, conversations, voice recordings,
  and models under terms that forbid passing them on; `data/freya_memory.db` is the
  owner's personal memory and `data/freya_control.db` the policy table and audit log.
  What you read is sent to your vendor (OpenAI or Google). If a task seems to need a
  file from `data/`, stop and say so instead.
- Never read `.env` files, credentials, or anything under `.venv/`.
- Never change an existing entry of `MIGRATIONS` in
  `src/freya/core/persistence/control_store.py`. Append a new entry instead.
- Never hand-edit `uv.lock`. Change `pyproject.toml`, run `uv lock`, commit both.
- Never modify files under `src/freya/gateway/static/vendor/`; they are checksummed.
- Never commit to or push to `main`, and never force-push. Work on a branch. Do not
  push, open or merge pull requests, or change git remotes unless a task says so; the
  owner merges.
- No secrets, keys or tokens anywhere in code, config, commits or logs.
- Do not run generated or untrusted code with access to Freya's Core storage or
  credentials (ARCH-017).
- Owner actions are not yours: installing system software, enabling Windows features
  (Hyper-V), changing ACLs or service accounts, changing policy rows, adopting spec
  amendments, approving ADRs.

## Spec rules

- `docs/specs/Freya_Foundation_v1.1_baseline.docx` is authoritative. Never renumber a
  requirement or reuse an ID. New proposals use provisional labels (NEW-x, UIA-x).
- Do not describe any ADR as approved, the architecture as frozen, or any acceptance
  test as passed unless the owner has said so.
- `docs/design/unified-intelligence-review.md` is a review, not an adopted design.

## Working

- Python 3.12, `uv sync`, then `python -m pytest` from the repository root, using
  `.venv`. The suite must pass before you open or update a PR. CI runs it on Windows
  (`.github/workflows/tests.yml`). Antigravity is started without permission to run
  commands: do not try, say in your report that the tests were not run, and Claude
  will run them.
- Keep the existing code style and comment density, LF line endings, and absolute
  imports (`from freya.core.llm import ...`).
- Keep the two command paths in step (`main.py` loop and `window_commands.py`) until
  they are merged.
- Add tests with every behaviour change. A refactor must not change behaviour and must
  leave the existing tests passing unmodified, except for import paths.
- Small PRs: one task per PR, named in the PR title (for example `[C-2] ...`).

## Sharing work with Claude

- Tasks delegated to you are in `docs/tasks/codex-delegation.md`. Take them in order
  unless the owner says otherwise. Do not start a task another agent owns.
- Do the task you were given and nothing wider. If you see something else worth
  doing, report it; do not do it.
- When you are given a folder to work in, stay inside it. Claude may be editing
  another checkout of the same repository at the same time.
- `.claude/` belongs to Claude Code's configuration. Do not change it except as a task
  says.
- End with a short report: what you changed (files), what you ran and its result,
  and what you did not verify. Say plainly when a test failed or was not run.
- In a review, give each finding as file and line, what goes wrong, and the input or
  state that makes it go wrong. Leave out style remarks.
- Cross-review: when you finish a PR, say in its description what you changed, how you
  tested it, and what you are unsure of. Claude reviews Codex PRs and Codex reviews
  Claude PRs; the owner merges.
- If a task conflicts with `CLAUDE.md`, the spec, or another open PR, stop and say so
  in the PR or task instead of guessing.

To hand Claude a task or ask it for a review, run it without a window, from the
folder it should work in, with the task on stdin:

    "<task>" | claude -p --permission-mode plan      (read and advise only)
    "<task>" | claude -p --permission-mode acceptEdits   (may edit files there)

Claude needs the network and its own settings folder, which your sandbox blocks, so
ask the owner to approve running that one command outside the sandbox. Never read or
write `~/.claude` yourself. Do not start Claude from a task that Claude gave you.
This is for Codex; Antigravity cannot run commands here.
