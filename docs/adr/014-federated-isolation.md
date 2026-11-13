# ADR-014: Federated Isolation

**Status: Proposed by owner, not approved.** Written 2026-11-13, and amended the
same day with OCI packaging and an optional Cloudflare route. This records the
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
    non-root, fresh per run, resource limits). Label this WSL2 profile explicitly
    as weaker; it contains mistakes, not a determined attacker.
  - T0, client-only: no untrusted execution. This is the default for phones
    (iOS always; Android unless AVF is verified) and for any node that fails
    its probe.

**Packaging and further profiles.** Docker / OCI is the common package. The
image does not set the tier. The runtime underneath does, and the enrollment
probe reports that runtime. One sandbox image is used on every node, pinned by
digest, signed, and the same digest everywhere. A node whose digest differs, or
whose signature does not check, reports T0. The built WSL image is pinned by
version, not by digest, so it does not yet meet this rule.

| Runtime the probe reports | Tier |
| --- | --- |
| Linux KVM/Firecracker micro-VM; Windows Pro/Enterprise Hyper-V VM (a minimal Linux guest or Windows Sandbox); macOS Virtualization.framework Linux VM; Android AVF protected VM | T1 |
| Docker with Kata Containers or Firecracker, one fresh micro-VM per task, on Linux | T1 |
| Cloudflare Containers / Sandbox SDK: each sandbox in its own VM on Cloudflare's network | T1-equivalent remote, off by default |
| Docker with gVisor (`runsc`). Works without hardware virtualization | T2 |
| Hardened WSL2 on Windows Home | T2, labeled weaker |
| Rootless Docker, hardened (network none, read-only rootfs, all capabilities dropped, seccomp, no-new-privileges, cpu/mem/pids limits, non-root user) | T2 at most, labeled weaker |
| Docker Desktop on Windows or macOS: one shared long-lived VM | No better than the WSL2 tier: T2, labeled weaker |
| Cloudflare Workers: V8 isolates, JavaScript or Wasm only | Not general `code.run` |
| Plain `runc` alone | Not a boundary for untrusted code. T0 |
| iOS; Android unless AVF is verified; a failed or unrun probe | T0 |

A labeled-weaker T2 does not satisfy a task that required T2 without that
label. gVisor is T2 and is not given that label. Rootless Docker and Docker
Desktop carry the same limit as hardened WSL2: they contain mistakes, not a
determined attacker. Plain `runc` is never reported as T1 or T2.

**Probe fields.** The enrollment probe reports these, and a claim without a
probe is T0:

- `runtime`: what was actually started (`kata`, `firecracker`, `gvisor`,
  `rootless-docker`, `docker-desktop`, `wsl2`, `hyper-v`, `kvm`, `apple-vz`,
  `avf`, `cloudflare-containers`, `cloudflare-workers`, `runc`, or `none`).
- `fresh_per_task`: yes only when that run gets a new VM or sandbox. Docker
  Desktop is no, because its VM is shared and long-lived.
- `hardware_virtualization`: yes or no, measured. gVisor can be T2 when this
  is no.
- `image_digest` and `signature_ok`: both must match the one pinned signed
  image.
- `weaker`: yes for hardened WSL2, rootless Docker, and Docker Desktop.
- `tier`: T1, T2, or T0, from the table. A claimed tier the runtime does not
  support is reported as T0.
- `remote` and `owner_opt_in`: `remote` is yes for Cloudflare. `owner_opt_in`
  is yes only for the task the owner just allowed.
- Container hardening, when the runtime is a container: network none,
  read-only root, capabilities dropped, seccomp, no-new-privileges, non-root,
  and cpu, memory and pid limits.

**Cloudflare, optional and off.** Cloudflare Containers / the Sandbox SDK run
each sandbox in its own VM on Cloudflare's network. The owner treats that as
T1-equivalent remote. Workers are V8 isolates. They fit narrow tool tasks, not
general `code.run`. Code and data leave the owner's machines, so the provider
is off by default. A run needs an explicit owner opt-in for that task. It never
receives memory, secrets, or private data. The broker never falls back to it
silently when a local node cannot take the task. The cost sits on the owner's
Cloudflare account, and there is no route without that account. The call requires
Cloudflare Access on the gateway that reaches the provider. Freya's window
gateway stays on loopback.

