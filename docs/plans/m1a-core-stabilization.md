# M1-A: Core stabilization — scoped implementation plan

Status: draft / review required. Base: main at 44da490. No merge or deployment authorized.

## Purpose

Establish reliable CI on the default branch before integrating the open feature stacks. Keep this change isolated from the local coding agent, cloud delegation, media, and Unreal.

## Scope and status

1. **Baseline CI (included):** cross-platform pytest tests for audit, policy, capability registry, provider registry; Core syntax; Ruff critical lint; Bandit HIGH findings. Jobs run on every PR without path filters so required checks are not silently skipped.
2. **Review ownership (included):** CODEOWNERS for Core and workflows. GitHub repository settings must require code-owner approval for enforcement; the file alone does not protect main.
3. **Audit reliability (pending separate port):** evaluate PR #21 changes to audit/log.py and control_store.py, plus tests/test_audit_reliability.py. Port only minimal fixes, not the coding-agent feature stack. Preserve fail-closed admission and migration compatibility.
4. **Backup/recovery (pending):** select encrypted backup target, restore validation, and privacy-safe deletion ledger. Do not automate copying unencrypted owner data.
5. **Alpha Profile (pending):** separate current workstation acceptance criteria from future fleet requirements without discarding future security obligations.

## Open PR disposition

- #17: design-only; defer Unreal implementation.
- #18: split Context Authority, deletion ledger, evaluation from media and Unreal.
- #19: extract generic security/CI improvements; defer autonomous coding agent.
- #20: split backup and evaluation from cloud delegation/model roles.
- #21: port audit and ControlStore reliability fixes separately; keep coding hardening on its own branch.

## Exit criteria

- Core CI passes on Linux and Windows and runs for every PR.
- Core security changes have owner review; main has required checks and no force pushes.
- Audit interruption/failure-injection tests pass; existing databases migrate safely.
- Backup and restore are tested against explicit RPO/RTO targets.
- No new model, tool, cloud, media, or avatar feature enters this stabilization PR.

## Safety notes

CI is a gate, not a guarantee of safety. No production credentials are needed in tests.
GitHub settings (required checks and branch protection) must be configured by an authorized repository administrator.
Avoid enabling actions that send code or private data to new services without approval.
