# Taskplanner lightweight modularization review

Status: 2026-08-27 working-tree baseline, sixth change-isolation pass applied

This review treats the complete dirty working tree as the baseline. It does not
assume that `HEAD` describes the current product.

> **Current policy override.** This is a historical review of extraction work.
> [`taskplanner_principles.toml`](../taskplanner_principles.toml) and
> [`AGENTS.md`](../AGENTS.md) are the binding policy for new work. In
> particular, references below to full reload transactions, duplicated
> preflight acknowledgements, static command lists, or Debug resource-idle
> gates describe legacy/opt-in paths unless they explicitly say otherwise.

## Production boundary applied in this pass

Production Live owns only these long-running services by default:

1. NInfer manager with the single pinned `qwen3.6-35b-a3b` model;
2. the static/operator web application;
3. operational typed ASR;
4. the Taskplanner ROS runtime;
5. the read-only public rosbridge.

Local RF-DETR is no longer a selectable browser/runtime profile. PNU, vLLM,
TTS, multicamera operations, Integrated Debug and LAN/Tailnet
proxies remain Lab/Ops capabilities rather than Production boot dependencies.

RF-DETR inference belongs to the configured `192.168.1.7` deployment inventory.
Production subscribes directly to the CAM3/CAM4 `ToolObservation2DArray` topics
and starts no local detector, checkpoint loader, HTTP bridge or perception
adapter. ROS messages do not attest the publisher host IP, and an advertised
topic is not proof of a fresh payload; source-host identity and fresh data still
require deployment-side verification.

## Second-pass extraction status

Line counts below are `wc -l` measurements of this working-tree snapshot, not
API promises or targets. The extracted files add no Production process and are
not reasons to rebuild a container image on an ordinary source-only restart.

| Boundary | Extracted artifact | Focused regression | Status |
| --- | --- | --- | --- |
| public ROS interface reference | `docs/contracts/taskplanner_public_ros_graph.snapshot.yaml` and `test_public_ros_graph_snapshot.py` | diagnostic source smoke | The YAML declares only intentionally documented public Topics, Services, and Actions. It is non-authoritative, has no runtime reader, and is not a release gate. Historical launch topology is retained as an advisory example only; adding an internal node, command router, or binding does not require updating it. The smoke test checks that declared interfaces still have source-level implementation witnesses. |
| BT runtime launch core | `bringup/runtime_core_launch.py` (35) and `test_runtime_core_launch.py` (91) | 2 focused factory/order tests; 64 total bringup tests | First incremental common-graph extraction complete. The factory owns the unconditional `btops_gateway` and `tree_executor` actions and returns fresh flat actions in their original order. Mock remains the public compatibility entrypoint and Live still includes Mock once, so there is no nested launch, duplicated node, copied argument set or changed ROS graph. |
| readiness calculation | `simulation_runtime/readiness_evaluator.py` (526) and `test_readiness_evaluator.py` (290) | 8 focused evaluator tests; 63 combined preflight/readiness passes | Pure evaluation extraction complete. The module has no ROS import or mutable state and produces the diagnostic checklist/snapshot, including actual validity and wall-clock lease age for a required bed-arm status. `integration_preflight.py` owns read-only observation and publication; route transactions remain with the execution router. |
| browser admission parsing | `webapp/src/ros/runtimeAdmissionMessages.ts` (483), `rosMessageBounds.ts` (41), and `runtime-admission-messages.spec.ts` (251) | 9 cases across 3 configured viewports | Readiness, execution-route and route-command-result normalization is extracted and bounded. Schema, freshness, cross-field agreement, collection size, string size and nesting depth fail closed before UI state changes. Authored scenario suppression is accepted without a duplicated bundle allow-list. This is presentation admission only and never replaces server preflight. |
| launcher mode and execution layers | `scripts/lib/taskplanner_mode_policy.sh`, `scripts/lib/taskplanner_execution.sh`, `test_taskplanner_mode_policy.sh`, and `test_taskplanner_execution.sh` | focused shell contracts and launcher dry-runs | Mode identity and service ownership remain declarative. Cold ABI/IDL builds stay in the launcher, while same-mode core restart, ASR restart, and scenario configuration reload have explicit owner-only paths. |
| browser observation and command-wire adapters | `webapp/src/ros/liveAsrMessages.ts` (175), `bedRobotArmMessages.ts` (148), `toolObservationMessages.ts` (609), and `modelCatalogMessages.ts` (380) | 10 focused new pure cases plus runtime build, static guards and affected browser flows passed | Bounded ASR JSON, complete procedure-bound bed-arm snapshots, typed RF-DETR/VLM tool facts and model/parameter wire contracts have feature-level owners. `useRosBridge` imports them while retaining the only WebSocket, generation/freshness refs, Service cancellation and action admission authority. |

