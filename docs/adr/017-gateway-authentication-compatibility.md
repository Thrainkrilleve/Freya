# ADR-017: Gateway Authentication and Compatibility

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Transport-independent ClientGateway; loopback HTTP/WebSocket alpha profile; user authentication, pairing, browser/native session protection, event scope and negotiation.

## What the code decided

A first step only: the gateway binds `127.0.0.1`; a request needs a matching Host
header and a session cookie issued once for a single-use key in the address Freya
opens; the WebSocket needs this server as its Origin. Keys and cookies are compared in
constant time and any text is accepted (#41). There is no user authentication, no
pairing beyond the Unreal prototype, no per-client scope, and every window's turns
are `local-owner`.

## Owner decision recorded

2026-10-21: this is the next architecture piece after the reconciliation and the
learning loop; no tool with new reach is added before it.

## Acceptance criteria already proposed

NEW-AZ (reachable is not authorized: one guard on every route, a test that fails on
an unguarded one), NEW-BA (Core issues identity) and NEW-BC (validated inputs), from
`openrig-lessons.md`.

## Validation owed

The whole decision.
