# Shared Surgical State Contract

This contract defines the read-only information that Taskplanner shares with
partner institutions. By default it projects a reviewed, structured subset of
internal state onto stable ROS 2 topics without exposing free-form speech or
clinical-summary text, planner rationale, raw model payloads, prompts, robot
trajectories, or private controller details.

The Git definitions in `surgical_interop_msgs/msg`, `.action`, and `.srv` are
the wire-format authority. This document defines runtime, ownership, freshness,
and consumer behavior. If an example conflicts with an IDL file, the IDL wins.

## Ownership and startup

Taskplanner is the sole publisher of every `/surgery/*` state topic in this
contract. Partners subscribe; they must not create another publisher on the
same name. The state Gateway is read-only and exposes no control service.

The base Taskplanner runtime starts the state Gateway by default in live and
simulation/LLM demonstration modes. The live integration wrapper additionally
starts the two camera aliases by default:

```bash
# Public state is enabled by default.
ros2 launch bringup taskplanner_live.launch.py

# Explicit isolation escape hatch.
ros2 launch bringup taskplanner_live.launch.py \
  publish_shared_state:=false publish_camera_aliases:=false

# Deliberate PHI-capable deployment opt-in; never use as a general default.
ros2 launch bringup taskplanner_live.launch.py \
  publish_shared_free_text:=true
```

The corresponding environment defaults are:

```text
PUBLISH_SHARED_STATE=true
PUBLISH_CAMERA_ALIASES=true
PUBLISH_SHARED_FREE_TEXT=false
PUBLIC_ROSBRIDGE_ALLOWED_ORIGINS=
PUBLIC_ROSBRIDGE_PORT=9092
ENABLE_PUBLIC_ROSBRIDGE=true
```

`PUBLISH_SHARED_FREE_TEXT=false` does not disable `/surgery/speech` or
`/surgery/clinical_observations`. It keeps their typed availability, sequence,
confidence, and structured identifier fields available while setting `text`
and `summary` to empty strings. A deployment may set it to `true` only after
the sending and receiving institutions approve the transport, retention,
access-control, and PHI-handling policy. The Gateway performs no de-identification
of opted-in free text and does not guarantee that it is PHI-free.

The public topics remain discoverable while no scenario is running. This is an
intentional development contract: an idle Taskplanner emits an inactive context
and safe empty/unknown dynamic snapshots instead of retaining the previous
scenario. Heartbeat, health, and static catalog data remain available.

`/surgery/robots` is a Taskplanner-owned projection, not a robot-controller
input. The bed-mounted controller publishes its source state on
`/external/bed_robot_arms/status`; Taskplanner sends commands through the
separate Action/Service contracts.

## Public topic set

All eleven state/event topics use `surgical_interop_msgs` 0.3.0.

| Topic | Type | Category | Purpose |
| --- | --- | --- | --- |
| `/surgery/gateway_info` | `GatewayInfo` | snapshot | Gateway heartbeat, schema/interface identity, catalog digest, process identity, and active run identity. |
| `/surgery/catalog` | `ProcedureCatalog` | snapshot | Procedure-scoped Korean/English phase and instrument display metadata. |
| `/surgery/context` | `SurgeryContext` | snapshot | Current procedure, phase, execution state, confidence, uncertainty, and public safety flags. |
| `/surgery/instruments` | `InstrumentStateArray` | snapshot | Current semantic location/state of instrument instances. |
| `/surgery/robots` | `RobotStateArray` | snapshot | Current controller-facing execution state of participating robots. |
| `/surgery/robot_end_effectors` | `RobotEndEffectorStateArray` | snapshot | Semantic empty/holding/unknown state of the humanoid hands and their held tool IDs. |
| `/surgery/tool_predictions` | `ToolPredictionArray` | snapshot | Ranked advisory next-instrument forecast; never a command or handover authorization. |
| `/surgery/speech` | `SpeechRecognitionState` | snapshot | ASR availability, connectivity, sequence, and measured response latency; finalized transcript text requires explicit privacy opt-in. |
| `/surgery/clinical_observations` | `ClinicalObservationArray` | snapshot | Structured VLM/perception observations marked as model evidence; free-form summary requires explicit privacy opt-in. |
| `/surgery/health` | `SurgeryHealth` | snapshot | Gateway-measured source availability, freshness, and stable error codes. |
| `/surgery/events` | `SurgeryEvent` | event | Ordered public state-change facts emitted while an active procedure is fresh. |

