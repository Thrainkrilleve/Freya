# ADR-005: Context Authority

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Agents/models receive authorized context; they do not own canonical memory.

## What the code decided

`kernel/context/authority.py` builds every reply's context: a budget (sources take
at most a third of the input), labelled items carrying source, trust and owner, a
report of what was included and omitted, and `fit_tools`, which clips each tool result
to 2,400 bytes and sheds old exchanges before the current request. Stored memories,
summaries and passages arrive through one `conversation.recall` broker call per turn
(seed `alpha-conversation-recall`); denied or unavailable recall supplies nothing and
is not reported as an empty search. The model never writes memory directly; memory
decisions are a separate background call.

## Validation owed

- Read-time trust downgrade (NEW-AV) and situation-tagged context (NEW-AT) are
  proposals, not built.
- A test that no tool result exceeds the clip: done for the file tools and world view
  (#41), `model.record.read` and `work.delegate`; the system tools,
  `conversation.recall`, `image.generate` and the media tools are not covered.
