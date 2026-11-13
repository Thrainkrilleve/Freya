# ADR-001: Freya Core Boundary

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Core owns governance/contracts; providers own implementations.

## What the code decided

Core is `src/freya/core/` plus `kernel/` and `control_plane.py`: the capability and
provider registries, the broker, the deny-by-default policy engine, the hash-chained
audit log, the usage ledger and the persistence contract. Everything that does work
is a provider registered with it: Ollama (`providers/model/ollama.py`), ComfyUI
(`providers/image/comfyui.py`), the system and file tools, world view, the avatar.
Core does not import the spatial avatar, and the overlay does not import Core; tests
enforce both directions.

## Departs from the baseline

The boundary is a package convention inside one process, not a process or a trust
boundary: a provider runs with Core's permissions and can reach Core's databases.
Several paths still call their backends directly, not through a provider: Whisper,
Kokoro, image generation itself, the memory store, the GPU manager's Ollama unload,
the startup warm-up and the voice studio (CLAUDE.md, "Known state").

## Validation owed

- A test that `freya.core` imports nothing outside `freya.core` and `freya.kernel`
  (the avatar test is the pattern).
- Each direct backend call above routed through a provider, or listed here as an
  accepted exception with an expiry.
