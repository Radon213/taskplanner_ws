# surgical_interop_execution

This package is a one-way adapter from Taskplanner's existing internal command
topics to focused public robot-capability endpoints.

It subscribes to `/bt/skill_command` and `/bt/bed_robot_arm_group_command`.
A stable next-tool decision, handover, unused held-tool return, and Mayo
retrieval all map to the single `/surgery/tool_handover` Action. Retraction
commands all map to the single `/surgery/retraction/command`
`ExecuteRetractionCommand` Service. The adapter subscribes to controller-owned state on
`/external/bed_robot_arms/status`.

Exact researcher-owned commands such as `suction` do not enter through this
legacy internal group envelope. They are catalog-routed directly to the same
typed Service by `voice_command/command_router`; `COMMAND_SUCTION=7` is named
in the public IDL, and suction withdrawal uses `COMMAND_SUCTION_OUT=8`. This
bridge remains the compatibility adapter for
BT-owned retraction workflow commands only.

The direct router does not copy a launch-time retraction endpoint. It consumes
the bridge-owned latched `/integration/execution_route/state` projection and
uses its selected `retraction_service_name` atomically; no command is sent
until a valid route state arrives. The transient-local projection also exposes
`restart_allowed` and a bounded `restart_blocker`: they are computed only from
the authoritative stopped state and this bridge's active Action/Service count.
Endpoint readiness, controller contracts, camera/ASR/VLM status, and preflight
telemetry are deliberately not restart blockers. Runtime endpoint changes use
the same stopped/no-inflight boundary, swap the reviewed endpoint pair, and
publish a new route revision without a Digital-Twin reset or preflight-ack
round trip. Endpoint type/payload validation, controller availability,
idempotency, Action cancel/result handling, and physical controller limits
remain in the dispatch path.

`spec_dir` is only this node's launch-time bootstrap.  Subsequent procedure
selection belongs exclusively to ScenarioStore: the bridge receives its
transient-local `/simulation/scenario_config` revision on every focused owner
restart, validates the bundle path under the fixed bootstrap root and verifies
the published digest, then swaps its local name/distance mapping at an
authoritative paused or stopped boundary when this bridge and its execution
proxy have no active Action or Service request.  A malformed, stale, or
deferred notice leaves the last known-good mapping active.  It never resets the
simulation, asks preflight for an acknowledgement, or changes endpoint
routing; endpoint route changes remain stopped-only.

The internal group envelope remains an inbound compatibility boundary. Only
its `retraction` group is accepted. The bridge maps direct-teach start/end,
retraction start/stop, generic tool change, and a losslessly representable
single-side adjustment onto the Service command enum. The legacy Action's
multi-arm, direction-vector, axis, arm-ID, and tool-ID fields do not exist in
the Service. They are never silently dropped: multi-axis or non-lateral legacy
adjustments are rejected locally. A left/right/bilateral adjustment is sent as
`TARGET_LEFT`/`TARGET_RIGHT`/`TARGET_BOTH` plus metres (`5 cm = 0.050`).
For an adjustment, `TARGET_BOTH` applies the same distance independently to
both retractor arms.

The Service response is admission only. `request_accepted=true` proves only
that the controller received the request; it does not prove direct teach,
retraction, or tool change physically completed. The compatibility status uses
`state=accepted`, `outcome=accepted` to mark the Service-call lifecycle and
never exposes a requested end-effector profile as confirmed physical state.

The tool Action sends only `command_id`, the real catalog instrument name, a
human-readable instance ID, and one of seven fixed location pairs:

- `tray -> robot`: pick up the planner-selected next tool and hold it ready
- `mayo -> robot`: pick up a reusable Mayo tool selected by the planner and hold it ready
- `tray -> surgeon`: handover
- `robot -> surgeon`: handover a held tool
- `robot -> mayo`: park an unused speculative preparation and free the hand
- `robot -> tray`: controller-directed tray recovery
- `mayo -> tray`: retrieve

For `tray -> robot` and `mayo -> robot`, success means stable holding has been
reached. The Action then terminates while the controller keeps holding the tool
until a later handover or `return_unused_preposition` `robot -> mayo` Goal.
Preparation prediction and the reuse decision remain internal to Taskplanner.
The normal unused-preposition path never returns to a rack or tray;
`canceled_recovered_to_tray` is a separate compensating Cancel result.
Taskplanner selects that path only for an explicit different-tool request or a
different system-final rank-1 tool maintained for at least 2.0 continuous
source-time seconds. It does not use elapsed hold, evidence-loss, completion, or
implicit-hand-signal timers, and it does not dispatch while a tracked tool Goal
is active.

For every tracked Goal, `SUCCEEDED` plus `success=true` and
`final_state=completed` is authoritative physical completion evidence. The
bridge emits a correlated projection step for every supported leg, and the
Digital Twin applies it without re-running local lifecycle or arm-occupancy
admission gates. It still rejects missing/mismatched provenance, instance,
instrument type, semantic leg, projection order, duplicate command steps, and
stale timestamps. Detector/VLM location updates remain advisory observation
evidence and cannot overrule a correlated Action completion.

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

When `require_bed_robot_status=true`, controller-owned
`BedRobotArmStateArray` is a dispatch prerequisite and is not inferred from a
Service or Action completion. The Live launcher intentionally uses the
reviewed Service-only retraction contract (`false`), so absence of that
optional telemetry does not block admission. Tool handover continues to report
its fixed feedback states and monotonic progress through the Action itself.
Interrupts use standard ROS 2 Action cancel; no custom `/interrupt` topic is
defined.

Only one tool-transfer Goal may be active, including its cancel recovery. The
caller sends the next Goal only after the prior Result is terminal. A canceled
Result is accepted only with `canceled_source_unchanged` or
`canceled_recovered_to_tray`; an ambiguous cancellation result fails closed.

`ExecuteRetractionCommand` cannot be canceled after Service dispatch. Its
`command_id` is reserved before sending and is never retried automatically. A
timeout, malformed response, or transport ambiguity leaves the command
suppressed until operator reset and controller-state review.
