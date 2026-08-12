# Taskplanner System Structure

Last reviewed: 2026-08-12 KST

This document describes the current post-`0.1.0` runtime structure of
`taskplanner_ws`.

## 1. Architectural Rule

The active system is:

```text
public surgeon cues + field image + skill events + VLM proposals
        |
        v
OR digital twin reducer
        |
        v
/twin/world_state + /simulation/state
        |
        v
BT decision -> robot skill action -> skill events -> OR digital twin
```

The LLM surgeon actor is a validation stimulus generator. It is not part of the
real deployed perception stack and must not leak hidden actor state to VLM.

Core ownership rules:

- `or_digital_twin` owns authoritative instrument lifecycle, phase belief,
  active robot task, event history, public simulation snapshot, and VLM reducer
  decisions.
- VLM publishes proposals and evidence. The reducer accepts, rejects, or delays
  their effect.
- BT reads the digital twin through blackboard mirroring and emits skill
  commands. It does not mutate world state directly.
- Skill execution reports action status and events back to the digital twin.
- The external bed-mounted controller owns detailed arm motion and safety state.
  Taskplanner consumes only the documented retraction-arm status fields and
  sends only the documented tool-change Service or adjustment Action request.
- The webapp is an operator/debug view, not a source of truth.

## 2. Packages

### `procedure_spec`

Loads YAML procedure prompt bundles and exposes query helpers for phases, tools,
transitions, display names, and priors.

Current bundle format:

```text
procedure_spec/specs/<procedure>/vlm_procedure_prompt.yaml
```

Available bundles:

- `thyroidectomy`
- `nephrectomy`
- `inguinal_hernia_repair`

`display_catalog.yaml` is the dashboard/runtime catalog. Adding a new surgery
should require adding a new YAML bundle and catalog entry, not modifying BT code.

### `vlm_node`

VLM and image input layer.

Executables:

- `real_vlm`: provider-aware OpenAI-compatible VLM node.
- `model_provider_registry`: concurrently discovers LM Studio, Unsloth Studio,
  and vLLM catalogs while keeping endpoint credentials inside the ROS runtime.
  LM Studio uses its native catalog for loaded/unloaded state when available.
- `vllm-manager`: optional always-on lifecycle manager and OpenAI-compatible
  proxy. It keeps port 8001 observable while the GPU worker on port 8002 is
  unloaded, loading, ready, sleeping, waking, or failed.
- `mock_vlm`: legacy/test VLM node.
- `no_image_camera`: 30 FPS synthetic black image with public overlay cues.
- `synthetic_scene_camera`: legacy synthetic scene image publisher.
- `snapshot_bridge`: HTTP snapshot to ROS image bridge.

Real VLM public inputs include:

- `/surgery/images/field/compressed`
- `/twin/world_state`
- `/twin/events`
- `/bt/decision`
- `/skill/status`
- admitted public voice transcript
- retraction-arm requests derived from speech and controller-owned arm status

The real VLM does not subscribe to validation-only `/surgeon/state`,
`/surgeon/actor_event`, or `/surgeon/actor_overlay`. The system-fused phase is
not fed back as the raw VLM phase answer. See
`docs/EXTERNAL_INPUT_CONTRACT.md` for the LAN integration boundary.

The no-image overlay may show visible/public cues only:

- surgeon hand extension
- visible Mayo-stand tool names
- public field interrupt label
- recent public speech when exposed through the runtime context

It must not show hidden actor state, next required tool answers, or event tool
hints from the YAML.

### `simulation_runtime`

Runtime control and surgeon actor package.

Executables:

- `simulation_manager`: start/stop/reset/bundle switch/override service surface.
- `llm_surgeon_actor`: default validation actor.
- `surgeon_actor`: rule-based fallback actor.
- `mock_surgeon`: legacy test path.

The LLM actor:

