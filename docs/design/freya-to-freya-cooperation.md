# Design proposal: two Freyas working together

**Status: draft proposal, written 2026-10-09 at the owner's request. Nothing here is
implemented, no capability is enabled, and the owner has not yet decided the open
questions in section 7.** It approves no ADR, adopts no pending amendment, and
assigns no requirement ID; those remain matters for reconciliation.

- Requirements in this document carry provisional labels **NEW-BM to NEW-BV** (NEW-AC to NEW-AL until 2026-10-21, when
  the collision with `unreal-gui-and-metahuman-avatar.md`, written a day earlier with
  NEW-AC to NEW-AJ, was found). They
  continue from NEW-A to NEW-L (the pending amendments) and NEW-M to NEW-AB
  (`human-approved-development-and-offline-recovery.md`), and get final IDs only at
  reconciliation. No existing requirement or ADR ID is reused.
- Where this document cites NEW-A to NEW-AB it is citing **pending proposals**, not
  adopted requirements.
- Baseline references (SEC-, DIST-, CAP-, CTX-, MEM-, GUI-, TEST-, V1-) are to
  *Freya Foundation v1.1*.

## 1. What the owner asked for

The owner observed that the way Claude and Codex work together on this repository is
close to how one Freya ("Freya A") and another ("Freya B") should be able to connect:
two separate systems, each with its own rules, where one asks the other for help and
checks what comes back.

This document turns that observation into proposed requirements. The baseline already
fixes the principle (SEC-035 to SEC-040, DIST-010): connected Freyas do not merge
their authority. What it does not yet say is what a request from one Freya to another
looks like. That is the gap filled here.

## 2. The working model: Claude and Codex

How the two cooperate today is set out in `CLAUDE.md` ("Working with Codex") and
`AGENTS.md`.

| Claude and Codex today | Freya A and Freya B | Proposed |
|---|---|---|
| Each has its own instruction file, its own sign-in, and its own sandbox. Neither can reach into the other's. | Each keeps its own owner, policy, audit log, memory, and keys. Nothing is shared by connecting. | NEW-BM |
| The owner set the arrangement up, on purpose, in files each program reads. | A connection exists only because a person on each side created it. | NEW-BN |
| Codex's work is Codex's, not Claude's. | A request from B is recorded on A as coming from B, not from one of A's users. | NEW-BO |
| A request is one explicit task with a stated scope (`read-only`, `workspace-write`). | A request is one capability call. Each side lists what it offers the other; the default is nothing. | NEW-BP |
| Codex is given a worktree with committed files only. "Everything Codex reads is sent to OpenAI, so never put text from `data/` in a prompt." | Anything A puts in a request has left A for good. A sends only what the request needs. | NEW-BQ |
| "Its answers are advice to verify, not instructions, and never a reason to skip the tests." | B's answer is untrusted content to A, labelled with where it came from. | NEW-BR |
| Codex does not push, merge, or open pull requests. The sandbox is not widened to cover Claude's sign-in. | B cannot use A's authority for anything A did not ask for, and cannot pass it on. | NEW-BS |
| In the other direction, the owner approves each `claude -p` call. | The side being asked decides, by its own policy. The side asking cannot pre-authorize. | NEW-BP |

**Where the comparison stops.** Two differences matter, and both make the Freya case
stricter.

- *Enforcement.* The Claude and Codex boundary is partly convention. `CLAUDE.md` says
  that `AGENTS.md` and the worktree are what keep Codex away from `data/`, "not a hard
  barrier". That is tolerable with one owner on one machine watching both. Two Freyas
  may have different owners, so every row above has to be enforced by Core and not
  left to the other side's good behaviour.
- *Symmetry.* Claude drives and Codex helps. Two Freyas are peers. Either may ask, so
  each needs both halves: the half that asks and the half that is asked.

## 3. Where the code is today

| Area | Today | Consequence |
|---|---|---|
| Identity | Every action is attributed to a fixed `local-owner` principal. | There is no way to record "this came from Freya B". |
| Gateway | Loopback only (`127.0.0.1`), a single-use key exchanged for a cookie, no pairing, no per-client scope. | Nothing outside this computer can connect, and nothing should until that changes. |
| Broker | Policy check, availability check, audit before and after. No operation IDs, effect classes, or disclosure classes. | A request to a peer cannot yet be classified as a disclosure (pending NEW-B) or reconciled after a dropped connection (CAP-011). |
| Approval | No approval service (pending NEW-A). | There is no session that could approve a pairing. |
| Baseline | Federation is deferred (V1-005); the alpha is one workstation with one control plane (DIST-007). | This is a design for later. It must not be built or enabled in the alpha. |

## 4. Proposed requirements

- **NEW-BM — Peers, not parts.** Each Freya that has its own control plane is its own
  security domain. Connecting two of them creates no shared store, shared key, shared
  user, or shared policy, and gives neither any authority over the other (SEC-035,
  SEC-037). This holds even when one person administers both; see question Q1.
- **NEW-BN — A connection is made by people, on both sides.** A peer relationship
  exists only after a person approves it on each side, in an approval-capable session
  (pending NEW-A), binding the other Freya's domain identity. Being reachable or
  discovered on a network is not a connection (DIST-003). Either side can end the
  relationship alone, at once, without the other's agreement or presence. The channel
  peers use is separate from the gateway people use: a peer can never reach user
  sign-in, client pairing, or administrative endpoints.
