# Architecture decision records

ADR-001 to ADR-018 are named in *Freya Foundation v1.1*, section 22, each as one line
"pending completion and approval". These files record, for each, what the code on
the release line has already decided, where, how it departs from the baseline, and
what validation is still owed. **None is approved.** Approval is the owner's, ADR by
ADR, and changes the status line of that file only.

Written 2026-10-22 as Week 2's main task of the reconciliation plan, done early.
Reconciled to the code monthly (owner decision, 2026-10-21: the tests are
authoritative, the spec follows).

| ADR | Decision | Built? | First validation owed |
| --- | --- | --- | --- |
| [ADR-001](001-freya-core-boundary.md) | Core boundary | As a package convention | Import test for `freya.core` |
| [ADR-002](002-capability-first-integration.md) | Capability-first integration | Mostly; the voice tab bypasses the broker | Import-graph test |
| [ADR-003](003-provider-lifecycle-isolation.md) | Provider lifecycle | Yes, in-process | A second provider behind one capability |
| [ADR-004](004-security-domains-federated-sovereignty.md) | Federated sovereignty | No | Not before a second node |
| [ADR-005](005-context-authority.md) | Context authority | Yes | Clip test for every model tool |
| [ADR-006](006-memory-vs-knowledge-vs-session.md) | Memory, knowledge, session | Memory and session; no knowledge; deletion ledger off | Decide the knowledge path |
| [ADR-007](007-resource-intelligence-vram-management.md) | VRAM management | Evict-on-demand only | See 016 |
| [ADR-008](008-model-runtime-independence.md) | Model runtime independence | Ollama + the Claude CLI route | A second local runtime |
| [ADR-009](009-trusted-runtime-vs-untrusted-execution.md) | Untrusted execution | By not executing | Blocked on 014 |
| [ADR-010](010-multi-gui-client-gateway.md) | Multi-GUI gateway | One page; Unreal prototype | Written message schema |
| [ADR-011](011-existing-infrastructure-first.md) | Existing infrastructure first | Yes | Record a policy-engine evaluation |
| [ADR-012](012-durable-workflow-abstraction.md) | Durable workflows | No | Decide whether the alpha needs them |
| [ADR-013](013-invocation-authorization-revocation.md) | Invocation authorization | Yes, single principal | Waits on 017 |
| [ADR-014](014-windows-alpha-trust-isolation.md) | Windows isolation | No; Hyper-V unavailable | Owner: WSL2 or execution stays off |
| [ADR-015](015-persistence-recovery.md) | Persistence and recovery | Yes, unencrypted | A timed restore drill |
| [ADR-016](016-resource-admission-ownership.md) | Resource admission | Cloud usage yes; GPU no | GPU reservations |
| [ADR-017](017-gateway-authentication-compatibility.md) | Gateway authentication | First step only | Next architecture piece |
| [ADR-018](018-external-effects-reconciliation.md) | External effects | Yes, for cloud sends | Restore older than a send |

Two decisions here are the owner's before anything can be validated: **ADR-014**
(this PC has no Hyper-V; record WSL2 as the alpha profile, or keep execution off) and
**ADR-012** (whether the alpha needs durable workflows at all).