- uses procedure YAML to drive randomized public behavior;
- listens to skill completion for timing;
- publishes public events such as `request_tool`, `voice_request`,
  `voice+hand`, `small_talk`, `place_on_mayo`, `field_event`,
  `advance_phase_cue`, and `complete_procedure`;
- maintains hidden phase/tool ground truth only for validation metrics.

The VLM/reducer path must evaluate public evidence rather than actor internals.

### `or_digital_twin`

Authoritative reducer and state publisher.

Inputs:

- `/surgeon/actor_event`
- `/surgeon/request`
- `/surgery/audio/request_text`
- `/vlm/result`
- `/vlm/tool_observations`
- `/skill/events`
- `/skill/status`
- `/simulation/control_state`

Outputs:

- `/twin/world_state`
- `/twin/events`
- `/simulation/state`
- `/simulation/event`
- `/vlm/reducer_decisions`
- `/vlm/inference_proposals`
- VLM context topics used by the real VLM node

Important reducer behavior:

- An admitted public surgeon sentence can become a canonical explicit request without
  passing through the VLM.
- Matching transcript and `/surgeon/request` messages are coalesced.
- VLM failure degrades to explicit voice handover; inferred phase, prediction,
  and Mayo classification remain closed.

- fail closed on stale/invalid/unhealthy inputs;
- reject impossible observations without mutating world state;
- stabilize Mayo recovery and next-tool prediction before BT can act;
- treat bleeding/hemostasis as an interrupt event that preserves the current
  normal phase;
- return unused prepositioned right-hand tools during cleanup.

### `bt_orchestrator`

Mirrors digital-twin state into the AutoAPMS/BehaviorTree blackboard and
publishes decision summaries.

Executable:

- `decision_bridge`

### `taskplanner_bt_trees`

Behavior Tree XML package.

Main tree:

- `surgical_assist_v1.xml`

The main recovery branch dispatches `retrieve_from_mayo`; direct hand retrieval
is legacy/manual-only. A handover request for a tool already on Mayo instead
dispatches `pick_up_from_mayo_and_handover` through the right arm.

### `taskplanner_bt_nodes`

C++ BehaviorTree.CPP custom nodes.

Responsibilities:

- select explicit request, recovery, anticipatory handover, cleanup, or hold;
- enforce hand occupancy and active bundle/tool guards;
- dispatch `/bt/skill_command`;
- publish `/bt/decision`.

### `skill_execution`

Mock robot execution and action bridge.

Executables:

- `skill_action_bridge`
- `mock_skill_server`

The bridge converts `/bt/skill_command` into `/skill/execute` ROS action goals.
The action interface remains wire-compatible and uses the `action` string
`pick_up_from_mayo_and_handover` for the Mayo-to-surgeon path. The mock server
always completes configured actions and emits public `/skill/status` plus
`/skill/events`.

Bed-mounted robot-arm integration is a separate, retraction-only lane. It maps
internal validated requests onto `/surgery/tool_change/request` for
thyroidectomy or `/surgery/retraction/adjust` for nephrectomy, and consumes
controller-owned state from `/external/bed_robot_arms/status`. There is no
bed-mounted suction-arm command or status path. Clinical suction instruments
and surgeon speech about suction remain normal tool/evidence semantics.

### `surgical_interop_msgs` and `surgical_interop_execution`

The external retraction contract is intentionally smaller than the internal
planner state:

- `RequestToolChange`: request `command_id`, `arm_id`, `target_tool_id`; response
  `success`, `result`, `reason_code`.
- `ExecuteRetractionAdjustment`: Goal `command_id`, `adjustment_mode`,
  `target_retractor_id`, `direction_frame`, `direction`, `axis`, `distance_mm`;
  Result `success`, `final_state`, `reason_code`; Feedback `state`.
- `BedRobotArmStateArray`: `stamp`, `revision`, `procedure_type`, `arms`, where
  each arm has `arm_id`, `role`, `role_instance_id`, `state`,
  `direct_teach_active`, and `reason_code`.