The live integration runtime also advertises two stable media aliases:

| Public topic | Type | Default native source |
| --- | --- | --- |
| `/surgery/images/flir/compressed` | `sensor_msgs/msg/CompressedImage` | `/synced/flir/color/image_raw/compressed` |
| `/surgery/images/cam4/compressed` | `sensor_msgs/msg/CompressedImage` | `/synced/cam_4/color/image_raw/compressed` |

## QoS and publication behavior

| Category | Reliability | Durability | History | Default behavior |
| --- | --- | --- | --- | --- |
| Ten state snapshots | Reliable | Transient Local | Keep Last 1 | Approximately 1 Hz; late joiners receive the latest snapshot. |
| `/surgery/events` | Reliable | Volatile | Keep Last 50 | Emitted immediately; no durable replay before subscription. |
| Camera aliases | Best Effort | Volatile | Keep Last 5 | Frames pass through only on active, fresh, matched demand. |

Snapshot subscribers that need the retained value should request reliable and
transient-local QoS. Event subscribers should use reliable and volatile QoS.
Image consumers should use best-effort and volatile QoS. Discovery without a
matching QoS does not prove that samples can be received.

`revision` is a process-local publication-cycle counter. It increases on every
Gateway timer cycle, even when semantic state does not change, and resets when
`gateway_instance_id` changes. It is not a database revision. The five legacy
snapshot types carry only `stamp` and `revision`; new v0.3 types additionally
carry schema, process, catalog, and run identity.

`SurgeryEvent.sequence` and `ClinicalObservation.sequence` are process-local
monotonic counters. `SpeechRecognitionState.utterance_sequence` is run-local
and resets when `procedure_run_id` or `gateway_instance_id` changes.

Every `SurgeryEvent` carries `schema_version`, `catalog_version`,
`gateway_instance_id`, `procedure_run_id`, and `procedure_type`. The Gateway
captures these fields atomically with event acceptance, so a first event may be
assigned to its run even if it arrives before the next one-hertz
`/surgery/gateway_info` heartbeat. Consumers must group events by
`(gateway_instance_id, procedure_run_id)` and then order them by `sequence`.
Sequence alone is never a globally unique event key.

## Identity, run, and reconnect rules

`/surgery/gateway_info` is the first topic a consumer should acquire. Its
fields have the following semantics:

- `schema_version`: public projection schema (`1.1.0` for this contract).
- `interface_version`: installed interface package (`0.3.0`).
- `catalog_version`: deterministic SHA-256 digest of the published catalog.
- `gateway_instance_id`: opaque non-PHI UUID created once per Gateway process.
- `procedure_run_id`: opaque non-PHI UUID for one active run; empty while idle.
- `procedure_type`: selected procedure catalog identifier.
- `procedure_active`: true only for fresh, matching, running WorldState.

On a new `gateway_instance_id`, consumers must discard revision/sequence
deduplication state and rebuild their cache. On a new `catalog_version`, they
must rebuild all phase/tool labels. On a new `procedure_run_id`, they must clear
the previous run timeline and dynamic state. A missing/stale heartbeat is
Gateway unavailable, not an idle procedure.

The Gateway fails closed if a running WorldState reports a procedure type that
does not match the loaded catalog. It publishes inactive/empty dynamic state and
adds `procedure_catalog_mismatch` to `/surgery/health.error_codes`.

