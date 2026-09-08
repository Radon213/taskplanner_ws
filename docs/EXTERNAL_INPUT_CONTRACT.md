# 기관 간 ROS 2 계약

This document defines the implementation target for multi-computer integration.
It is not a claim that an external controller already exists. Until each
provider implements and validates its endpoint, Taskplanner uses its internal
or mock execution path.

The contract has two directions:

- **기관 → Taskplanner:** camera, speech, and robot-controller endpoints that a
  provider is asked to implement.
- **Taskplanner → 기관:** a small, read-only set of shared surgical context,
  instrument, robot, event, and VLM-observation topics.

External systems never publish surgeon-actor ground truth, fused digital-twin
state, or robot decisions into the Taskplanner authority topics.

## Runtime Profiles

`input_profile=simulation`

- Starts the LLM/rule surgeon actor selected by `surgeon_actor_mode`.
- Starts the built-in camera selected by the camera launch arguments.
- Routes simulated speech through the legacy structured utterance adapter.

`input_profile=external`

- Does not start a surgeon actor.
- Does not start the built-in no-image or synthetic camera.
- Uses `execution_backend=external`, so no mock robot Action server is started.
- Keeps the typed speech adapter, digital twin, BT, action bridges, and dashboard.
- The Live wrapper accepts only the typed ASR path described below; it does
  not inherit generic `SPEECH_INPUT_MODE` or the legacy String topic.
- Is used only after the requested external endpoints have been implemented and
  passed `/integration/check_readiness`.

For the first wired-LAN integration, start with the reviewed launcher:

```bash
scripts/taskplanner up live --ensure-build
```

The launcher pins `TASKPLANNER_RUNTIME_MODE=live`,
`INPUT_PROFILE=external`, and `EXECUTION_BACKEND=action` as one contract. Do
not create an arbitrary subset of owner containers with a bare
`docker compose up`: each owner rejects an unlabelled or inconsistent mode,
and the launcher is the one place that selects the complete owner set. A
controlled source deployment may use `config/integration.env.example`, which
carries the same marker.

The reviewed Production profile launches
`TASKPLANNER_LIVE_DEFAULT_BUNDLE=thyroidectomy_demo` with `VLM_MODE=real`,
`PERCEPTION_PROVIDER=external_rfdetr_topics`, `PERCEPTION_LOCATION=remote`,
and an empty `PERCEPTION_ENDPOINT`. It does not start local RF-DETR/PNU
inference or an HTTP perception bridge. Camera, ASR, and VLM health are
reported as diagnostics; they are not global scenario-start gates. Robot
execution still requires an implemented external endpoint. The contract below
is a request to controller teams, not evidence of a running server.

## Live ASR input modes

Taskplanner Live keeps both ingress contracts available. The default is the
external tagged-sentence mode; the existing typed microphone mode remains a
runtime-selectable fallback through `SPEECH_INPUT_MODE` or the ASR panel.

### Local typed microphone mode

When `SPEECH_INPUT_MODE=utterance`, the route is:

```text
/sensors/surgeon/utterance                         surgical_msgs/msg/SpeechUtterance
  -> speech_input_adapter (source metadata + final-text validation + dedupe)
  -> /surgery/audio/admitted_utterance              surgical_msgs/msg/SpeechUtterance
  -> CommandRouter (only deterministic command consumer)
  -> catalog-selected typed Topic / Service / Action adapter
  -> endpoint server

CommandRouter
  -> /surgery/audio/observed_utterance               read-only observers
  -> VLM, UI, logs, TTS presentation
```

`/input/asr/runtime_status` reports ASR availability diagnostically. The
adapter is the only ASR ingress owner; raw text never directly triggers an
Action or Service.

### External tagged sentence mode

When `SPEECH_INPUT_MODE=tagged_sentence`, the external ASR publishes
`[partial]` or `[final]` prefixed text to the same adapter. The adapter strips
the marker, emits partial hypotheses on the read-only partial stream, and
admits only `[final]` text to the normal typed command route:

```text
/sensors/surgeon/sentence                         std_msgs/msg/String
  -> speech_input_adapter (tag parsing + dedupe)
  -> /surgery/audio/partial_utterance              surgical_msgs/msg/SpeechUtterance (partial)
  -> /surgery/audio/admitted_utterance              surgical_msgs/msg/SpeechUtterance (final)
```

The existing sentence-only compatibility mode remains available for isolated
Debug/replay callers, but it is not the Live default.

