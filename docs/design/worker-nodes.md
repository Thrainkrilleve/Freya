# Design proposal: worker nodes (Freya on more than one machine)

**Status: proposal, written 2026-11-10 at the owner's request ("the federation
system"). The first part, finding what a machine could lend and recording the
owner's decisions about it, is built in `freya.federation`. Nothing in it opens a
network connection, sends work to another machine, or is wired into Core.** It
approves no ADR, adopts no pending amendment and assigns no requirement ID.

- Provisional labels **NEW-DB to NEW-DM**, taken after `model-tiers.md`
  (NEW-CV to NEW-DA). They get final IDs only at reconciliation.
- Baseline references (DIST-, SEC-, CAP-, RES-, TEST-, V1-) are to
  *Freya Foundation v1.1*. NEW-AC to NEW-AL here mean the requirements of
  `freya-to-freya-cooperation.md` as written on the release line (relabelled
  NEW-BM to NEW-BV in the label register on `docs/adrs`).

## 1. What "federation" means here

The baseline uses *federation* for two Freyas with their own owners, policies and
audit logs cooperating without merging authority (SEC-035 to SEC-040). That is
`freya-to-freya-cooperation.md`, and it is unchanged by this document.

The owner's goal, "expand to new hardware automatically", is the other half: one
Freya using more machines. On 5 November the owner accepted the recommendation of
that document's Q1: **a machine that only lends hardware is a worker under the one
control plane, not a peer** (DIST-007). The owner's mesh review (6 November) chose the
same shape. On 10 November the owner said what "automatically" means: Freya finds the
hardware and suggests it, and the owner approves once.

So this document is about workers. A worker has no users, memory, policy or audit
authority of its own. It runs what the main PC assigns, inside limits the main PC
sets, and can be removed at any moment.

## 2. Decision 10 and workers

Decision 10 accepted Q3 of the cooperation document: no effects across machines in
the first version, read-only and inference only. **Running a model on a worker is
inference**: it changes nothing on the lender and nothing on the main PC beyond the
reply. So enrolling a worker for models needs no exception to decision 10.

Two roles would need one, and are refused by the code until the owner decides:

- *Sandbox host* (running generated code on a spare laptop) is an effect on another
  machine. A draft exception, for the owner to accept, change or reject: "A machine
  enrolled as a sandbox host may run code a model wrote, only inside a sandbox that
  meets `code-sandbox.md`, on a guest network that cannot reach the main PC except
  through the worker channel; it holds no data from `data/`; each run is approved as
  on the main PC." Nothing in this proposal depends on it.
- *Home Assistant host* is a smart-home role, and Freya has no smart-home code;
  PHY-006 bars actuation in the alpha.

## 3. Proposed requirements

- **NEW-DB — Worker or peer, decided per machine.** A machine that only lends
  hardware is a worker in Freya's own domain. A machine with its own users, memory
  or policy is a peer and follows `freya-to-freya-cooperation.md`. A worker never
  holds Core's credentials, the memory database, the audit log or policy (SEC-012).
- **NEW-DC — Found is not joined.** A machine becomes known to Freya only as an
  *offer*: a profile it measured itself and a one-time join code it showed on its
  own screen. Being on the network, answering a discovery message, or sending an
  offer enrolls nothing (DIST-003). Freya shows waiting offers with what each
  machine could do; that is the whole of "automatic". Open offers are bounded (16)
  and lapse after seven days.
- **NEW-DD — The owner enrolls by typing the machine's code.** Enrollment needs the
  owner principal, at the keyboard or in the window, entering the code the machine
  showed, so the machine approved is the one in front of the owner. A spoken request
  cannot enroll or renew (voice is not authentication; pending NEW-I). Three wrong
  codes withdraw the offer.
- **NEW-DE — Enrolled for roles, not for everything.** An enrollment names roles. In
  the first version only inference roles exist to enroll: `model-worker` (whole
  models on the machine's graphics card) and `cpu-model-worker` (small models on its
  processor). Split inference, sandbox hosting and Home Assistant are listed as
  suggestions and refused at enrollment, each until its own decision.
- **NEW-DF — Enrollment expires; removal is immediate.** An enrollment lasts 1 to 90
  days (30 by default) and lapses unless the owner renews it (cooperation Q5).
  Revoking takes effect at the next question and needs no approval: removing
  authority is never harder than giving it.
- **NEW-DG — One question before any work, failing closed.** Every assignment asks
  the registry first. Unknown, offered, declined, revoked, expired, wrong role, not
  bound to a key, or a registry file that cannot be read all mean no. An unreadable
  file is never replaced silently.
- **NEW-DH — A worker is bound to a key.** The worker channel uses mutual TLS. Freya
  keeps a small certificate authority whose key is held by Windows' protected key
  storage on the main PC (SEC-012) and signs a certificate for each enrolled worker,
  valid no longer than its enrollment. When an offer arrives over the channel, it
  carries the fingerprint of the key the machine presented, so approving it with the
  code binds that key; a later connection with any other key is refused. A machine enrolled from a
  file carried by hand has no key and gets no work until it is bound, with the owner
  approving again. SPIFFE/SPIRE can replace the small CA later without changing the
  flow. Tailscale (or Headscale) may run underneath as an option, never instead.