## Idle and stale-state contract

The authoritative active predicate is a fresh WorldState with `running=true`
and the selected catalog's procedure ID. WorldState expires after three seconds
by default. The following output is required when the predicate is false:

| Topic | Idle/stale output |
| --- | --- |
| `/surgery/gateway_info` | Heartbeat continues; `procedure_active=false`, `procedure_run_id=""`. |
| `/surgery/catalog` | Phase and instrument metadata remains populated; `procedure_active=false`. |
| `/surgery/context` | `procedure_active=false`, `phase_uncertain=true`, `evidence_status=UNKNOWN`; phase/execution strings empty. |
| `/surgery/instruments` | Empty `instruments`. |
| `/surgery/robots` | Empty `robots`. |
| `/surgery/robot_end_effectors` | Empty `end_effectors`. |
| `/surgery/tool_predictions` | Empty `predictions`. |
| `/surgery/speech` | `available=false`, `connected=false`, `state=unavailable`, empty text. |
| `/surgery/clinical_observations` | Empty `observations`. |
| `/surgery/events` | No event is emitted. |
| `/surgery/health` | Continues to report actual source availability/freshness. |
| Camera aliases | Publishers remain discoverable, but no native subscription or frame forwarding occurs. |

Run-scoped speech, VLM, robot, and controller caches are cleared on the active
boundary. A new run therefore cannot replay a still-fresh value from the
previous run.

## Authority and evidence status

Consumers must preserve the authority label instead of flattening every row
into one kind of fact.

| Value | Meaning | Consumer rule |
| --- | --- | --- |
| `DT_ACCEPTED` | The digital twin accepted this operating-state/event fact. | May represent current Taskplanner state; not clinical confirmation and not proof an operation succeeded. |
| `MODEL_OBSERVED` | A model observed or inferred the item. | Display as model evidence; never command a robot from this alone. |
| `CLINICIAN_CONFIRMED` | An authorized clinician explicitly confirmed the item. | Apply the consuming system's own safety policy. |
| `GATEWAY_OBSERVED` | The Gateway measured runtime availability/freshness. | Transport-health evidence only. |
| `GATEWAY_OBSERVED_REDACTED` | A finalized speech item exists, but its free-form text is suppressed by public-boundary policy. | Use sequence/state/latency metadata only; display no inferred transcript. |
| `MODEL_OBSERVED_REDACTED` | Structured model evidence is available, but its free-form summary is suppressed by public-boundary policy. | Display only the typed IDs/confidences; do not synthesize a summary. |
| `UNKNOWN` | Evidence is missing, stale, insufficient, or intentionally cleared. | Do not infer a default. |

For `/surgery/events`, `evidence_status=DT_ACCEPTED` means the event fact was
accepted for publication. The event's actual outcome is in `state`; for
example `PhaseTransitionRejected` has `state=rejected`, not success. The
Gateway exposes only a reviewed `command_id` or `task_id` as `correlation_id`
from private event detail. Other detail remains private.

## Topic details

### `/surgery/gateway_info`

Use this periodic heartbeat to distinguish idle from stopped, identify process
restart, and bind every v0.3 snapshot to the matching catalog and run. Do not
treat a UUID as a patient or case identifier; it is deliberately opaque and
ephemeral.

### `/surgery/catalog`

`ProcedureCatalog` remains populated while idle so a UI can build screens before
a run starts. `PhaseCatalogEntry` provides authored order, stable ID,
English/Korean label, normal/interrupt kind, possible next phase IDs, and
expected tool IDs. `InstrumentCatalogEntry` provides stable ID,
English/Korean label, aliases, category, configured inventory count,
requestability, and role.

Catalog transitions and expected tools are authored metadata. An empty list
means nothing was declared; it does not prove a clinical transition is
impossible. Instrument IDs are procedure-scoped and must be joined with
`procedure_type` and `catalog_version`.

### `/surgery/context`