Topic:

```text
/sensors/surgeon/sentence
```

Type:

```text
std_msgs/msg/String
```

Required producer behavior:

- Prefix every message with exactly `[partial]` or `[final]`.
- Publish one current hypothesis per message; only `[final]` is executable.
- Publish only text attributed to the surgeon.
- Do not publish partial hypotheses, token streams, or word-by-word updates.
- Do not include phase labels, hidden actor state, or robot decisions.
- Keep the publisher node alive while this compatibility input is intentionally
  in use. The optional integration observer can report publisher availability,
  but that observation never authorizes or blocks a scenario start.

The adapter trims whitespace, rejects empty messages, suppresses short-window
duplicates, and constructs the same typed `SpeechUtterance` used by Live. Its
only command output is `/surgery/audio/admitted_utterance`; it does not create
a parallel raw-text execution route. Receipt time is the observation time for
this compatibility input.

Adapter activity:

```text
/input/speech/status  surgical_msgs/msg/InputSourceStatus
```

The selected external route reports `source_id=external_sentence_topic` and
`modality=external_topic`. The local route reports
`source_id=local_microphone` and `modality=microphone`. Live exposes typed ASR
runtime status at `/input/asr/runtime_status`; the optional
`/integration/readiness` observer reports that status diagnostically.

### Command routing

`CommandRouter` is the only subscriber that can execute an admitted utterance.
It performs exact catalog matching and sends the matching typed request once.
Catalog misses are non-executable; no observer, model, Digital Twin, or
Behavior Tree gets a second chance to reinterpret the same utterance as a
command.

The router relays each admitted utterance unchanged to
`/surgery/audio/observed_utterance`. VLM and other observers consume that
read-only stream for dialogue and presentation only. The normative extension
guide is [`VOICE_COMMAND_MODULARIZATION.md`](VOICE_COMMAND_MODULARIZATION.md).

Controller motion and safety remain controller-owned. A Service acceptance
response is not evidence of physical completion.

## Vision Input

The browser, Taskplanner observer, and externally owned perception runtime
consume VIPLab's timestamp-preserving `/synced` plane. There is no runtime fallback to
driver-native `/camera/*` or the retired `/preview/*` namespace:

```text
/synced/cam_1/color/image_raw/compressed  sensor_msgs/msg/CompressedImage
/synced/cam_2/color/image_raw/compressed  sensor_msgs/msg/CompressedImage
/synced/cam_3/color/image_raw/compressed  sensor_msgs/msg/CompressedImage
/synced/cam_4/color/image_raw/compressed  sensor_msgs/msg/CompressedImage
/synced/flir/color/image_raw/compressed   sensor_msgs/msg/CompressedImage
```

CAM1, CAM2, and FLIR remain RGB-only. CAM3 and CAM4 additionally publish native
depth, CameraInfo, and the retained factory depth-to-color extrinsics on the
same synchronized contract:

```text
/synced/cam_{3,4}/color/image_raw/compressed
/synced/cam_{3,4}/depth/image_rect_raw/compressedDepth
/synced/cam_{3,4}/color/camera_info
/synced/cam_{3,4}/depth/camera_info
/synced/cam_{3,4}/extrinsics/depth_to_color
```

Large `CompressedImage` streams use `BEST_EFFORT / VOLATILE / KEEP_LAST(1)`.
CameraInfo remains `RELIABLE / VOLATILE / KEEP_LAST(20)` and extrinsics remains
`RELIABLE / TRANSIENT_LOCAL / KEEP_LAST(1)`. The perception computer fans each
CAM3/CAM4 input out locally so Tool, Pose, Hand, and Blood workers do not create
additional VIPLab DDS readers.

Taskplanner monitors source health through small retained status documents,
not by opening another full-rate camera reader:

```text
/synced/cam_{1,2,3,4}/status  std_msgs/msg/String
/synced/flir/status           std_msgs/msg/String
/synced/cam_{3,4}/depth/status std_msgs/msg/String
```

The perception computer may render one shared two-camera Debug raster and one
small status document for Ops only:

```text
/perception/debug/final_overlay/compressed sensor_msgs/msg/CompressedImage
/perception/debug/final_overlay/status     std_msgs/msg/String
```

Production consumes recognition results directly from the reviewed 192.168.1.7
runtime:

```text
/perception/cam_3/tool/observations  surgical_perception_msgs/msg/ToolObservation2DArray
/perception/cam_4/tool/observations  surgical_perception_msgs/msg/ToolObservation2DArray
```

