# ADR-004: Security Domains and Federated Sovereignty

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Federation authenticates/cooperates without merging roots of authority.

## What the code decided

Nothing: there is one installation, one principal (`local-owner`) and no federation.
The design is `docs/design/freya-to-freya-cooperation.md` (NEW-BM to NEW-BV), not
built.

## What the code already constrains

Memories and conversation records never leave by kind (`work.delegate`
offers only the owner's own recent words and PC-reading tool results), which is the
local half of sovereignty. The usage ledger and the dispatcher are per installation.

## Validation owed

Everything, and not before a second node exists. Recording it now only so the
ADR register has no gaps.
