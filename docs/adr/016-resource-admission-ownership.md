# ADR-016: Resource Admission Ownership

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Atomic reservations; router/manager/executor boundaries.

## What the code decided

- **Cloud usage:** the usage ledger (`core/usage/ledger.py`) is the admission
  authority for cloud allowances (a provider's own limit notice also holds sends, in
  memory, through `CloudDelegation.note_limit`): atomic reservations under `BEGIN IMMEDIATE`, settled
  with the provider's figure, marked unknown when the outcome is not known, and since
  #41 a reservation a dead send left open ages out after two hours. A route with no
  limit is refused; a zero cap cannot be overridden.
- **GPU:** no admission. The newcomer (an image, a large speech engine) unloads the
  Ollama models through the GPU manager; ComfyUI starts are serialised by
  `ControlPlane.image_start_lock`.

## Validation owed

- GPU reservations before load, with an estimate of what the load needs.
- Crash reconciliation for GPU allocations (a helper killed mid-load).
- A second process competing for the card (the avatar's web renderer, a game).
