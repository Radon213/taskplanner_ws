# CAM4 direct hand-handover contract

Taskplanner does not ask the VLM to classify hands. The runtime reducer joins
the following typed CAM4 topics by an identical `(stamp, frame_id)` header:

| Topic | Type | QoS |
| --- | --- | --- |
| `/perception/cam_4/hand/gestures` | `hand_keypoint_interfaces/msg/HandGestureArray` | reliable, volatile, keep-last 10 |
| `/perception/cam_4/hand/facing` | `hand_keypoint_interfaces/msg/HandFacingArray` | reliable, volatile, keep-last 10 |
| `/perception/cam_4/hand/health` | `std_msgs/msg/String` | reliable, transient-local, keep-last 1 |

## Frozen positive tuple

A frame is positive only when the two arrays contain exactly the same
frame-local hand-index set and exactly one common Right-hand index has all of
the following valid classifications:

- handedness: `Right` on both observations;
- hand shape: `Open_Palm`;
- palm facing: `PALM_UP`.

Both source timestamps and local monotonic receipt time must span at least
`0.300 s`, with at least four joined samples. A source- or receipt-time gap over
`0.200 s`, an unknown result, asymmetric/ambiguous hands, provenance mismatch,
stale health, stale source timestamp, or timestamp regression resets the dwell.
This prevents a buffered/replayed burst from satisfying a live hold instantly.
One continuous positive episode can authorize at most one handover. Stream
silence longer than `0.400 s` is checked by a `0.100 s` watchdog and withdraws
visibility within `0.500 s` worst case, but does not re-arm the episode. A continuously
observed non-request pose must persist for `0.500 s`. Pause, stop, and
completion/finishing withdraw the signal immediately; after pause/resume a
fresh `0.500 s` release is mandatory before another episode.

## Pinned perception provenance

- source frame: `cam_4_color_optical_frame`
- gesture model: `VIPLab Top-View Landmark Gesture Classifier`
- gesture version: `landmark-geometry-v2-world-closed`
- gesture asset SHA-256: `258ed1676863df9317fb084a446a2ae9645c1f4acc468a4c4ad92979cf0ef821`
- palm estimator: `VIPLab CAM4 Depth Palm-Facing Estimator`
- palm estimator version: `depth-palm-normal-v1`
- palm specification SHA-256: `45c61773790b8510f373bd9f0940ce1189567e7f4e74ff3c0d6c5b5ee3785b37`
- calibration: `cam4_live_aprilgrid_depth_workplane_20260822`
- handedness mapping: `cam4_forced_right_camera_constraint_v1_pending_live_check`
- required depth-registration backend: `cuda_cabi_v1`

The base and shadow launch profiles remain fail-closed while the upstream
health document reports an unverified palm mapping. The live launch records the
operator's 2026-08-26 check with `hand_mapping_operator_approved=true`, but only
for the exact provenance above.

## Authority boundary

The signal is stored in the existing `WorldState.implicit_request_*` fields for
wire compatibility, with `implicit_request_tool` always empty. It does not add
a `SurgeonRequest` and cannot choose a tool. The BT first uses a verified tool
already in `prepositioned_right` only when its selected, prepositioned, and
right-hand instance IDs match and its type, lifecycle, and owner prove the
right-hand slot. The Digital Twin's canonical generic `location=robot` /
`location_type=robot` projection is accepted only together with
`owner=robot_right_hand`; the detailed `robot_right_hand` location form is
accepted as equivalent. Otherwise it may use the independently stabilized
next-tool prediction. Normal availability, contamination, robot
occupancy, execution-route, controller-readiness, deduplication, and completion
checks remain in force before any command is sent. The direct branch is valid
only while `execution_state == running`; a non-empty legacy
`implicit_request_tool` fails closed.

No VLM request context, prompt, response schema, procedure mock, or fallback
contains a human-hand signal. Legacy `sg`, `gesture`, `hand_pose`,
`palm_facing`, `handedness`, and `surgeon_gesture` fields are rejected instead
of being silently interpreted.

## Restart-safe episode identity

Every accepted runtime start creates a new lowercase 32-hex
`procedure_run_id`. Direct-hand `SkillCommand` messages carry that run ID and
the reducer's positive-episode generation, and use the deterministic command
ID `skill-hand-<run>-<generation>-<action>`. The execution bridge checks a
fresh, running `SimulationState` with the identical run ID at receipt and again
immediately before controller I/O.

Immediately before an Action Goal is sent, the bridge commits a SQLite
reservation for the run/episode. A second command for the same episode is
suppressed even after a BT or bridge process restart; a conflicting tool or
payload fails closed. A crash after reservation is treated as an unknown
physical submission and is never replayed automatically. Compose mounts this
ledger from `TASKPLANNER_EXECUTION_STATE_DIR`; reset and procedure-bundle
changes do not erase it. A fresh release followed by a new positive episode,
or a newly started procedure run, creates a new admissible identity.