This is the concise accepted operating context. `current_phase`, confidence,
uncertainty, execution state, and safety flags are current state, not a future
phase prediction. When idle/stale, the Gateway overwrites its retained sample
with an explicit unknown state.

### `/surgery/instruments`

Each row identifies an instrument type/instance, semantic location, holder,
state, confidence, and evidence. Locations are not calibrated 3D poses or robot
frames. `visible=false` currently means the Gateway makes no visibility
assertion; it must not be shown as proof that a tool was visually absent.

The public physical ontology deliberately collapses private planner detail:

- A surgeon-side tool uses `location_type=surgeon` and
  `location_id=surgeon`. Current surgeon-used tools are rows with
  `holder_role=surgeon` and `state` equal to `handed_over` or `in_use`.
- Every tool physically on Mayo uses `location_type=mayo_stand` and
  `location_id=mayo_stand`. `state=parked_for_reuse` versus
  `state=awaiting_retrieval` expresses policy; these are not separate zones.
- Private values such as `surgeon_hand`, `surgical_field`, `bed_fixed_tool`,
  `mayo_reuse_zone`, and `mayo_recovery_zone` are never public location values.

### `/surgery/robots`

This snapshot combines fresh Taskplanner skill status for the humanoid and
fresh controller-owned bed retraction-arm state. A row may be absent when its
source has not produced fresh status. `connection_state=unknown` is deliberate
when the controller contract has no authoritative connection boolean.

`progress` is a goal lifecycle estimate, not a pose or remaining-time promise.
`active_command_id` is present only when the source contract supplies one.

### `/surgery/robot_end_effectors`

While active and fresh, the current implementation publishes `right_hand` and
`left_hand` for `robot_id=humanoid`. `state` is `empty`, `holding`, or
`unknown`. `holding` names the catalog tool and, when known, its instance.
This is semantic possession only; joint, pose, force, and trajectory data are
intentionally excluded.

### `/surgery/tool_predictions`

The current implementation publishes up to three reducer-accepted candidates,
ordered by descending confidence with contiguous ranks `1..N`. Rank 1 is the
same candidate used by the legacy private control scalar; ranks 2 and 3 never
enter BT or robot-control policy. Their `stability_sec` is conservatively `0.0`
until an independent advisory continuity contract exists. An empty array means
no public prediction is available. Every row is advisory display information
and is never authorization to issue a handover.

### `/surgery/speech`

This snapshot is the supported public ASR interface. It contains bounded public
runtime status and finalized-utterance metadata. It is not audio, a
partial-token stream, or proof that the planner accepted or executed a sentence.

With the default `PUBLISH_SHARED_FREE_TEXT=false`, a finalized utterance keeps
its `utterance_sequence`, receipt stamp, ASR state, availability, and validated
latency fields, but `text=""` and
`evidence_status=GATEWAY_OBSERVED_REDACTED`. With explicit opt-in, `text`
contains the upstream finalized sentence and `evidence_status=GATEWAY_OBSERVED`.
Consumers must never infer hidden words from the structured metadata.

`latency_available` must be true before using `response_latency_ms`;
`latency_basis` names the measurement basis. The plain String input has no
source timestamp, so `utterance_stamp` is Gateway receipt time. ASR status must
be fresh and connected before text becomes available. `latency_basis` is an
allowlisted machine token, not upstream free-form diagnostic text; an unknown
basis suppresses the latency fields.

### `/surgery/clinical_observations`

The latest fresh VLM result is exposed as structured `MODEL_OBSERVED` evidence.
The phase ID/confidence arrays and observed tool/location/confidence arrays are
parallel by contract. The Gateway validates their lengths and excludes invalid
rows; consumers must nevertheless length-check before joining them. All public
confidence and uncertainty values are finite and in `[0,1]`. A malformed
numeric claim is omitted or published with safe `UNKNOWN` evidence rather than
clamped into an apparently valid observation. Raw JSON, prompts, hidden
reasoning, and private prediction fields are not published.

