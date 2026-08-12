# Taskplanner release verification

Taskplanner uses a two-stage release boundary.

1. The software gate verifies reproducible builds, public contracts, degraded
   operation, deterministic fault recovery, UI behavior, and recorded-data
   performance.
2. The site gate verifies the real robot, calibrated poses, grasp success,
   collision avoidance, E-stop behavior, and the deployment network.

Passing the software gate does not mean that the physical system is approved.

## One command

Run the release harness from the repository root:

```bash
scripts/taskplanner verify-release --tier quick
scripts/taskplanner verify-release --tier rc
scripts/taskplanner verify-release --tier full
```

Each run writes `result.json`, `checks.csv`, `checks.svg`, `SUMMARY.md`, and a
log for every check beneath `reports/release/<run-id>/`. A failed command still
leaves the evidence bundle in place.

The tiers are cumulative:

| Tier | Intended use | Included gates |
| --- | --- | --- |
| `quick` | Local edit loop | source checks, Compose contracts, prompt and source-health contracts, web build, external-asset manifest |
| `rc` | Software release candidate | clean Docker image build, product package tests, fault campaign, live ROS/Action probe, Playwright viewports, SBOM |
| `full` | Final software-stage evidence | all RC gates plus restart and soak campaign |

The configured full gate is 100 restarts and a 24-hour soak. Reducing these
values is useful for development, but the result is not equivalent to the full
durability gate:

```bash
scripts/taskplanner verify-release \
  --tier full \
  --restart-iterations 3 \
  --soak-hours 0.02
```

Use `--require-clean` only after the release changes have been committed. The
harness always records the commit, branch, worktree cleanliness, and changed
path count.

## Recorded-surgery performance gate

The clinical replay data and case annotations remain outside the source and
container image. Supply them as read-only roots together with an accepted
baseline report:

```bash
scripts/taskplanner verify-release \
  --tier rc \
  --shadow-dataset-root /data/0704_rosbags \
  --shadow-annotation-root /data/observable_tool_events \
  --shadow-baseline-report-dir /data/baselines/taskplanner-shadow/report \
  --shadow-provider-id ninfer \
  --shadow-base-url http://127.0.0.1:8080 \
  --shadow-model-id qwen3.6-35b-a3b
```

The harness does not load a model. Load the requested model explicitly before
starting this gate. It runs the configured `0704_6` through `0704_17` cases,
aggregates JSON/CSV/Markdown/SVG results, and then enforces:

- no required core accuracy regression greater than 2 percentage points;
- no provider, parse, timeout, or inference failure on the clean replay;
- visual-input-unavailable degradation is counted separately and must never
  promote stale evidence or stop the voice path;
- a maximum prompt length of 16,000 characters;
- fresh-frame VLM p95 latency no greater than 1.0 seconds;
- complete cases, intact public-input boundaries, zero invariant violations,
  zero post-terminal commands, and complete Action fulfillment.

Proactive preparation metrics are reported as advisory because the available
recordings are not a complete or perfectly consistent clinical target. A wrong
preparation must remain reversible, while a wrong direct handover is a safety
failure.

Run the same immutable bags through a seeded mild-noise timeline without
creating a second dataset:

```bash
scripts/taskplanner verify-release \
  --tier rc \
  --shadow-dataset-root /data/0704_rosbags \
  --shadow-annotation-root /data/observable_tool_events \
  --shadow-baseline-report-dir /data/baselines/taskplanner-shadow/report \
  --shadow-fault-scenario config/fault_scenarios/noisy_operating_room.yaml \
  --shadow-max-regression-pp 10
```

For intentionally severe provider or media faults, use
`--shadow-safety-only`. Accuracy and injected inference-latency failures remain
in the report as warnings, while completion, information boundaries, prompt
budget, Action fulfillment, invariant violations, and post-terminal commands
remain release-blocking gates.

## Fault evidence

`config/fault_scenarios/` contains seeded YAML timelines for camera, speech,
VLM, ROS, and Action faults. The release harness records the seed and the
source-state transitions (`READY`, `STALE`, `MISSING`, `RECOVERING`, `ERROR`,
and `DISABLED`).

For interactive shadow replay, the controller's public FLIR, CAM4, transcript,
VLM result, and VLM health publishers are remapped to test-only raw topics.
The opt-in injector starts its timeline on the first replay image and republishes
the transformed messages on the unchanged `/surgery/*` and `/vlm/*` contracts.
The source bag and evaluation-only annotation topics are never modified or
exposed to the runtime decision path.

The software gate requires these properties:

- video publication never waits for VLM inference;
- VLM processing keeps one in-flight request and one replaceable latest frame;
- stale or previous-epoch visual evidence cannot become a digital-twin fact;
- explicit speech remains usable when cameras or VLM are unavailable;
- BT reads validated digital-twin state and never mutates it directly;
- command IDs are idempotent and terminal states emit no new robot command;
- ambiguous Action cancellation fails closed.
- Tool Change reports success only for `result=completed`, and Retraction
  Adjustment reports success only for `final_state=completed`;
- missing, stale, malformed, or procedure-mismatched retraction-arm status
  blocks bed-mounted commands;
- no bed-mounted suction-arm command or state is emitted, while clinical
  suction tool and speech semantics remain available.

## External asset integrity

Generate a metadata-only manifest without copying videos or model weights:

```bash
python3 scripts/create_release_asset_manifest.py \
  --output reports/release/external_assets.json
```

Add `--verify-payloads` to calculate and verify every configured SHA-256 value.
This can be slow on the complete dataset and is intentionally separate from
normal startup. Dataset, annotation, perception-weight, and model roots are
mounted read-only for replay evaluation.

## Physical site gate

The following checks must be completed with the target robot and operating
environment before calling the physical deployment approved:

- robot-to-room and camera calibration;
- pose, trajectory, grasp, release, and collision-clearance acceptance;
- E-stop, protective stop, cancel, reconnect, and ambiguous-recovery tests;
- end-to-end timing on the site network under packet loss and disconnects;
- clinician-approved tool presentation and recovery behavior.
- retraction Tool Change sequence completion and failure propagation;
- single/multi Malleable adjustment, Action cancel recovery, direct-teach
  reporting, and monotonic controller-status revision behavior.

Record these results separately from the software report. A mock Action server
or recorded replay cannot satisfy the physical gate.