The bridge does not synthesize controller poses, trajectories, progress, force,
collision state, or attachment verification. Exact allowed values and endpoint
semantics are defined in `docs/EXTERNAL_INPUT_CONTRACT.md`.

### `surgical_msgs`

Shared ROS interface definitions.

Important messages:

- `WorldState.msg`
- `SimulationState.msg`
- `InstrumentState.msg`
- `BTDecision.msg`
- `SkillCommand.msg`
- `SkillStatus.msg`
- `SurgeonActorEvent.msg`
- `SurgeonLLMDecision.msg`
- `SurgeonRequest.msg`
- `SurgeonState.msg`
- `VLMResult.msg`
- `VLMHealth.msg`
- `VLMReducerDecision.msg`

Important services/actions:

- `ControlSimulation.srv`
- `SelectSimulationBundle.srv`
- `InjectSurgeonOverride.srv`
- `ExecuteSkill.action`

### `bringup`

Launch and validation package.

Executables:

- `taskplanner_smoke_test`
- `taskplanner_manual_probe`
- `taskplanner_bt_audit`
- `taskplanner_edge_probe`
- `taskplanner_multi_bundle_runtime_probe`
- `taskplanner_thyroidectomy_llm_e2e_probe`
- `taskplanner_thyroidectomy_prediction_probe`

Launch:

- `taskplanner_mock.launch.py`

Despite the historical filename, the default launch now runs real VLM mode and
the LLM surgeon actor unless overridden.

### `webapp`

Vite/React operator dashboard.

Main responsibilities:

- procedure selection from YAML catalog;
- start phase selection for mid-procedure insertion;
- VLM model selection and health state;
- LLM surgeon actor model selection and on/off control;
- digital twin scene rendering;
- VLM input image preview;
- BT/VLM/reducer observability;
- public validation scoreboards.

## 3. Default Runtime

Default Docker runtime:

```bash
scripts/taskplanner up live
```

For a profile-aware Compose-only startup, bring up the selected profile rather
than individual services so that its shared provider control planes are present:

```bash
docker compose \
  --env-file .env.example \
  --env-file .env \
  --env-file docker/orchestration/live.env \
  --profile live up -d taskplanner-runtime webapp
```

Default launch values:

- `vlm_mode=real`
- `vlm_provider_id=vllm`
- `vlm_base_url=http://127.0.0.1:8001`
- `vlm_model_id=unsloth/gemma-4-E4B-it-NVFP4`
- `vlm_response_format=json_schema`
- `surgeon_actor_mode=llm`
- `actor_model_id=google/gemma-4-12b-qat`
- `enable_no_image_camera=true`
- `enable_synthetic_scene_camera=false`

## 4. Runtime Flow

### Bundle selection/start/reset

```text
webapp
  -> /simulation/select_bundle or /simulation/control
simulation_manager
  -> /simulation/control_state
  -> spec_dir parameter updates
  -> BT executor start/terminate
```

### Public actor stimuli

```text
llm_surgeon_actor
  -> /surgeon/actor_event
  -> /surgeon/request
  -> /surgeon/state
  -> /surgery/audio/request_text
```

The actor may know hidden ground-truth phase internally, but that hidden state is
used only for validation scoring.

### VLM

```text
/surgery/images/field/compressed + public runtime context
  -> real_vlm
  -> /vlm/result
  -> /vlm/health
  -> /vlm/tool_observations
```

### Model providers

```text
React model selector
  -> ROS list/select/control services
  -> model_provider_registry
     -> LM Studio native catalog + load/unload API
     -> Unsloth Studio catalog + load/unload API
     -> vLLM manager :8001 /v1/models
        -> lifecycle API
        -> on-demand vLLM worker :8002
     -> NInfer manager :8080 /v1/models
        -> lifecycle API
        -> on-demand NInfer worker :8082
```