## Third-pass latency and control boundaries

This pass intentionally does not split stable code merely to create more
modules. It targets operator-visible restart latency and the configuration and
Debug boundaries that are expected to change frequently.

| Boundary | Single owner / contract | Behavior now |
| --- | --- | --- |
| repeated mode selection | `taskplanner_runtime_control.runtime_request_is_already_ready` | An unambiguous healthy request for the already-active mode is a `202`, idle no-op. It does not require a stopped scenario and does not invoke Compose. Conflicting markers, contracts or running candidates fail closed to the normal transition path. |
| owner-only restart | `scripts/taskplanner restart core|asr`, `reload config`, and `warm_restart_runtime_service` | A same-mode request restarts/recreates only its core and never scans package/source contracts or restarts sidecars. ASR and configuration reload remain explicit owner operations. |
| scenario edit discovery and apply | `SelectSimulationBundle` revision fields and `ScenarioStore` | Preview is read-only and available while running. The default `fast` path parses a candidate minimally and atomically swaps the ScenarioStore while preserving the last good bundle. Any bundle selection applies at an explicit paused or stopped boundary (and at initial idle before a lifecycle frame); resident owners converge from the retained snapshot without a topology restart. The former all-participant transaction remains an explicit `transactional` opt-in. |
| Debug observation versus intervention | `integration_debug` operational-state gate | Standalone Debug remains its own mode. Graph-wide read-only observation remains usable during a procedure. Debug writes require fresh authoritative paused/stopped state; a physical write additionally requires explicit arm and single-flight ownership. Resume, stale state or publisher change disarms the Debug control window. |
| Live surgery-record submission | dedicated `surgery-record` owner plus Manager terminal receipt | Live always starts one least-privilege record owner. Voice and UI Stop share the same run-bound receipt; natural completion uses that receipt too. Pause/reset/raw Twin state cannot POST. The owner mounts only one private state directory and one read-only API-key file, and Debug/manual record testing remains separate. |

## Fourth-pass revision and ownership boundaries

This pass completes the two frequent-edit seams left by the third pass without
turning stable geometry or every ROS topic into a separate module.

| Boundary | Single owner / contract | Behavior now |
| --- | --- | --- |
| operator scenario revision | `webapp/src/ros/scenarioRevision.ts` and `SelectSimulationBundle` | Bundle selection is no longer an implicit apply. The operator explicitly previews a bounded coherent server response, sees active and candidate revisions, then receives an apply affordance only from fresh authoritative runtime state. Apply carries the previewed candidate revision and rejects a changed candidate. Preview remains read-only while running; the server remains final admission authority. |
| procedure tool placement | `procedure_spec` bundle `tool_placement` | `rack_order` and per-instance `initial_states` now own editable placement. The procedure scene, Mock bootstrap and Digital Twin layout metadata derive from the same bundle, while lifecycle/location contradictions fail validation. Web no longer carries procedure-specific copied layout tables and scales its rack from authoritative slots, including more than ten tools; stable OR geometry remains a code fallback. |
| authored scenario policy | each shipped procedure bundle `scenario_policy` and `runtime_requirements` | All shipped bundles carry their behavior and lane requirements explicitly. Shadow launch reads the bundle contract rather than matching bundle names, so adding a procedure does not require another launch allow-list. |
| revision transaction participants | `SimulationManager` participant transaction | This retained stopped-only transaction is available only with `bundle_reload_mode=transactional`, for changes that genuinely need coordinated endpoint mutation. Normal scenario edits use the fast ScenarioStore path; malformed YAML always preserves current state. |

## Fifth-pass change-isolation boundaries

This pass does not add another ROS process or connection. It narrows the files
that change for two recurring classes of work while preserving the existing
authority owners.

