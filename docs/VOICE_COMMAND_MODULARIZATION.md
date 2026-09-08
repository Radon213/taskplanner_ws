# Voice command routing for research iteration

The workspace-wide source of truth is
[`taskplanner_principles.toml`](../taskplanner_principles.toml). This document
applies its single-owner rule to explicit speech commands and direct operator
commands.

## One executable path

```text
final ASR input
  -> speech_input_adapter
  -> /surgery/audio/admitted_utterance
  -> CommandRouter
  -> exact catalog transport
  -> typed Topic / Service / Action adapter
  -> endpoint server

CommandRouter
  -> /surgery/audio/observed_utterance
  -> VLM / UI / logs / TTS presentation (read-only observation)
```

`speech_input_adapter` owns the one ingress check set: final utterance,
expected source, freshness, TTS echo suppression, and `utterance_id`
deduplication. `CommandRouter` is the sole consumer of the admitted topic and
the sole deterministic executor. It makes an unchanged, one-way relay to
`/surgery/audio/observed_utterance`; observers have no path back to admission
or dispatch.

The router selects a catalog record, renders its per-request `command_id`, and
uses the record's exact typed transport. The execution-owner proxy selects its
own external or virtual endpoint. The router does not watch route state or
choose the endpoint, so changing an execution route remains local to its
owner. The endpoint server remains responsible for admission, idempotency,
physical limits, and physical completion.

VLM dialogue, Digital Twin, BT, UI, logging, and TTS may observe the relay or
their own state. They are not a prerequisite for, or a second authority over,
a deterministic command.

## Adding or changing a command

For an installed ROS interface, add one catalog record and reload the command
router. A record contains exact phrases and the transport binding: `kind`,
`type`, `endpoint`, `payload`, and optional Action `timeout_sec`. Wire constants
such as `protocol_version` stay explicit in the record; `{command_id}` is
rendered by the router once per request.

For `surgical_interop_msgs/srv/ExecuteRetractionCommand`, the catalog keeps
the fixed execution-owner proxy endpoint and `protocol_version: 1`. The
receiving endpoint validates the actual request fields.

```yaml
schema: taskplanner.command-catalog.v1
commands:
  - name: suction
    phrases: [suction]
    command_id_prefix: suction-start
    dispatch:
      kind: service
      type: surgical_interop_msgs/srv/ExecuteRetractionCommand
      endpoint: /taskplanner/execution/retraction/command
      payload:
        protocol_version: 1
        source_id: taskplanner
        command_id: "{command_id}"
        command: 7
        target_side: 0
        distance_m: 0.0
```

This change needs neither a scenario edit, a BT vocabulary update, a VLM
change, nor a workspace build. For a new custom ROS `.msg`, `.srv`, or
`.action`, build only the interface package and its direct producers or
consumers; do not rebuild or restart ASR, UI, VLM, rosbridge, or unrelated
owners.

## Boundary checks

The adapter owns text-oriented ingress checks. The typed adapter preserves the
router-generated `command_id` and validates the ROS type, finite/range values,
and endpoint availability. The physical boundary still owns:

- Controller E-stop and collision, force, velocity, joint, and workspace
  limits.
- Per-resource in-flight protection and endpoint/server idempotency.
- Action feedback, result, cancellation, and ambiguous-completion recovery.
- A stopped state with no in-flight request before a physical route change.

Camera, ASR, VLM, UI, and unrelated service health are diagnostic observations,
not global command or scenario-start gates.

## Scenario and Debug interaction

An explicit operator command can be sent while a scenario is running if its
target server accepts it. Autonomous workflow choices remain under scenario
policy. Debug observation is always read-only; Debug writes require an
authoritative paused or stopped state, and physical Debug writes additionally
require an explicit arm plus single-flight protection. Debug is not a second
voice-command pipeline.

## Retired path

The former raw-text resolver/proposal path is kept only as a clearly marked
legacy migration reference in
[`VOICE_COMMAND_CONTRACT.md`](VOICE_COMMAND_CONTRACT.md). Do not add new
runtime consumers, compatibility projections, schema fingerprints, static
allowlists, or duplicate execution checks for it.

This document describes the intended source architecture; it does not claim a
live ROS or physical-controller validation.
