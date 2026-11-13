# ADR-012: Durable Workflow Abstraction

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Long-running tasks use a durable workflow contract; Temporal leading candidate.

## What the code decided

A turn is still synchronous, bounded (three tool rounds, twelve tool
attempts, one delegation) and lost if Freya stops mid-turn. Dispatch rows
without an outcome are marked `unknown-outcome` at the next start
(`SqliteAuditLog.reconcile_interrupted`), and usage reservations left open
age out.

A separate journal, `freya.tasks`, stores one local task at a time through
the states created, authorized, running, verifying, and done or failed, plus
cancelled. It is off unless `FREYA_TASKS=1`, it lives in `freya_tasks.db`
beside the control database, and every transition is a broker call
(`task.create` through `task.resume`) with no policy seed. Authorization is
`task.authorize`. Before a task can be done, a check function registered in
code runs; the journal does not ask a model. A task left running or
verifying is marked interrupted on the next process and can be resumed or
cancelled. This is the bounded sequential journal in the baseline's ORCH-008.
It is not Temporal, it does not schedule work onto another machine, and
`code.run` stays unregistered.

## Departs from the baseline

No workflow engine, Temporal or otherwise. The task journal above is one
table and fixed transitions, not goal orchestration. The proposed learning
loop (NEW-BG) is a bounded in-turn loop, deliberately not a workflow.

## Validation owed

Decide whether the alpha needs a workflow engine before goal orchestration
(baseline section 11) is started. The local journal does not decide that.
It is off by default, and turning it on grants nothing until a rule allows
each transition.