The VLM receives a bounded structured projection: class ID/name, bbox,
observation point, confidence, optional depth, source timestamp, view, model and
ontology version. Detector images, overlays, mask RLE and raw detector payloads
are not VLM observations. Optional integration diagnostics separately report
both topic leases and mandatory provenance metadata, including a non-empty
`model_version`. Production
does not pin that value by default, so a provider checkpoint/version rollout
does not stop planner admission. An exact version pin remains an explicit,
temporary deployment override for incident isolation. An empty but otherwise
valid array is an executed no-detection result; it is not treated as a missing
frame.

`TASKPLANNER_RFDETR_SOURCE_HOST=192.168.1.7` is deployment inventory and the
CycloneDDS profile lists that peer. The current message IDL does not carry a
cryptographic host identity, so application admission is based on typed topic,
schema, view, freshness, monotonic source time and model provenance. Strong host
attestation would require a producer-signed/source-ID contract or DDS Security;
the UI and docs must not claim that a topic name alone proves the machine.

CAM4 hand intent is a separate typed perception path consumed directly by the
Digital Twin:

```text
/perception/cam_4/hand/gestures  hand_keypoint_interfaces/msg/HandGestureArray
/perception/cam_4/hand/facing    hand_keypoint_interfaces/msg/HandFacingArray
/perception/cam_4/hand/health    std_msgs/msg/String
```

Gesture and facing arrays must carry an identical source header and matching
frame-local `hand_index` set containing exactly one detected hand. That one
validated `Right` hand must be classified as `Open_Palm` and faced `PALM_UP`,
continuously fresh for at least 0.300 seconds in both source and receipt time,
to create one tool-agnostic handover evidence episode. A frame with two or more
detected hands is discarded from the implicit-request channel even when only
one hand otherwise qualifies; it still asserts Mayo occupancy. Missing, stale,
asymmetric, ambiguous,
misaligned, unpinned, or unhealthy evidence fails closed. This evidence does
not select an instrument, publish a robot command, or bypass reducer, Behavior
Tree, Action admission, or downstream controller safety. It is not projected
through `ClinicalObservation`.

Taskplanner subscribes to preview images with the provider's
`BEST_EFFORT / VOLATILE / KEEP_LAST(1)` profile. The public
`/surgery/images/*` aliases remain best-effort low-latency outputs.
Populate `header.stamp` with the frame
acquisition time and `format` with `jpeg` or `png`. The dashboard marks a view
disconnected after three seconds without a new frame instead of retaining a
stale surgical image.

### Lab-only PNU/custom CV adapters

PNU, local RF-DETR and future custom CV adapters remain mutually exclusive Lab
providers. CameraInfo/depth streams, timing limits, TF/calibration and ontology
are never inferred from topic names. The standard-message monitor at
`/integration/cv_contract/status` remains diagnostic; it is not part of the
Production RF-DETR typed-topic lease and cannot replace either required view.

See [CV_EXTERNAL_INTERFACE_SCAFFOLD_KO.md](CV_EXTERNAL_INTERFACE_SCAFFOLD_KO.md)
for the exact prepared endpoints, current `WAITING_FOR_PUBLISHER` behavior, and
the on-site package/ownership verification sequence.

## Requested Robot Endpoints

The following are the direct implementation requests. Their names say what the
robot must do; the retired generic `/skill/execute` and
`/bed_robot_arm_group/*/execute` contracts are not runtime entry points or
cross-institution APIs.

```text
/surgery/tool_handover        surgical_interop_msgs/action/ExecuteToolHandover
/surgery/retraction/command   surgical_interop_msgs/srv/ExecuteRetractionCommand
/external/bed_robot_arms/status  surgical_interop_msgs/msg/BedRobotArmStateArray
```

`ExecuteToolHandover` is the single tool-transfer Action. It accepts exactly
seven transitions: `tray -> robot`, `mayo -> robot`, `tray -> surgeon`,
`robot -> surgeon`, `robot -> mayo`, `robot -> tray`, and `mayo -> tray`.
`tray -> robot` and
`mayo -> robot` mean that the robot picks up the next tool already selected by
Taskplanner and holds it ready; neither is a robot-side prediction request.
Taskplanner emits `mayo -> robot` only for a stable next-tool prediction whose
selected instance is a verified `mayo_reuse` candidate with future use expected;
`mayo_recovery` remains ineligible. Success means stable holding has been
reached, and holding persists until a later handover or the normal
`return_unused_preposition` `robot -> mayo` Goal. The only location values are
`tray`, `mayo`, `robot`, and `surgeon`; every
other pair is invalid.

