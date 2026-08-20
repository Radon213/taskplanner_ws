# surgical_interop_execution

This package is a one-way adapter from Taskplanner's existing internal command
topics to focused public robot-capability endpoints.

It subscribes to `/bt/skill_command` and `/bt/bed_robot_arm_group_command`.
A stable next-tool decision, handover, unused held-tool return, and Mayo
retrieval all map to the single `/surgery/tool_handover` Action. Retraction
commands all map to the single `/surgery/retraction/command`
`ExecuteRetractionCommand` Service. The adapter subscribes to controller-owned state on
`/external/bed_robot_arms/status`. No suction-arm endpoint is exposed.

The internal group envelope remains the inbound compatibility boundary. Only
its `retraction` group is accepted. The bridge maps direct-teach start/end,
retraction start/stop, generic tool change, and a losslessly representable
single-side adjustment onto the Service command enum. The legacy Action's
multi-arm, direction-vector, axis, arm-ID, and tool-ID fields do not exist in
the Service. They are never silently dropped: multi-axis or non-lateral legacy
adjustments are rejected locally. A left/right adjustment is sent as
`TARGET_LEFT`/`TARGET_RIGHT` plus metres (`5 cm = 0.050`).

The Service response is admission only. `request_accepted=true` proves only
that the controller received the request; it does not prove direct teach,
retraction, or tool change physically completed. The compatibility status uses
`state=accepted`, `outcome=accepted` to mark the Service-call lifecycle and
never exposes a requested end-effector profile as confirmed physical state.

The tool Action sends only `command_id`, the real catalog instrument name, a
human-readable instance ID, and one of six fixed location pairs:

- `tray -> robot`: pick up the planner-selected next tool and hold it ready
- `mayo -> robot`: pick up a reusable Mayo tool selected by the planner and hold it ready
- `tray -> surgeon`: handover
- `robot -> surgeon`: handover a held tool
- `robot -> tray`: return an unused held tool
- `mayo -> tray`: retrieve

For `tray -> robot` and `mayo -> robot`, success means stable holding has been
reached. The Action then terminates while the controller keeps holding the tool
until a later handover or `robot -> tray` return Goal. Preparation prediction
and the reuse decision remain internal to Taskplanner.

The only public location values are `tray`, `mayo`, `robot`, and `surgeon`.
Compound internal actions that require returning one tool and handing over
another are rejected at this boundary; they must be decomposed into these
single-tool Goals. Internal codes such as `T04`, detailed anchors, planner
metadata, and arm selection are
not sent to the external server. The adapter resolves the real instrument name
from the active procedure specification and fails closed when it cannot do so.

The adapter publishes compatibility status on `/skill/status` and
`/bed_robot_arm_group/status`. Its public Goal and service Request construction
uses only the fields defined in `surgical_interop_msgs`; internal rationale,
policy mode, owner, confidence, request generation, and raw input text are not
sent to external servers.

It also observes `/simulation/control_state`. It starts with dispatch disabled;
only `start` or `start_actors` enables it, while `start_runtime` keeps it
disabled. `stop` and `reset` prevent new dispatches, cancel pending or accepted
public tool-transfer Action goals. For tool transfer, the adapter keeps the active Goal until
the controller reports either verified cancel recovery or failure; recovery
feedback remains visible instead of being suppressed. Retraction Service calls
cannot be canceled after dispatch; an admission response received after a local
stop leaves physical state unknown and blocks further dispatch. A bounded
ledger prevents the same `command_id`, or the same explicit request generation,
from causing a second outbound command.

The retraction controller publishes `BedRobotArmStateArray`; this state is a
dispatch prerequisite and is not inferred from service or Action completion.
Tool handover continues to report its fixed feedback states and monotonic
progress through the Action itself. Interrupts use standard ROS 2 Action cancel;
no custom `/interrupt` topic is defined.

Only one tool-transfer Goal may be active, including its cancel recovery. The
caller sends the next Goal only after the prior Result is terminal. A canceled
Result is accepted only with `canceled_source_unchanged` or
`canceled_recovered_to_tray`; an ambiguous cancellation result fails closed.

`ExecuteRetractionCommand` cannot be canceled after Service dispatch. Its
`command_id` is reserved before sending and is never retried automatically. A
timeout, malformed response, or transport ambiguity leaves the command
suppressed until operator reset and controller-state review.
