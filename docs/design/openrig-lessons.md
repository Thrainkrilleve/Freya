# Design proposal: what to take from OpenRig

**Status: draft proposal, written 2026-10-18 at the owner's request. Nothing here is
built.** It approves no ADR, adopts no pending amendment, and assigns no requirement
ID. Requirements in this document carry provisional labels **NEW-AS to NEW-BD**,
continuing from NEW-AM to NEW-AR (`code-sandbox.md`); they get final IDs only at
reconciliation. (The draft of 2026-10-18 used NEW-AM to NEW-AX; those were relabelled
on 2026-10-21 because `code-sandbox.md` had already taken NEW-AM to NEW-AR.) Baseline references (CTX-, MEM-, SEC-, DIST-, ARCH-) are
to *Freya Foundation v1.1*, which remains authoritative.

## 1. Where this comes from

OpenRig (github.com/mvschwarz/openrig, Apache-2.0, reviewed at v0.6.11, commit
`49b12a0`, 2026-10-18) runs teams of coding agents as one system. Its author built
its context layer to stop agents "reinventing the wheel": building a second copy of
something that already exists because nothing told them it was there. That is the
same problem as Freya's cognitive principles "discover existing tools before
building" and "reuse and disclose prior verified solutions".

Claude reviewed the code against the author's claims. The context ideas are sound
and partly well built. The trust model is not: identity is asserted by the caller,
network reachability stands in for authorization, and a shared team package can
switch off every agent's permission prompts without a confirmation. So this
document has two halves: what to adopt (section 2) and what to rule out (section 3).

No OpenRig code is to be copied. Freya stays in Python; everything below is small
text-processing or policy logic that belongs in Core's existing modules.

## 2. Adopt

### NEW-AS — Addressable knowledge

Any Markdown record Freya keeps (design notes, lessons, the reference library) can be
cited by `path#section` or `path#section/subsection`, so the Context Authority can
load one section instead of a whole file.

- One slug rule: lowercase; letters and digits kept by Unicode class (so `Résumé`
  and non-Latin headings work, and `C++` and `C#` do not collide); underscores kept;
  every other run of characters becomes one hyphen.
- Resolution fails loudly and lists the valid sections. It never returns an empty
  section silently.
- Two headings with the same address are an error at resolve time, not a guess.
- Headings inside code fences are ignored (same marker, same or greater length
  closes a fence; an unclosed fence is reported).
- A test in `python -m pytest` resolves every in-repository address and link, so a
  renamed heading fails the build instead of rotting. OpenRig lacks this.

Where: `src/freya/kernel/context/` next to `authority.py`.

### NEW-AT — Context for the situation

Context items are tagged with the situations they serve: a fresh session, a handover
from another model or helper, and after a conversation summary replaces older turns.
The Context Authority composes the items for the current situation inside its
existing budget.

- The budget is enforced, not reported. OpenRig's budgets only warn, and its
  after-compaction path reloads about 20,000 tokens at the moment space is
  scarcest. Freya's existing rules (sources get at most one third of the input
  budget; truncation is labelled) apply to these items too.
- Anything left out is listed in `/context`, as omissions already are.

### NEW-AU — A map of what Freya already has

Each turn that may lead to building or delegating carries a short, generated index:
the enabled capabilities, tools, and prior verified solutions, one line each, with
the address to read more. Knowing something exists costs far fewer tokens than
knowing everything about it.

- Generated from the `ToolRegistry`, capability registry and solution records at
  request time, so it cannot drift from what is installed.
- Has its own token cap inside the source budget.
- `work.delegate` briefs (`delegation-turn-spec.md`) include the relevant part, so a
  cloud or helper model is told what already exists before it proposes building.

### NEW-AV — How established a record is

Stored knowledge carries a stage (`observation`, `note`, `insight`, `established`)
and, for `insight` and `established`, the date it was last verified and the records
that support it.

- At read time, an `established` record whose verification is older than a
  configurable window, or missing, is treated as `insight`. The downgrade is never
  stored. This is OpenRig's best idea and the only part of its maturity model that
  actually runs.
- The stage travels in the data packet's trust label, alongside the existing trust,
  owner and permitted-use fields.