- **NEW-DI — A separate worker channel with assignments.** Workers talk to the main
  PC on a channel apart from the window's gateway, which stays loopback only
  (SEC-019). The main PC sends signed assignments carrying a task ID and an operation
  ID (DIST-007); a worker cannot call capabilities, start work, or reach any
  endpoint for people.
- **NEW-DJ — Worker models are providers behind the broker.** Each worker's model
  runtime is a provider with that worker's `node_id`. A call to it goes through the
  broker on the main PC like any other: policy, availability, audit before and
  after. The worker keeps its own short record under the same request ID, and
  stores no prompt or reply after answering.
- **NEW-DK — What may go to a worker is the owner's choice, background first.**
  Sending a prompt to a worker discloses it to that machine, even though the owner
  owns both. Recommended start: the `background` model role (memory decisions,
  ranking, summaries) and embeddings may go to a worker; Freya's own replies stay on
  the main PC's card. The owner can widen or narrow this per worker.
- **NEW-DL — Losing a worker loses only what it provided.** Every assignment has a
  time limit. A worker that stops answering makes its provider unavailable and says
  so; an inference call (read-only, safe to repeat, CAP-010) is sent again to the
  main PC's runtime, and the owner is told which model answered (DIST-006).
- **NEW-DM — Tested before it is enabled.** Before a worker may receive work
  (V1-005, TEST-010), these pass: an unenrolled, declined, revoked and expired worker
  each get nothing; a voice approval and a wrong code are refused; a worker
  presenting a different key from the one approved is refused; a revoked
  certificate is refused at the next connection; an unreadable registry refuses
  everything; and the kill test: a scripted run that kills the worker's process and
  its link mid-reply, hundreds of times, and checks every turn gets exactly one
  answer from somewhere. The registry tests exist; the channel and kill tests do not
  yet.

## 4. Built in this step

| Part | Where | What it does |
|---|---|---|
| Profile program | `freya/federation/profile.py` | Standard library only, imports nothing from Freya, opens no connection. Copied onto a spare machine and run there, it prints OS, processor, memory, NVIDIA cards and free disk (no host name, serial or MAC), and with `--save` writes an offer with a random eight-digit join code. |
| Suggestions | `freya/federation/roles.py` | From a profile, the roles a machine could fill and why, with a rough model size that fits its card. Marks which roles may be enrolled now. |
| Registry | `freya/federation/registry.py` | Offers, the owner's approval with the code, decline, renew, revoke, `may_assign`, bounded history, all in `data/federation_nodes.json` (listed in storage placement and in the closing backup). |
| Owner commands | `python -m freya.federation` | `profile`, `add`, `nodes`, `approve`, `decline`, `renew`, `revoke`. `approve` asks for the code at the keyboard. |
| Tests | `tests/test_federation.py` | Profile parsing, suggestions, every refusal in NEW-DC to NEW-DG, persistence, an unreadable file, the commands, and that Core does not import the package. |

Not built: the worker program, the certificate authority, the worker channel,
remote providers, discovery on the network, a window tab, broker capabilities for
the registry (its changes are recorded in its own history, not yet the audit log),
and the kill test.

## 5. Next steps, in order

1. **Try the profile program on the spare laptops** (owner, five minutes each).
   Copy `profile.py` to each, run `python profile.py --save offer.json --name "..."`,
   bring the files back, `python -m freya.federation add offer.json`. This also
   answers the open question of what each laptop has.
2. **Worker program and channel.** A small program on the worker that makes its key,
   shows the join code, and serves assignments over mutual TLS; the CA on the main
   PC. Needs the `cryptography` package as a new dependency.
3. **Remote model provider.** The worker's Ollama behind a `ollama-worker-<id>`
   provider for the `background` role, registered only while `may_assign` says yes.
4. **Discovery.** The worker program announces itself on the home network; Freya
   lists it as an offer in the window ("Freya found a machine"). Still only an offer.
5. **Kill test and enabling**, by the owner, after the tests in NEW-DM pass.
6. **Split inference** for models larger than the 4090 holds, using the runtime
   chosen by the larger-model work (llama.cpp RPC or mesh-llm, measured first),
   inside the worker channel.

## 6. Questions for the owner

| # | Question | Recommendation |
|---|---|---|
| W1 | What may go to a worker? | Background work and embeddings first; Freya's replies stay on the main PC. |
| W2 | Tailscale under the worker channel? | Not needed while every machine is on the home network; mutual TLS alone. Revisit for a machine elsewhere. |
| W3 | Is the sandbox-host exception in section 2 acceptable, for later? | Decide when a laptop is ready, not now. |
