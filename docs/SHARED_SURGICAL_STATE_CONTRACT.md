# Shared Surgical State Contract

This contract is the read-only information that the Taskplanner shares with
partner institutions.  It turns selected, useful internal state into stable
topics without exposing the Taskplanner's private planning state.

It is intentionally separate from the topics and Action/Service servers that
other institutions implement for Taskplanner.  A partner may subscribe to the
topics below to understand the current case, but must not use them to write
into the digital twin or to bypass its own robot safety controller.

In particular, `/surgery/robots` is a Taskplanner-owned read-only projection;
it is not the controller input contract. The bed-mounted controller publishes
retraction-only source state on `/external/bed_robot_arms/status`, while
Taskplanner sends Tool Change through `/surgery/tool_change/request` and
Malleable fine adjustment through `/surgery/retraction/adjust`. No bed-mounted
suction-arm state or command belongs to either contract. Clinical suction
instruments and surgeon speech about suction remain ordinary surgical evidence.

Start the public publisher only when the integration network is ready:

```bash
ros2 launch bringup taskplanner_live.launch.py publish_shared_state:=true
```

With the default `publish_shared_state:=false`, none of the topics in this
document is published.

## Authority and evidence status

Every public message or row carries `evidence_status`.  Consumers must retain
this distinction in their own displays, logs, and control policy.

| Value | Meaning | May be treated as current operating state? |
| --- | --- | --- |
| `DT_ACCEPTED` | The digital twin accepted the observation or execution evidence after its consistency checks. | Yes, as Taskplanner's current operating state. It is not a clinical confirmation. |
| `MODEL_OBSERVED` | A VLM or perception model observed or inferred the item. | No. It is model evidence, not a confirmed fact. |
| `CLINICIAN_CONFIRMED` | An authorized clinician explicitly confirmed the item. | Yes, subject to the consuming system's own safety rules. |
| `GATEWAY_OBSERVED` | The shared-state gateway measured source availability or freshness. | Only as a transport-health observation; it makes no surgical claim. |
| `UNKNOWN` | The source is unavailable, stale, insufficient, or not classified. | No. |

`DT_ACCEPTED` and `CLINICIAN_CONFIRMED` are deliberately different.  The
former is a reproducible system-state decision; the latter is a human clinical
confirmation.  A VLM observation must never be relabeled as `DT_ACCEPTED`
merely because it was received.

## Public topics

All public topics are in the simple `/surgery/` namespace.  They are
one-way: Taskplanner publishes them and partner institutions subscribe.

| Topic | Type | Purpose |
| --- | --- | --- |
| `/surgery/context` | `surgical_interop_msgs/msg/SurgeryContext` | The current procedure and operating context. |
| `/surgery/instruments` | `surgical_interop_msgs/msg/InstrumentStateArray` | Current semantic state and location of known instruments. |
| `/surgery/robots` | `surgical_interop_msgs/msg/RobotStateArray` | Current state of the participating robots. |
| `/surgery/events` | `surgical_interop_msgs/msg/SurgeryEvent` | Important accepted state transitions in chronological order. |
| `/surgery/clinical_observations` | `surgical_interop_msgs/msg/ClinicalObservationArray` | Structured VLM/perception observations, explicitly marked as model evidence. |
| `/surgery/health` | `surgical_interop_msgs/msg/SurgeryHealth` | Freshness and availability of the shared-state sources. |

### `/surgery/context`

This is the latest concise answer to: *what operation is running, what phase
is the Taskplanner currently in, and is it safe to treat that phase as
settled?*

`SurgeryContext` contains the timestamp and revision, `procedure_type`,
`procedure_active`, `current_phase`, `phase_confidence`, `phase_uncertain`,
`execution_state`, `evidence_status`, and the applicable `safety_flags`.

It is a current-state snapshot, not a prediction of the next phase or the
next requested instrument.

### `/surgery/instruments`

`InstrumentStateArray` is a snapshot of the known instruments.  Each
`InstrumentState` carries its time, `instrument_id`, `instance_id`,
`location_type`, `location_id`, `holder_role`, `state`, `visible`,
`confidence`, and `evidence_status`.

Locations are **semantic locations**, such as `mayo`, `surgeon_right_hand`, or
`surgical_field`, together with a stable location identifier.  They do not
claim a calibrated 3D position, robot-frame transform, depth, orientation, or
collision-free grasp pose.  A geometric pose interface may be proposed only
after the relevant calibration, frame ownership, covariance, and validation
rules are jointly agreed.

