# Freya supervised developer alpha

Status: proposal-only, not installed or activated. Base: main at 44da490.
Related: #19 local coding alpha, #21 reliability hardening, #22 foundation CI.

## Initial permission profile

- READ_SOURCE: allowed only for explicitly selected, non-sensitive repository files.
- PROPOSE_PATCH: allowed; output is an artifact, never automatically applied.
- VALIDATE_PATCH: allowed only in an isolated, disposable checkout after a security review of the patch.
- RUN_TESTS: disabled by default. When enabled, use a sandbox without production secrets, user data, network access, or host write access.
- APPLY_PATCH: disabled; requires a separate scoped approval, clean worktree, and isolated branch.
- CREATE_PR: disabled until identity, approval, audit, and provenance contracts are integrated.
- MERGE, DEPLOY, CHANGE_POLICY, EDIT_SECRETS: forbidden for this profile.

This document is an implementation target, not a claim that all controls are enforced by the current runtime.

## Current reusable prototype

PR #19 includes devtools/local_coding_agent.py, selected_source(), a loopback-only Ollama transport, and patch proposal validation. PR #21 adds reliability and adversarial tests. Keep this feature on its own branch until the broker, approval and audit boundaries are verified. Do not copy all of #19 or #21 into main.

## Trial task

Task: propose a non-functional readability improvement to one selected test helper, with no API changes and no edits to Core, policy, persistence, audit, or deployment code.

Inputs: one explicit allowlisted file, a clean checkout, local model, task text, and a time/VRAM budget.
Output: unified diff plus model ID, code commit, file digest, elapsed time, validation results and an explanation of unresolved concerns.

## Acceptance tests

1. Reject absolute paths, traversal, symlinks, excluded directories, secrets and oversized inputs.
2. Reject patch paths outside the allowlist, binary patches, renames and malformed diffs.
3. Never apply a patch during proposal generation.
4. Abort or deny if permissions are revoked; account for uncertain provider cancellation.
5. Log denied, attempted, succeeded and unknown outcomes without leaking file contents.
6. Run any patch-check and test commands only in a disposable sandbox with no production secrets.
7. Compare proposed patch to baseline via correctness, tests, human review, duration and resource usage.
8. Security-sensitive modules require independent reviewer and explicit owner authorization.

## Next engineering work

- Port the proposal-only prototype and its adversarial tests to a dedicated branch.
- Implement a typed invocation envelope with actor, task, target, selected paths, effects, arguments digest, approval and expiry.
- Integrate broker-mediated authorization and durable task journal; verify recovery and idempotency.
- Add sandbox isolation provider and independent patch verifier before enabling RUN_TESTS.
- Add benchmark ledger and structured experience records only after verified trial outcomes.

No changes to a live Freya installation, merge or deployment are authorized by this plan.
