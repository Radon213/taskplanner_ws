# Observable surgical-tool event pilot

This directory implements the first 0704_5 annotation and MCAP-injection unit
from `docs/REAL_SURGERY_EVENT_ANNOTATION_MCAP_PLAN.md`.

The code is intentionally separate from Taskplanner runtime nodes. It writes
evaluation-only reference labels to the historical derived-MCAP topic namespace
`/evaluation/ground_truth/*`; it does not subscribe VLM, reducer, BT, or skill
nodes to those topics. Each record's `label_origin`, `reviewer_kind`, and
`review_status` are the authoritative provenance and acceptance boundary.

## Review the 0704_5 pilot

```bash
tools/real_surgery_annotation/run_0704_5_gui.sh
```

Open `http://127.0.0.1:8877/`. The workbench shows synchronized CAM4 and FLIR
RGB frames, the legacy-derived proposal queue, the physical from/to state, and
human review controls.

- `Enter`: confirmed
- `A`: ambiguous
- `R`: rejected
- left/right: one corrected video frame
- Shift + left/right: one second

Enter a reviewer ID before saving. A confirmed event cannot retain an unknown
tool, holder, or location; save it as ambiguous until it can be resolved.
Saving moves a proposal into the active `annotation_manifest.json` `event_file`,
keeps model provenance, and updates the manifest counts. For the promoted
0704_5 reference this file is `tool_events.final.v1.jsonl`. The source MCAP is
never opened for writing.

## Validation and derived bag

```bash
python3 -m tools.real_surgery_annotation.validate_annotations \
  annotations/observable_tool_events/cases/0704_5 \
  --schema annotations/observable_tool_events/schema/observable_tool_event.v1.schema.json \
  --tools annotations/observable_tool_events/catalogs/tools.yaml

source /opt/ros/jazzy/setup.bash
python3 -m tools.real_surgery_annotation.inject_annotations \
  --source-bag /path/to/0704_멀티모달_ROS2_MCAP_v1.0.0/bags/0704_5 \
  --case-dir annotations/observable_tool_events/cases/0704_5 \
  --schema annotations/observable_tool_events/schema/observable_tool_event.v1.schema.json \
  --tools annotations/observable_tool_events/catalogs/tools.yaml \
  --output annotated_bags/0704_5_reviewed
```

The injector refuses to overwrite an existing output. It writes to a staging
directory, verifies every original serialized payload and timestamp, and only
then renames staging to the requested derived output.

GT injection also refuses to start while a proposed candidate remains or while
the completion gate is false. New manifests use
`annotation_adjudication.complete`; legacy human-only manifests may still use
`human_annotation.complete`. An empty proposal queue means only that the
candidate-review round is finished. Set the general completion gate only after
the full video has been checked for model-missed transitions.

Confirmed records retain their authority explicitly:

- `human_video_review` with `reviewer_kind=human` means a person directly
  reviewed the source.
- `assistant_video_adjudication` with `reviewer_kind=ai_assistant` means an AI
  assistant performed the source review under the named user's authorization.
  `review.authorized_by` is required.
- `assistant_visual_proposal` and `temporal_grounding_model` remain proposal
  evidence and cannot be represented as assistant-confirmed reference labels.

`annotation_adjudication.confirmed_origin_counts` and
`confirmed_reviewer_kind_counts` must match the confirmed JSONL records. This
prevents a mixed reference set from being reported as exclusively human ground
truth. Ambiguous records may be injected into the historical reference topic
for audit context, but retain `review_status=ambiguous` and are excluded from
shadow metrics. Once `annotation_adjudication.complete` is true, the review GUI
rejects further writes. A follow-up round must be opened explicitly by setting
that gate back to false so an already promoted set is not changed accidentally.

## TimeLens2 proposal boundary

`generate_timelens2_candidates.py` accepts temporal intervals from an external
TimeLens2 run. It always emits `review_status=proposed`, leaves physical facts
unknown, and writes no ground-truth message. `run_timelens2.py` executes the
local checkpoint and preserves its model-native intervals and timing report;
`merge_candidate_proposals.py` validates and combines only `proposed` records
without overwriting an earlier candidate file.

## Mage-VL streaming evidence boundary

`run_mage_vl_streaming.py` invokes Microsoft's official
`mage_vl/inference_streaming.py` as a subprocess and preserves its stdout as
segment/gate evidence. The output schema is deliberately not an observable
event schema: it contains no tool, holder, location, event type, or exact event
time and cannot be published as ground truth.

```bash
python3 -m tools.real_surgery_annotation.run_mage_vl_streaming \
  --video /path/to/proxy.mp4 \
  --case 0704_5 \
  --mage-repo "${MAGE_REPO:-$HOME/.cache/taskplanner/mage}" \
  --threshold 0.5 \
  --segment-sec 8 \
  --max-segments 4 \
  --max-new-tokens 80 \
  --attn-impl sdpa \
  --output annotations/observable_tool_events/proposals/0704_5.mage.raw.jsonl \
  --report annotations/observable_tool_events/reports/0704_5_mage_run.json
```

