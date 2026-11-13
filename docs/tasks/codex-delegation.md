# Work delegated to Codex

Owner: Justin. Written by Claude, 2026-10-16. Read `AGENTS.md` and `CLAUDE.md` first.

These tasks prepare the repository for phase 0 and phase 1 of the roadmap in
`docs/design/unified-intelligence-review.md` (a review, not an adopted design). They
are small, independent, and avoid the files Claude is working on.

## How to work a task

1. Take the lowest-numbered task marked **Ready** that nobody owns. Change its status
   line to `In progress — Codex` in your branch.
2. One branch and one PR per task, titled `[C-n] <task title>`.
3. Meet every acceptance criterion. Run the full suite before opening the PR.
4. In the PR description: what changed, how you tested it, anything you are unsure of,
   and any acceptance criterion you could not meet and why.
5. Set the task's status to `In review — PR #<number>`. The owner merges; Claude
   reviews.
6. If a task turns out to be wrong, larger than described, or in conflict with
   `CLAUDE.md` or the spec, stop and write that in the PR instead of widening the scope.

## Ownership right now

| Area | Owner | Codex may touch it? |
| --- | --- | --- |
| `.claude/`, `.github/workflows/` | Claude | Only as a task says |
| Phase 0 split of `main.py` and `assistant.py` into a turn engine and context builder | Claude, after C-3 merges | No, apart from C-3 |
| `freya-dev` MCP server (spec and gate lookup for agents) | Claude | No |
| Everything in the tasks below | Codex | Yes |

## Tasks

### C-1 · Review the guardrails PR

Status: Ready

Review the PR that adds `.claude/settings.json`, `.claude/hooks/freya_guard.py`,
`tests/test_dev_guard.py`, `.github/workflows/tests.yml`, `AGENTS.md`, this file and
`docs/design/unified-intelligence-review.md`.

Acceptance:
- Comment on any rule in `freya_guard.py` that would block legitimate work, or miss a
  rule from `CLAUDE.md` "Working rules" that a text check could catch.
- Check that `AGENTS.md` does not contradict `CLAUDE.md`.
- Post findings as PR review comments. Do not push to that branch.

### C-2 · Make the test suite pass on a clean Windows runner

Status: Ready (after the guardrails PR merges, so the CI workflow exists)

Some tests depend on what is installed on the developer's machine. For example, the
`plane` fixture in `tests/test_model_tools.py` registers the real ComfyUI provider, and
`image.generate` is only offered when ComfyUI is healthy or `can_start`
(`src/freya/control_plane.py`, `register_image_generation`). `can_start` is true only
when Comfy Desktop is installed (`src/freya/providers/image/comfyui.py`, `metadata`),
so `test_image_generation_is_offered_as_a_tool` and the two image tests after it fail
wherever Comfy Desktop is absent.

Acceptance:
- The `tests` workflow passes on `windows-latest` with no Ollama, ComfyUI, Comfy
  Desktop, GPU, LibreHardwareMonitor or microphone.
- Tests get these conditions from fixtures or fakes, never from the machine. Tests that
  can only run against real hardware are marked and skipped with a stated reason.
- No production code changes unless a test cannot be isolated without one; if so, keep
  it minimal and explain it in the PR.
- The suite still passes on the owner's workstation (say in the PR that the owner must
  confirm this).

### C-3 · Delete the unused `FreyaAssistant.respond()`

Status: Ready

`FreyaAssistant.respond()` (`src/freya/core/assistant.py`, from line 34) repeats about
90 lines of `respond_stream()` and has no caller in `src/` or `tests/`.

Acceptance:
- `respond()` is removed; `grep -rn "\.respond(" src tests` finds nothing.
- Any helper used only by `respond()` is removed too; anything else is untouched.
- No other change to `assistant.py`. Full suite passes.

### C-4 · One place that decides where `data/` is

Status: Ready

About ten modules each compute `data/` relative to the source tree, which is why Freya
only works from an editable install. Examples: `settings.py`, `window.py`,
`launcher.py`, `image/gallery.py`, `speech/helper.py`, `speech/store.py`,
`speech/references.py`, `core/persistence/control_store.py`,
`memory/memory_manager.py`. Find them all with
`grep -rn "\"data\"" src/freya`.

Acceptance:
- A new `src/freya/paths.py` with a `data_dir()` function: `FREYA_DATA_DIR` when set,
  otherwise exactly the path used today (the repository's `data/`).
- Every module uses it. The default resolved path is byte-for-byte the same as before,
  so the owner's existing data is found without moving anything.
- Existing environment overrides (for example `FREYA_SPEECH_ENVS`,
  `FREYA_AUDIT_CHECKPOINT_PATH`) keep working.
- Tests: the default equals the old path; the override moves every data path; no test
  writes outside its `tmp_path`.
- Never open, move or create files in the real `data/` folder while working.
- Add a line about `FREYA_DATA_DIR` to `CLAUDE.md`'s "Running locally" section.

### C-5 · Audit completeness test (UIA-14)

Status: Ready

Write a test-only PR proving every protected invocation leaves a complete audit trail.
Use the existing fakes in `tests/conftest.py` and a control plane on a temporary
database.

Acceptance:
- Through the real broker, run: a chat, a streamed chat consumed to the end, a streamed
  chat abandoned partway, an embedding, a model tool call, a denied call (no policy
  rule), and a call to an unavailable capability.
- For each allowed operation, assert one `decision`, one `dispatch` and one `outcome`
  row share its `operation_id`; for the denied call, a `decision` row with
  `decision = 'deny'` and no dispatch.
- Assert the audit chain verifies afterwards, and that no row contains prompt text or
  tool payloads (only the argument digest).
- If any case fails, do not fix production code in this PR: mark the test `xfail` with
  the reason and report it in the PR description.

### C-6 · Merge the two command paths

Status: Waiting — do not start until the owner moves it to Ready. It touches
`main.py`, which Claude is splitting in phase 0.

`main.py`'s loop and `window_commands.py` handle the same slash commands in separate
code. Plan: one command table both use.