Instance selection uses the same policy for system-predicted preparation and
an explicit surgeon request. An exact matching tool already prepared in the
robot's right hand is reused first. Otherwise, when eligible instances of the
same instrument type exist on both Mayo and the tray, Taskplanner selects the
Mayo instance before the tray instance. CAM4 hand detection blocks autonomous
Mayo preparation/recovery and Mayo-target commands. A live accepted open-palm
request may pick its confirmed rank-1 `mayo_reuse` instance, and a validated
voice request keeps its confirmed Mayo supplier; both exceptions cover only the
Mayo-to-right-hand `prepare_tool` leg and are rechecked at final dispatch. An
uncommitted non-voice request already bound to Mayo is rebound to an eligible
non-Mayo duplicate when the hand arrives without changing its request
generation. Hand-free admission for every other Mayo command requires a
continuously fresh, pinned empty gesture stream; detector-health loss or more
than `0.400 s` of silence restores the fail-closed occupied state, and
stale/future empty frames cannot clear it.
If a
different tool is already prepared in the right hand, Taskplanner first parks
that held instance with a `robot -> mayo` Goal, but only while the pinned CAM4
Mayo view is hand-free. While occupied, the replacement remains pending and no
Mayo Goal is sent. Only after that parking transition completes does
Taskplanner pick the newly selected tool. The
controller's cancel-recovery `robot -> tray` result is a separate failure path,
not the normal replacement destination.

For a scenario-declared exchangeable population, a fresh admitted typed CAM4
Mayo observation may activate one previously dormant logical instance, but
never beyond `tool_population.capacity`. That activation is owned by the
Digital Twin, preserves the separately addressable rack instance, and makes the
new Mayo instance eligible for the same Mayo-first selection above. Generic VLM
observations and the observation-only belief tracker cannot activate a control
instance or move home inventory by themselves.

The tool-transfer Action is single-flight regardless of request provenance. A
new prediction, hand signal, or voice-backed explicit request never cancels or
overlaps an active Goal, and no second Goal is admitted. Prediction and explicit
request observations may remain upstream, but that is not execution admission.
During an active direct delivery, the CAM4 receiving-hand cue is instead
withdrawn and ignored; task completion does not re-arm it without a fresh
`0.500 s` release. Any later retry or re-evaluation is permitted only after the
active Goal's authoritative terminal result has entered WorldState. The
execution bridge independently rejects any raced command with
`tool_transfer_busy`.

`instrument_id` carries the shared real name (for example
`Bovie surgical cautery`), never an internal catalog code such as `T04`.
Taskplanner also converts an internal instance such as `T04#1` to
`Bovie surgical cautery#1`. The Goal has no arm field: the receiving robot
controller chooses the arm. It also excludes planner rationale, detailed
internal anchors, target-owner, cleaning policy, and execution mode.

A terminal success is accepted only for the currently tracked Goal when ROS
reports `SUCCEEDED` and the Result reports `success=true`,
`final_state=completed`, with an empty or `completed` reason. Taskplanner then
projects the completed semantic leg into the Digital Twin even if its local
lifecycle or arm-occupancy belief conflicts. A stale detector/VLM observation
cannot veto physical completion. Command, instance, instrument type, semantic
leg, projection-step, duplicate, and timestamp correlation still fail closed;
failed or canceled Results never enter this success path.

### Retraction command Service

`/surgery/retraction/command` is the single Service for direct teach,
retraction, adjustment, tool change, and retraction stop. Its wire shape is
exactly:

```text
# ExecuteRetractionCommand.srv
uint16 PROTOCOL_VERSION_V1=1
uint8 COMMAND_START_DIRECT_TEACH=1
uint8 COMMAND_FINISH_DIRECT_TEACH=2
uint8 COMMAND_START_RETRACTION=3
uint8 COMMAND_ADJUST_RETRACTION=4
uint8 COMMAND_CHANGE_TOOL=5
uint8 COMMAND_STOP_RETRACTION=6
uint8 TARGET_NONE=0
uint8 TARGET_LEFT=1
uint8 TARGET_RIGHT=2
uint8 TARGET_BOTH=3
uint16 protocol_version
string source_id
string command_id
uint8 command
uint8 target_side
float64 distance_m
---
uint16 RESULT_ACCEPTED=0
uint16 RESULT_INVALID_COMMAND=1
uint16 RESULT_INVALID_PARAMETER=2
uint16 RESULT_REJECTED=3
uint16 RESULT_ERROR=255
bool request_accepted
uint16 result_code
string command_id
string message
```

