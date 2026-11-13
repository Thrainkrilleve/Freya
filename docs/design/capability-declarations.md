# Capability declarations

Status: recorded from implementation, 2026-11-13. Not approved. This is not an
ADR and it assigns no requirement id. ADR-002 still records that a feature
reaches another through the broker. The owner is Thrain.

`CapabilityDefinition` already had a name, a description, `requires_gpu` and
`destructive`. Registration now also requires a `CapabilityDeclaration`:

- what the capability does, in the description, which cannot be blank;
- what data or resources it accesses, each as a kind and a detail
  (`filesystem`, `network`, `gpu`, `device`, `secret`, `memory`, `process`,
  or `none`);
- what it requires, a list of names such as `gpu` or `sandbox`;
- whether it is `read-only` or `consequential`.

Consequential means it modifies software, hardware, files, or an external
system. `destructive` stays the stronger flag the model is never offered.
A destructive capability must be declared consequential. A capability with
`requires_gpu` must list `gpu` among what it requires. An incomplete
declaration is refused and the registry is left unchanged.

The policy engine is still deny-by-default for both kinds. A read-only
capability may be allowed by a wildcard rule, as before. A consequential
capability is allowed only by a rule that names that subject and that
capability. A wildcard allow does not authorize it. A deny rule still wins.

`code.run` and `decide` stay unregistered on the control plane, and they are
in no policy seed. Filling in their declarations does not grant them.