The vLLM and NInfer managers are control planes, catalogs, and proxies rather
than loaded model servers. Their workers are started only on demand and are not
enabled as host boot services. Lifecycle commands return immediately and state
changes are observed through the normal five-second catalog refresh. Native LM
Studio and Unsloth requests run on background threads so a long model load never
blocks a ROS service callback. Their provider-native catalogs remain the final
source of truth after an optimistic `loading` or `unloading` state. Taskplanner
exposes only the actions supported by each provider: LM Studio and Unsloth
provide load/unload, managed vLLM also provides sleep/wake, and managed NInfer
provides load/unload.

Selecting an unloaded managed model invokes its load operation automatically.
The same controls are available explicitly beside both the VLM and LLM surgeon
selectors. Provider credentials remain inside the ROS processes and are not
included in catalog messages sent to the browser.

### Digital twin

```text
public actor events + VLM proposals + skill events + control state
  -> or_digital_twin
  -> /twin/world_state
  -> /simulation/state
  -> /vlm/reducer_decisions
```

### BT and skill execution

```text
/twin/world_state
  -> decision_bridge blackboard mirror
  -> BT tree
  -> /bt/decision
  -> /bt/skill_command
  -> /skill/execute
  -> /skill/status + /skill/events
  -> or_digital_twin
```

## 5. Known Boundaries

- The deployed robot action server is not implemented in this repository. The
  current action server is a deterministic mock server.
- VLM quality depends on the selected local provider/model and structured JSON
  behavior. Provider discovery confirms API reachability, not task suitability.
- The no-image camera is a test replacement for the unavailable surgery video
  feed; it is intentionally not a realistic visual model.
- The LLM surgeon actor exists for validation diversity and must remain separate
  from real perception truth.
- The old multi-file bundle format has been replaced by
  `vlm_procedure_prompt.yaml`.

## 6. Common Development Targets

Add or change a procedure:

- `src/procedure_spec/procedure_spec/specs/<procedure>/vlm_procedure_prompt.yaml`
- `src/procedure_spec/procedure_spec/specs/display_catalog.yaml`

Change lifecycle or invariant behavior:

- `src/or_digital_twin/or_digital_twin/twin.py`
- `src/or_digital_twin/or_digital_twin/node.py`
- relevant `surgical_msgs/msg/*.msg`

Change BT decisions:

- `src/taskplanner_bt_trees/behavior/surgical_assist_v1.xml`
- `src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp`

Change LLM actor behavior:

- `src/simulation_runtime/simulation_runtime/llm_surgeon_actor.py`

Change VLM behavior:

- `src/vlm_node/vlm_node/real_vlm.py`
- `src/vlm_node/vlm_node/schema.py`
- `src/vlm_node/vlm_node/no_image_camera.py`

Change frontend operator view:

- `webapp/src/App.tsx`
- `webapp/src/hooks/useRosBridge.ts`
- `webapp/src/hooks/useDigitalTwinViewModel.ts`
- `webapp/src/components/stage/OperatingRoomStage.tsx`
- `webapp/src/components/observability/ObservabilityPanel.tsx`
- `webapp/src/components/command/*`
- `webapp/src/styles.css`

## 7. Validation

Release-level validation is orchestrated by `scripts/taskplanner
verify-release`. It records build, contract, deterministic fault, ROS Action,
Playwright, supply-chain, optional recorded-surgery metric, restart, and soak
results in one report bundle. The detailed thresholds and software-versus-site
release boundary are defined in `docs/RELEASE_VERIFICATION.md`.

The commands below are focused developer probes rather than the release gate.

```bash
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source install/setup.bash

ros2 run bringup taskplanner_edge_probe
ros2 run bringup taskplanner_smoke_test --spec-name thyroidectomy
ros2 run bringup taskplanner_smoke_test --spec-name nephrectomy
ros2 run bringup taskplanner_smoke_test --spec-name inguinal_hernia_repair
ros2 run bringup taskplanner_multi_bundle_runtime_probe --duration-sec 60
```

Frontend:

```bash
cd webapp
npm run build
```
