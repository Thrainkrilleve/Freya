# Freya security-first local coding alpha — implementation plan

Status: DRAFT implementation plan; no security claims until tested. This branch is intentionally separate from main.

## Priority 1: reconcile architecture corrections

Source of truth: Foundation v1 (2026-10-07) plus independent Codex review when recovered. Do not mark v1.1 approved without the full review.

Mandatory acceptance gates:
1. Capability Broker is the sole privileged-action entry point; deny unknown or stale identities and revocations.
2. Tool permissions are evaluated per actor, client, workload, target and task; agent-provided text cannot confer authority.
3. Untrusted code and tools run outside the trusted Freya process, with explicit filesystem/network/resource boundaries.
4. Durable tasks have cancellation, timeouts, idempotency keys, state transitions and recovery tests.
5. GPU reservations are atomic; stale reservations are reclaimed; non-Freya workload priorities are honored.
6. Secrets never enter model context unless explicitly necessary and approved; audit logs omit secret material.
7. Federation never expands an administrator's authority across independent security domains.
8. Verify fail-closed behavior with unit/integration tests before enabling write/execute capabilities.

## Priority 2: narrow vertical slice

Request -> task record -> policy -> local model -> proposed patch -> independent verification -> approval -> commit.

Start with a read-only code-context tool and a patch-proposal artifact. Do not add shell execution or direct repository writes to the model yet. Keep task status and verification results separate from model assertions.

## Priority 3: local coding now

Prototype: `local_coding_agent.py` (development-only) can ask an installed Ollama model to propose a unified diff from explicitly selected source files. It does not apply patches or run generated code. Review patches before applying; validate with `git apply --check` and test in an isolated worktree or VM.

Suggested immediate setup:
- Confirm Ollama responds on loopback and list installed models with `ollama list`.
- Benchmark one local coding model on 3 small, testable issues before making it default.
- Use the patch-proposal prototype for routine, low-risk changes; retain Codex/Claude for high-risk architecture and security review.
- Next implement a dedicated sandbox runner and verified patch-application pipeline behind Freya's capability broker, not inside the chat runtime.

## Do not do

Do not automatically execute generated commands, disable auditing, merge agent changes to main, trust repository prompt injections, or claim unverified tests passed.
