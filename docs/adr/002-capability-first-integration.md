# ADR-002: Capability-First Integration

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** No direct cross-module implementation coupling.

## What the code decided

A feature reaches another through a named capability invoked on `CapabilityBroker`
(`core/capabilities/broker.py`): policy is checked, availability is checked, a
dispatch row is written, the handler runs, the outcome is written. The model reaches
tools only through `ModelToolHost` (`tools/model_tools.py`), which offers only
capabilities marked `model_tool` and never destructive ones. Conversation recall,
cloud delegation, the model record, file reading and world view all follow this.

## Departs from the baseline

`assistant.py`, `conversation_memory.py` and the evaluation runner import
`freya.memory` directly; `main.py` imports most packages. The Media tab's requests
are broker calls (`media.*`, granted by `python -m freya.media.activate`, not by a
seed).

The voice path is brokered (`voice.wake`, `voice.turn`, `voice.speak`,
`voice.studio` on `voice-local`, seed `alpha-voice`). A spoken transcript is
authorized before the turn, and speech and the Voice tab run only inside the
capability the broker allowed. The seed names those four and no tool. A tool a
spoken turn reaches is the same `ModelToolHost` call a typed turn uses, so it is
denied unless that tool's own rule allows it. Whisper, Kokoro and the studio
engines still run in this process; the broker is the gate in front of them, not
a new provider for the audio itself.

## Validation owed

- An import-graph test listing the allowed direct imports, so new coupling fails.
- Multi-user identity for the voice path. The capabilities and the audit entries
  exist; the principal is still the fixed `local-owner`.
