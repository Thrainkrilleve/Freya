# ADR-018: External Effects and Reconciliation

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Idempotency scope, unknown outcome, no unsafe automatic retry.

## What the code decided

The only effect that leaves the machine is a cloud send, and it follows the baseline.
Local effects exist (`media.delete` and `media.export` on files, `image.generate`
writing pictures, `world.show` opening the browser) and are not classified yet:

- Every send has a serial, a brief hash, a ledger reservation and an owner's record
  line (`data/sent_briefs.jsonl`, separate from the audit log) whose outcome is
  `answered` or `outcome unknown`.
- No automatic retry: a send that may have left is recorded as unknown and counted at
  its full estimate; one that did not leave is released.
- Since #41 the ledger is settled even when the owner's record cannot be written, and
  a damaged record line no longer hides the rest.
- Every broker dispatch without an outcome is marked `unknown-outcome` at the next
  start.

## Validation owed

- Restore from a backup older than a send (the ledger and record restored to before
  it): the send is not repeated, but the allowance it used is forgotten.
- Effect classification for the next effectful capability, before it is built.
- The API route for Claude (ADR-008's decision) carries effect ids end to end.
