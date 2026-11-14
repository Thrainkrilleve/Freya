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
- Explicit forgetting is one transaction. `main.py` opens the memory store in
  protected mode (`memory/protection.py`). The deletion ledger lives outside
  `data/` (`deletion_ledger.ledger_path_for`). A dated backup copies it
  (`storage/backup.py`). `storage/restore.py` puts that backup back and does not
  replace a newer ledger with an older one, then applies the ledger, so a fact
  forgotten after the backup stays forgotten. If the newer ledger is gone, or
  both files are copied by hand, a forgetting made after the backup can come
  back. This is not full MEM-008.
- New backups are authenticated per-file encryption. AES-256-GCM, format
  version 1, in `storage/crypto.py`. Each file carries a header (magic,
  format version, key id, nonce) and that header, with the file name, is
  the associated data. A tampered file or a wrong key fails the
  authentication check. Plaintext is written only after the whole file
  checks, so a failure does not leave a partial plaintext file. One random
  data key seals every file in that backup. Key-encryption keys wrap it,
  and the wraps travel in `keys.bin`. The data key is not written in the clear.
- Key custody is the wrap and unwrap interface in `storage/custody.py`.
  Windows uses DPAPI (`CryptProtectData`), which is how Credential Manager
  protects a secret for the current user, and the wrapped bytes travel with
  the backup. Linux uses the Secret Service when a session offers it,
  otherwise the kernel keyring, which does not survive a reboot. macOS uses
  the Keychain and does not sync the item off the Mac. A missing store is a
  refusal. There is no silent fallback to plaintext, or to a key file beside
  the data. Callers then pass a passphrase or the recovery key, and a failed
  attempt does not try the next one.
- A passphrase is stretched with argon2id at the OWASP Password Storage
  Cheat Sheet's minimum Argon2id configuration: 19 MiB of memory, 2
  iterations, parallelism 1. The same numbers are used on every OS and are
  stored in the wrap. Any other set is refused before the stretch runs.
- A 256-bit recovery key is created with the data key, shown once on a
  terminal so it can be written down, and unwraps that data key on any OS.
  It is not logged and it is not stored in plaintext. Until it has been
  shown, a sealed copy is kept outside `data/` so it can still be printed;
  acknowledging it removes that copy.
- A backup made before encryption restores only when the caller passes the
  explicit migration flag (`allow_unencrypted`, or `--allow-unencrypted` on
  the command). New backups are encrypted. The proposed decision below is
  unchanged and is not approved. Live database encryption and key rotation
  are not built.

## Validation owed

- The restore drill is measured on CI scratch data (2026-11-13, Linux,
  `python devtools/restore_drill.py`, synthetic rows in a temporary directory).
  Three runs seeded control, usage, memory, media and tasks, backed them up,
  deleted them, restored them and checked integrity in 0.064 s, 0.066 s and
  0.046 s. One of those spent 0.010 s backing up, 0.003 s restoring and
  0.005 s checking. That is inside the 4 h RTO. The scratch backup was made
  in the same run; the gap from that backup to the end of the check was about
  0.01 s, inside the 24 h RPO. This is not a measurement of the owner's
  databases, and it is not an acceptance result. Later runs of
  `tests/test_restore_drill.py` check the same drill stays inside those
  targets. They do not rewrite this figure. Those three times were taken
  before backup encryption.
- Protected mode is on at startup. A fact forgotten after a backup stays
  forgotten when that backup is restored, covered by
  `test_a_fact_forgotten_after_a_backup_stays_forgotten_after_restore` with
  made-up text in a temporary directory. The owner's own backup has not been
  drilled. Full MEM-008 is still open.
- Backup encryption and key recovery. The scratch restore drill now seals
  each file, opens the backup with the recovery key from that run, and
  checks integrity, row counts, the audit chain, and the ledger rule.
  `tests/test_backup_encryption.py` covers a round trip, a changed byte, a
  wrong key, the header and format version, the passphrase, the recovery
  key, and that a plaintext backup is refused unless the migration flag is
  set. `tests/test_key_custody.py` covers a fake store for Windows DPAPI,
  Linux Secret Service, the Linux kernel keyring, and the macOS Keychain.
  On each CI runner the real store either round-trips a throwaway key or
  refuses; a refusal is the assertion, not a skipped test. The foundation
  workflow runs that file on ubuntu, windows, and macos. Measured on Linux
  scratch data on 2026-11-14 (`python devtools/restore_drill.py`, encrypted
  files, synthetic rows in a temporary directory): three runs finished in
  0.076 s, 0.085 s and 0.087 s. One of those spent 0.037 s backing up,
  0.005 s restoring and 0.005 s checking. The gap from that backup to the
  end of the check was 0.015 s. That is inside the 4 h RTO and the 24 h RPO.
  This is not a measurement of the owner's databases, and it is not an
  acceptance result. It does not replace the 2026-11-13 figures above.
  Live database encryption and key rotation remain open.
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
