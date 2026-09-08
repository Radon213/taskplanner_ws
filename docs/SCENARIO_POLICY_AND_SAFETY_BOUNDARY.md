# Scenario Policy and Safety Boundary

> **Current-policy note (2026-08-27).**
> [`taskplanner_principles.toml`](../taskplanner_principles.toml) is the
> binding policy. This document remains useful for the authored policy model,
> but its older references to mandatory integration preflight and route ACK are
> historical transactional-mode behavior. The normal path is minimal ScenarioStore
> validation plus an atomic fast reload; endpoint route changes need only stopped
> state and no in-flight request for the affected resource.

The planner now treats authored scenario choices and physical/runtime safety as
different contracts. A scenario change should normally be a YAML edit and unit
test, not a new gate in the Digital Twin, BT, launch file and UI.

ScenarioStore parses the next bundle before atomically publishing it and keeps
the last-good bundle on an error. A stopped Twin takes the new authored layout.
A quiescent paused Twin keeps matching observed/authoritative tool placements
and swaps only the scenario configuration; an active robot, Action, Service or
execution proxy defers that swap. Starting a procedure likewise uses the
current quiescent Twin state. **Reset** is the explicit operation that restores
the canonical authored layout.

## Scenario policy (editable per bundle)

`procedure_spec.scenario_policy.ScenarioPolicy` is the single read-only query
surface for:

- requestable instruments;
- enabled bed-arm groups;
- allowed operations and voice commands per group;
- anticipatory/preposition behavior;
- where an unused preposition goes (`mayo`, `rack`, or `retain`).

These values say what the current demonstration intends to do. A denied policy
decision means “not part of this scenario”, not “unsafe hardware state”. The
policy lives under `scenario_policy:` in the procedure bundle; code should query
the facade instead of reading several YAML sections independently.

`scenario_policy.runtime_requirements` is the single typed configuration record
for resident feature owners and Taskplanner-owned workflow ordering. It controls
behavior such as tool-handover and retraction policy, image/dialogue VLM use,
perception, voice resolution and local retraction workflow state. It never
creates or removes a node at scenario selection time, so changing a bundle does
not imply a topology restart. It is not a safety approval and cannot bypass any
admission check below.

To add or change a scenario, edit the complete `runtime_requirements` mapping in
that bundle's `vlm_procedure_prompt.yaml` and add/update the focused catalog or
scenario test that owns the changed behavior. Existing bundles without the
mapping use centralized compatibility defaults in `procedure_spec.scenario_policy`;
launch, preflight, orchestrator and execution bridge code must not add
bundle-name switches.

## Runtime admission (fail closed)

Policy approval never authorizes execution. The following remain independent
runtime interlocks:

- typed request/schema validation and active-procedure binding;
- authoritative execution state and stopped-only route changes;
- endpoint availability and request admission. Integration preflight is
  diagnostic by default; the legacy route-ACK transaction is an explicit
  opt-in for coordinated endpoint mutation;
- Action/Service discovery and request admission;
- source timestamp, freshness, monotonicity and duplicate/idempotency checks;
- controller-owned completion/state feedback.

## Controller safety (outside scenario policy)

The physical controller remains authoritative for motion limits, collision and
protective-stop behavior, drive/fieldbus state, force/torque limits, emergency
stop, direct-teach state and physical completion. Taskplanner does not weaken or
reimplement those controls.

## Things that must not become safety gates again

- “this demo does not use that tool/arm/command”;
- the preferred destination of an unused prepared tool;
- whether uncertain phase permits preparation;
- UI workspace visibility or model-selection preferences;
- diagnostic controller metadata that is not an execution admission receipt.

## Adding a modular feature

1. Define a typed ROS message, Service or Action contract and one owner.
2. Add a small adapter/subscriber package; do not add another parser to the
   Digital Twin, BT, launch script and webapp simultaneously.
3. Configure the resident capability in `scenario_policy.runtime_requirements`;
   do not add a bundle-name condition to a launch file or consumer.
4. Keep physical admission in the execution bridge/controller boundary.
5. Add contract and policy tests; Production starts no Lab provider by default.

For perception, the Production contract consumes typed CAM3/CAM4 observations
from the configured `192.168.1.7` deployment and starts no local RF-DETR. That
inventory address is not a safety assertion: the ROS payload does not prove the
publisher IP, and topic advertisement alone does not prove fresh data. Freshness,
schema and provenance admission therefore remain runtime checks, with source
identity verified at deployment.