**Common rules for every tier:** no credentials or host files in the guest,
deny network by default, a fresh environment per run, output returned as data
only, every run audited, and an owner-approved policy. Docker Desktop's shared
VM fails the fresh-environment rule, so it stays labeled weaker. Untrusted
execution stays disabled on a node until its profile is implemented AND
validated. Nothing about this decision enables execution by itself.

**Service identity:** a dedicated service account per OS remains the baseline
target. SEC-012 names that account on Windows; the same kind of account is the
target on each other OS. On this alpha, Freya runs as the owner's user. There
is no service identity and no ACL on the control database. That gap stays until
the owner creates the account.

**Departs from the baseline:** Hyper-V stays the preferred Windows profile, but
the decision generalizes it to per-node tiers. Hardened WSL2 is an allowed,
labeled-weaker profile. Section 22 asks this record for one Windows host: edition
and virtualization prerequisites, a Hyper-V guest, and one Core service identity.
Those remain the Windows T1 case. OCI runtimes and an off-by-default Cloudflare
route sit under the same tier rules. Plain `runc` is not an equivalent profile.
SEC-013 already says Hyper-V is not a permanent architecture dependency, and
that another profile needs an ADR and its own tests before it may run. This
file is that proposal. Approval stays the owner's.

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
- OCI and Cloudflare, still to write: the probe must name the runtime. Kata or
  Firecracker is T1 only when the micro-VM is fresh per task and the T1 tests
  above pass. gVisor is T2 only after those four tests against `runsc`, with or
  without hardware virtualization. Rootless Docker and Docker Desktop are
  labeled-weaker T2 only while every hardening flag holds, and Docker Desktop
  keeps `fresh_per_task` no. Plain `runc`, or a digest or signature that does
  not match the pinned image, reports T0. With Cloudflare off, or without this
  task's opt-in, the broker does not choose it. Memory, secrets, and private
  data are refused on that route. A Worker is refused for general `code.run`.
  `tests/test_code_sandbox.py` and `tests/test_wsl_sandbox.py` do not cover
  Docker, Kata, gVisor, or Cloudflare.
- The broker refuses `code.run` unless an enrolled node's verified tier is the
  one the task requires or a stronger one. A labeled-weaker profile is not
  substituted for an unlabeled T2 task, and Cloudflare is not substituted for a
  missing local node. The enrollment checks belong with
  `tests/test_federation.py`: a tier that was only claimed, with no passing
  probe, is T0, and `may_assign` still fails closed.

**Relationship to other docs:** `docs/design/code-sandbox.md` (NEW-AM to NEW-AR,
not adopted) is the contract every tier states: no network, no host files, no
credentials, nothing kept between runs, bounded text and a named command, and a
policy rule only the owner adds. T2 is that document's WSL2 profile, named and
labeled weaker. `docs/design/worker-nodes.md` (NEW-DB to NEW-DM, not adopted) is
how a node is enrolled, including a sandbox host under that document's
conditions. The worker channel (NEW-DI) is not built. Remote execution is future
work: nothing is sent to another machine, and the broker has nowhere to place
`code.run`. Cloudflare is a separate optional provider, off by default, and is
not a stand-in for that channel. Enrollment today records hardware a machine
measured (`freya.federation.profile`). An isolation tier is still to be probed.
ADR-004 (`004-security-domains-federated-sovereignty.md`) still holds: reporting
a tier does not merge roots of authority. A worker stays a worker (NEW-DB), with no
policy or audit authority of its own. ADR-009 stays met by not executing until
a tier here is validated.

**Open questions:**

- Which key signs the one sandbox image, and which registry holds the pinned
  digest.
- gVisor is T2 and is not labeled weaker. Whether it, too, only contains
  mistakes is still for the owner to say. Until then it is not treated as T1.
- Which narrow tool tasks a Cloudflare Worker may run. None of them are
  `code.run` under this decision.
- What limit the usage ledger puts on the Cloudflare account, and which Access
  application authenticates the call. The window gateway stays on loopback
  either way.

No new provisional label is taken. As of 2026-11-13, `docs/labels.md` on this
line says the next free range starts at NEW-DB, while `worker-nodes.md` already
uses NEW-DB to NEW-DM and `security-self-assessment.md` uses NEW-EA. Open PR #4
is not merged; it registers those labels and names NEW-EB as the next free one.
This record leaves NEW-EB unclaimed.
