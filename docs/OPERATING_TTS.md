# Operational TTS

`taskplanner-tts` is a Live-only ROS 2 sidecar. It receives admitted one-shot
`surgical_msgs/msg/HumanoidReply` messages on `/tts/admitted_reply`, generates
Korean speech with the local Supertonic 3 model, and plays through Ubuntu's
current PipeWire default output. It never publishes a robot command and never
calls a robot Action or Service.

## Start contract

Use the normal launcher; there is no separate operator step:

```bash
scripts/taskplanner up live --build
```

The launcher verifies the model and selected voice files, writable private
state/cache directories, and the current user's PipeWire socket. It starts the
TTS endpoint before ASR capture and before the planner/VLM runtime so no
one-shot reply is missed during normal startup.

The planner, ASR, and TTS containers run as the configured host
`TASKPLANNER_UID:TASKPLANNER_GID`. The launcher creates both the TTS and
execution-ledger state directories with mode `0700`; the SQLite files remain
host-user-owned and are rejected if their owner or directory permissions drift.

The model defaults are configured in `.env.example`:

- Supertonic 3 SDK model revision
  `724fb5abbf5502583fb520898d45929e62f02c0b`
- voice `F1`, language `ko`, 8 steps, speed `1.05`
- empty `TASKPLANNER_TTS_OUTPUT_TARGET`, which follows the current PipeWire
  default output

Set `TASKPLANNER_TTS_OUTPUT_TARGET` only when a reviewed fixed PipeWire node is
required. No ALSA hardware device is passed into the container.

## One-shot and recovery behavior

The VLM assigns a stable logical reply ID from
`(gateway_instance_id, procedure_run_id, utterance_id)`. Before committing the in-memory dialogue
turn or publishing DDS, the VLM stores the complete first-writer reply in its
private SQLite producer outbox. It republishes only the exact active gateway
epoch and run until the
TTS consumer returns a typed lifecycle ACK.

Live does not route the language resolver directly to execution. The resolver
publishes `/surgery/voice/proposal`; the durable
`vlm_function_admission_gate` joins that proposal with the exact admitted ASR
turn, the VLM `function_call`, and fresh `GatewayInfo`. Only an exact semantic
match is promoted to the existing `/surgery/voice/intent` boundary. Its reply
is independently promoted to `/tts/admitted_reply`. The gate calls no Action
or Service; Digital Twin, BT, and the execution bridge retain their existing
admission authority.

The public gateway adopts the active Digital Twin
`WorldState.procedure_run_id` instead of creating a second run identity. The
Digital Twin then checks a fresh three-second `GatewayInfo` lease, exact
gateway/run/procedure metadata, and its own current run before applying a gated
function. Its immutable first-writer result is committed to
`/taskplanner-execution-state/odt_voice_intent_receipts.sqlite3` before the
typed receipt event is published. Exact DDS retries therefore re-emit the
stored receipt without mutating the reducer a second time; collisions, expired
leases, retired gateway epochs, and metadata changes fail closed.

On receipt, the sidecar first writes the reply into its separate SQLite
playback ledger. A `queued` or `waiting_*` status is therefore an ACK of durable
consumer ownership, after which producer retries stop. The consumer ledger
uses the same `reply_id` as its deduplication key. The `queued`/`waiting_*`
message is the TTS-side ACK: no operator action is required. DDS redelivery, a sidecar
restart, a VLM restart, or a crash between those processes cannot play the same
admitted utterance twice.

`queued`, `waiting_*`, and `playing` prove delivery only. `played` is the sole
successful terminal audio outcome; terminal `failed` is sticky and cannot be
overwritten by a later provisional or success ACK. A rejected or colliding
reply also returns a correlated typed `failed` NACK, so the producer does not
retry an invalid request indefinitely. `/tts/admitted_reply` is reliable and
volatile because SQLite owns replay; `/tts/playback_status` remains reliable
and transient-local so a restarted producer can recover the latest ACK.

The sidecar waits for an active `/surgery/gateway_info` scope before recovering
or playing queued work. Queued or waiting rows from another procedure run are
terminally marked `stale_procedure_run`. A row that was already `playing` when
the process stopped becomes `failed/interrupted_unknown` and is not replayed,
because its audible completion cannot be proven.

If the gateway epoch, procedure run, or active flag changes while a WAV is
being synthesized or played, queued work is failed and an armed/running
`pw-play` process is interrupted. Speech from an old procedure is therefore
not allowed to cross into the next run or an idle state.

`GatewayInfo` is a heartbeat, not a one-time latch. Three seconds without a
fresh receipt fences both the VLM producer outbox and the TTS consumer, and
interrupts active playback. A later heartbeat with the same IDs establishes a
new authority boundary; replies made stale by the outage are not resurrected.

Typed lifecycle acknowledgements are published on `/tts/playback_status`:

```text
queued | waiting_function_accepted | waiting_function_completed
playing | played | duplicate_suppressed | failed
```

`reply_id`, synthesis latency, audio duration, playback latency, cache key, and
terminal result are included. The ACK is local audio lifecycle evidence only;
it is not controller admission or physical-motion evidence.

## Dialogue and fixed feedback

- Questions and conversational responses use `timing=immediate` and play once.
- The VLM may emit a reply and a supported function call for the same ASR turn;
  the reply is still spoken only once, while the function follows the guarded
  typed intent path.
- Tool-handover wording waits for the exact admitted ASR utterance to join the
  reducer request generation, voice-backed `SkillCommand`, and matching
  accepted/completed `SkillStatus` plus `ExecutionTrace`.
- Retraction wording can claim only Service admission, never physical
  completion.
- In `thyroidectomy_demo`, the fixed phrase `도구 회수중입니다` is emitted once
  for the first successful controller feedback of an actual retrieval Action.
- `inguinal_hernia_repair_demo` supports natural text-only VLM dialogue and
  typed Army-Navy retraction calls, but publishes no image-derived VLM facts
  and never emits tool-handover or tool-retrieval feedback. Its legacy raw
  transcript command parser is disabled.

The two frequent phrases `바이폴라 전달드리겠습니다` and `도구 회수중입니다`
are synthesized into the content-addressed cache during sidecar readiness.
Prewarming generates WAV data only and does not play audio.

## Speaker echo boundary

Live ASR subscribes to `/tts/playback_status` with reliable transient-local
QoS. While a TTS WAV is audible, and for the configured short tail afterward,
a matching final ASR transcript is consumed and reported as
`tts_echo_suppressed:<reply_id>` instead of being admitted as a surgeon command.
Retained DDS history is bounded by the event timestamp and WAV duration, so an
old `playing` event cannot suppress speech indefinitely.

## Read-only checks

```bash
docker compose --profile live ps taskplanner-tts
ros2 topic info --verbose /tts/playback_status
ros2 topic echo --once /tts/playback_status
```

Do not use a successful TTS ACK as evidence that a handover, retrieval, or
retraction physically completed. Use the controller's reviewed execution
contract for that claim.