| Boundary | Single owner / contract | Behavior now |
| --- | --- | --- |
| bounded ASR and bed-arm observations | `webapp/src/ros/liveAsrMessages.ts` and `bedRobotArmMessages.ts` | Versioned payload bounds, procedure canonicalization, complete-array validation and equality/staleness metadata no longer share the React transport hook. `useRosBridge` remains the sole connection and state-lifecycle owner and keeps its compatibility ASR export. |
| Compose/build/readiness execution | `scripts/lib/taskplanner_execution.sh` behind `taskplanner_mode_policy.sh` | Stop/remove, serialized overlay and image builds, stale static-Web refresh, warm core crossing and route/semantic readiness have one shell owner. The warm boundary revalidates the stopped reservation before marker clear/core restart, and the active marker is written only after every required readiness check succeeds. Runtime-control's cheap 9090 liveness remains separate from deployment readiness. |

## Sixth-pass browser contract and feature removal boundaries

This pass finishes the frequent-change browser seam without creating another
connection or React state owner. It also removes dormant paths rather than
turning unsupported behavior into more modules.

| Boundary | Single owner / contract | Behavior now |
| --- | --- | --- |
| typed tool observations | `webapp/src/ros/toolObservationMessages.ts` | Direct CAM3/CAM4 `ToolObservation2DArray` and structured VLM tool-context validation share one pure contract owner. View/frame provenance and geometry remain strict, while arbitrary bounded schema/model/ontology version strings remain forward-compatible. Masks and detector images are never retained. |
| model command wire format | `webapp/src/ros/modelCatalogMessages.ts` | VLM and actor catalog parsing, legacy projection, ROS parameter builders and SetParameters result validation no longer have duplicated implementations. Transport calls, freshness, cancellation, single-flight and runtime-state updates remain in `useRosBridge`. |
| remote-only RF-DETR | `missionSubscriptionPlan.ts` plus `rfdetrObservationSources.ts` | Every Live observation profile selects only the reviewed `192.168.1.7` CAM3/CAM4 typed topics. Non-Live profiles select no RF-DETR worker. The `internal-lab` source, Taskplanner-local detector topics, derived detector rasters and local enable Service client are removed. |
| dormant browser intervention | server-authored state and event topics only | The unreferenced surgeon-override Service client and its browser-only acknowledgement fallback are removed. The stage now derives requests and voice evidence only from authoritative runtime/event observations; Debug intervention keeps its paused/stopped admission path. |
| embedded Debug observation transport | `DebugReadOnlyRosSession` from `useIntegrationDebugBridge` | The Debug Multicam tab reuses the existing Debug WebSocket through a bounded subscribe/topic-list/retry adapter. It receives no raw ROS object, publish method or arbitrary Service caller. Standalone Multicam keeps its own mutually exclusive observer connection, and World Anchor mutation remains unavailable. |

## One owner per mutable decision

| Concern | Owner | Consumers |
| --- | --- | --- |
| procedure choices | `procedure_spec.ScenarioPolicy` | prior, VLM, Digital Twin, voice and bed-arm orchestration |
| scenario runtime lanes | `procedure_spec.ScenarioRuntimeRequirements` | launch, preflight, bed-arm orchestration and execution bridge |
| procedure tool placement | `procedure_spec` bundle `tool_placement` | procedure scene, Mock bootstrap, Digital Twin layout and Web rendering |
| scenario revision admission | `SimulationManager` plus procedure-aware reload participants | Web preview/apply client and runtime consumers |
| typed RF-DETR diagnostics | `simulation_runtime.perception_readiness` | integration preflight |
| perception provider/location | `simulation_runtime.cv_contract` | launcher and contract monitor |
| readiness calculation | `simulation_runtime.readiness_evaluator` | integration preflight ROS/state-machine shell |
| runtime graph truth | launch files and endpoint-owning nodes | validation-only graph snapshot test |
| BT engine launch composition | `bringup.runtime_core_launch` | Mock compatibility entrypoint and Live include |
| browser subscription cost | `webapp/src/ros/missionSubscriptionPlan.ts` | `useRosBridge` |
| browser readiness/route admission | `webapp/src/ros/runtimeAdmissionMessages.ts` plus structural bounds | `useRosBridge` and operation UI |
| browser typed tool-observation contract | `webapp/src/ros/toolObservationMessages.ts` | `useRosBridge`, stage overlay and observability UI |
| browser model/parameter wire contract | `webapp/src/ros/modelCatalogMessages.ts` | VLM and actor command clients in `useRosBridge` |
| embedded Debug read-only ROS session | `useIntegrationDebugBridge` | Debug perception, TF and Multicam panels through bounded observer adapters |
| Production feature exposure | `webapp/src/runtimeFeatures.ts` | application/workspace routing |
| launcher mode/service selection | `scripts/lib/taskplanner_mode_policy.sh` | `scripts/taskplanner` |
| launcher Compose/build/readiness execution | `scripts/lib/taskplanner_execution.sh` | authorized calls from `scripts/taskplanner` |
| physical request admission | execution bridge and external controller | BT/Service/Action callers |

