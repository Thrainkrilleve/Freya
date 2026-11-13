# Reliability hardening: findings, evidence, and residual risk

Branch `work/claude-reliability-hardening`, based on `work/security-local-coding-alpha`
at `d76cd21`. Status: draft for review. Nothing here claims a test passed unless it
was run; results are listed with the platform they ran on.

## 1. Audit subsystem

### F-1 Security audit records were dropped silently (confirmed, fixed)

`CapabilityBroker._record_quietly()` caught every exception from `audit.record()` and
discarded it. It wrote denials, unavailability decisions, and outcomes. Evidence:
`broker.py` lines 156, 173, 227, and 251 at `d76cd21`; with the audit store refusing
writes, an unauthorized request returned `denied` with nothing in the log, the
result, or any output, and the next allowed request ran normally.

Execution was never at risk: the pre-execution `dispatch` record was already
required. What was lost was evidence.

Classification adopted:

| Record | When | On write failure |
| --- | --- | --- |
| `dispatch` | before the capability runs | capability does not run (unchanged) |
| `decision` (denied / unavailable) | after deciding not to run | logged, `BrokerResult.audit_recorded=False`, queued |
| `outcome` | after the capability ran | logged, `audit_recorded=False`, queued |
| checkpoint | supplementary | logged; never affects execution |

Queued records are written, in order, before any later dispatch; while any remain,
every new dispatch is refused. The queue holds 256 records; beyond that each loss is
logged as critical and counted (`lost_audit_records`). A lost outcome is still
closed out as `unknown-outcome` at the next start, because its dispatch was written.
Log lines carry operation ID, capability, kind, and the storage error only; a test
checks that arguments never appear.

### F-2 Shared SQLite connection returned rows outside the lock (confirmed, fixed)

`ControlStore.execute()` returned a live cursor; callers fetched after the lock was
released. Evidence: 3 threads deciding an allowed capability while one decided an
unruled one produced 272 errors in 20,000 decisions (`TypeError`, `IndexError`,
`InterfaceError` from rows of the wrong shape or none). All became denials (fail
closed); no allow for an unruled capability was observed in 60,000 trials, but the
same race could have produced spurious tamper reports during `verify()`. Rows are now
read under the lock. Regression test: `test_concurrent_policy_decisions_never_fail_or_mix_up_rows`
fails on the base code and passes on this branch.

### F-3 A failed COMMIT wedged the store (confirmed, fixed)

`transaction()` committed in the `else` branch, so a failed `COMMIT` skipped the
rollback and left the transaction open; every later `BEGIN IMMEDIATE` then failed.
Evidence: deterministic with a deferred foreign key (`test_failed_commit_leaves_the_store_usable`).

### F-4 Checkpoint failure reported a committed event as unrecorded (confirmed, fixed)

`record()` ran `_checkpoint_if_due()` after the commit and let its exceptions escape,
so a broker would report "not performed" for a dispatch that was in the log. The
checkpoint is now best effort after the commit.

### F-5 Unreadable audit log crashed startup (confirmed, fixed)

An exception during `verify()` (for example a malformed database page) propagated
out of `build_local_control_plane()`. `start()` now returns a failed verification,
so the broker refuses everything and the operator is told why. Failed
reconciliation does the same.

## 2. Local coding provider

### F-6 Complete replies stalled until timeout (confirmed, fixed)

`ask_ollama_cancellable` waited on `select()` of the raw socket, but read through
`http.client`, which buffers. A reply that arrived in one read on a kept-alive
connection sat in the buffer while `select()` reported nothing. Evidence (base
code, loopback server sending a finished two-event stream and keeping the connection
open): `TimeoutError` after 3.01 s with `timeout=3`; with the default it is 240 s.

### F-7 Header wait and blocking reads (confirmed, fixed)

Headers were awaited with a 0.5 s socket timeout, which a model still loading
exceeds. After headers the socket timeout became 240 s, so a read inside a stalled
chunk could block that long without checking authorization.

Fix: a plain socket and a small incremental HTTP/1.1 parser (chunked, length- or
close-delimited; status other than 200, ambiguous framing, bad chunk framing, and
oversized headers rejected). Every wait is bounded by `POLL_INTERVAL` (0.1 s);
overall and idle deadlines are separate. Memory is bounded per event
(`MAX_EVENT_BYTES`), for generated text (`MAX_RESPONSE_BYTES`), and for the whole
body (`MAX_STREAM_BYTES`). Requests over `MAX_REQUEST_BYTES` are refused before
connecting. 24 loopback HTTP tests cover these paths; revocation is observed within
0.5 s while waiting for headers, inside a stalled chunk, and during a continuous
stream, and the server sees the connection closed.

End to end (real broker, SQLite policy and audit, loopback model): removing the
policy rule mid-stream ends the invocation within 1 s, leaves no patch artifact, the
journal ends `failed`, and the audit outcome is `failed-known`. A successful
proposal is verified with `git apply --check` only; the repository is unchanged.