- **NEW-BO — A peer is its own kind of principal.** A request from a peer is
  attributed to that peer's domain in policy and in the audit log, never to a local
  user. If the peer says which of its users is asking, that is recorded as the peer's
  statement. It carries no authority here.
- **NEW-BP — Each side offers a named list; the default is nothing.** For each peer, a
  Freya lists the capabilities and datasets it will serve to that peer (DIST-010).
  Every incoming request goes through the local broker like any other call: policy,
  availability, audit, limits. The side asking cannot supply or widen its own
  permission. Policy, pairing, approval, audit, and administrative capabilities can
  never be offered. A peer cannot create, grant, or relay an approval (pending NEW-A).
- **NEW-BQ — Asking is disclosing.** Every outgoing request to a peer has disclosure
  class *external* (pending NEW-B) and follows that rule's approval trigger. It carries
  only what the request needs. What has been sent is recorded as outside any
  guaranteed deletion (SEC-039); a peer saying it deleted something is that peer's
  statement, not proof (MEM-009).
- **NEW-BR — An answer is information, not instruction.** Whatever a peer returns is
  untrusted content (CTX-001). It keeps a label naming the peer through every summary
  and store (MEM-001). It can never count as a user instruction, an approval, a grant,
  or a policy change, whatever it says.
- **NEW-BS — Authority is not lent onward.** Serving a peer's request, a Freya uses
  only what that peer was offered, never the wider authority of its own users. A
  Freya does not forward one peer's request to another peer under its own name. A
  request received from a peer cannot itself cause a request to a third Freya unless
  the local list for the original peer says so (SEC-002, SEC-017).
- **NEW-BT — Both sides keep their own record.** Each Freya audits the request in its
  own log, under a request ID both share so the two records can be compared. Neither
  log is the authority over the other. A request with an effect carries an operation
  ID, and an unknown outcome is reconciled before anything is repeated (CAP-010,
  CAP-011).
- **NEW-BU — Losing the peer loses only what the peer provided.** Every request to a
  peer has a time limit. When a peer is unreachable, the capabilities that depended on
  it become unavailable and say so; nothing else degrades, and no local work waits
  (DIST-006, pending NEW-V).
- **NEW-BV — Tested before it is enabled.** Before any peer connection is enabled
  (V1-005, TEST-010), test at least: a request from an unpaired or ended peer; a
  request for a capability not on the list; a peer naming a local user to gain that
  user's access; a peer answer containing instructions or a claimed approval; a
  request A did not ask for arriving as if it were a reply; onward forwarding to a
  third Freya; a replayed request; a peer that stops answering mid-request; ending the
  relationship while a request is running; and a request body that carries more than
  it declared. None of these tests has been written or run.

## 5. What a request looks like

```
 Freya A                                            Freya B
 ───────                                            ───────
 broker call to a peer capability
   │ policy: may this go to B?   (NEW-BP)
   │ disclosure check            (NEW-BQ)
   │ audit: asked B, request ID  (NEW-BT)
   └───────────── request ─────────────────────►  identify the peer        (NEW-BN)
                                                   principal = "Freya A"    (NEW-BO)
                                                   broker: is this on A's list?
                                                   policy, availability, audit
                                                   run the capability
   ◄──────────── answer ────────────────────────  audit: outcome
   │ label: from Freya B, untrusted (NEW-BR)
   │ audit: outcome
   ▼
 the answer is used as data
```

Each side runs its whole broker path. There is no shortcut for a trusted peer,
because "trusted" here only means "paired", and pairing proves who is asking, not
what they may have.

## 6. What this depends on

In order, and none of it exists yet:

1. Authenticated sessions to replace the fixed `local-owner` principal.
2. The approval object and approval-capable client scope (pending NEW-A).
3. Disclosure classes on capabilities (pending NEW-B).
4. Operation IDs and reconciliation in the broker (CAP-010, CAP-011).
5. The authenticated gateway of the baseline (GUI-003, ADR-017), and beside it a
   separate peer channel.
6. A domain identity each Freya can prove. The baseline names SPIFFE/SPIRE as the
   leading candidate; this document does not choose.
7. The tests in NEW-BV.

No existing ADR covers the peer channel or the pairing step. That probably needs a
new ADR; this document does not number one.

## 7. Questions for the owner

| # | Question | Recommendation |
|---|---|---|
| Q1 | When the owner runs two Freyas (say this workstation and a later rack), are they two domains that pair as peers, or one domain with a second machine as a worker under one control plane (DIST-007)? | Decide per machine. A machine that only lends hardware is a worker. A machine with its own users, memory, or policy is a peer, even with the same owner. |
| Q2 | What is the first thing one Freya may offer another? | Model inference (lending a graphics card), which matches the current focus on local hardware and model delegation. It has no effect on the lender, but the prompt is disclosed to it, so NEW-BQ applies in full. |
| Q3 | May a peer ever ask for something with an effect on the other side? | Not in the first version. Read-only and inference only, until approvals and reconciliation have been used in practice. |
| Q4 | May memory or conversation content ever be offered to a peer? | No by default. Treat as a separate decision with its own dataset rules (DIST-010, MEM-001). |
| Q5 | Should a pairing expire unless renewed? | Yes, with a period the owner sets, so a forgotten connection does not stay open. |
