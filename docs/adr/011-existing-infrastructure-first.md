# ADR-011: Existing Infrastructure First

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Integrate/adapt/borrow before custom-building infrastructure.

## What the code decided

Followed throughout: Ollama for models, ComfyUI (Comfy Desktop's install) for images,
faster-whisper and Kokoro for speech, Chatterbox and Qwen3-TTS as installed engines,
Jellyfin for media, LibreHardwareMonitor for sensors, three.js and three-vrm for the
avatar, God's Eye View for world data, SQLite for storage, the owner's Claude Code for
cloud work, Blender for model conversion. Freya's own code is the governance around
them.

## Departs from the baseline

The governance itself is custom: the policy engine, audit chain, usage ledger and
dispatcher. That is the baseline's intent (Freya owns governance), but no existing
policy engine (OPA, Cedar) was evaluated and recorded.

## Validation owed

A one-paragraph evaluation of an off-the-shelf policy engine and audit log, recorded
here, before multi-user policy is written.
