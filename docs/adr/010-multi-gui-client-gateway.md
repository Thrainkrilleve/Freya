# ADR-010: Multi-GUI Client Gateway

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** No canonical GUI; custom simultaneous clients supported.

## What the code decided

The gateway (`gateway/server.py`) is one loopback HTTP server and one WebSocket per
client; every interactive request is a typed message, listed in CLAUDE.md and handled
in `Gateway._handle_message`. Several windows can be open at once; each joins with
the conversation so far (`subscribe_with_history`) and receives every later event
once. A second client exists: the paused Unreal window (`unreal_client.py`), which
pairs with a single-use key passed in its environment.

## Departs from the baseline

The page is the canonical GUI in practice: the protocol is shaped by it, and
documented only as a list of message types in CLAUDE.md, with no schema. The Unreal pairing is a prototype the code itself says is
not the paired, authenticated client the architecture calls for.

## Validation owed

The baseline's freeze gate: two independent clients through the documented gateway
without Core-specific GUI code. Needs a written message schema and ADR-017.
