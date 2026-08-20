# Mayo instrument-recognition prompt evaluation

This directory is an evaluation-only prompt experiment for NInfer
`qwen3.6-35b-a3b`. It does not import, alter, or invoke `real_vlm.py`, the
digital twin, BT dispatch, ROS topics, or a physical controller.

## Input and output contract

The model receives only overhead CAM4 JPEG pixels, a closed tool vocabulary,
and an instruction that forbids procedure, speech, timing, and state priors.
It never receives a reviewed label, event id, source timestamp, bbox coordinate,
or ground-truth JSON.

Two task-specific outputs are tested:

- `inventory`: `{"visible":[["tool_id",count,confidence]],"abstain":false}`
  for a one-frame Mayo inventory.
- `arrival`: `{"newly_on_mayo":[["tool_id",confidence]],"abstain":false}`
  for a chronological CAM4 `BEFORE` / `AFTER` pair. It must list only items
  newly settled on the Mayo surface.

`crop` uses a truth-localized outlined crop and is explicitly **calibration
only**. It measures morphology classification after localization, not
deployable end-to-end Mayo recognition.

The `optimized` prompt adds explicit spatial/difference rules and morphology
checks. The `baseline` uses the same image and output contract without those
task-specific checks. NInfer has no constrained-JSON decoder, so the script
stores raw output and records JSON validity separately.

## Ground-truth boundary and split

The local reference is
`annotations/observable_tool_events/cases/0704_5/tool_events.final.v1.jsonl`,
paired with the immutable reviewed MCAP bag
`annotated_bags/0704_5_reviewed_gt_v2`.

Data availability is limited: `0704_5` is currently the only 0704 case with
confirmed tool-event labels. Its `t=0` frame has 11 confirmed Mayo instance
labels; seven later confirmed, clear CAM4 `place_on_mayo` events have a source
frame but no reviewed bbox. Therefore a case-level held-out score is not
currently possible.

`audit_0704_mayo_coverage.py` audits all `0704_5`–`0704_17` case directories.
At this checkout, `0704_6`–`0704_17` have no confirmed Mayo tool-event rows
and no local CAM4 MCAP. Their raw CAM4 AVIs on the mounted 0704 archive are
audited separately as media-coverage evidence, but are not timestamp-aligned
to the reviewed MCAP labels; they cannot establish accuracy eligibility.
These cases are availability findings only: they are never inserted as negative
examples, false positives, false negatives, or accuracy denominators.

The 52 confirmed records are a mixed-authority reference (11 human video
review and 41 authorized assistant video adjudications). It is suitable for
offline prompt evaluation, but neither this experiment nor its scores should
be described as exclusively human clinical ground truth.

The evaluation policy is deliberately conservative:

1. Use t=0 inventory and truth-localized crops plus early arrival events
   `E0008` and `E0012` (44.7–53.9 s) as calibration-only evidence.
2. Freeze the prompt before reading results on the five later events `E0016`,
   `E0020`, `E0031`, `E0037`, and `E0041` (63.9–145.0 s).
3. Score the later paired-frame arrival samples only after the request returns.
4. Report the latter as a within-case, time-separated challenge—not as clinical
   or cross-case generalization. A labeled second case is required for that.

The report separates semantic recognition (`target_recall`, `exact_match`) from
operationally accepted recognition (`accepted_target_recall`,
`accepted_exact_match`), which also requires a valid strict output contract.
Thus a correct tool name in malformed JSON cannot be mistaken for a deployable
result.

Every result records the SHA-256 of the event reference file. This prevents an
accidental moving target between prompt runs.

## NInfer concurrency and fresh-worker rule

The native vision worker has shown crash risk after several requests in one
lifetime.  For an explicitly assigned live slot, every non-dry run takes the
exclusive `/tmp/taskplanner-ninfer-eval.lock` **for the whole batch** and does
the following before sending any image:

1. Calls the NInfer manager `unload`, waits for its `unloaded` state, then calls
   `load`.
2. Waits for both the manager catalog (`loaded`) and the direct worker catalog
   (`http://127.0.0.1:8082/v1/models`) to expose `qwen3.6-35b-a3b`.
3. Sends at most three inference POSTs, then verifies manager and direct-worker
   readiness again before releasing the lock.

