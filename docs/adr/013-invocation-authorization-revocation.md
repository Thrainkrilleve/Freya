# ADR-013: Invocation Authorization and Revocation

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Argument/target-bound dispatch, grant freshness, revocation checkpoints.

## What the code decided

- **Policy:** deny by default, rules in SQLite (`core/policy/engine.py`), applied
  once per named seed so a removed rule stays removed. Seeds today:
  `alpha-system-tools`, `alpha-system-hardware`, `alpha-system-sensors`,
  `alpha-model-runtime`, `alpha-model-background`, `alpha-conversation-recall`,
  `alpha-image-generate`, `alpha-file-read`, `alpha-world-view`, `alpha-avatar`,
  `alpha-avatar-library`, `alpha-avatar-creator`, `alpha-cloud-settings`,
  `alpha-cloud-tighten`, `alpha-model-record`, `alpha-work-delegate`.
- **Identity:** one fixed principal, `local-owner`, for everything.
- **Binding:** each dispatch row records the capability, the policy revision and
  grant epoch it was decided under, and a digest of the arguments, never the
  arguments. A decision's revision now matches the rules it read (#43).
- **Cloud sends:** `cloud.anthropic_plan.ask` is in no seed; switching the provider
  on in the Cloud tab is the owner's act that adds its rule. An authorization is
  serial-keyed, single-use, bound to the brief's hash, two minutes long, and void if
  the owner's settings changed.
- **Standing rules:** "Always allow" rules today are keyed on provider, task kind and
  ask-first material (`delegation/material.py`), and the task kind is the model's own
  claim. **Owner decision 2026-10-21** (reconciliation plan; comment on #41): drop the
  task kind, provider and material only. Not yet built; it goes in with the
  learning-loop work.

## Validation owed

- Authenticated identity derivation: waits on ADR-017.
- Revocation of a turn already running (a rule removed mid-turn takes effect at the
  next dispatch, not the current one).
- Approval replay prevention is tested for cloud sends only.
