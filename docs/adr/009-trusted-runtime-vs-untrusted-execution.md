# ADR-009: Trusted Runtime vs Untrusted Execution

**Status: recorded from implementation, 2026-10-22. Not approved.** This records what
the code on the release line already decides, so the decision can be read, argued
with and approved or reversed. It does not claim the decision is validated, and no
acceptance test is passed by it. Baseline: *Freya Foundation v1.1*, section 22.

**Baseline summary:** Generated code never executes in Core environment.

## What the code decided

No generated code runs anywhere. The delegation benchmark reads replies and never
executes them; tasks graded by running code are ruled out by the project's rules (CLAUDE.md).
`freya.sandbox` defines the contract a sandbox must meet (no network, no host files,
no credentials, nothing kept, bounded text files and a fixed command list) and
`code.run` is registered by nothing and in no seed. The developer tools under
`devtools/` propose patches and check them with `git apply --check` in a temporary
copy; they never apply or run them.

## Validation owed

Depends on ADR-014: there is no sandbox to run code in. Until there is, this ADR is
met by not executing, and any feature that needs execution is blocked on 014.