This lifecycle action is part of the evaluation harness only; it is never
invoked by `--dry-run`, VLM runtime code, BT dispatch, or ROS. A lifecycle,
worker-readiness, or inference failure halts the run and leaves an incomplete
artifact that the frozen-report gate refuses. Start a **new** run id after the
runtime owner assigns a recovery slot; never append to a partial batch.

### Current runtime gate

The original crop and a same-geometry JPEG Q95 re-encode each caused a native
worker loss after their single allowed POST. A 512-square, aspect-preserving
black-letterbox probe completed and preserved worker readiness. The runtime
owner approved this transform only for a new, independent **calibration
baseline**: it normalizes every request image, records source/normalized
hashes and geometry, runs one POST per fresh worker, and suppresses every
metric unless all 14 calibration samples finish.

The profile rejects retries, partial sample selections, and batches larger than
one. `baseline` and `optimized_v4` are permitted only for complete normalized
calibration; the frozen suite is now permitted only for the separately locked
`optimized_v4` selection, `letterbox_512_q95`, batch size one, zero retries,
and a supplied immutable selection artifact. It does not establish a causal
runtime explanation or a production preprocessing change. See
[`RUNTIME_PROBE_FINDINGS.md`](RUNTIME_PROBE_FINDINGS.md) and
[`CALIBRATION_NORMALIZED_BASELINE_FINDINGS.md`](CALIBRATION_NORMALIZED_BASELINE_FINDINGS.md).

## Run

Run the following only after the runtime owner assigns the exclusive live slot.
The historical baseline/v2 frozen commands below are intentionally invalid and
are retained nowhere as executable instructions. The one permitted frozen run
uses a fresh worker for **each** of the five pre-registered samples.

```bash
cd /home/arl/Documents/ARPA-H/taskplanner_ws
python3 tools/prompt_optimization/mayo_recognition/mayo_prompt_eval.py \
  --suite frozen_arrival --variant optimized_v4 \
  --image-preprocess letterbox_512_q95 --run-normalizer-unit-tests \
  --batch-size 1 --retries 0 \
  --frozen-selection tools/prompt_optimization/mayo_recognition/FROZEN_V4_SELECTION.json \
  --run-id mayo-frozen-arrival-optimized-v4-letterbox-fresh
```

Use `--dry-run` to validate frame extraction and the information boundary
without sending a model request. Each run creates a new directory beneath
`runs/` and refuses to overwrite it. Raw model text is stored only there;
the base64 image request body is never persisted. The default `--retries 0`
avoids spending a fresh-worker request budget on a failed POST. If a retry is
explicitly enabled, it is still capped by the same maximum-three budget.
Use `--offset` and `--max-samples` only for a separate, explicitly labelled
calibration probe; do not use them to replace a frozen challenge run.

Render the original evaluated CAM4 frame(s) after a completed run with:

```bash
python3 tools/prompt_optimization/mayo_recognition/render_mayo_review.py \
  --result path/to/result.json --output-dir path/to/review
```

The rendered reference/prediction overlays are post-inference audit artifacts,
not model inputs. When any response has a transport, JSON, contract, class,
count, or arrival mismatch, the renderer also writes
`failure_review_sheet.jpg`, an annotated composite, and exact extracted source
JPEGs (`source_*`) listed in its manifest. Review those only after the
corresponding run is complete; they must not be fed back into the model or used
to edit a frozen prompt.

Run the local contract tests with:

```bash
pytest -q tools/prompt_optimization/mayo_recognition
```

For the approved normalized calibration profile, use only the complete,
fresh-worker command below. It includes a recorded deterministic preprocessor
test and refuses a partial score:

```bash
python3 tools/prompt_optimization/mayo_recognition/mayo_prompt_eval.py \
  --suite calibration --variant baseline \
  --image-preprocess letterbox_512_q95 --run-normalizer-unit-tests \
  --batch-size 1 --retries 0 --run-id mayo-calibration-baseline-letterbox
```

The calibration review selected `optimized_v4` specifically for the frozen
within-case temporal-arrival objective: arrival recall improved from 0/2 to
1/2 and false positives from 1 to 0, while crop semantic correctness regressed
from 7/11 to 5/11. `FROZEN_V4_SELECTION.json` records that trade-off, exact
prompt/preprocessor/request/threshold hashes, event-reference hash, and the
five sample IDs before any frozen POST. It is not a cross-case selection.