| Request field | Interpretation |
| --- | --- |
| `protocol_version` | `PROTOCOL_VERSION_V1` for this interface version. |
| `source_id` | Calling client identifier. |
| `command_id` | Caller-generated Request/Response correlation ID. |
| `command` | One of the six documented `COMMAND_*` constants. |
| `target_side` | `TARGET_NONE`, `TARGET_LEFT`, `TARGET_RIGHT`, or `TARGET_BOTH`; adjustment accepts `LEFT`, `RIGHT`, or `BOTH` (3), applying the same distance independently to both arms for `BOTH`. Direct-teach finish accepts `NONE`, `LEFT`, or `RIGHT` as an optional target selector. |
| `distance_m` | Metres; a 5 cm adjustment is `0.050`. All non-adjustment commands use `0.0`. |

`request_accepted` and `result_code` describe only whether the server admitted
the Request. They do not indicate physical completion, progress, controller
state, cancellation, tool attachment, or a retry/idempotency policy. The
controller owns implementation and safety behavior after admission.

### Retraction-arm status Topic

The controller publishes `/external/bed_robot_arms/status` with exactly these
messages:

```text
# BedRobotArmStateArray.msg
builtin_interfaces/Time stamp
uint64 revision
string procedure_type
BedRobotArmState[] arms

# BedRobotArmState.msg
string arm_id
string role
string role_instance_id
string state
bool direct_teach_active
string reason_code
```

| Field | Allowed values and interpretation |
| --- | --- |
| `stamp` | Wall-clock ROS 2 publication time for the snapshot. It must be fresh at reception and at command dispatch; replay `/clock` must not be used for this controller-owned status. |
| `revision` | Non-negative, monotonically increasing snapshot sequence. |
| `procedure_type` | `thyroidectomy` or `nephrectomy`. |
| `arms` | One entry for thyroidectomy; two entries for nephrectomy. |
| `arm_id` | `arm_1` or `arm_2`. |
| `role` | Always `retraction`. |
| `role_instance_id` | `army_navy` for thyroidectomy; `left_malleable` and `right_malleable` for nephrectomy. |
| `state` | `standby`, `direct_teach`, `retracting`, `changing_tool`, `moving_to_standby`, `fault`, `protective_stop`, or `unknown`. |
| `direct_teach_active` | Auxiliary flag for quick direct-teach-state checking. |
| `reason_code` | Machine-readable context such as `ok`, `teach_button_active`, `estop`, `protective_stop`, or `controller_unavailable`; may be empty when appropriate. |

Detailed controller motion state remains controller-owned. Taskplanner neither
requests nor synthesizes joint state, pose, trajectory, velocity, force,
collision state, E-stop internals, or verified tool attachment through this
contract.

The controller must publish a strictly newer source timestamp for each status
heartbeat. A recently received message with an old or future-dated source stamp
is rejected and cannot authorize dispatch. A lower revision is accepted only
with a strictly newer, fresh source stamp as evidence that the controller
restarted; any in-flight retraction command then remains unresolved and ordinary
dispatch is blocked.

The bed-mounted suction arm is not part of this ROS 2 contract or Taskplanner
control path. In the source interface document, the thyroidectomy suction arm
is pure direct-teach operation and is therefore excluded from Taskplanner ROS
integration. Clinical suction instruments and surgeon speech about suction
remain ordinary surgical evidence and tool semantics; no medical-device
suction on/off, pressure, or flow command is exposed here.

Controllers return the machine-readable result and reason fields defined by
each interface. They must enforce their own homing, E-stop, collision, force,
distance-limit, and protective-stop policy. A controller must not publish
synthetic digital-twin state; Taskplanner converts observed execution results
into its internal status and event streams.

### Tool handover state and interrupt contract

KAIST does not need to add a separate integration-state topic for the initial
handover integration. `/surgery/tool_handover` Action feedback is the
goal-scoped robot-state report, and Taskplanner owns the public
`/surgery/robots` projection.

`ExecuteToolHandover.Feedback.state` accepts only the following values:

