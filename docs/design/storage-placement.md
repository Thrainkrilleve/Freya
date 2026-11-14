# Where Freya's data is kept

Status: a first step, written 2026-10-09. It works out a plan and reports it. It
moves nothing, and Freya does not yet place new data by it.

## The owner's rule

All long-term data stays on this machine, and where each kind lives follows from what
the drive is good at.

| Drive | Good at | Suited to |
|---|---|---|
| SSD | Fast lookups and frequent small writes | Databases and their indexes, settings, anything read during a turn, model files loaded often |
| Hard disk | Large capacity, long sequential reads and writes | Generated images and clips, imported media, avatar models, logs, backups |

A backup belongs on a different drive from what it protects, whatever the drive types.

## What is built

`freya.storage.placement` lists every store under `data/` with what it needs (fast,
bulk, or a copy kept elsewhere), asks Windows which drive is an SSD and which a hard
disk, and says where each store would best live. Placement goes by drive type, not
drive letter. Where Windows does not say what a drive is, it gives no advice.

```powershell
python -m freya.storage.placement
```

It reads file sizes only, never contents. A test fails if the code starts writing a
store under `data/` that the list does not name.

## What it found on this workstation (2026-10-09)

One NVMe SSD (C:) and one hard disk (D:). Everything is on the SSD.

- The databases, settings, voice models (about 20 GB) and voice engine environments
  (about 10 GB) are where they should be.
- The data that would suit the hard disk (avatar models, converted-avatar leftovers,
  images, media, voice benchmarks, logs) comes to under 300 MB in total. Moving it
  would free almost nothing today. It matters later, as images, media and clips grow.
- **The backups are on the same drive as the databases they protect.** This is the
  one finding worth acting on now: if the SSD fails, the memory database and its
  backup go together.

The largest files Freya uses are the Ollama models, which Ollama keeps in its own
folder outside `data/`. They are loaded often and belong on the SSD; they are not part
of this plan.

## Backups

Looking at the backups showed that there were none to speak of: the `backups` folder
held one copy of the memory database, made once when the conversation tables were
added. Nothing copied the databases regularly, against the baseline's target of losing
at most a day's data.

`freya.storage.backup` makes a dated backup on another drive:

```powershell
python -m freya.storage.backup            # make one, on the other drive
python -m freya.storage.backup --check D:\FreyaBackups\2026-10-09_170618
```

- It copies the memory, control, usage, tasks and media databases with SQLite's own
  backup, which gives a consistent copy even while Freya is running, and checks each
  copy. It also copies the deletion ledger, which lives outside `data/`.
- It copies the small settings files, including the cloud settings, the screening
  lists and the record of sent briefs.
- It refuses a target on the same drive as the data.
- Each run makes a new folder. The manifest lists sizes and checksums of the
  plaintext and is itself encrypted. It never moves or deletes anything, so old
  backups are not cleared away; that is not built.
- It does not copy images, media files, voice clips or models.

## Encryption and restore

New backups are encrypted, one data key for the folder, AES-256-GCM on each file.
The header (magic, format version, key id, nonce) is bound as associated data, so a
changed header fails the same check as a changed file. The data key is not written
down. Wraps of it travel in `keys.bin`.

The machine's keystore holds the key-encryption key when it has one: DPAPI on
Windows, the Secret Service on Linux, and the Keychain on macOS. The Keychain item
is not synced off that Mac. Linux does not fall back to the kernel keyring. That
store is session memory and is gone after a reboot, so it is used only when
`FREYA_BACKUP_KERNEL_KEYRING` is exactly `1`. Without Secret Service, and without
that setting, the command asks for a passphrase. Nothing here falls back to a
plaintext backup or to a key file beside the data.

The passphrase is stretched with argon2id at 256 MiB, 3 iterations and parallelism
4, the same on every OS. That is stronger than RFC 9106's second recommended
profile (64 MiB, 3 iterations, parallelism 4). The numbers are stored in the wrap,
and an unlock uses the numbers it finds there, so they can be raised later. A wrap
below that floor is refused. On the Linux host that timed the scratch drill, one
stretch took about a quarter of a second; a slower machine should still finish in
a few seconds. The passphrase is typed at the prompt. It is not an argument and it
is not read from the environment.

The first backup also makes a recovery key and prints it once on a terminal, to be
written down and kept off the machine. That key unwraps the data key on any OS.
`--show-recovery-key` prints it if it has not been shown yet, and then it is not
shown again. It is not stored in the clear.

```text
python -m freya.storage.backup
python -m freya.storage.backup --check <folder>
python -m freya.storage.backup --show-recovery-key
python -m freya.storage.restore --from <folder>
```

A backup made before encryption is not the default. Restoring one is an explicit
migration, and it is the only use of the flag:

```text
python -m freya.storage.restore --from <folder> --allow-unencrypted
```

`python devtools/restore_drill.py` times a scratch backup that is encrypted, then
deleted, restored and checked. It does not read the owner's data directory and it
does not use the machine's keystore. Live database encryption and key rotation are
not part of this step.

The first backup was made on 2026-10-09 to `D:\FreyaBackups`, at the owner's request.
It is personal data. That copy predates encryption. A backup made now is encrypted
even when the drive it sits on is not.

Freya now makes a backup as she closes (owner decision, 2026-10-09), unless one was
made in the last hour. It runs beside the rest of closing and is given 20 seconds, so
a slow or missing drive cannot keep her from closing; if it cannot be made, she closes
without one and says nothing. `FREYA_BACKUP=0` turns it off and `FREYA_BACKUP_DIR`
names another place.

Not built: removing old backups. A Freya left running for days makes no backup in
that time. Restoring, checking a backup, and saying so when a close-time backup
failed, are built. The owner's own databases have not been drilled.

## Keeping bulk data off the SSD

The owner asked (2026-10-09) to keep on the SSD only what needs to be there.

`freya.storage.locations` gives each store its place. Six stores that hold only files
and are large or rarely read can be kept somewhere else by one setting
(`data/storage_locations.json`): generated images, voice clips, the recordings voices
were made from, voice benchmarks, avatar models, and avatar conversion work. Databases
and settings always stay in `data/`. The media library stays too, because it keeps a
database beside its files.

```powershell
python -m freya.storage.move --to D:\FreyaData
```

The move copies each store, compares every file with its original by size and
checksum, and only then switches Freya over. It refuses while Freya is running, on the
same drive, onto a folder that already holds something, and a second time. It deletes
nothing: the originals stay in `data/`, each with a note saying it is a stale copy,
for the owner to remove.

If the drive holding the bulk data is missing, Freya does not quietly start a second,
empty set of images in `data/`: she fails to find them, which is the safer mistake.

Logs stay on the SSD; they are a few kilobytes. `avatar_previews` and `avatar_build`
are leftovers that no current code writes, and are not moved.

## Not built

- Moving the databases, the media library, or the voice models and environments
  (30 GB, used often; they belong on the SSD).
- Moving the bulk data back, other than by editing the setting by hand.