With the default free-text policy, typed observation fields stay populated but
`summary=""`. If the upstream observation contained a summary, its public
`evidence_status` is `MODEL_OBSERVED_REDACTED`; otherwise it remains
`MODEL_OBSERVED`. Explicit opt-in publishes the upstream summary without
de-identifying it.

### `/surgery/health`

This reports all configured sources in `unavailable_sources` and
`stale_sources`, but the default overall `healthy` gate requires only
`world_state` and `speech_input`. Optional camera, VLM, and robot-source
availability therefore must be read from the arrays; `healthy=true` does not
mean every optional source exists. Stable errors include
`procedure_catalog_mismatch`, model/robot errors, and accepted input error
codes. Free-form diagnostic text remains private.

### `/surgery/events`

Events are live notifications, not durable replay. Use snapshots to rebuild
current state after reconnect. `sequence` orders events within one
`gateway_instance_id`; a gap means the consumer missed events and should refresh
snapshots. Rejected and failed outcomes are published with explicit `state` and
must not be displayed as successful transitions.

## Camera media boundary

The camera relay never decodes, re-encodes, resizes, persists, or synthesizes
frames. It creates a native source subscription only when all three conditions
hold: a consumer is matched, WorldState is fresh and running, and its procedure
ID matches the selected bundle. Stop, mismatch, or stale state releases the
native subscription and the frame callback independently rechecks the same gate
to drop late queued frames.

The native and public topic must be distinct. A source configured directly on
the public name, duplicate public aliases, and cross-topic cycles are rejected
at startup so another publisher cannot bypass the idle privacy gate.

The public rosbridge independently forces camera subscriptions to queue length
one and at most 10 Hz, even if a browser requests an unbounded queue or a faster
rate. Camera subscriptions are normalized to CBOR even if a client requests PNG;
PNG and unknown compression modes are never admitted to the camera encoder.
Each client has a four-message drop-oldest egress queue. Consumers should still
request CBOR and render only the latest frame. The media alias resolution, frame
ID, and encoding follow the external camera source and are not fixed by this
contract.

## Consumer startup and reconnect sequence

1. Use the deployment-provided ROS domain, discovery, RMW, and network values.
2. Confirm `surgical_interop_msgs` 0.3.0 is installed and types resolve.
3. Subscribe to `/surgery/gateway_info` with reliable/transient-local QoS.
4. Validate `schema_version`, `interface_version`, and `catalog_version`.
5. Subscribe to `/surgery/catalog` and build procedure-scoped display labels.
6. Subscribe to `/surgery/health` and the required dynamic snapshots.
7. If `procedure_active=false`, show the idle UI and do not reuse prior rows.
8. Group each event by its own `gateway_instance_id` and `procedure_run_id`; on a new run ID, clear the previous timeline before processing it.
9. Start camera subscriptions only when the user opens a live view; use best-effort/volatile QoS.
10. On heartbeat loss, mark all public state unavailable and reconnect from step 3.

Native DDS access and browser ROSBridge access are separate deployment choices.
Native DDS is **not** an access-control boundary in this deployment. Any
participant on the same ROS domain/discovery network can discover and subscribe
to internal topics and can publish conflicting or spoofed samples on public or
internal topic names. `PUBLISH_SHARED_FREE_TEXT=false`, camera-alias gating,
and the read-only 9092 allowlist constrain only Taskplanner-owned
Gateway/ROSBridge output; they do not filter native DDS traffic or publications
from another participant. Only mutually trusted, managed controller computers
may join the DDS subnet. Browser-only UI computers must use port 9092 and must
not join the ROS domain. Do not route or bridge DDS to Wi-Fi, Tailscale/VPN, or
the Internet. Any broader or untrusted native-DDS deployment requires ROS 2/DDS
Security governance, permissions, and identity provisioning.

