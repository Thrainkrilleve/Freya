# Sandbox probe results

This is the empty shape of the file `python -m freya.sandbox probe` writes.
It is not a result. The command has not been run. code.run stays unregistered.

Run it from the repository root, with Freya's virtual environment:

```
python -m freya.sandbox probe
```

Needs Python 3.12 and this repository's environment. It does not need WSL,
Docker, or an administrator. It does not install them and it does not change
a system setting. The written file is `docs/results/sandbox-probes.md`, which
is left uncommitted. Copy a result here only after reading it.

- Recorded: not yet run
- Verified tier: T0
- Reason: not yet run
- Runtime: none
- Candidate facility: none
- Fresh per task: not yet run
- Hardware virtualization: not yet run
- Image digest: absent
- Signature ok: no
- Weaker: not applicable
- Remote: no
- Owner opt-in: no

## Facilities

| Facility | Result |
| --- | --- |
| Hyper-V | not yet run |
| Windows Sandbox | not yet run |
| WSL | not yet run |
| KVM | not yet run |
| Firecracker | not yet run |
| Apple virtualization | not yet run |
| Docker client | not yet run |
| Rootless Docker | not yet run |
| Docker Desktop | not yet run |
| gVisor | not yet run |
| Kata | not yet run |
| runc | not yet run |
| Podman | not yet run |

## Checks

| Check | Result |
| --- | --- |
| Process limits | not yet run |
| Filesystem confinement | not yet run |
| Network denial | not yet run |
| Timeout | not yet run |
| Fresh environment | not yet run |
| Credentials absent | not yet run |
| Read-only root | not yet run |
| Capabilities dropped | not yet run |
| Seccomp | not yet run |
| No new privileges | not yet run |
| Non-root | not yet run |
| CPU limit | not yet run |
| Memory limit | not yet run |
| PID limit | not yet run |
| Hardware virtualization | not yet run |
| Image digest | absent |
| Signature | no |
| Host drive attach | skipped |

A skipped or failed check leaves the verified tier at T0.
Host drive attach is skipped because attaching a drive changes the guest.
