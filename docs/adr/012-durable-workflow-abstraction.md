# ADR-012: Durable Workflow Abstraction

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Long-running tasks use a durable workflow contract; Temporal leading candidate.

## What the code decided

Nothing durable: a turn is synchronous, bounded (three tool rounds, twelve tool
attempts, one delegation) and lost if Freya stops mid-turn. The only work that
survives a restart is reconciliation: dispatch rows without an outcome are marked
`unknown-outcome` at the next start (`SqliteAuditLog.reconcile_interrupted`, run at start), and usage
reservations left open age out (#41).

## Departs from the baseline

No workflow engine, Temporal or otherwise. The proposed learning loop (NEW-BG) is a
bounded in-turn loop, deliberately not a workflow.

## Validation owed

Decide whether the alpha needs durable workflows at all before goal orchestration
(baseline section 11) is started; if not, record "deferred" with the feature list it
blocks.
