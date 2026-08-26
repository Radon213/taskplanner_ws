# surgical_interop_msgs

`surgical_interop_msgs` is the small, public ROS 2 interface package used for
sharing safe surgical context between institutions and requesting focused robot
capabilities. Its default runtime policy excludes free-form speech/clinical
summary text, patient identifiers, raw model output, internal planner rationale,
prompts, and diagnostic text.

## Public state topics

| Topic | Type | Meaning |
| --- | --- | --- |
| `/surgery/context` | `SurgeryContext` | Current procedure type, current phase, execution state, and public safety flags. |
| `/surgery/instruments` | `InstrumentStateArray` | Latest semantic locations and states for instrument instances. A location is not a calibrated Cartesian pose. |
| `/surgery/robots` | `RobotStateArray` | Connection and execution status for each robot capability. |
| `/surgery/events` | `SurgeryEvent` | Ordered public state changes; `sequence` establishes the event order. |
| `/surgery/clinical_observations` | `ClinicalObservationArray` | Structured model observations, confidence, and authority; free-form summary is redacted by default. |
| `/surgery/health` | `SurgeryHealth` | Public freshness and availability summary for the integration. |
| `/surgery/gateway_info` | `GatewayInfo` | Gateway heartbeat, schema/interface identity, process identity, and active procedure-run identity. |
| `/surgery/tool_predictions` | `ToolPredictionArray` | Ranked advisory next-instrument predictions; never a robot command or handover authorization. |
| `/surgery/robot_end_effectors` | `RobotEndEffectorStateArray` | Semantic empty/holding/unknown state and held instrument for each robot end effector. |
| `/surgery/catalog` | `ProcedureCatalog` | Procedure name, target site, approach, phase, and instrument display metadata in Korean/English. |
| `/surgery/speech` | `SpeechRecognitionState` | ASR availability, connectivity, finalized sequence, and measured latency; transcript text is redacted by default. |
| `/external/bed_robot_arms/status` | `BedRobotArmStateArray` | Controller-owned state of the bed-mounted retraction arms. |

State snapshots carry a `revision` that increases within one gateway instance.
Events and clinical observations carry a `sequence`. Each item that asserts an
observed state carries an `evidence_status` value. Recommended values are `MODEL_OBSERVED`,
`DT_ACCEPTED`, `CLINICIAN_CONFIRMED`, `GATEWAY_OBSERVED`, `UNKNOWN`, and
`REJECTED`. `GATEWAY_OBSERVED_REDACTED` and `MODEL_OBSERVED_REDACTED`
explicitly mean an upstream free-text value was suppressed by public policy.

### Consumer identity and idle semantics

`/surgery/gateway_info` is a periodic heartbeat and remains available while no
procedure is active. A new `gateway_instance_id` means the gateway restarted;
consumers must discard revision/sequence deduplication state from the previous
instance. `revision` is a process-local publication-cycle counter and may reset
on restart. `procedure_run_id` is an opaque, non-PHI identifier that is populated
only while a procedure run is active and changes between runs.

`schema_version` identifies the public projection schema. `interface_version`
is the installed `surgical_interop_msgs` package version. `catalog_version` is a
deterministic digest of the published catalog content, allowing a UI to rebuild
its label cache only when metadata changes.

`SurgeryEvent.sequence` and `ClinicalObservation.sequence` increase for the
gateway process lifetime and reset only when `gateway_instance_id` changes.
Each `SurgeryEvent` embeds `schema_version`, `catalog_version`,
`gateway_instance_id`, `procedure_run_id`, and `procedure_type`, so an event
that arrives before the next one-hertz heartbeat can still be assigned to the
correct run. `SpeechRecognitionState.utterance_sequence` is run-local and
resets when `procedure_run_id` changes. Consumers should still receive
`gateway_info` before attaching timelines, and must group events by
`(gateway_instance_id, procedure_run_id)` rather than sequence alone.

When `procedure_active=false`, dynamic state topics publish safe empty/unknown
snapshots instead of replaying a previous scenario. In particular,
`ToolPredictionArray.predictions` and
`RobotEndEffectorStateArray.end_effectors` are empty. `ProcedureCatalog` is safe
static configuration and keeps its phase and instrument entries populated while
idle so clients can build screens before a run begins. A missing
`/surgery/gateway_info` heartbeat is not the same as idle and must be treated as
gateway unavailable.