### `/surgery/robots`

`RobotStateArray` reports controller-facing state without exposing a robot's
private implementation.  Each `RobotState` includes `robot_id`, `robot_type`,
`connection_state`, `execution_state`, `active_command_id`, `progress`,
`reason_code`, its timestamp, and `evidence_status`.

`reason_code` is intended for a small machine-readable outcome such as a
connection, rejection, cancellation, or safety reason.  It is not a free-form
controller log and does not replace the controller's local diagnostic record.

For the initial humanoid integration, the partner controller reports
goal-scoped execution through `/surgery/tool_handover` Action feedback rather
than publishing this snapshot directly. Taskplanner derives the humanoid entry
from accepted goals, the fixed feedback state, progress, and the terminal
result. This keeps one owner for `/surgery/robots` and avoids conflicting robot
state publishers.

During an operational interrupt, `execution_state` remains `stopping`,
`retreating`, or `recovering_to_tray` until the server has verified the
compensating outcome. It must not change to `canceled` when the Cancel request
is merely accepted. The next ordinary tool command is eligible only after the
terminal canceled Result; `cancel_recovery_failed` leaves execution blocked for
operator or explicit recovery handling.

### `/surgery/events`

`SurgeryEvent` is an append-only notification of a meaningful state change.
It has a timestamp and monotonically increasing `sequence`, plus
`event_type`, subject type and identifier, phase, semantic location, state,
`correlation_id`, confidence, and `evidence_status`.

Consumers should use the snapshot topics to recover current state after
joining.  They should use `/surgery/events` only to react to or record changes
observed while subscribed; it is not a durable event-replay service.

### `/surgery/clinical_observations`

`ClinicalObservationArray` exposes useful structured model output without
turning it into a clinical fact.  Each `ClinicalObservation` includes source,
summary, phase candidates and confidences, observed tools and their semantic
locations, gesture evidence, uncertainty, sequence, timestamp, and
`evidence_status`.

Normal VLM observations are published as `MODEL_OBSERVED`.  A consumer that
needs a decision-grade state must use `/surgery/context`,
`/surgery/instruments`, or an explicitly clinician-confirmed observation as
appropriate.  No partner may command a robot solely from a model-observed
clinical observation.

### `/surgery/health`

`SurgeryHealth` tells consumers whether the shared view is usable.  It carries
the time and revision, overall `healthy` flag, `state`,
`unavailable_sources`, `stale_sources`, `error_codes`, and `evidence_status`.
Its `evidence_status` is normally `GATEWAY_OBSERVED`, because it reports the
gateway's own freshness and availability measurement rather than a surgical
observation.

Consumers must check this topic and each source timestamp before relying on
any public state.  A retained snapshot is not necessarily fresh.

## Delivery and freshness behavior

`/surgery/context`, `/surgery/instruments`, `/surgery/robots`,
`/surgery/clinical_observations`, and `/surgery/health` are latest-state
snapshots.  They use reliable delivery, `KEEP_LAST(1)`, and
`TRANSIENT_LOCAL` durability so a late-joining partner receives the current
snapshot.  The gateway publishes them at its configured rate (one hertz by
default).

`/surgery/events` uses reliable delivery, `KEEP_LAST(50)`, and `VOLATILE`
durability.  It is emitted when an accepted event arrives; it must not be used
to reconstruct state that predates a subscription.

The gateway marks stale or unavailable inputs through `/surgery/health` rather
than silently presenting old data as live state.

## Explicitly not public

The gateway must never export the following through this contract:

- `VLMResult.raw_json`, `TwinEvent.detail_json`, prompts, hidden model
  reasoning, or unreviewed free-form internal traces;
- internal next-tool predictions, planner rationale, private action aliases,
  recovery queues, or robot command construction details;
- actor simulation state or validation-only ground truth;
- an unvalidated 3D pose, TF transform, or surgical geometry claim;
- credentials, network topology, controller logs, or patient-identifying data.

If a future collaboration needs one of these categories, it requires a
separate contract, ownership decision, review of its information boundary, and
an explicit change to this document and the interface definitions.

## Compatibility boundary

Internally, Taskplanner may continue to use `surgical_msgs/WorldState`,
`InstrumentState`, `TwinEvent`, `VLMResult`, and robot status messages.  The
public message types deliberately project only the fields above.  Partners
should depend only on `surgical_interop_msgs` and the six public topic names,
not on Taskplanner's internal topics or message layout.
