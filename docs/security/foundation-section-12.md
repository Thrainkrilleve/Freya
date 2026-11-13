# Foundation section 12, and what the tests actually prove

Status: a record of evidence, written 2026-11-13. Not an ADR, not adopted,
and not an acceptance result. Baseline: *Freya Foundation v1.1*, section 12
(Distributed Nodes and Messaging, DIST-001 to DIST-010). The owner is Thrain.

Section 12 is about nodes and messaging. The security suite in
`tests/security/` proves local claims from other sections. Those claims are
listed second so they are not mistaken for section 12.

## Section 12

| Principle | What it requires | Proven by a test? |
| --- | --- | --- |
| DIST-001 | A Freya node abstraction that does not depend on Ray, K3s, Nomad, or any one scheduler | No. `freya.federation.profile` can describe one machine without naming a scheduler, and `tests/test_federation.py` checks that the profile program imports nothing from Freya. There is no node runtime, and nothing is sent to another machine. |
| DIST-002 | Heterogeneous Windows and Linux nodes, and future accelerators, through declared capabilities | No. One workstation. No second node is enrolled into a working channel. |
| DIST-003 | Connectivity does not imply trust or authorization | Partly, for the local list only. `test_an_offer_is_only_a_suggestion`, `test_being_on_the_tailnet_enrolls_nothing`, `test_voice_and_other_principals_cannot_enroll`, `test_a_machine_from_a_file_is_never_given_work_until_bound_to_a_key`, and `test_workers_live_on_the_home_network_or_the_tailnet_only` in `tests/test_federation.py`. An offer, a tailnet listing, and a file do not grant work. Not proven as a property of a live distributed system: Core does not import `freya.federation`, and no worker channel exists. |
| DIST-004 | NATS or an equivalent behind a messaging abstraction | No. There is no distributed bus. |
| DIST-005 | Do not require Ray, Maze, Dynamo, or K3s on a single node | The negative half, yes. `tests/security/test_foundation_claims.py` reads `src/freya` and fails if those schedulers are imported. The judgement of when scale would justify them is not a test. |
| DIST-006 | Losing a node degrades capabilities in a bounded way, without silent corruption | No. Nothing here runs on a second node, so loss of one is not exercised. |
| DIST-007 | One authoritative control plane per domain; workers take only authenticated, audience-bound assignments | No. Alpha is one installation and one fixed principal. No assignment is sent. |
| DIST-008 | Ownership transfer uses leases and fencing generations, checked at the protected resource | No. Not built. |
| DIST-009 | A partitioned worker does no new protected work after its grant is stale, and interrupted work is reconciled before reassignment | No. Not built. |
| DIST-010 | Consistency is defined separately for identity, task ownership, canonical data, and telemetry; authority is not merged by last-write-wins | No. One database, no peer. |

Nothing in section 12 is claimed as passed acceptance. Federation stays unregistered with the control plane.

## Claims the security suite does prove

These are the local claims the suite names. They are not section 12 principles.

| Claim | Where the baseline puts it | Test |
| --- | --- | --- |
| Deny by default: a registered capability with no allow rule does not run | SEC-015, section 6 | `test_a_registered_capability_is_denied_without_a_rule` |
| Every brokered call leaves an audit entry, including a denial | SEC-007 and OBS-006, sections 6 and 16 | `test_every_brokered_call_has_an_audit_entry` |
| The audit chain shows a rewritten row | SEC-008, section 6. A local hash chain does not stop a host administrator who can rewrite the file and the triggers; the test shows the chain detects that rewrite | `test_a_rewritten_audit_row_breaks_the_chain` |
| The gateway's window key works once and is exchanged for a cookie; a missing or invented cookie is refused | Section 14, the loopback gateway profile (ADR-010, ADR-017) | `test_window_key_is_single_use_and_a_cookie_is_required` |
| The gateway socket is bound to 127.0.0.1 | Same | `test_the_gateway_binds_loopback_only` |
| A spoken turn's tool call is denied without a grant, is audited, and uses the same tool host as a typed turn | ADR-002, as recorded from the voice gate | `tests/security/test_voice_origin.py` |

Still not proven by this suite, and not claimed: authenticated sessions, revocation of a turn already running, an off-host audit sink, a dedicated service account, obligations and expiry on policy decisions, and any distributed or physical-device test in section 19.
