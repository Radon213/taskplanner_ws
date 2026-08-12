# surgical_interop_msgs

`surgical_interop_msgs` is the small, public ROS 2 interface package used for
sharing safe surgical context between institutions and requesting focused robot
capabilities. It deliberately excludes patient identifiers, raw model output,
internal planner rationale, prompts, and diagnostic text.

## Public state topics

| Topic | Type | Meaning |
| --- | --- | --- |
| `/surgery/context` | `SurgeryContext` | Current procedure type, current phase, execution state, and public safety flags. |
| `/surgery/instruments` | `InstrumentStateArray` | Latest semantic locations and states for instrument instances. A location is not a calibrated Cartesian pose. |
| `/surgery/robots` | `RobotStateArray` | Connection and execution status for each robot capability. |
| `/surgery/events` | `SurgeryEvent` | Ordered public state changes; `sequence` establishes the event order. |
| `/surgery/clinical_observations` | `ClinicalObservationArray` | Model observations, their confidence, and authority; not automatically confirmed clinical facts. |
| `/surgery/health` | `SurgeryHealth` | Public freshness and availability summary for the integration. |

State snapshots carry a monotonically increasing `revision`. Events and clinical
observations carry a `sequence`. All public state and observation messages carry
an `evidence_status` value. Recommended values are `MODEL_OBSERVED`,
`DT_ACCEPTED`, `CLINICIAN_CONFIRMED`, `GATEWAY_OBSERVED`, `UNKNOWN`, and
`REJECTED`.

## Focused capability requests

The recommended public endpoints are:

| Endpoint | Type | Meaning |
| --- | --- | --- |
| `/surgery/tool_handover` | `ExecuteToolHandover` action | Use one Action for preparation, handover, unused-tool return, and Mayo retrieval. |
| `/surgery/retraction` | `ExecuteRetraction` action | Perform one retraction operation. `operation` is `MOVE`, `RELEASE`, or `CHANGE_END_EFFECTOR`; only the fields applicable to that operation are populated. |
| `/surgery/suction/set` | `SetSuction` service | Set suction on or off when long-running feedback or cancellation is not needed. |

Every request has a caller-provided `command_id` for correlation and idempotency.
Action results and service responses expose only `success`, final `state`, and a
stable machine-readable `reason_code`. Action feedback is limited to current
`state` and `progress`.

`ExecuteToolHandover` accepts only `tray`, `mayo`, `robot`, and `surgeon` as
location values. The only valid transitions are `tray -> robot` (pick up the
Taskplanner-selected next tool from the supply tray and hold it ready),
`mayo -> robot` (pick up a reusable Mayo tool selected by the same stable
next-tool policy and hold it ready), `tray -> surgeon` (direct pickup and
handover), `robot -> surgeon` (held-tool handover), `robot -> tray` (return an
unused held tool), and `mayo -> tray` (retrieve a used tool).
`instrument_id` is
the shared real instrument name (for example `Bovie surgical cautery`), not a
private procedure-catalog code such as `T04`. The server chooses the arm; arm
selection is intentionally absent from the Goal. A successful `tray -> robot`
or `mayo -> robot` Result means stable holding has been reached and the robot
keeps holding the tool until a later handover or `robot -> tray` return Goal.

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
no longer cancellable. Returning that stably held but now-unneeded tool is a
new `robot -> tray` Goal; Cancel applies only while a Goal is still active.
