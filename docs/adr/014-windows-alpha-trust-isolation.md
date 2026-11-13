# ADR-014: Windows Alpha Trust and Isolation

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Dedicated service identity; replaceable IsolationProvider and preferred Hyper-V alpha profile; unsupported or unvalidated untrusted execution disabled.

## What the code decided

- Freya runs as the owner's own Windows user; there is no service identity and no
  ACL on the control database (an owner action, not taken).
- No isolation provider exists. `freya.sandbox` is the contract; the proposal
  (`docs/design/code-sandbox.md`, NEW-AM to NEW-AR) is a Linux container under WSL2,
  because this PC runs Windows 11 Home, which has no Hyper-V.
- Untrusted execution is disabled, as the baseline requires when isolation is
  unavailable.
- Programs Freya starts get an environment with credential-named variables removed
  (`child_environment.py`).

## Departs from the baseline

The agreed decision names Hyper-V as the preferred profile. The hardware cannot run
it, so this ADR must either record WSL2 as the alpha profile (and what it does not
hold: the proposal describes it as holding mistakes, not a determined attack) or
keep execution disabled until a Pro edition is installed.

## Validation owed

Everything in the baseline's ADR-014 list. The first decision is the owner's: WSL2
profile, or execution stays off.
