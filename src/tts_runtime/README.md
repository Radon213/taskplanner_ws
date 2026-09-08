# Taskplanner TTS runtime

`tts_runtime_node` consumes validated `surgical_msgs/msg/HumanoidReply` messages
from `/vlm/humanoid_reply` and read-only already-sent execution facts from
`/bt/skill_command` plus `/surgery/execution_trace`. It generates deterministic
cached WAV files with a resident Supertonic 3 worker and plays them through
PipeWire's current default output device. Playback status is published on
`/tts/playback_status`.

The execution-announcement resolver is deliberately VLM-free and
presentation-only. It joins a `SkillCommand` with an endpoint receipt:
`ExecutionTrace(stage="accepted", dispatch_submitted=true)`. Tool Actions must
carry `transport="action", evidence="goal_response"`; retraction Services must
carry `transport="service", evidence="service_admission_only"`. A local client
submission alone is silent, so a predicted, rejected, or unacknowledged request
cannot produce a success-like Korean acknowledgement. It cannot admit, route,
delay, or invoke a robot endpoint. The `tool_aliases` parameter (or
`TASKPLANNER_TTS_TOOL_ALIASES`) accepts comma-separated
`instrument=spoken Korean name` values and defaults to 보비, 바이폴라, 애드슨,
and 모스키토 aliases.

The runtime deliberately does not call robot-control Actions or Services.
`immediate` replies enter the playback queue. `on_function_accepted` and
`on_function_completed` replies remain durably in `waiting` until an
authoritative integration calls the pure-core
`PlaybackDispatcher.release_waiting(reply_id)` method. The ROS adapter performs
that release only by joining existing read-only evidence:

- tool handover: accepted voice-intent generation → matching voice-backed
  handover command → matching `SkillStatus` and `ExecutionTrace`
- retraction acceptance: matching `request_id` and terminal successful
  admission-only `BedRobotArmGroupStatus`

Tool Action wording and retraction Service wording are announced at endpoint
acknowledgement, not controller physical completion. The TTS node never sends a
robot command or calls a robot Action or Service.

The VLM producer first persists each validated reply before DDS publication and
retries it until this runtime returns a lifecycle ACK. This runtime then uses a
separate SQLite consumer ledger with `reply_id` as its primary key. A duplicate
delivery is never played again. On restart, `queued` and `waiting` rows are
recovered; a row left in `playing` is terminally marked
`failed/interrupted_unknown`, because replaying it could produce duplicate
speech.

The default installation on this workstation uses:

- model: `supertonic-3`
- voice: `F1`
- language: `ko`
- runtime diffusion steps: `8` (low-latency fallback for cache misses)
- fixed/common phrase prewarm: `100` (`prewarm_steps`, Supertonic maximum)
- speed: `1.05`
- output: PipeWire automatic/default target

At startup, the TTS owner pre-generates a bounded catalog of likely Korean
execution, lifecycle, tool, and retraction-adjustment phrases at
`prewarm_steps=100` in the same deterministic WAV cache.  An exact text match
uses that high-quality WAV once it exists; a phrase outside the catalog, or a
phrase requested while background prewarm is still running, falls back to the
normal `steps=8` synthesizer immediately.  Prewarm is background work and does
not delay endpoint readiness or command admission.

The ROS Python and Supertonic Python ABIs differ on this workstation. The node
therefore starts a persistent Supertonic subprocess lazily on the first cache
miss. Parameters `synth_python` and `model_dir` select that environment and the
already-downloaded model.

`model_identity` defaults to the pinned model revision
`supertonic-3:model-sdk-1.3.1:724fb5abbf5502583fb520898d45929e62f02c0b`.
It is deliberately independent of `model_dir`, so a host path and a container
mount path share deterministic cache keys when they contain the same model.
At startup the runtime appends a content-manifest SHA-256 covering the selected
voice JSON, all four ONNX networks, `tts.json`, and `unicode_indexer.json`; a
changed artifact therefore cannot reuse stale audio.

Playback recovery is additionally gated by the exact active, nonempty
`(gateway_instance_id, procedure_run_id)` pair from `/surgery/gateway_info`.
Both identifiers are persisted in the consumer ledger; replies outside that
exact gateway epoch and run are never submitted or recovered, and stale
`queued`/`waiting` rows become terminal failures. An active `pw-play` attempt is
interrupted when either identifier changes, including the small interval
between the durable `playing` transition and process spawn. The GatewayInfo
lease watchdog uses a steady clock, so paused simulation time cannot keep audio
authority alive.
Admission-gated replies also fail terminally when `waiting_timeout_sec` elapses.
