# ADR-015: Persistence and Recovery

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Replaceable persistence contracts; SQLite and canonical-file alpha profile; journal, deletion ledger, recovery/migration guarantees; configurable operational targets pending validation.

## What the code decided

- Four SQLite databases. Control (policy and audit) and usage go through
  `ControlStore` (`core/persistence/control_store.py`): shipped migrations are never
  edited, and the shared connection's rows are read under its lock. Memory
  (`MemoryManager`) and media (`media/store.py`, `freya_media.db`) open SQLite
  directly.
- Backup on close, at most once an hour, to a dated folder on another drive, using
  SQLite's backup API; it refuses the same drive and never deletes
  (`storage/backup.py`).
- Bulk stores can be moved to another drive by copy, verify, switch
  (`storage/move.py`), and `storage/placement.py` reports where each store should
  live. All under one `data_dir()` once C-4 (#38) merges.
- Explicit forgetting is one transaction. A restore-safe deletion ledger (tombstones
  replayed on open, `memory/deletion_ledger.py`) is built and tested but not switched
  on: `main.py` does not open the memory store in protected mode.
- No encryption at rest.

## Validation owed

- The 24 h RPO / 4 h RTO targets have never been measured. A restore drill: delete a
  scratch `data/`, restore from the last backup, time it.
- Restore quarantine: with the ledger off, a backup taken before a forgetting
  restores the forgotten fact. Switching protected mode on is the first step toward
  MEM-008; the owner's backup then needs the ledger too.
- Backup encryption and key recovery.
- SQLite durability settings (each audited call costs 30 to 60 ms in synchronous
  writes; provisional).
