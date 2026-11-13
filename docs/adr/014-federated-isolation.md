# ADR-014: Federated Isolation

**Status: Proposed by owner, not approved.** Written 2026-11-13. This records the
owner's decision so it can be read, argued with and approved or reversed. It does
not claim the decision is validated, and no acceptance test is passed by it.
Nothing here enables untrusted execution or registers `code.run`. Baseline:
*Freya Foundation v1.1*, section 22. The baseline titles this record Windows Alpha
Trust and Isolation.

**Baseline summary:** Dedicated service identity; replaceable IsolationProvider and preferred Hyper-V alpha profile; unsupported or unvalidated untrusted execution disabled.

**Context:** Freya is a federated service. Nodes can be Linux, Windows (Pro,
Enterprise or Home), macOS, Android and iOS. Untrusted execution means model- or
Freya-written code.

**Decision: tiered isolation capability per node.**

- Each node reports, as part of federation enrollment, the isolation profiles it
  can actually provide (verified by a probe, not just claimed). The broker sends
  `code.run` only to a node whose profile meets the tier the task requires, and
  refuses otherwise. A task never falls back to a weaker tier silently.
- Tiers, strongest first:
  - T1, a hardware-virtualized throwaway VM: Linux KVM/Firecracker micro-VM,
    Windows Pro/Enterprise Hyper-V VM (a dedicated minimal Linux guest or Windows
    Sandbox), macOS Virtualization.framework Linux VM, or an Android AVF
    (Android Virtualization Framework) protected VM where available.
  - T2, a hardened container inside a VM boundary: hardened WSL2 on Windows
    Home, with automount, Windows interop and host networking off and a locked
    container inside it (no network, read-only root, dropped capabilities,
    non-root, fresh per run, resource limits). Label T2 explicitly as weaker; it
    contains mistakes, not a determined attacker.
  - T0, client-only: no untrusted execution. This is the default for phones
    (iOS always; Android unless AVF is verified) and for any node that fails
    its probe.
- Common rules for every tier: no credentials or host files in the guest, deny
  network by default, a fresh environment per run, output returned as data
  only, every run audited, and an owner-approved policy.
- Untrusted execution stays disabled on a node until its profile is implemented
  AND validated. Nothing about this decision enables execution by itself.
- Service identity: a dedicated service account per OS remains the baseline
  target. SEC-012 names that account on Windows; the same kind of account is
  the target on each other OS. On this alpha, Freya runs as the owner's user.
  There is no service identity and no ACL on the control database. That gap
  stays until the owner creates the account.

**Departs from the baseline:** Hyper-V stays the preferred Windows profile, but
the decision generalizes it to per-node tiers. Hardened WSL2 is an allowed,
labeled-weaker profile. Section 22 asks this record for one Windows host: edition
and virtualization prerequisites, a Hyper-V guest, and one Core service identity.
Those remain the Windows T1 case. SEC-013 already says Hyper-V is not a permanent
architecture dependency, and that another profile needs an ADR and its own tests
before it may run. This file is that proposal. Approval stays the owner's.

**Validation owed:** No tier is validated. Before any tier may run, the baseline
acceptance tests still apply: TEST-006 (a guest must not read Core files or
credentials, reach administrative endpoints, escape mounts, or use undeclared
egress; untrusted execution stays disabled when containment fails) and SEC-013
(the profile is defined, approved and validated, or execution stays off). The
section 22 list is still owed for Windows T1: host and guest trust boundary,
edition and virtualization prerequisites, Core service identity and ACLs,
Hyper-V guest configuration, input/output exchange and worker authentication,
egress, mounts, clipboard, device and credential restrictions, VM cleanup and
allocation reconciliation, disabled-feature behavior, and the SEC-019 worker
endpoint.

What exists today is the contract and an unregistered WSL2 shape. The tests below
cover that shape. They do not validate a live profile, and `code.run` stays
unregistered:

- `tests/test_code_sandbox.py` refuses a profile missing a required property,
  bounds the files, the command and the time, keeps files and output out of the
  audit log, and checks that Freya as she starts has no way to run code.
- `tests/test_wsl_sandbox.py` checks the container arguments (no network, a
  read-only root, dropped capabilities, a non-root user) against a fake WSL, and
  runs the in-container runner directly on Linux. The probe list in
  `freya.sandbox.probes` covers outside network, loopback, Windows drives, a
  fresh run, resource limits, cut output and Freya's variables. Probe 8 records
  whether an administrator in the WSL guest can attach the Windows drives. The
  owner has not run those probes on the PC.

Still to write and run, on the node, before that node may report the tier:

- T1, once per mechanism (KVM/Firecracker, Hyper-V guest or Windows Sandbox,
  Virtualization.framework, AVF): escape (leave the VM, shared folders,
  clipboard, host services); network (no egress, including the host's loopback
  and administrative endpoints); credential leak (no host credentials, no Core
  identity, none of Freya's variables); filesystem (no host or Core files, and a
  file from one run is gone after the VM is destroyed). The enrollment probe
  reports T1 only when that mechanism's tests have passed on that node.
- T2: the same four, against the locked container, plus the probes in
  `docs/design/code-sandbox.md` section 6. Escape includes the weaker boundary
  probe 8 already names. Its answer is recorded and does not raise the tier.
  The built distribution turns automount and interop off (`wsl_setup.py`) and
  each container uses `--network=none`. WSL's own networking stays on, one
  setting for every distribution. This decision requires host networking off
  before a node may report T2, so the built profile still falls short of T2. The
  enrollment probe may report T2, labeled weaker, only when these pass.
- T0: the enrollment probe reports T0 for iOS, for Android unless an AVF
  protected VM is verified, and for any node whose probe fails or has not been
  run. A task that requires T1 or T2 is refused. Nothing is executed, so the
  containment tests are the refusal.
- The broker refuses `code.run` unless an enrolled node's verified tier is the
  one the task requires or a stronger one. A weaker node is never substituted.
  The enrollment checks belong with `tests/test_federation.py`: a tier that was
  only claimed, with no passing probe, is T0, and `may_assign` still fails
  closed.

**Relationship to other docs:** `docs/design/code-sandbox.md` (NEW-AM to NEW-AR,
not adopted) is the contract every tier states: no network, no host files, no
credentials, nothing kept between runs, bounded text and a named command, and a
policy rule only the owner adds. T2 is that document's WSL2 profile, named and
labeled weaker. `docs/design/worker-nodes.md` (NEW-DB to NEW-DM, not adopted) is
how a node is enrolled, including a sandbox host under that document's
conditions. The worker channel (NEW-DI) is not built. Remote execution is future
work: nothing is sent to another machine, and the broker has nowhere to place
`code.run`. Enrollment today records hardware a machine measured
(`freya.federation.profile`). An isolation tier is still to be probed. ADR-004
(`004-security-domains-federated-sovereignty.md`) still holds: reporting a tier
does not merge roots of authority. A worker stays a worker (NEW-DB), with no
policy or audit authority of its own. ADR-009 stays met by not executing until
a tier here is validated.

No new provisional label is taken. As of 2026-11-13, `docs/labels.md` on this
line says the next free range starts at NEW-DB, while `worker-nodes.md` already
uses NEW-DB to NEW-DM and `security-self-assessment.md` uses NEW-EA. Open PR #4
is not merged; it registers those labels and names NEW-EB as the next free one.
This record leaves NEW-EB unclaimed.