| State | Meaning |
| --- | --- |
| `moving_to_source` | Moving toward the Goal's source location. |
| `grasping` | Acquiring and verifying a stable grasp. |
| `moving_to_target` | Moving the secured tool toward the Goal's target. |
| `waiting_for_takeover` | Holding at the surgeon handover pose until takeover is confirmed. |
| `placing` | Placing and releasing the tool on the Goal's Mayo or tray target. |
| `holding` | Holding the selected tool stably on the robot (`tray -> robot` or `mayo -> robot`). |
| `stopping` | A cancel was accepted and safe stopping is in progress. |
| `retreating` | No grasp was confirmed; the robot is retreating while the tool remains at the source. |
| `recovering_to_tray` | A grasp was confirmed; the robot is returning the held tool to its configured tray recovery pose. |

States that do not apply to a location pair are skipped. `progress` is a
monotonic Action-lifecycle value in `[0.0, 1.0]`, including cancel recovery. It
reaches `1.0` at any terminal Result; consumers must use `success` rather than
progress to determine the outcome and must not infer a physical pose or a
deadline. The only Result `final_state` values are `completed`, `canceled`, and
`failed`. `completed` requires `success=true`; the other two require
`success=false`.

Interrupt uses the standard ROS 2 Action cancel request, not a custom message
or topic. It is a compensating recovery protocol rather than an instantaneous
terminal transition:

1. Taskplanner requests Cancel for the active Goal and does not send the next
   tool Goal yet.
2. The server reports `stopping` while arresting the current trajectory.
3. If grasp was not confirmed, the server reports `retreating`, moves to a safe
   non-contact pose, leaves the instrument at `source_location`, and returns
   `final_state=canceled`, `reason_code=canceled_source_unchanged`.
4. If grasp was confirmed, or the Goal started with `source_location=robot`,
   the server reports `recovering_to_tray`, places the instrument at its
   preconfigured semantic `tray` recovery pose, and returns
   `final_state=canceled`, `reason_code=canceled_recovered_to_tray`.
5. Only after one of those terminal Results may Taskplanner send the next Goal.

The controller must serialize tool Goals and must not execute or queue a new
tool Goal while normal motion or cancel recovery is active. The recovery tray
pose is controller configuration, not a Goal field. Because the public contract
contains no exact slot or trajectory, it guarantees semantic recovery to
`tray`, not reversal to the exact original pose or slot.

Confirmed release to the surgeon is the physical commit point. Once it has
occurred, the Goal finishes `completed`; the server must not attempt to retrieve
the tool from the surgeon as a rollback. If grasp state is uncertain, the tray
pose is unavailable, or recovery cannot be verified, the server returns
`final_state=failed`, `reason_code=cancel_recovery_failed`. Taskplanner then
blocks the next ordinary command until an operator or a separately defined
recovery procedure resolves the state. A hardware E-stop or protective stop
remains controller-local and also aborts the Goal as `failed`.

Cancel is never retroactive. If `tray -> robot` or `mayo -> robot` has already
completed and the robot is holding a prepared tool that is no longer needed,
Taskplanner sends a new `return_unused_preposition` `robot -> mayo` Goal. This
fast reversible leg frees the preparation hand before the next tool is selected.
It is selected only for a canonical explicit request for a different tool, or
when Taskplanner's system-final rank 1 changes to a different eligible tool and
remains there for at least 2.0 continuous source-time seconds. Raw VLM rank
changes, elapsed holding time, evidence disappearance, and procedure completion
do not select it. A tracked tool Action blocks dispatch until its terminal result
has been projected into WorldState.
It is distinct from `canceled_recovered_to_tray`, where an in-flight canceled
Goal has already crossed the grasp boundary and the controller verifies a safe
compensating placement at its generic tray recovery pose.

## Shared Surgical State Published by Taskplanner

The `surgical_interop_gateway` is enabled by default and publishes the following
read-only, human-readable topics. An intentionally isolated deployment may set
`PUBLISH_SHARED_STATE=false`.

Free-form text has a separate fail-closed switch. The default
`PUBLISH_SHARED_FREE_TEXT=false` keeps ASR/VLM typed metadata public but emits
empty `SpeechRecognitionState.text` and `ClinicalObservation.summary`. Evidence
status ends in `_REDACTED` when an upstream value was suppressed. Setting this
switch true is a deployment-level PHI decision; the Gateway does not
de-identify the resulting transcript or summary.