Both output paths are create-only. The report retains the exact secret-free
argv, subprocess return code and elapsed time, raw stdout/stderr, unparsed
stdout, official-script digest, and package versions. A failed subprocess still
writes the evidence already emitted and a failure report; it never creates or
changes an observable event or MCAP ground-truth topic.

## Shadow evaluation boundary

`shadow_evaluate.py` reads decision JSONL and confirmed-reference annotation
JSONL offline. Proposed, ambiguous, and rejected rows are excluded. The report
includes label-origin and reviewer-kind counts plus `reference_authority`. With
zero confirmed events it returns `awaiting_confirmed_reference` and
`metrics: null` instead of manufacturing a score.

The executable end-to-end path is:

```text
CAM4 + public transcript
  -> real/replay VLM
  -> authoritative digital-twin reducer
  -> Behavior Tree
  -> shadow skill sink
  -> trace recorder

confirmed reference events -> offline evaluator only
```

Strict mode replays only the configured image and transcript topics. It rejects
any runtime subscription or launch wiring to `/evaluation/ground_truth/*`.
Counterfactual success feedback is generated from an admitted skill command,
never from a reference event, and is marked `shadow_counterfactual` with
`ground_truth_used=false`.

Every explicit request carries a monotonically increasing
`request_generation` through WorldState, BTDecision, and SkillCommand. This
keeps adjacent requests for two instances of the same instrument separate while
still suppressing duplicate ticks from one request. A missing per-instance
inventory may be reconciled only in shadow mode, is marked
`additional_instance_assumed`, and is counted in the report.

The BT bridge republishes each WorldState after it has queued the matching
blackboard update on the internal `/bt/context_ingress` topic. Shadow traces use
that boundary to keep three latency meanings separate:

- `DT fact -> BT context ingress` measures reducer-to-BT transport and callback
  latency. This is the software latency gate.
- `DT fact -> first BT decision publication` includes time until the active tree
  can publish a decision, including a currently running skill branch.
- `DT fact -> BT action acceptance` additionally includes policy, availability,
  and fail-closed safety waiting before a handover action is admitted.

Explicit request links require the same `request_generation`; visual implicit
request links require the same public request source and tool. Tool name and a
broad time window alone are not sufficient correlation evidence.

### Deterministic fixture

Generate the fixture once, run it twice in isolated ROS domains, then compare
semantic traces:

```bash
python3 -m tools.real_surgery_annotation.generate_shadow_fixture \
  --output-root output/shadow_fixture/run-001

docker compose --profile shadow run --rm shadow-runner \
  python3 -m tools.real_surgery_annotation.run_shadow_replay \
  --case-dir /workspaces/taskplanner_ws/output/shadow_fixture/run-001/annotations/cases/shadow_fixture \
  --source-bag /workspaces/taskplanner_ws/output/shadow_fixture/run-001/bag \
  --mode strict \
  --run-id synthetic-strict-a \
  --ros-domain-id 81 \
  --response-mode replay \
  --replay-response-path /workspaces/taskplanner_ws/output/shadow_fixture/run-001/replay_response.v4.json

python3 -m tools.real_surgery_annotation.compare_shadow_determinism \
  --run-a output/shadow_runs/synthetic-strict-a \
  --run-b output/shadow_runs/synthetic-strict-b \
  --output output/shadow_runs/synthetic-determinism.json
```

Replay-response mode is triggered by source image timestamps, not wall-clock
timer order. The comparison excludes correlation IDs and executor jitter, but
requires identical public inputs and collapsed VLM, reducer, BT, skill, and
shadow-sink trajectories.

### Layered scoring

`shadow_evaluate.py` reports these layers independently:

- `vlm_model_raw`: parsed model response after ontology ID normalization but
  before speech anchoring, temporal-prior merging, Mayo corroboration, and
  intent suppression;
- `vlm_raw`: operational VLM output after runtime stabilization but before
  reducer policy. The legacy layer name is retained for trace compatibility
  and must not be interpreted as the native model response;
- `reducer_fused`: accepted public evidence plus deterministic reducer state;
- `bt_decision`: selected policy branch;
- `skill_command`: command that would reach robot execution.

Only handover/preparation actions enter handover precision and recall.
`retrieve_from_mayo` is audited separately against the confirmed observable
timeline. A recovery can be a blocker, suspicious, review, or informational;
those labels are evidence-based audit categories, not clinical correctness.

Phase accuracy remains `not_available` until a separate confirmed phase-interval
JSONL is passed with `--phase-ground-truth`. Tool-event timestamps are never
silently converted into phase labels.

See `docs/SHADOW_REPLAY_EVALUATION.md` for the frozen 0704_5 baseline, result
interpretation, and remaining reference limitations.