The operator bridge on `127.0.0.1:9090` remains local and is not the partner
endpoint. Live and LLM demonstration profiles start a dedicated
`public-rosbridge` sidecar on loopback `127.0.0.1:9092` (or the deployment's
`PUBLIC_ROSBRIDGE_PORT`); the existing LAN proxy exposes the same host port only
on `TASKPLANNER_DEBUG_NETWORK_INTERFACE` and
rejects peers outside that interface's directly connected IPv4 subnet. A LAN
browser therefore connects to `ws://<wired-host-ip>:9092`. The sidecar also
rejects every direct non-loopback TCP peer before WebSocket upgrade. This
second gate is required on hosts where a VPN/Tailscale rule DNATs a virtual
address to loopback; only the designated wired proxy may reach the sidecar.

Port 9092 registers only the Subscribe capability (`subscribe` and `unsubscribe`
operations), uses an exact allowlist for the eleven public topics and two camera
aliases, and exposes no Defragment, publish, advertise, service, Action,
parameter, or rosapi operation. Incoming `fragment` frames and every unknown
operation are rejected rather than reassembled. It also
runs in a separate read-only container with a 512 MiB memory/swap cap, bounded
queues, client/message limits, and restart policy so a slow camera consumer
cannot take down the planner runtime. Its default Origin policy accepts
localhost and private-IPv4 HTTP(S) origins only; deployments may further set an
exact comma-separated `PUBLIC_ROSBRIDGE_ALLOWED_ORIGINS`. This Origin check is
defense in depth, not authentication or the network boundary. Direct-peer
loopback enforcement plus the wired proxy's subnet check form that boundary.
The wired subnet is a trusted integration
boundary; add an authenticated reverse proxy/VPN before any broader exposure.

Each incoming WebSocket frame must contain one complete JSON request and is
limited to 64 KiB measured as UTF-8 bytes. Invalid/incomplete JSON clears the
protocol parse buffer and closes the connection with code 1007; an oversized
frame or cumulative parse buffer closes with code 1009. Outgoing rosbridge
fragmentation is disabled. The complete serialized logical output is checked
against the 4 MiB cap before any WebSocket frame is queued; an oversized sample
is dropped as zero frames and is never converted to an outgoing `fragment` op.

## Minimal ROS 2 checks

```bash
ros2 topic echo /surgery/gateway_info \
  surgical_interop_msgs/msg/GatewayInfo \
  --qos-reliability reliable --qos-durability transient_local --once

ros2 topic echo /surgery/catalog \
  surgical_interop_msgs/msg/ProcedureCatalog \
  --qos-reliability reliable --qos-durability transient_local --once

ros2 topic info /surgery/tool_predictions --verbose
ros2 topic info /surgery/images/flir/compressed --verbose
```

## Explicitly not public

The Gateway must never export:

- patient identifiers, surgical-record text, or other PHI in the default
  configuration;
- `VLMResult.raw_json`, private `TwinEvent.detail_json`, prompts, hidden model
  reasoning, or unreviewed free-form traces;
- planner rationale, recovery queues, actor simulation state, validation-only
  ground truth, or robot command construction details;
- calibrated pose/TF/force/trajectory/grasp/collision data;
- credentials, infrastructure topology, or private controller logs.

Enabling `PUBLISH_SHARED_FREE_TEXT=true` is the one documented exception to the
default PHI boundary: it permits finalized ASR `text` and VLM `summary` fields
without de-identification. It does not permit any other private field. The
deployment owner, not the Gateway, is responsible for its authorization and
receiver controls.

The reviewed ranked tool forecast and semantic hand possession added in v0.3
are narrow exceptions, represented only by their dedicated public IDLs. They do
not make the remaining internal `WorldState` public.

Partners should depend only on `surgical_interop_msgs`, the public topic names,
and the two documented `sensor_msgs/CompressedImage` aliases—not Taskplanner's
internal messages or topic layout.