Browser-only consumers use the dedicated read-only WebSocket endpoint
`ws://<Taskplanner-wired-IP>:9092`. It registers only the Subscribe capability
(`subscribe` and `unsubscribe` operations) and has an exact allowlist containing
the eleven topics below plus the five gated camera aliases. Incoming `fragment`
frames and every unknown operation are rejected rather than reassembled. The
endpoint is a memory-limited sidecar behind the designated
wired-interface/subnet proxy; the sidecar rejects direct non-loopback peers
before WebSocket upgrade, including VPN/Tailscale loopback-DNAT bypasses. It is
not the operator rosbridge on port 9090. Origin validation is defense in depth,
not authentication. Each incoming frame must be one complete JSON request and
is capped at 64 KiB; malformed/incomplete input clears the parser and closes the
connection. Camera compression is forced to CBOR, and outgoing fragmentation is
disabled so a logical message over 4 MiB is dropped without emitting fragments.
The reviewed browser media names are `/surgery/images/flir/compressed`,
`/surgery/images/cam4/compressed`, `/surgery/images/cam3/overlay/compressed`,
`/surgery/images/suction/overlay/compressed`, and
`/surgery/images/right_ee/overlay/compressed`. Native `/synced/*` and
`/perception/*` names are not part of the browser contract.

Native DDS is deliberately limited to mutually trusted, managed controller
computers. It is not an authentication or ACL boundary: any participant on the
same ROS domain/discovery network can discover internal topics and can publish
conflicting samples under public or internal topic names. Gateway free-text
suppression, camera gating, and the port-9092 allowlist constrain only
Taskplanner-owned outputs; they do not filter another DDS participant. A
browser-only UI computer must use port 9092 and must not join the ROS domain.
Do not route DDS over Wi-Fi, Tailscale/VPN, or the Internet. Untrusted native
DDS requires ROS 2/DDS Security identities, governance, and permissions.

```text
/surgery/context                surgical_interop_msgs/msg/SurgeryContext
/surgery/instruments            surgical_interop_msgs/msg/InstrumentStateArray
/surgery/robots                 surgical_interop_msgs/msg/RobotStateArray
/surgery/robot_end_effectors    surgical_interop_msgs/msg/RobotEndEffectorStateArray
/surgery/tool_predictions       surgical_interop_msgs/msg/ToolPredictionArray
/surgery/speech                 surgical_interop_msgs/msg/SpeechRecognitionState
/surgery/events                 surgical_interop_msgs/msg/SurgeryEvent
/surgery/clinical_observations  surgical_interop_msgs/msg/ClinicalObservationArray
/surgery/health                 surgical_interop_msgs/msg/SurgeryHealth
/surgery/gateway_info           surgical_interop_msgs/msg/GatewayInfo
/surgery/catalog                surgical_interop_msgs/msg/ProcedureCatalog
```

These are projections of DT and VLM data, not aliases of the internal topics.
The gateway never publishes `raw_json`, `detail_json`, planner rationale,
hidden actor state, or an unvalidated clinical conclusion. The reviewed top-3
next-tool forecast and semantic robot-hand possession state are narrow v0.5
projections with dedicated public types; the rest of internal `WorldState`
remains private.
`ClinicalObservation` contains VLM phase, tool, semantic location, uncertainty,
and optional summary only. Gesture category, hand pose/facing, requested tool,
and handover intent are deliberately absent. The direct CAM4 handover evidence
described above remains internal and is never copied into this public projection.
Each `SurgeryEvent` embeds schema/catalog, gateway-instance, procedure-run, and
procedure-type identity. Consumers group events by
`(gateway_instance_id, procedure_run_id)` before ordering them by `sequence`;
this remains unambiguous when the first event precedes the next heartbeat.
Every public confidence/uncertainty value is finite and within `[0,1]`.
Malformed claims become `UNKNOWN` or are omitted, and clinical parallel arrays
are length-validated at the Gateway boundary.
Public surgeon-side instrument rows use the single semantic location
`surgeon`; public Mayo rows use the single physical location `mayo_stand` and
distinguish reuse from recovery with `state`. Internal hand/field/bed and
reuse/recovery-zone values are not public. Locations are not 3D poses unless a
separately calibrated pose contract is
added.

When no procedure is active or WorldState is stale, dynamic topics overwrite
their retained samples with empty/unknown values. `gateway_info`, `catalog`,
and `health` remain available so an external UI can distinguish idle from an
unreachable runtime. See `docs/SHARED_SURGICAL_STATE_CONTRACT.md` for the full
QoS, run identity, reconnect, media-alias, and idle-state contract.

