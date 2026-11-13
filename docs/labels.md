# Provisional requirement labels

Every proposal takes its labels from here, in the same pull request that adds the
proposal, and `tests/test_labels.py` fails when a label is defined in two documents
or a document defines a label this register does not give it. Final requirement IDs
are assigned only at reconciliation with *Freya Foundation v1.1*; until then a label
is provisional and binds nothing.

A label is "defined" where it is introduced in bold or as a heading (`**NEW-X`,
`### NEW-X`, `NEW-X (proposed`); anywhere else it is a reference, which may point
at any defined label.

| Range | Document | Written | Status |
| --- | --- | --- | --- |
| NEW-A to NEW-L | `specs/Freya_Foundation_v1.1_Claude_amendments_redline.docx` | 2026-10-07 | Pending review and reconciliation |
| NEW-M to NEW-AB | `design/human-approved-development-and-offline-recovery.md` | 2026-10-08 | Proposal with owner decisions; devtools partly built |
| NEW-AC to NEW-AJ | `design/unreal-gui-and-metahuman-avatar.md` | 2026-10-08 | Proposal, nothing built |
| NEW-AK, NEW-AL | unassigned (NEW-AC to NEW-AL were given twice; see below) | | |
| NEW-AM to NEW-AR | `design/code-sandbox.md` | 2026-10-11 | Proposal; contract and stand-in only |
| NEW-AS to NEW-BD | `design/openrig-lessons.md` | 2026-10-18, relabelled 2026-10-21 | Proposal, nothing built |
| NEW-BE to NEW-BL | `design/learning-loop.md` | 2026-10-21 | Proposal with owner decisions, nothing built |
| NEW-BM to NEW-BV | `design/freya-to-freya-cooperation.md` | 2026-10-09, relabelled 2026-10-21 | Proposal, nothing built |
| NEW-BW to NEW-CG | `design/skill-learning.md` | 2026-10-22 | Proposal, not adopted; supersedes the mechanism of `learning-loop.md`. Built: the NEW-CB gate for hint and model changes (build step 1) |
| NEW-LC11 | `design/attributed-chat-responses.md` | 2026-10-09 | Built; renumber at adoption |
| NEW-CI to NEW-CU | `design/local-decider.md` | 2026-11-05 | Proposal, not adopted. The contract, today's rules and the `decide` capability exist; the control plane does not register `decide` |
| NEW-CV to NEW-DA | `design/model-tiers.md` | 2026-11-05 | Proposal, nothing built; extends `skill-learning.md` |
| NEW-DB to NEW-DM | `design/worker-nodes.md` | 2026-11-10 | Proposal, not adopted. Finding a machine and recording the owner's decision are built in `freya.federation`; nothing is sent and Core does not import it |
| NEW-EA | `design/security-self-assessment.md` | 2026-11-06 | Proposal, not adopted. The read-only `security.self_assessment` checks are built |
| NEW-EB to NEW-EG | `adr/015-persistence-recovery.md` | 2026-11-13 | Proposed decision inside ADR-015 (encryption at rest and key custody). Not approved. Nothing built |

**Next free range starts at NEW-EH.**

NEW-CH is unused. It was left free beside `design/local-decider.md` (NEW-CI to
NEW-CU). Do not give it to a new proposal.

Relabelled on 2026-10-21, when the register was made: `openrig-lessons.md` had taken
NEW-AM to NEW-AX a week after `code-sandbox.md` took NEW-AM to NEW-AR; and
`freya-to-freya-cooperation.md` had taken NEW-AC to NEW-AL a day after
`unreal-gui-and-metahuman-avatar.md` took NEW-AC to NEW-AJ. In each case the later
document was moved. NEW-AK and NEW-AL are left unassigned so an old reference to them
cannot be mistaken for a new one.