Adding a procedure feature should therefore normally require one typed ROS
contract, one owning adapter, one scenario-policy entry and contract tests. It
must not require duplicate allow-lists in the launch file, Digital Twin, VLM,
BT and UI.

## Policy is not safety

These are editable scenario choices, not physical-safety faults:

- requestable instruments;
- enabled bed-arm groups and their allowed voice commands;
- anticipatory/preposition behavior;
- the destination of an unused prepared tool;
- whether a demonstration uses a capability at all.

The following remain fail-closed runtime/controller boundaries:

- typed schema, procedure/run binding and bounded payload validation;
- source freshness, monotonicity and duplicate/idempotency checks;
- stopped-only execution-route changes and no in-flight target resource;
- route-suppression projection refreshed after every validated route selection,
  including a scenario switch that reuses the same procedure revision;
- physical endpoint discovery and local payload admission (integration preflight is diagnostic by default);
- Action/Service discovery and request admission;
- Debug intervention admission from fresh authoritative paused/stopped state,
  including resource-idle, single-in-flight and fault-lock checks;
- controller-owned state, completion and physical safety.

Read-only Debug observation is not part of that intervention gate. Seeing
state, VLM evidence, graph health or endpoint status must not require pausing a
procedure and must not itself be treated as control authority.

## Build and restart changes

- A same-mode `scripts/taskplanner up live` is a core-only warm restart. It
  does not issue a global Compose `down`, scan source/package stamps, rebuild,
  restart ASR/UI/VLM/rosbridge, or unload an already healthy NInfer model.
- `scripts/taskplanner restart core`, `restart asr`, and `reload config <bundle>`
  express the owner to change directly. They avoid the general launcher path;
  ASR restarts resume capture and config reload calls the running scenario
  owner without a process restart.
- A reboot exposed stale optional-container restart policies. Lab/Ops
  accessories now use `restart: "no"`; enabling Debug, TTS, multicamera, media
  or LAN/Tailnet proxies once must not resurrect them at the next host reboot.
- Host colcon output remains under `install/`; container builds use the isolated
  `build/docker`, `log/docker` and `install/docker` roots. ROS containers invoke
  the bind-mounted source `docker/entrypoint.sh`, so an old image cannot source
  a host-built overlay or restore a stale ABI through its baked entrypoint.
- `--ensure-build` fingerprints ABI/IDL/C++/entry-point inputs only on a cold
  start. An already-running same-mode core takes the fast restart path; use
  explicit `--build` for a deliberate ABI/IDL/C++ rebuild.
- Explicit `--build` builds each image once instead of forwarding `--build` to
  every later Compose `up` call.
- rosbridge is supplied by the Jazzy apt package; the workspace no longer
  carries a duplicate vendor tree.
- generated reports and test evidence are excluded from Docker context.
- static web startup has its own argv-safe script and rebuilds the bundle only
  when its source fingerprint changes. Its `/healthz` reports static serving
  health independently of optional runtime-control availability.
- launcher mode identity, optional lanes and stale-service convergence now live
  in `scripts/lib/taskplanner_mode_policy.sh`; the entrypoint consumes that
  policy instead of maintaining another inline copy.
- Compose/build/readiness mechanics now live in
  `scripts/lib/taskplanner_execution.sh`. The entrypoint still owns transition
  authorization and selects when that execution layer may cross the stopped
  boundary; the helper adds no process or wait to the warm path.
- Production leaves the deprecated `RFDETR_SERVICE_URL` empty. The Live
  container now omits its legacy `rfdetr_service_url:=...` launch argument
  when empty, rather than producing the invalid token `rfdetr_service_url:=`.

## Latest deployment validation (host-specific)

- The first isolated `install/docker` overlay build completed all 23 packages
  in 31.96 seconds. Its launcher-owned ABI contract stamp was then recorded.
- A subsequent `scripts/taskplanner up live --ensure-build` skipped that build
  and converged the Production runtime in 16.51 seconds. The resulting core
  is exactly NInfer manager, Web, ASR, Taskplanner runtime and public rosbridge.