`/surgery/speech` is the supported public speech snapshot. By default its
`text` is empty while the finalized-utterance sequence, ASR state, and validated
latency metadata remain available. A deployment may explicitly enable free-text
publication only after privacy review; that transcript is not confirmation that
the planner accepted or executed a command and is not de-identified by the
Gateway. `latency_available` gates `response_latency_ms`; clients must not
interpret a zero latency when that flag is false. While no procedure is active
it reports `available=false` with empty text. Existing plain String speech topics
remain compatibility/internal inputs and are not the public UI contract.

`ClinicalObservation` intentionally contains only VLM phase, tool, semantic
location, uncertainty, and optional summary fields. It has no gesture, hand
pose, requested-tool, or handover-intent field. Taskplanner consumes the typed
CAM4 hand streams directly inside the Digital Twin: only the exact
`Right + Open_Palm + PALM_UP` condition sustained for at least 0.300 seconds in
both source and receipt time can create a tool-agnostic handover evidence
episode. That internal evidence is
not copied to `/surgery/clinical_observations`, does not select an instrument,
and is not itself a robot command or authorization.

Confidence and uncertainty fields are finite values in `[0.0, 1.0]`.
Malformed scalar claims are made `UNKNOWN` or omitted; malformed clinical
parallel-array rows are never emitted as aligned evidence. Tool-prediction
`stability_sec` is elapsed seconds for which the same prediction remained
selected. Empty identifiers mean not available, never an inferred default.
Instrument IDs are procedure scoped: consumers must join them with
`procedure_type` and the matching `catalog_version`, not assume that a code such
as `T04` has the same meaning in every procedure.

`/surgery/instruments` uses a deliberately small public location ontology.
Surgeon-side rows use `location_type=surgeon`; current-use rows are selected by
`holder_role=surgeon` and `state=handed_over|in_use`. All Mayo rows use
`location_type=mayo_stand`; `state=parked_for_reuse|awaiting_retrieval`
distinguishes lifecycle policy without inventing physical Mayo zones.

`/surgery/tool_predictions` contains zero to three contiguous ranks in
descending confidence order. Rank 1 matches the private control-compatible
scalar's tool identity and stability; its display confidence may differ because
the ranked distribution is normalized to 100%. Ranks 2 and 3 are display-only
and currently use `stability_sec=0.0`.

## Focused capability requests

The recommended public endpoints are:

| Endpoint | Type | Meaning |
| --- | --- | --- |
| `/surgery/tool_handover` | `ExecuteToolHandover` action | Use one Action for preparation, handover, unused-tool return, and Mayo retrieval. |
| `/surgery/retraction/command` | `ExecuteRetractionCommand` service | Send one direct-teach, retraction, adjustment, tool-change, or stop command to the retractor controller. |

`ExecuteRetractionCommand` is the single public endpoint for the retractor
controller. Its Request contains `protocol_version`, `source_id`,
caller-provided `command_id`, `command`, `target_side`, and `distance_m`.
`PROTOCOL_VERSION_V1` is `1`. The supported commands are
`COMMAND_START_DIRECT_TEACH`, `COMMAND_FINISH_DIRECT_TEACH`,
`COMMAND_START_RETRACTION`, `COMMAND_ADJUST_RETRACTION`,
`COMMAND_CHANGE_TOOL`, and `COMMAND_STOP_RETRACTION`.

For commands other than `COMMAND_ADJUST_RETRACTION`, callers send
`distance_m=0.0`. `COMMAND_FINISH_DIRECT_TEACH` accepts `TARGET_NONE`,
`TARGET_LEFT`, or `TARGET_RIGHT`; the value is passed as the optional finish
target selector and the controller owns its per-arm interpretation. The other
non-adjustment commands use `TARGET_NONE`. An adjustment sends `TARGET_LEFT`,
`TARGET_RIGHT`, or `TARGET_NONE` and a metre distance. For an adjustment,
`TARGET_NONE` is the peer contract's bilateral value: the same distance is
applied once to each arm. For example, “both arms by 1 mm” is
`target_side=TARGET_NONE, distance_m=0.001`, while 5 cm is
`distance_m=0.050`.

