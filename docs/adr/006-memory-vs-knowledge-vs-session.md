# ADR-006: Memory vs Knowledge vs Session

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Separate semantics and storage/retrieval concerns.

## What the code decided

- **Memory:** `memory/memory_manager.py`, facts with embeddings and history in
  `data/freya_memory.db`; explicit forgetting removes a fact, its embedding and its
  history in one transaction.
- **Session:** one run of Freya is one conversation; every exchange is kept word for
  word (`memory/conversations.py`) with a summary and an FTS5 search, in the same
  database. The live context folds older exchanges into a summary past 40 messages.
- **Knowledge:** not built. The owner's reference library (kept on the PC under `Documents\Freya Library`;
  nothing in the repository refers to it) and the design documents are not indexed or
  retrievable by Freya.

## Departs from the baseline

Memory and session share one database file and one connection, so they are separate
in meaning, not in storage. Experience (proposed, `learning-loop.md`) would be a
fourth kind.

## Validation owed

- A knowledge store, or the decision that the file reader over an opened folder is
  the alpha's knowledge path.
- MEM-008 (restore-safe deletion): built but not switched on; see ADR-015.