- NInfer reports the single `qwen3.6-35b-a3b` model as loaded. Optional
  Debug/Ops/Lab containers are absent after convergence.
- This was deployment readiness, not physical execution readiness: the route
  is external with zero active requests, but the external handover Action and
  retraction Service each had zero servers. Integration preflight therefore
  remains `ready=false` and names those two missing checks; no Action goal was
  issued.
- A Production UI follow-up found that runtime-control was using the optional
  9091 LAN/Tailnet router as the Live liveness test. With the Ops plane removed,
  it incorrectly cleared the Live marker and disabled a healthy 9090 browser
  bridge. The controller now verifies the `ROSBRIDGE_PORT` declared by the
  running core container, while retaining the router probe for the legacy
  non-core profiles. A warm Live re-convergence took 15.94 seconds; both the
  existing browser tab and a clean diagnostic tab retained `ROS 런타임 연결됨`.
- After the first BT launch-core extraction, a source-only
  `scripts/taskplanner up live --ensure-build` skipped the image/colcon build
  and converged in 18.25 seconds. The restarted graph contains exactly one
  `/btops_gateway` and one `/tree_executor`, with the original executor
  parameters. The execution route remains virtual with zero active requests,
  all eight required readiness checks pass, and the simulation status is
  `running=false`, `execution_state=idle`; no Action goal or mutating Service
  request was issued.
- This third pass rebuilt the stale isolated overlay in 22.6 seconds and
  completed guarded Live convergence in 46.57 seconds. The resulting runtime
  remained `running=false`, `execution_state=idle`, with virtual tool and
  retraction endpoints ready and zero active requests. The generated
  `SelectSimulationBundle` service exposed the revision/apply fields; a
  read-only preview returned matching active/candidate SHA-256 revisions,
  `preview_unchanged`, and `applied=false`.
- That convergence also exposed a stale-static-bundle edge: Compose preserved
  the healthy Web container, so its startup-only fingerprint never saw changed
  frontend source. The deployment path now compares the same source/build
  stamp used in the container and recreates only `webapp` when stale, with
  `--no-deps`; unchanged mode selection remains a Compose-free no-op. The
  one-time Web refresh reached healthy in 7 seconds (Vite build 325 ms), while
  NInfer, ASR and the ROS core kept their existing containers. A fresh browser
  session then showed `ROS 런타임 연결됨` with no warning/error console logs.
- The fourth-pass additive Service IDL required one coordinated overlay rebuild:
  23 packages built in 22.4 seconds and guarded Live convergence completed in
  56.47 seconds. The immediately repeated source refresh then used the warm-core
  path in 16.79 seconds: NInfer, Web, ASR and public rosbridge retained their
  container IDs while only the ROS core restarted. The resulting runtime was
  `running=false`, `execution_state=idle`; no local RF-DETR container/worker was
  present. A live read-only preview returned equal
  `sha256:588252683084a22f...` active/candidate revisions,
  `preview_unchanged`, and `applied=false`. The in-app browser showed
  `ROS 런타임 연결됨`, the revision panel kept Apply disabled for that unchanged
  result, and emitted no warning/error console logs.
- The fifth change-isolation pass needed no overlay or image build. Its first
  guarded Live convergence detected stale Web source, recreated only `webapp`,
  and completed in 21.83 seconds. An immediately repeated, source-identical
  convergence restarted only the ROS core in 16.04 seconds; NInfer, ASR and
  public rosbridge retained container IDs across both runs. The restarted core
  remained stopped with zero active execution-route requests. No local RF-DETR
  container or worker was present: host `192.168.1.7` owned the CAM3/CAM4
  publisher processes, and each delivered fresh typed `ToolObservation2DArray`
  v1.3 samples through the external perception route at roughly 0.34/0.23
  seconds source age. The selected virtual Action/Service
  route passed all eight required readiness checks, and the refreshed in-app
  browser showed `ROS 런타임 연결됨`, the scenario revision controls and that
  same 8/8 virtual-route result. No Action goal or mutating ROS Service request
  was issued during this validation.
- The sixth browser-boundary pass also needed no overlay or container-image
  build. The first guarded convergence rebuilt only the stale static Web
  bundle, restarted the ROS core and completed in 21.68 seconds. The immediate
  source-identical warm convergence kept Web, NInfer, ASR and public rosbridge
  container IDs and completed in 16.06 seconds. The refreshed browser showed
  `ROS 런타임 연결됨`, scenario revision controls and `2/2 typed 수신` from
  the configured `192.168.1.7` CAM3/CAM4 contracts, including an arbitrary
  payload model-version string; no local/internal detector path appeared.
