# surgical_interop_execution

This package is a one-way adapter from Taskplanner's existing internal command
topics to focused public robot-capability endpoints.

It subscribes to `/bt/skill_command` and `/bt/bed_robot_arm_group_command`.
A stable next-tool decision, handover, unused held-tool return, and Mayo
retrieval all map to the single `/surgery/tool_handover` Action. Retraction,
release, and end-effector changes map to `/surgery/retraction`; suction start
and stop map to `/surgery/suction/set`.

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
public Action goals. For tool transfer, the adapter keeps the active Goal until
the controller reports either verified cancel recovery or failure; recovery
feedback remains visible instead of being suppressed. Retraction late results
and ROS service results are ignored after runtime control stops. A bounded
ledger prevents the same `command_id`, or the same explicit request generation,
from causing a second outbound command.

The external robot does not need to publish a separate integration-state topic
for the initial contract. It reports the fixed `ExecuteToolHandover` feedback
states and monotonic progress through the Action itself. Taskplanner projects
those observations into its shared `/surgery/robots` snapshot. Interrupts use
the standard ROS 2 Action cancel request; no custom `/interrupt` topic is
defined.

Only one tool-transfer Goal may be active, including its cancel recovery. The
caller sends the next Goal only after the prior Result is terminal. A canceled
Result is accepted only with `canceled_source_unchanged` or
`canceled_recovered_to_tray`; an ambiguous cancellation result fails closed.
