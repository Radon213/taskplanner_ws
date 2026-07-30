# Unified 0704 surgical-video review workbench

The primary workbench combines the final observed/DT event navigator,
functional Phase timeline, two-field clinical drafts, voice context, and
optional RF-DETR overlays in one case-switchable page. Source MCAP, immutable
AI candidates, and already-published references remain read-only; review writes
use their dedicated append-only audit layers.

## Launch

From the repository root:

```bash
tools/real_surgery_annotation/run_0704_multicase_clinical_review.sh
```

Then open `http://127.0.0.1:8878/?case=0704_7&mode=final-dt`, changing the case
query as needed. The launcher currently serves `0704_6` through `0704_17`.

The launch script sources an installed ROS 2 environment, preferring Jazzy and
falling back to Lyrical on this host. This is required for exact-frame CAM4 and
FLIR fallback reads even when the browser normally plays the validated proxy
videos. A single-case legacy launch remains available as
`run_0704_6_interaction_review.sh`.

The equivalent multi-case command is:

```bash
source /opt/ros/lyrical/setup.bash
python3 -B -m tools.real_surgery_annotation.interaction_review_gui \
  --cases-root annotations/observable_tool_events/cases \
  --case-id 0704_6 --case-id 0704_7 --case-id 0704_8 \
  --case-id 0704_9 --case-id 0704_10 --case-id 0704_11 \
  --case-id 0704_12 --case-id 0704_13 --case-id 0704_14 \
  --case-id 0704_15 --case-id 0704_16 --case-id 0704_17 \
  --review-media-root "${TASKPLANNER_ANNOTATION_CACHE:-$HOME/.cache/taskplanner_annotation}" \
  --default-case 0704_17 \
  --default-review-mode final_dt \
  --host 127.0.0.1 \
  --port 8878
```

The default inputs and output are:

- AI proposals, read-only:
  `annotations/observable_tool_events/cases/0704_6/interaction_candidates.ai_review.v1.jsonl`
- exact CAM4 index/timestamp map, read-only:
  `annotations/observable_tool_events/cases/0704_6/cam4_frame_timeline.v1.json`
- source CAM4 and FLIR compressed-image messages, read-only: the original
  `0704_6` MCAP named by the timeline
- human decisions, append-only:
  `annotations/observable_tool_events/cases/0704_6/human_review_decisions.v1.jsonl`

`--candidates` and `--decisions` can select an independent stream. For the
separate phase proposal file, keep its human decisions separate as well:

```bash
python3 -m tools.real_surgery_annotation.interaction_review_gui \
  --case-dir annotations/observable_tool_events/cases/0704_6 \
  --candidates annotations/observable_tool_events/cases/0704_6/phase_candidates.ai_review.v1.jsonl \
  --decisions annotations/observable_tool_events/cases/0704_6/phase_human_review_decisions.v1.jsonl \
  --stream-kind phase \
  --source-bag /path/to/0704_멀티모달_ROS2_MCAP_v1.0.0/bags/0704_6 \
  --port 8879
```

`--stream-kind phase` makes `phase_start` the only allowed event type, so the
inspector exposes only `phase_id`; tool/from/to stay hidden and are stored as
null. The default `--stream-kind interaction` allows only request and transfer
events. The backend rejects a decision whose type does not belong to the
selected stream.

## RF-DETR CAM4·FLIR overlay

The unified page can draw the RF-DETR tool and `Hand_request` detections over
the existing source-review videos. The source video and audio remain unchanged,
and the RF-DETR result is explicitly treated as AI inference rather than human
ground truth. The `AI 인식` checkbox is shown only when a validated payload is
available and is off by default for each case.

Import or refresh the browser-safe payloads from the read-only NAS artifacts:

```bash
python3 -m tools.real_surgery_annotation.import_rfdetr_overlays
```

The importer checks all of the following before publishing a case:

- reconstruction schema/status and gzip SHA-256
- contiguous zero-based frame indexes
- source dimensions and in-bounds bounding boxes
- RF-DETR CAM4/FLIR timestamps against the canonical annotation timeline

Only class, confidence, bounding box, and optional FLIR tracker ID are
published. NAS paths, checkpoint paths, and FLIR segmentation RLE are omitted.
The UI uses `source_frame_idx` directly, so the overlay remains aligned across
VFR sections and recorded video gaps. CAM4 is mapped to the 640×360 proxy;
FLIR's 2048×1496 source is mapped through its 492×360 content area with 74-pixel
side padding. Exact-frame fallback images use the original source dimensions.

It is valid to launch while the proposal file is missing or empty. The page
shows a recoverable empty state and does not create the decision output until a
human submits a decision.

## Review behavior

The selected `source_frame_idx` is the authoritative coordinate. The backend
serves the exact Nth CAM4 message and exact Nth FLIR message independently; it
does not approximate a frame by wall time. The displayed and saved
`time_sec` is always recomputed from
`cam4_frame_timeline.v1.json.timestamps_sec[source_frame_idx]`.

Controls:

- Left/right arrow: −/+ 1 source frame
- Shift + left/right arrow: −/+ 5 source frames
- `Enter`: confirm
- `A`: ambiguous
- `R`: reject

Keyboard shortcuts do not fire while an input, select, textarea, or button has
focus.