### F-8 Journal failure swallowed (confirmed, fixed)

When a task failed and the journal could not record it, the error was dropped. It
is now attached to the original exception as a note.

## 3. Patch validation

### F-9 Executable resolution (confirmed risk, fixed)

`subprocess.run(["git", ...])` resolved Git by name. On Windows, process creation
can search the current directory, and a `git.cmd` wrapper runs through `cmd.exe`,
which reparses arguments. Git is now found only in absolute `PATH` entries and, on
Windows, only as `git.exe`.

### F-10 Environment and isolation (hardening)

Git now runs with no stdin, output discarded (never decoded), a minimal environment
(no system or global config, `HOME` in the disposable directory,
`GIT_CEILING_DIRECTORIES`), and `CREATE_NO_WINDOW` on Windows.

### F-11 Disposable copy cleanup (confirmed risk, fixed)

`TemporaryDirectory` cleanup could fail on Windows (read-only or briefly locked
files) and either mask the real error or leave copies of the owner's source in the
temp directory. Removal now retries, makes files writable, and reports a failure
with the original error chained.

### F-12 Non-portable paths (confirmed risk, fixed)

`selected_source` accepted `name:stream` (an NTFS alternate data stream), Windows
device names, and trailing dots or spaces, which Windows strips. These, backslashes,
and `.`/empty components are now rejected on every platform, as are selections that
differ only by case.

### F-13 Valid patches rejected for files without a final newline (confirmed, fixed)

The validator compared git's `\ No newline at end of file` marker with a
two-backslash literal. Fails closed, but blocked legitimate proposals.

## 4. Bandit (1.8.0, `bandit -r devtools src/freya/core`, as in CI)

Base `d76cd21`: 0 high, 1 medium, 13 low. This branch: 0 high, 1 medium, 11 low,
no scan errors. One targeted `# nosec B603` with a reason; no blanket suppressions.

| Finding | Location | Classification | Evidence / action |
| --- | --- | --- | --- |
| B110 | `core/capabilities/broker.py` (base) | Confirmed | F-1; removed |
| B110 | `devtools/coding_task_runner.py` (base) | Confirmed | F-8; removed |
| B607 | `devtools/patch_applicability.py` (base) | Confirmed | F-9; absolute path |
| B603 | `devtools/patch_applicability.py` | False positive after fix | fixed argv, absolute `git`, no shell; `# nosec B603` with reason |
| B404 | `patch_applicability.py`, three test files | Accepted (informational) | subprocess is required; every call is reviewed |
| B603 ×4 | `devtools/test_security_report.py` | False positive | test-only; runs `sys.executable` on a fixed script |
| B608 | `core/llm.py:589` | False positive | a model prompt containing "select … from"; `llm.py` performs no SQL |
| B110 | `core/conversation_memory.py:164` | Accepted risk | summary on close is retried at next start (documented behaviour) |
| B110 | `core/conversation_memory.py:338` | Accepted risk | semantic search failure falls back to keyword matches |
| B110 | `core/conversation_memory.py:451` | Accepted risk | re-summarizing after a deletion; the summary was already cleared (`conversations.py:373`), so failure cannot reveal deleted text |

The three accepted B110s are not security decisions, but they are silent. Follow-up:
log them.

## 5. Residual risks

- The audit queue is in memory: if Freya exits while records are queued, denials
  in it are lost (outcomes are reconciled as `unknown-outcome`). Losses beyond the
  queue limit are counted and logged, not recorded in the audit log, because
  `audit_event.kind` allows no event for it without a migration.
- `audit_recorded=False` is reported on `BrokerResult`; callers do not yet surface it
  to the owner.
- Integrity is verified only at startup; tampering while Freya runs is detected at
  the next start.
- `error_summary` stores up to 200 characters of provider error text. Provider
  errors could in principle echo request content.
- Non-streaming `ask_ollama` (command line only) still has a per-operation, not
  total, timeout and is not cancellable.
- Closing the model connection does not guarantee that Ollama stops GPU work.
- `GIT_CONFIG_GLOBAL=os.devnull` (`nul` on Windows) and `CREATE_NO_WINDOW` are
  exercised on Windows only through CI, and not with Git on a path with spaces
  or long names.
- `freya/memory/conversations.py` shares a connection across threads in a way
  similar to F-2; not reviewed here.

## 6. Recommended follow-up

1. Show `audit_recorded=False` and `pending_audit_records` in the window's status
   strip and at shutdown.
2. Add an audit event kind for "records lost" through a new migration.
3. Review `ConversationStore` for the F-2 pattern.
4. Replace the three accepted B110 `pass` handlers with logging.
5. Run the full `tests/` suite on Windows in CI; this branch adds only the
   control-plane tests (audit, broker, policy, registries) to the Windows job.
6. Periodic in-process verification of the chain since the last checkpoint.
