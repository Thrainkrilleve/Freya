# Design proposal: a sandbox for code a model wrote

**Status: draft proposal, written 2026-10-11 at the owner's request. It proposes a
change to one agreed decision and to decision D6 of
`human-approved-development-and-offline-recovery.md`; neither is changed until the
owner says so.** It approves no ADR, adopts no pending amendment, and assigns no
requirement ID. Nothing here can run code yet: the part that is built is a contract
and a capability with a stand-in sandbox, registered nowhere.

- Requirements in this document carry provisional labels **NEW-AM to NEW-AR**. They
  continue from NEW-AL (then `freya-to-freya-cooperation.md`, since relabelled NEW-BM to NEW-BV) and get final IDs only at
  reconciliation. No existing requirement or ADR ID is reused.
- Baseline references (ARCH-, SEC-) are to *Freya Foundation v1.1*.

## 1. What the owner asked for

Freya is meant to learn as she works, and to have models work together on code.
Working on code means running it: a test that passes is the only proof that code
works, and it is the plainest signal there is of whether a delegated coding task
succeeded. Today nothing a model writes is ever run (ARCH-017; the evaluation's rule
that replies are read, never executed).

The owner asked on 2026-10-11 whether a sandbox could be made for generated code,
using made-up information instead of the owner's memories.

## 2. Why this is a change to an agreed decision

The agreed isolation profile is Hyper-V, with untrusted execution disabled until it
is validated. Decision D6 says Freya runs no repository code until then.

This workstation runs **Windows 11 Home**, which does not offer the Hyper-V role.
The owner has decided not to buy the upgrade to Pro for now. So the agreed profile
cannot be validated on this machine, and under D6 nothing would ever run.

Checked on 2026-10-11 (read-only): Windows 11 Home build 26200; a hypervisor is
already running, so the processor and firmware support virtual machines; WSL is not
installed; neither Docker nor Podman is installed; 64 GB of memory.

This proposal adds a second, weaker profile that works on this machine, and says
exactly what it may and may not be trusted with. Hyper-V stays the preferred profile
wherever it is available.

## 3. What the weaker profile is, in plain words