AI recommendation, evidence, generator, and query are read-only. A proposal is
never automatically promoted. Only an explicit human `confirmed` decision has
`resulting_label_origin="human_video_review"`; ambiguous and rejected decisions
have a null resulting label origin.

Every decision includes the immutable candidate digest, the canonical
frame-index/time pair, the reviewed core fields, reviewer identity, and UTC
review time. The server uses an OS append lock, `fsync`, optimistic revision,
and a semantic request digest. Retrying an identical request is idempotent.
A different second decision for the same candidate is rejected because the log
is append-only.

## Tests

The store and append-only/idempotency rules require only Python's standard
library:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.real_surgery_annotation.test_interaction_review_gui
```

Actual frame delivery additionally requires the ROS 2 Python bindings and the
source MCAP.

## Materialize reviewed point labels

The review decision log is an audit trail, not itself a point-label stream.
After review, create a new validated JSONL and report without changing the
candidate or decision inputs:

```bash
python3 -m tools.real_surgery_annotation.materialize_human_review_decisions \
  --candidates annotations/observable_tool_events/cases/0704_6/interaction_candidates.ai_review.v1.jsonl \
  --decisions annotations/observable_tool_events/cases/0704_6/human_review_decisions.v1.jsonl \
  --schema annotations/observable_tool_events/schema/observable_interaction_point.v1.schema.json \
  --timeline annotations/observable_tool_events/cases/0704_6/cam4_frame_timeline.v1.json \
  --tools annotations/observable_tool_events/catalogs/tools.yaml \
  --case-id 0704_6 \
  --stream-kind interaction \
  --require-all \
  --output annotations/observable_tool_events/cases/0704_6/interaction_points.human_reviewed.v1.jsonl \
  --report annotations/observable_tool_events/reports/0704_6_interaction_materialization.v1.json
```

Materialize the independent phase review with the same gate:

```bash
python3 -m tools.real_surgery_annotation.materialize_human_review_decisions \
  --candidates annotations/observable_tool_events/cases/0704_6/phase_candidates.ai_review.v1.jsonl \
  --decisions annotations/observable_tool_events/cases/0704_6/phase_human_review_decisions.v1.jsonl \
  --schema annotations/observable_tool_events/schema/observable_interaction_point.v1.schema.json \
  --timeline annotations/observable_tool_events/cases/0704_6/cam4_frame_timeline.v1.json \
  --tools annotations/observable_tool_events/catalogs/tools.yaml \
  --case-id 0704_6 \
  --stream-kind phase \
  --require-all \
  --output annotations/observable_tool_events/cases/0704_6/phase_points.human_reviewed.v1.jsonl \
  --report annotations/observable_tool_events/reports/0704_6_phase_materialization.v1.json
```

Both output paths are create-only and are staged before atomic publication.
Without `--require-all`, only reviewed candidates are materialized and the
report lists every unreviewed ID. With it, any unreviewed candidate blocks both
outputs.

The materializer verifies the candidate digest, decision/case identity,
duplicates, exact frame-index/time pair, stream event types, and conditional
core fields before validating the final JSONL against the point schema.
Confirmed records receive `human_video_review`; ambiguous and rejected records
keep the proposal's original `label_origin` and receive the human review block.
AI review and proposal evidence are retained. If the human changes an event
type, the materializer allocates the first unused deterministic ID in the new
`R`/`T`/`PH` namespace and records the mapping in the report.
For phase records, the candidate's validated `phase_boundary_kind` is retained
so a clip-initial state cannot silently become an observed transition.

This tool does **not** adapt the new point schema to the legacy tool-event
evaluator. That adapter belongs to the evaluator integration layer and is
outside this materializer.

Materializer tests:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.real_surgery_annotation.test_materialize_human_review_decisions
```

## Finalize the timeline review and DT reference

The current workbench writes candidate corrections, request intervals, and
human-created events to the shared append-only
`human_timeline_actions.v1.jsonl`. After every candidate has been reviewed,
materialize both the direct observation layer and the separate Taskplanner-DT
evaluation layer:

```bash
PYTHONPYCACHEPREFIX=/tmp/taskplanner_pycache \
python3 -m tools.real_surgery_annotation.finalize_interaction_review \
  annotations/observable_tool_events/cases/0704_6
```

The command validates the latest superseding human actions plus the
hash-anchored, authorized
`assistant_annotation_adjudications.final.v2.jsonl`, then publishes three
create-only artifacts:

- `interaction_events.observed.final.v3.jsonl`: every confirmed request
  interval and physically observed transfer;
- `interaction_events.dt_reference.final.v3.jsonl`: only the transitions used
  by Taskplanner evaluation;
- `reports/0704_6_dt_projection.final.v3.json`: source hashes, every excluded
  or collapsed source event, output mapping, counts, and singleton-DT
  continuity warnings.

Publication is create-only. A later adjudication must use a new output version;
earlier outputs remain immutable audit intermediates.

The case-local `dt_projection_policy.v1.json` is part of the audited input.
For 0704_6 it excludes continuous `Mayo → scrub → Mayo` tidying/correction
roundtrips, collapses `surgeon → scrub → Mayo` to one `surgeon → Mayo` event at
Mayo arrival, and retains `Mayo → scrub → surgeon` because the scrub role
represents the future humanoid. Raw observations are never deleted. Phase
proposals are not included in this finalized interaction reference.
