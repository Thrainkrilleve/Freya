# Security self-assessment (proposal NEW-EA)

Status: proposal, not adopted. Phase 1 is built on this branch. Nothing here claims a
check passed on the owner's PC until it has been run there.

Label note: the first write-up of this proposal (6 Nov 2026) called it NEW-M. That
label already belongs to "Granted targets" in
`human-approved-development-and-offline-recovery.md`, so this proposal now carries
**NEW-EA**. Do not cite NEW-M for it.

## What it is

One capability, `security.self_assessment` (provider `security-local`, policy seed
`alpha-self-assessment`), that reads Freya's own state and reports how well her
documented security rules are holding. It is defensive and read-only: it runs no
exploit, opens no socket, starts no program other than `git ls-files`, and changes
nothing. Fixing a finding is a separate, reviewed change (NEW-D).

The owner runs it with `/self-check` in the terminal or in the window's chat. It goes
through the broker, so each run is policy-checked and audited like any other action.
As Freya starts she runs it once and prints one line: the result, and the titles
of any checks that did not pass, never their evidence (`FREYA_SELF_CHECK_AT_START=0`
skips it). It is not a model tool, so a model decision, a spoken sentence or untrusted tool
output cannot start it.

## Checks

Each check verifies a rule Freya already documents in `CLAUDE.md`; none invents a new
one. A check that cannot run becomes a warning, so the report is always produced.

| Id | Checks | Fails when |
| --- | --- | --- |
| `audit-chain` | the hash-chained audit log verifies | the chain does not verify |
| `audit-unwritten` | the broker holds no unwritten audit record | a record was lost (warning while one waits) |
| `audit-checkpoint` | checkpoints go to `FREYA_AUDIT_CHECKPOINT_PATH` | never fails; a warning while it is unset |
| `policy-deny-default` | an unknown capability is denied | it is allowed |
| `policy-grants` | grants name an exact subject and capability | a destructive capability is granted (warning for a wildcard) |
| `owner-locks` | the cloud route is allowed only by the Cloud tab's switch rule; `code.run` is allowed by no rule | either is open another way |
| `model-tools` | the model is offered no destructive or owner-only capability | one is marked `model_tool` |
| `local-endpoints` | Ollama is pinned to 127.0.0.1 and ignores proxies; world data, when on, comes from this machine | either could leave the machine |
| `child-environment` | programs Freya starts get no credential-named variables | made-up credential names pass the filter |
| `gitignore` | `data/` and `*.db` are ignored | either is missing |
| `tracked-sensitive` | git tracks no file under `data/`, no database and no key | one is tracked |
| `gateway-loopback` | the gateway binds 127.0.0.1 | it binds anything else |
| `gateway-csp` | the page's CSP has no `unsafe-inline` or `unsafe-eval` | it has either |
| `avatar-isolation` | Core imports no avatar code; the avatar imports no Core and no screen or input API | any of those appears |

The child-environment check feeds the filter made-up names holding no value; it never
reads the real environment's values. No check reads `data/freya_memory.db`.

## Rules to keep

- Read-only. A check that would change something belongs in a separate proposal.
- Owner-invoked only; never offered to the model and never triggered by voice.
- The report is a map of weaknesses. It is shown to the owner in the terminal and the
  window and is never sent off this machine. In window-only mode the terminal text is
  also written to `data/freya.log`, which stays under `data/`.
- No automatic fixes.
- Active exploitation tests (Phase 3) are a separate decision and would run only in a
  validated sandbox against a throwaway copy with the real `data/` absent.

## Phases

1. Read-only checks above (built).
2. Dependency and advisory checks. Needs a network decision, because Freya is
   offline-first.
3. Active tests in the sandbox. Separate proposal, after the WSL2 sandbox passes its
   probe on the owner's PC.

## Open decisions for the owner

1. Where it runs: `/self-check` and the startup line are built; a Security tab in the
   window is not.
2. Whether the model may read a finished report (default: no, owner-only).
3. Whether Phase 2 is worth a network exception.
