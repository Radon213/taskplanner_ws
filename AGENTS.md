# Taskplanner workspace instructions

Read [`taskplanner_principles.toml`](taskplanner_principles.toml) before changing
runtime architecture, ROS interfaces, commands, scenarios, launch scripts, or
the web UI. It is the binding workspace policy for humans and coding agents.

For browser UI work, also read the shared
[`../ui_iteration_principles.toml`](../ui_iteration_principles.toml). It defines
the presentation-only fast path and its owner boundaries.

This is a research workspace. Optimize for a small, local change being usable
immediately without rebuilding or restarting unrelated services. Do not add
enterprise-style receipts, schema fingerprints, static allowlists, launch-time
contracts, compatibility projections, or global readiness gates unless the
physical controller actually needs them.

## Ownership

- One concept has one runtime owner. Consumers observe state; they do not
  repeat admission, dedupe, dispatch, or route selection.
- Commands belong in the command router and a typed endpoint adapter. A new
  command using an installed ROS type should be a catalog edit plus reload.
- Scenarios belong to the scenario store. Use minimal validation, atomic swap,
  and keep the last known-good configuration.
- Execution endpoint routing belongs only to the execution router.
- Digital Twin, UI, logs, and VLM are observers or optional producers; they do
  not become a mandatory path for a deterministic local command.

## Safety boundary

Keep controller E-stop, collision/force/velocity/joint/workspace limits,
transport type/range checks, endpoint availability, command-id idempotency,
and Action cancellation/result semantics. Do not repurpose scenario policy,
camera/ASR/VLM health, UI state, or a duplicate receipt as physical safety.

Debug observation is always permitted. Debug writes require paused or stopped
authoritative state; physical writes additionally require an explicit arm.
Physical route changes require stopped state and no in-flight request for the
affected resource.

## Change and restart discipline

- Existing Topic/Service/Action type: catalog/config reload; do not build.
- Existing Python adapter entrypoint: restart only that adapter. Adding or
  changing an installed console/launch entrypoint builds only its package.
- Custom ROS IDL or C++ ABI: build only the interface package and direct
  consumers.
- Same-mode core restart must not rebuild the workspace or recreate ASR, UI,
  VLM, rosbridge, or other sidecars.
- Use focused smoke tests by default. Keep broad release-contract tests in CI
  or an explicit release command.

## Compatibility

Prefer a one-time migration and delete the live legacy path. Preserve an
annotated Git tag or converter rather than carrying runtime compatibility logic
indefinitely.
