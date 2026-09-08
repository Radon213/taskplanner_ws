# Operational TTS

`taskplanner-tts` is a Live-only ROS 2 sidecar. It keeps the local Supertonic 3
model prewarmed, plays Korean through Ubuntu's current PipeWire default output,
and has two strictly separate input lanes:

- conversational `surgical_msgs/msg/HumanoidReply` on `/vlm/humanoid_reply`;
- deterministic, read-only observation of an already-sent Action or Service
  through `/bt/skill_command` and `/surgery/execution_trace`.

The second lane never asks VLM for wording and never publishes a robot command
or calls an Action/Service. It only speaks after the execution owner has
recorded `stage=sent` and `dispatch_submitted=true`.

## Start contract

Use the normal launcher; there is no separate operator step:

```bash
scripts/taskplanner up live --build
```

Live always selects this owner. The launcher checks the model, selected voice,
private state/cache directories, and PipeWire socket, then starts the sidecar
without waiting for it. Its health becomes ready only after a real Supertonic
prewarm and `/tts/playback_status` publisher exist. A local audio/model failure
is visible in `status tts`; it never blocks deterministic Action/Service
admission or the remaining Live owners.

For a source-only TTS change, restart only this owner:

```bash
scripts/taskplanner restart tts live
```

Use `--ensure-build` only after changing the installed console-entrypoint or
package contract. Normal Python wording/alias changes are bind-mounted and need
only the owner restart.

The planner, ASR, and TTS containers run as the configured host
`TASKPLANNER_UID:TASKPLANNER_GID`. The launcher creates both the TTS and
execution-ledger state directories with mode `0700`; the SQLite files remain
host-user-owned and are rejected if their owner or directory permissions drift.

The model defaults are configured in `.env.example`:

- Supertonic 3 SDK model revision
  `724fb5abbf5502583fb520898d45929e62f02c0b`
- voice `F1`, language `ko`, runtime fallback 8 steps, speed `1.05`
- fixed/common phrase prewarm uses `TASKPLANNER_TTS_PREWARM_STEPS=100`, the
  Supertonic SDK maximum-quality setting
- empty `TASKPLANNER_TTS_OUTPUT_TARGET`, which follows the current PipeWire
  default output

The background prewarm catalog covers the 2026-08-31 execution phrases plus
the full default tool set, lifecycle messages, and common signed
retraction-adjustment sentences. Exact catalog matches play the precomputed
100-step WAV; uncatalogued or not-yet-warmed text is synthesized immediately
with the normal 8-step fallback.

Set `TASKPLANNER_TTS_OUTPUT_TARGET` only when a reviewed fixed PipeWire node is
required. No ALSA hardware device is passed into the container.

## One-shot and recovery behavior

The VLM assigns a stable logical reply ID from
`(gateway_instance_id, procedure_run_id, utterance_id)`. Before committing the in-memory dialogue
turn or publishing DDS, the VLM stores the complete first-writer reply in its
private SQLite producer outbox. It republishes only the exact active gateway
epoch and run until the
TTS consumer returns a typed lifecycle ACK.

The VLM receives `/surgery/audio/observed_utterance` only as a dialogue
observation. `speech_input_adapter` publishes the typed admitted utterance and
`CommandRouter` alone decides whether its catalog creates a deterministic
Topic, Service, or Action request. A `HumanoidReply`, TTS state, gateway scope,
or VLM presentation metadata cannot admit, delay, or re-dispatch that command.

The VLM remains an independent dialogue/reply producer. It persists a valid
reply in its private outbox and publishes the original `HumanoidReply` directly
on `/vlm/humanoid_reply`; the TTS sidecar accepts only `valid && speak` replies
whose gateway/run scope matches a fresh active `GatewayInfo`. TTS does not
admit, reject, or delay a command.

Reply timing controls playback only. A presentation may wait for its declared
audio-side evidence, but it never changes command routing or endpoint
admission. The command path is intentionally independent of VLM and TTS
availability.

On receipt, the sidecar first writes the reply into its separate SQLite
playback ledger. A `queued` or `waiting_*` status is therefore an ACK of durable
consumer ownership, after which VLM outbox retries stop. The consumer ledger
uses the same `reply_id` as its deduplication key. The `queued`/`waiting_*`
message is the TTS-side ACK: no operator action is required. DDS redelivery, a sidecar
restart, a VLM restart, or a crash between those processes cannot play the same
reply twice.

`queued`, `waiting_*`, and `playing` prove delivery only. `played` is the sole
successful terminal audio outcome; terminal `failed` is sticky and cannot be
overwritten by a later provisional or success ACK. A rejected or colliding
reply also returns a correlated typed `failed` NACK, so the producer does not
retry an invalid request indefinitely. `/vlm/humanoid_reply` is reliable and
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
fresh heartbeat fences both the VLM producer outbox and the TTS consumer, and
interrupts active playback. A later heartbeat with the same IDs establishes a
new authority boundary; replies made stale by the outage are not resurrected.

Typed lifecycle acknowledgements are published on `/tts/playback_status`.
They distinguish queued/waiting playback, playing, terminal playback,
duplicate suppression, and failure; they remain audio-lifecycle state only.

`reply_id`, synthesis latency, audio duration, playback latency, cache key, and
terminal result are included. The ACK is local audio lifecycle evidence only;
it is not controller admission or physical-motion evidence.

## Dialogue and deterministic execution announcements

- Free-form VLM replies stay visible on `/vlm/humanoid_reply` but are not
  spoken by default (`TASKPLANNER_TTS_ENABLE_VLM_FREE_SPEECH=false`). TTS
  returns a terminal non-playback ACK so the VLM outbox does not retry them.
  The setting can be changed live with
  `ros2 param set /tts_runtime enable_vlm_free_speech true`; deterministic
  execution announcements are independent of this setting.
- When explicitly enabled, questions and conversational responses use
  `timing=immediate` and play once. VLM presentation metadata remains
  non-executable and the independent catalog command path remains owned by
  `CommandRouter`.
- A sent tool Action is spoken deterministically using the concise configured
  Korean tool alias: `보비를 전달드리겠습니다.`,
  `바이폴라를 준비하겠습니다.`, or `애드슨을 회수하겠습니다.`
- A sent retraction Service is spoken as one of: `직접교시를 시작합니다.`,
  `직접교시를 종료합니다.`, `리트랙션을 시작합니다.`,
  `리트랙션을 조정합니다.`, `도구를 교체합니다.`,
  `리트랙션을 종료합니다.`, `석션 들어가겠습니다.`, or
  `석션 빼겠습니다.`
- These sentences mean dispatch submission only. They never claim controller
  acceptance, physical completion, or tool location.

The common 보비·바이폴라·애드슨·모스키토 handover/prepare/retrieve sentences
and every retraction sentence are synthesized into the content-addressed cache
during sidecar readiness. Prewarming generates WAV data only and does not play
audio. Set `TASKPLANNER_TTS_TOOL_ALIASES` as comma-separated
`instrument=spoken Korean name` pairs for a scenario-local alias and restart
only `tts`.

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
