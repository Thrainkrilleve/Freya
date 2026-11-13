# ADR-008: Model Runtime Independence

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Ollama first, multiple runtimes supported.

## What the code decided

Callers name a role (`conversation`, `background`), not a model, through
`core/model_runtime.py`, which asks the broker for `llm.chat`, `llm.chat_stream` or
`llm.embed`. Ollama is the only local runtime; its client is pinned to `127.0.0.1`
and ignores `OLLAMA_HOST` and proxies. A second route exists for cloud work: the
Claude plan route (`providers/model/claude_plan.py`), reachable only through the
dispatcher.

## Owner decision recorded

2026-10-21: the Claude route that runs the owner's signed-in Claude Code program
stays for the alpha and is marked temporary; an API route is planned under ADR-018
after the authenticated gateway.

## Validation owed

- A second local runtime behind the same capabilities, to prove the contract.
- The CLI route's dependence on undocumented wording (the limit notice) is a known
  fragility; it is not a supported interface.