A Linux container inside WSL2 (Microsoft's free Linux layer, available on Home).

**It holds mistakes. It is not proven to hold a determined attack.**

- Holds: a script that deletes or reads the wrong thing, code that calls out to the
  internet, code written under the influence of something malicious a model read.
- Not proven to hold: code that deliberately uses a flaw in Linux to break out of the
  container. An administrator inside the WSL guest can, as far as is known, attach
  the Windows drives with the owner's rights even when automatic sharing is off. The
  hypervisor under WSL is therefore not the barrier here; the container is, together
  with running the code as an ordinary user. This is to be confirmed by test when the
  real runner is built (section 6), not taken from this paragraph.

Nobody should later describe this profile as more than that.

## 4. Proposed requirements

- **NEW-AM — Isolation sits behind one contract.** Code a model wrote runs only
  through a sandbox that implements one contract and states its properties. Profiles
  (a container, a virtual machine, Hyper-V) are replaceable behind it without
  changing any caller.
- **NEW-AN — What every profile must state.** No network at all, this machine's
  loopback included. None of this machine's files beyond those a run is given. No
  sign-in, key, or environment variable of Freya's. Nothing kept from one run to the
  next. A limit on time, memory and processes. A sandbox that does not state all of
  these cannot be registered.
- **NEW-AO — Only made-up material goes in.** A run is given generated code and
  synthetic fixtures. Nothing from `data/`, no memory, no conversation, no file of
  the owner's. This is in addition to NEW-AN, not instead of it: made-up contents
  protect nothing if the real files can be reached.
- **NEW-AP — A run is files and a named command.** The files are bounded text with
  plain relative names. The command is one of a fixed list, never a command line a
  model wrote. What comes back is how the run ended and what it printed, bounded, and
  is untrusted content. Neither the files nor the output are written to the audit
  log. It records that a run was asked for, a digest of what was given, and whether
  the sandbox ran it; it should also record how the run ended (passed, failed, or
  stopped for time), which is not built: see section 5.
- **NEW-AQ — A profile's grade limits its use.** A container profile may run code
  Freya's models wrote against synthetic fixtures. Whether it may also serve
  `dev.check` (pending NEW-S: running a repository's own tests) is question Q2 below
  and is not assumed.
- **NEW-AR — Only the owner switches it on.** The capability is in no policy seed, is
  not offered to the model as a tool, and cannot be triggered by speech. The owner
  adds the policy rule, and only after the profile's own tests (section 6) have
  passed on this machine.

## 5. What is built

In `src/freya/sandbox/`, tested in `tests/test_code_sandbox.py` with a stand-in
sandbox that runs nothing:

- `contract.py`: `SandboxProfile` and `profile_problems` (NEW-AN), `build_request`
  (NEW-AP: the checks on files, command, target and time limit), `checked_result`
  (bounded output with nothing a terminal would act on), and the `Sandbox` contract.
- `service.py`: provider `code-sandbox-local` and the capability `code.run`. It
  refuses a sandbox whose profile has a problem, runs one request at a time, and
  keeps what a sandbox said went wrong out of the message the broker records.

`build_local_control_plane` does not register it, and a test checks that Freya as she
starts has no way to run code.

Not built: the audit log cannot yet tell a passing run from a failing or timed-out
one. A run whose tests fail is still a run the sandbox carried out, so the broker
records it as succeeded, and the broker has nowhere to put an exit code. Recording it
needs the broker's fuller outcome record (CAP-010, CAP-011), which is a prerequisite
of the development proposal as well. Found by Codex's review of this change.

The commands are `python` (one of the files) and `pytest` (all of them, or one file
or folder of them). The limits are 200 files, 1 MB each, 8 MB together, 100,000
characters of output, and 60 seconds by default, 600 at most.

## 6. What is not built: the real runner

This needs WSL, which the owner installs (an administrator action and a restart):
`wsl --install --no-distribution`.

The intended shape, to be settled when it is built:

- A WSL distribution used for nothing else, with automatic sharing of Windows drives
  and the running of Windows programs both switched off, and WSL's networking off.
- A container image made once, by the owner, while the network is on: Python, pytest
  and nothing else. No package can be installed during a run, because there is no
  network.
- Each run starts a new container: no network, a read-only system, a small temporary
  work folder, an ordinary user with no extra rights, limits on memory, processor and
  number of processes. It is removed when the run ends.
- Freya passes the files in on standard input and never as arguments, starts the
  program directly (not through a command interpreter), and stops the run herself at
  the time limit.

Tests the real runner must pass on this machine before the owner allows it, each run
from inside the sandbox:

1. It cannot look up or connect to an outside address.
2. It cannot reach this machine's loopback (Ollama on 11434, the window's gateway).
3. It cannot list or read any Windows drive.
4. A file written in one run is gone in the next.
5. An endless loop, a memory hog and a process storm are each stopped.
6. Enormous output arrives cut, and Freya's memory use stays bounded.
7. Its environment holds none of Freya's variables.
8. As an administrator inside the WSL guest, outside the container: can the Windows
   drives be attached? The answer is recorded here either way.

## 7. What does not change

- ARCH-017: nothing generated runs in Core or with access to Core storage or
  credentials.
- The delegation evaluation still reads replies and never runs them. Grading by
  running code inside the sandbox would be a separate decision.
- Hyper-V remains the preferred profile. Nothing is enabled by merging this.

## 8. Alternatives considered

| | Why not, for now |
|---|---|
| Windows 11 Pro and Hyper-V | The agreed profile and the strongest. Costs about $99; the owner declined for now. Fits behind the same contract later. |
| A free virtual machine program (VirtualBox) | A stronger wall than a container, and shares nothing by default. Each run would take 10 to 30 seconds against about one, and passing files in and out needs more machinery. Fits behind the same contract. |
| A restricted Windows account, no virtual machine | Shares the Windows kernel with everything else. Not suitable for untrusted code. |

## 9. Questions for the owner

| | Question | Recommendation |
|---|---|---|
| Q1 | Accept a container profile, as described in section 3, for code Freya's models wrote against made-up fixtures? | Yes, once the tests in section 6 pass. |
| Q2 | May it also run a repository's own tests (`dev.check`, pending NEW-S, decision D6)? | Not yet. Decide after the answer to test 8 is known. |
| Q3 | May a passed or failed run be recorded as a result of live work for the model that wrote the code? | Yes, kept apart from the measured evaluation figures and labelled so. This is a later piece of work. |