The Response contains `request_accepted`, `result_code`, `command_id`, and
`message`. `RESULT_ACCEPTED` means the server accepted the Request;
`RESULT_INVALID_COMMAND`, `RESULT_INVALID_PARAMETER`, `RESULT_REJECTED`, and
`RESULT_ERROR` distinguish the minimum non-acceptance cases. This response does
not report physical execution progress, completion, or controller state. The
controller owns all execution, safety, and completion behavior.

`BedRobotArmState.role` is `retraction`. Its `role_instance_id` is
`left_malleable`, `right_malleable`, or `army_navy`, and `state` is `standby`,
`direct_teach`, `retracting`, `changing_tool`, `moving_to_standby`, `fault`,
`protective_stop`, or `unknown`. The status intentionally contains no medical
device control values.
`BedRobotArmStateArray.stamp` is fresh wall-clock ROS time, independent of replay
`/clock`; both source age and reception age are checked before dispatch.

`ExecuteToolHandover` accepts only `tray`, `mayo`, `robot`, and `surgeon` as
location values. The only valid transitions are `tray -> robot` (pick up the
Taskplanner-selected next tool from the supply tray and hold it ready),
`mayo -> robot` (pick up a reusable Mayo tool selected by the same stable
next-tool policy and hold it ready), `tray -> surgeon` (direct pickup and
handover), `robot -> surgeon` (held-tool handover), `robot -> mayo` (park an
unused speculative preparation on Mayo to free the robot hand), `robot -> tray`
(controller-directed tray recovery), and `mayo -> tray` (retrieve a used tool).
`instrument_id` is
the shared real instrument name (for example `Bovie surgical cautery`), not a
private procedure-catalog code such as `T04`. The server chooses the arm; arm
selection is intentionally absent from the Goal. A successful `tray -> robot`
or `mayo -> robot` Result means stable holding has been reached and the robot
keeps holding the tool until a later handover or a new
`return_unused_preposition` `robot -> mayo` Goal.

For any supported tool-transfer leg, a correlated `SUCCEEDED` Result with
`success=true` and `final_state=completed` is authoritative physical completion
evidence. Taskplanner must project that location/lifecycle result even when a
local detector/VLM, arm-occupancy, or lifecycle belief disagrees. Provenance,
Goal/instance/type/leg correlation, projection order, duplicate, and stale-time
checks still fail closed; failed and canceled Results are not success evidence.

`ExecuteToolHandover.Feedback.state` uses exactly nine lower-case values:
`moving_to_source`, `grasping`, `moving_to_target`,
`waiting_for_takeover`, `placing`, `holding`, `stopping`, `retreating`, and
`recovering_to_tray`. A transition may skip phases that do not apply; for
example, `robot -> surgeon` does not need a source grasp. `progress` is
monotonic in `[0.0, 1.0]` across normal execution and cancel recovery. It
reaches `1.0` when the Action becomes terminal, but it is not a success flag,
pose, or remaining-time guarantee.

The only Result `final_state` values are `completed`, `canceled`, and `failed`.
They must agree with `success` and the ROS 2 Action terminal status. A standard
Action cancel request may be accepted before the physical transfer commit
point. Cancel is a compensating operation: before confirmed grasp the server
stops and retreats while leaving the tool at its source; after confirmed grasp,
or when the Goal starts at `robot`, it places the tool at its configured `tray`
recovery pose. These outcomes return `canceled_source_unchanged` or
`canceled_recovered_to_tray`. The server returns `canceled` only after the
outcome is verified and must not execute a new Goal while recovery is active.

An exact path reversal or original tray-slot restoration is not promised: the
public Goal intentionally carries no slot or pose. If safe recovery cannot be
verified, the server returns `failed` with `cancel_recovery_failed`, and the
caller must not issue the next ordinary command. Once release to the surgeon
has been confirmed, rollback is no longer physically valid and that Goal
finishes `completed`. This operational interrupt does not replace the
controller's local E-stop or protective stop.

A `tray -> robot` or `mayo -> robot` Goal that already returned `completed` is
no longer cancellable. Taskplanner parks that stably held but now-unneeded tool
with a new `robot -> mayo` Goal; Cancel applies only while a Goal is active.
The new Goal is admitted only for an explicit different-tool request or a
different system-final rank-1 tool held for at least 2.0 continuous source-time
seconds, and never while another tracked tool Goal is active.
`canceled_recovered_to_tray` remains a separate compensating recovery outcome.