Every public state, event, and observation carries an `evidence_status`:

- `DT_ACCEPTED`: accepted by the deterministic digital-twin reducer as current
  operational state.
- `MODEL_OBSERVED`: a VLM observation or hypothesis, not a clinical fact.
- `GATEWAY_OBSERVED_REDACTED` or `MODEL_OBSERVED_REDACTED`: a free-text value
  existed upstream but was suppressed; use only the remaining typed metadata.
- `CLINICIAN_CONFIRMED`: available only after a clinician confirmation workflow
  supplies it.
- `UNKNOWN` or `REJECTED`: insufficient or rejected evidence.

## Optional integration diagnostics

`/integration/check_readiness` uses `std_srvs/srv/Trigger`. It is enabled for a
real external integration only after the relevant provider has agreed to the
contract. It reports unavailable or stale dependencies, including:

- the sentence topic has no publisher;
- the required tool-handover Action, the active procedure's applicable
  tool-change Service or retraction-adjustment Action, or the retraction-arm
  status publisher is unavailable;
- real VLM mode is selected but RF-DETR has no fresh, aligned FLIR/CAM4 result.

The launch-only observer switch is
`enable_integration_preflight_diagnostics`. It defaults on for Live and off for
Mock/LLM profiles; it has no legacy `require_integration_preflight` alias.

This observer is read-only: its result must not block scenario lifecycle,
voice procedure control, or typed Action/Service dispatch. The selected
endpoint still performs its own type/range validation, endpoint availability,
command-id idempotency, cancellation, and result handling. Physical controller
homing, E-stop, protective-stop, collision, and force limits remain the
downstream controller's responsibility and must reject goals when unsafe.

## Allowed Public Evidence

The VLM may consume:

- admitted surgeon speech transcripts
- camera images
- completed or observable robot skill events
- digital-twin tool locations derived from public observations and action status
- retraction-arm requests derived from public speech and controller-owned status
- the previous VLM result as temporal memory
- the active procedure YAML as a prior

The VLM does not consume:

- `/perception/cam_4/hand/gestures`
- `/perception/cam_4/hand/facing`
- `/perception/cam_4/hand/health`
- `/surgeon/state`
- `/surgeon/actor_event`
- `/surgeon/actor_overlay`
- a structured request emitted directly by a validation actor
- the actor's internal phase or planned next tool
- the system-fused phase as the raw VLM phase answer

The three `/surgeon/actor_*` topics are validation-only simulation outputs.
External systems must not publish them.

The VLM does not infer or authorize handedness, palm openness, palm facing, or
handover intent. Those properties are evaluated only by the typed CAM4 path
above. The resulting evidence is tool-agnostic; instrument selection remains a
separate reducer/Behavior Tree decision and execution still uses the guarded
`ExecuteToolHandover` Action path.

## Debug/replay sentence-only baseline

A Debug/replay sentence compatibility input is converted by
`speech_input_adapter` into the normal typed admitted utterance. `CommandRouter`
then applies the same hot-reloadable catalog used by Live. VLM, Digital Twin,
and BT are observers or independent workflow owners; none is a second
speech-command admission path.

## Wired-LAN Checklist

1. Assign stable IP addresses or DHCP reservations.
2. Put every ROS computer on the same `ROS_DOMAIN_ID`.
3. Set `ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET`.
4. Confirm host firewalls allow ROS 2 DDS UDP traffic on the integration NIC.
5. Synchronize clocks with NTP or PTP.
6. Verify `ros2 topic info -v` before starting a scenario.
7. For Live, inspect the selected ingress: typed microphone
   `/sensors/surgeon/utterance` or external tagged
   `/sensors/surgeon/sentence`, plus `/input/speech/status` and
   `/input/asr/runtime_status`.
8. Implement and validate only the controller endpoints needed for the planned
   experiment. `/integration/check_readiness` is diagnostic, not a global start
   gate.
9. Inspect VLM and perception health when their observation is part of the
   experiment; a deterministic catalog command does not wait for either one.
10. Add CAM1-CAM4 and FLIR only when their dashboard/debug observation is
    needed; source health remains independently visible.

For routed networks, replace multicast discovery with a DDS discovery server
or explicit peers. Do not expose DDS beyond the isolated integration network.
