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
- Backup encryption and key recovery. The proposed decision below is not
  approved, and nothing in it is built.
- SQLite durability settings (each audited call costs 30 to 60 ms in synchronous
  writes; provisional).

## Proposed decision: encryption at rest and key custody

**Proposed by Thrain, 2026-11-13. Not approved.** This section does not change
the status above, does not record what the code does, and passes no acceptance
test. Nothing here is built. SQLCipher is not a dependency. `uv.lock` is
unchanged. Requirements carry provisional labels **NEW-EB to NEW-EG**
(`docs/labels.md`). Final IDs wait for reconciliation.

The format is the same on every operating system, so a backup made on Windows
restores on Linux or macOS.

- **NEW-EB — One format everywhere.** Live databases use SQLCipher, or an
  equivalent page-level SQLite encryption. Backups use authenticated per-file
  encryption. One random data key encrypts everything. Key-encryption keys wrap
  that data key. The wrapped keys travel with the backup; the data key does not
  travel in the clear.
- **NEW-EC — One KeyCustody interface.** Callers ask that interface to wrap,
  unwrap, and refuse. One backend per operating system:
  - Windows: DPAPI / Credential Manager
  - Linux: Secret Service keyring, or the kernel keyring for a headless server
  - macOS: Keychain
  - Optional, where the machine has one: a TPM-sealed key
  - Optional: a YubiKey as an additional unlock factor

  A backend that is missing is not swapped for another in silence.
- **NEW-ED — No silent fallback.** With no keystore (a headless box is the
  example), encrypted stores refuse to open until a passphrase unlock is
  provided. The passphrase is stretched with argon2id. The data key is never
  written as a plain file beside the data.
- **NEW-EE — A printed recovery key.** An offline printed recovery key wraps
  the data key and works on any operating system. It is paper, not a file next
  to the databases.
- **NEW-EF — The interface is tested on three runners.** CI tests the
  KeyCustody interface on Windows, Linux, and macOS runners. A backend that
  the runner does not have is the refusal in NEW-ED, not a skip that counts as
  a pass.
- **NEW-EG — Rollout order.** Restore drill and the protected-mode deletion
  ledger first, then backup encryption, then live database encryption, then
  key rotation and revocation. A later step does not start by declaring an
  earlier one done.

### Threats this covers

A stolen disk, and a stolen or copied backup drive. Someone who has the files
and not a key-encryption key, the passphrase, or the printed recovery key
cannot read the databases or the backup.

### Threats this does not cover

A compromised running process, or a compromised user session. The process
that Freya is holds the unwrapped data key. The logged-in session can ask the
operating system's keystore to unwrap it. Encryption at rest does not stop
either of those. It also does not cover a person who has the files and the
passphrase or the printed key, or copies that were made before encryption
(the pre-protection memory copy, exports, logs, and whatever the model has
already been shown).

### Open questions

- Which SQLCipher build, or which equivalent, is pinned on each operating
  system, and who checks that the page format matches. Recommendation: one
  pinned build per OS, same page size and KDF, decided in the implementation
  change. Not this proposal.
- argon2id memory, time, and parallelism. Recommendation: one set, written
  down in that change, identical on every OS. OWASP's interactive values are
  the starting point.
- Where the owner keeps the printed recovery key, and how a lost key is
  revoked without destroying the only remaining copy of the data key.
  Recommendation: paper, off the machine. Revocation is the last rollout
  step and needs a second wrapping key already in place, or it locks the
  data.
- Whether TPM and YubiKey stay optional. This proposal says optional.
  Requiring either would exclude machines that have neither.
- A headless Linux server has no Secret Service session. Recommendation:
  the kernel keyring when it is available, otherwise the passphrase in
  NEW-ED. Never a key file beside the data.
- Where a passphrase is typed. Recommendation: the machine the owner is at.
  It is not stored, and it is not taken from a model or from a spoken turn.

### Acceptance tests each step needs

None of these is passed by this proposal.

1. Restore drill and protected-mode deletion ledger. A scratch data directory
   holding control, usage, memory, media, and tasks is backed up, deleted,
   restored, and checked (`PRAGMA integrity_check`, row counts, audit hash
   chain). The run is timed against the 24 h RPO and 4 h RTO. A fact forgotten
   after the backup stays forgotten after the restore, and the backup carries
   the deletion ledger. The tests run on Linux and Windows with no
   Windows-only path. Measuring scratch data does not validate the owner's
   databases.
2. Backup encryption. A backup file does not parse as SQLite or as JSON
   without the key. A wrong key fails the authentication check. A backup
   written on one OS opens on another with the same data key. The data key
   is not present in the backup folder, in the manifest, or in the log.
3. Live database encryption. The database file does not open as ordinary
   SQLite without the key. Freya opens it through KeyCustody. No keystore
   means the open is refused until the passphrase unlock, and no plain key
   file appears beside the data. A wrong passphrase does not unlock.
4. Key rotation and revocation, and NEW-EF. After rotation, old backups
   still open by unwrapping with a key that has not been revoked. A revoked
   key-encryption key does not unwrap. The KeyCustody tests run on the
   Windows, Linux, and macOS CI runners.
