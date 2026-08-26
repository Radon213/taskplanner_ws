# Scenario Policy and Safety Boundary

The planner now treats authored scenario choices and physical/runtime safety as
different contracts. A scenario change should normally be a YAML edit and unit
test, not a new gate in the Digital Twin, BT, launch file and UI.

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

`scenario_policy.runtime_requirements` is the single typed selection record for
runtime feature lanes and Taskplanner-owned workflow ordering. It selects such
things as the tool-handover Action lane, retraction Service lane, image/dialogue
VLM lanes, perception, voice routing and whether Taskplanner enforces its local
retraction workflow state. It is not a safety approval and cannot bypass any
admission check below.

To add or change a scenario, edit the complete `runtime_requirements` mapping in
that bundle's `vlm_procedure_prompt.yaml` and add/update the resolver contract
test. Existing bundles without the mapping use centralized compatibility
defaults in `procedure_spec.scenario_policy`; launch, preflight, orchestrator and
execution bridge code must not add bundle-name switches.

## Runtime admission (fail closed)

Policy approval never authorizes execution. The following remain independent
runtime interlocks:

- typed request/schema validation and active-procedure binding;
- authoritative execution state and stopped-only route changes;
- fresh integration preflight and route ACK. A validated route selection must
  refresh its suppression projection even when a scenario switch reuses the
  same procedure revision; stale or mismatched projection closes readiness;
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
3. Select the capability in `scenario_policy.runtime_requirements`; do not add
   a bundle-name condition to a consumer.
4. Keep physical admission in the execution bridge/controller boundary.
5. Add contract and policy tests; Production starts no Lab provider by default.

For perception, the Production contract consumes typed CAM3/CAM4 observations
from the configured `192.168.1.7` deployment and starts no local RF-DETR. That
inventory address is not a safety assertion: the ROS payload does not prove the
publisher IP, and topic advertisement alone does not prove fresh data. Freshness,
schema and provenance admission therefore remain runtime checks, with source
identity verified at deployment.
