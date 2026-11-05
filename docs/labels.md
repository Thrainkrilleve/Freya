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
| NEW-BW to NEW-CG | `design/skill-learning.md` | 2026-10-22 | Proposal, nothing built; supersedes the mechanism of `learning-loop.md` |
| NEW-LC11 | `design/attributed-chat-responses.md` | 2026-10-09 | Built; renumber at adoption |
| NEW-CV to NEW-DA | `design/model-tiers.md` | 2026-11-05 | Proposal, nothing built; extends `skill-learning.md` |

**Next free range starts at NEW-DB.**

NEW-CH to NEW-CU: NEW-CI to NEW-CU are used by `design/local-decider.md` in open PR
#47, which does not register them yet; NEW-CH is unused. Register them with that
document when it lands on this line, and do not give them to anything else.

Relabelled on 2026-10-21, when the register was made: `openrig-lessons.md` had taken
NEW-AM to NEW-AX a week after `code-sandbox.md` took NEW-AM to NEW-AR; and
`freya-to-freya-cooperation.md` had taken NEW-AC to NEW-AL a day after
`unreal-gui-and-metahuman-avatar.md` took NEW-AC to NEW-AJ. In each case the later
document was moved. NEW-AK and NEW-AL are left unassigned so an old reference to them
cannot be mistaken for a new one.