- Raising a record to `established` is an owner action or goes through review
  (consistent with NEW-D), never a side effect of a task.
- Unused `observation` records expire under the existing retention targets.

### NEW-AW — Lessons

A lesson is a short, named story of something that went wrong and why ("the
doghouse that became a moonbase": many reasonable steps adding up to something
nobody asked for). Lessons are addressable records (NEW-AS) that the model can be
pointed at by name, so a correction is one reference instead of a new rule.

- Lessons belong to a principal and are never sent off the machine, by their kind,
  not by content scanning (consistent with NEW-BQ, `freya-to-freya-cooperation.md`).

### NEW-AX — Asking whoever did the work

Freya can answer "why was this done this way?" from the record of the run that did
it: a past delegation, a helper's report, a stored conversation.

- Searches go through the broker with the same scope checks as any retrieval, over
  indexed records, never by building a file path from the request.
- Excerpts are redacted for secrets and labelled as historical, with their date.
- A past session is never resumed in place to ask it something. OpenRig's
  `rig ask --wake` appends to the original session; Freya would use a read-only copy
  or the stored record.

### NEW-AY — Measure it

Add workflow-evaluation scenarios (`docs/design/workflow-evaluation.md`) in which a
synthetic fixture already provides what the request needs, and grade whether the
model uses it or builds a second one, with and without NEW-AU. OpenRig never
measured whether its context layer helps; Freya's runner already has the structure
to.

## 3. Rule out

These describe failures found in OpenRig, stated as requirements so Freya cannot
drift into them. Most extend work already planned: the authenticated gateway and
Freya-to-Freya cooperation.

### NEW-AZ — Reachable is not authorized

Every gateway route that changes state or starts work requires an authenticated
principal, whatever address the gateway is bound to, including private-network
addresses such as a Tailscale tailnet.

- One guard is applied to all routes, with a short, listed set of exemptions.
- A test enumerates every registered route and fails if one is unguarded. In OpenRig
  the token check is attached route by route and the routes that start agents were
  missed.
- Pages never contain tokens.

### NEW-BA — Core issues identity

The sender of any message or request (a user, a model, a helper, another Freya) is
established by Core from a credential Core issued, never from a name the caller
supplies in a header, environment variable or message body.

- The "from" line a model sees is written by Core, not by the sender.
- Text from another agent or person has control characters and anything resembling
  Core's envelope removed before it reaches a model or a terminal.

### NEW-BB — Shared packages cannot raise privilege

A package from outside (a capability pack, a skill, a helper team, or a request from
another Freya) can never lower a permission floor, enable bypass modes, or add hooks,
servers, or global configuration by itself.

- Any of those requires owner-side policy that names it, shown to the owner before
  install.
- Packages install into their own namespace and cannot replace a Core component or
  another package.
- Each install records what it wrote, so a failed install is fully undone.
- Integrity checks (hashes) are not treated as proof of who made a package; that
  needs a signature checked against a key the owner trusts.

### NEW-BC — Validated inputs

Identifiers that reach a file path, a command or a query are checked against their
expected form before use, and resolved paths are checked to stay inside their root.

### NEW-BD — Approval prompts belong to the owner

No model or helper answers a permission or trust prompt on the owner's behalf. A
prompt that blocks work is reported to the owner.

## 4. Build order

1. **Now, small:** NEW-AY (evaluation scenarios), so later changes can be measured.
2. NEW-AS (address resolver and the link test).
3. NEW-AU (generated capability map), then measure again with NEW-AY.
4. NEW-AT and NEW-AV, alongside the pending compaction and derived-data work in
   `context-system.md`.
5. NEW-AZ, NEW-BA and NEW-BC as acceptance criteria for the authenticated gateway
   (already in "Next planned work").
6. NEW-AW, NEW-AX and NEW-BB with Freya-to-Freya cooperation and any package
   installer.

## 5. Open questions for the owner

1. The verification window for NEW-AV (OpenRig uses 90 days).
2. Whether stage changes other than promotion to `established` may be made by a model
   (for example, recording an `observation`) without review.
3. Which existing records, if any, should be the first lessons.
4. Whether NEW-AU's map should list helper programs (Codex, Antigravity) as well as
   Freya's own capabilities.
