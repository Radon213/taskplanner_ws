# Runtime owner control

`config/taskplanner_runtime_owners.toml` is the single operational inventory
for Taskplanner's independently restartable lanes. It describes process
ownership only; it does not grant ROS endpoint or physical-control authority.

## Canonical mode sets

Live starts `state-core`, `command`, `tool-state`, `perception`, `cam4-mayo`,
`projection`, `execution`, `operator-bridge`, `scenario`, `surgery-record`, and
the read-only Debug observer. `surgery-record` is an unconditional Live-only
owner; it is not started in LLM Surgeon, Replay, or standalone Debug because
those modes can create synthetic/test records. LLM Surgeon adds
`simulation-input`, and Live ASR remains an independent sidecar. Replay
continues to use `shadow-runner` as its core.

The record owner observes the Manager-owned
`/simulation/lifecycle_terminal` receipt. Both UI Stop and voice Stop converge
through `/simulation/control`, so neither source has a separate upload lane.
Only a same-run settled `stop/halted` or `completed/completed` receipt can save
and submit. `paused`, `idle/reset`, failed Stop, missing run identity, and raw
Digital Twin terminal frames do not authorize POST.

There is no legacy runtime Compose service. New startup, status, scenario
reload, and restart paths address only the registered owner set. If a
pre-partition container is still present after updating this workspace,
inspect it once with
`docker ps --filter label=com.docker.compose.service=taskplanner-runtime`
and remove that identified stale container before starting the owner plane;
the launcher never adopts or targets it.

## Operator commands

```bash
scripts/taskplanner owners
scripts/taskplanner status owners --mode live
scripts/taskplanner status command --mode live
scripts/taskplanner restart command live
scripts/taskplanner restart scenario live
scripts/taskplanner restart surgery-record live
scripts/taskplanner reload config thyroidectomy_demo --mode live
```

An owner restart performs no source census, image build, workspace build, or
unrelated restart. It requires that owner's Compose container to exist. A
normal `up live` or `up llm-surgeon` deploys the complete canonical owner set.

Owner status is read-only. Common states are `running`, `exited`,
`not-deployed`, `not-applicable`, and `unavailable` (the owner launch file is
missing). It only projects services named by the owner registry.

Scenario config reload always calls the standalone `taskplanner-scenario`
owner. Its selected bundle is stored at
`/taskplanner-scenario-state/selected_bundle.json`, backed by
`TASKPLANNER_SCENARIO_STATE_DIR`. The workspace and owner root filesystem stay
read-only; only explicit state mounts and `/tmp` are writable.

## Dashboard API

The existing loopback-only runtime controller is the only browser-facing
owner API. Both endpoints require the existing
`X-Taskplanner-Runtime-Control-Token` header.

`GET /v1/runtime/owners?mode=live` returns:

```json
{
  "mode": "live",
  "owners": [
    {
      "owner": "command",
      "mode": "live",
      "state": "running",
      "service": "taskplanner-command",
      "detail": "Up 12 seconds"
    }
  ]
}
```

`POST /v1/runtime/owners/restart` additionally requires a UUIDv4
`X-Taskplanner-Request-Id` and an exact body:

```json
{"owner":"command","mode":"live"}
```

Success is HTTP 200 with
`{"accepted":true,"owner":"command","mode":"live","message":"The owner restarted."}`.
A mode mismatch, stopped/missing owner, or rejected restart is HTTP 409 with
the same identity fields and an `error` string. Unknown names/modes and extra
body fields are HTTP 400. The UI must not infer Docker state or construct a
second restart implementation.