- A final audit removed the remaining `LOCAL RF-DETR` presentation and dead
  detector-raster constants, renamed the browser contract to `TypedRfdetr`, and
  made embedded Debug Multicam reuse its existing read-only ROS session. The
  final source convergence again rebuilt only Web, restarted the ROS core and
  completed in 21.64 seconds; NInfer, ASR and public rosbridge retained their
  container IDs. Shared-session regressions passed at FHD, QHD and UHD, while
  standalone Multicam retained its dedicated observer path. The live browser
  remained connected, showed the scenario revision panel and `2/2 typed 수신`,
  and contained neither the old local badge nor an internal detector topic.
- That validation exposed one remaining warm-restart product seam: the core
  restart changed the previously selected virtual execution route from
  `stopped` to the Live launch default `external/launch_default`. The runtime
  itself remained `running=false`, `execution_state=idle`, with zero active
  requests, and the UI correctly changed from 8/8 ready to six passes plus two
  waiting external endpoints. No Action goal, route command or other mutating
  ROS Service request was issued. Route continuity therefore needs an explicit
  stopped-state persistence/re-admission contract; it must not be inferred from
  browser state or silently replayed after a running restart.

## Remaining large seams

The following files are still large and should not be described as fully
modularized by this pass:

- `vlm_node/real_vlm.py`: provider control, image selection, request
  backpressure, dialogue and proposal state still share one 8,363-line ROS
  node;
- `or_digital_twin/twin.py` and `or_digital_twin/node.py`: state reduction and
  ROS projection remain broad at 6,195 and 4,818 lines respectively;
- `bringup/taskplanner_mock.launch.py` (2,302 lines): retained only as a
  source-only validation harness. Its skill execution lane now uses the same
  direct interop adapter as managed owners; managed startup and scoped builds
  do not load this composite graph;
- `simulation_runtime/integration_preflight.py` (2,499 lines): pure readiness
  calculation is extracted, while ROS discovery, evidence receipt, freshness
  leases and publication remain around one read-only diagnostic state machine;
- `scripts/taskplanner` (2,122 lines): the 257-line mode policy and 455-line
  execution layer are extracted, but Production, Lab, Debug and release
  transitions still share one authorization/sequencing entrypoint;
- `webapp/src/hooks/useRosBridge.ts` (3,862 lines): subscription planning,
  readiness/route admission, common bounds, ASR, bed-arm, typed tool and model
  wire adapters are extracted. Transport lifecycle, generation/freshness refs,
  cancellation, single-flight commands and their React projections intentionally
  remain co-located as one authority; smaller stable observation reducers remain
  until product work actually changes them.

## Safe next sequence

The graph snapshot, pure readiness evaluator, bounded Web runtime-admission
parser, launcher mode/execution layers, warm-core path, operator revision
client, bundle-owned tool placement, Debug observation/intervention split,
remote-only typed perception and high-churn Web wire adapters are complete.
Continue only along seams that shorten frequent edits or remove duplicate
mutable ownership:

1. Close the remaining server-side scenario revision CAS gap: every mutating
   `SelectSimulationBundle` request that can change active configuration must
   carry a non-empty valid preview revision. Keep read-only preview and truly
   unchanged compatibility explicit, and cover direct ROS clients rather than
   relying on the browser affordance.
2. Give same-mode warm restart an explicit execution-route continuity contract.
   Persist only a versioned operator selection, re-admit it from authoritative
   stopped/zero-request state, and fail closed to the launch default when the
   record or current endpoint contract is invalid. Do not replay a browser
   selection or issue an implicit route change while running.
3. Split preflight observation collection from stopped-route transactions only
   if it removes duplicated mutation; retain one preflight state owner and the
   pure evaluator.
4. Extract Digital Twin pure reduction and RealVLM pure kernels when those
   files are being changed for product work. Do not add ROS processes or module
   boundaries solely to reduce line counts.
5. Revisit the smaller Shadow/CAM4/world-state browser reducers only when their
   contracts change. Do not split stable reducers or move transport authority
   merely to reduce the hook's line count.

Each step should preserve the same typed ROS interfaces. A file split that
copies the current 109 Live-to-base arguments or creates a second state owner is
not a modularization improvement.
