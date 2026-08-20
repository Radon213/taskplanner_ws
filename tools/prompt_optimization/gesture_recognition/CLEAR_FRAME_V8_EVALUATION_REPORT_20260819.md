# V8 clear-frame open-hand evaluation — 2026-08-19

## What changed

This run does not use random samples or local boundary controls.

- Positive: exactly one temporal midpoint from every existing confirmed
  `open_receive` interval.
- Negative control: the midpoint of an internal gap between two confirmed
  open-hand intervals, retained only when it is at least 45 CAM4 frames
  (about 3 seconds) from **both** neighbouring interval boundaries.
- Video heads/tails and shorter gaps were excluded rather than treated as
  negatives.
- Existing event labels were not edited.  The model received only the fixed
  upper-right CAM4 crop and returned `{"open_hand":true|false}`.

The manifest, prompt, crop, model, and runner policy were hash-locked before
inference.  All 192 requests completed in fresh-worker batches; there were zero
transport, output-format, or uncertain-output failures.

## Dataset shape

| Source | Samples |
| --- | ---: |
| One event midpoint per confirmed interval | 133 positive proxies |
| High-clearance internal gap midpoints | 59 negative proxies |
| Total | 192 |

The 3-second rule is deliberately conservative, but it is still an
**event-derived proxy**, not a new frame-level human visual annotation.

## Raw agreement with the clear-frame proxy

| Partition | N | TP | FP | TN | FN | Proxy agreement | Recall | Specificity |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Development calibration, 0704_6–14 early | 88 | 56 | 6 | 20 | 6 | 86.4% | 90.3% | 76.9% |
| Frozen temporal challenge, 0704_6–14 late | 49 | 28 | 6 | 8 | 7 | 73.5% | 80.0% | 57.1% |
| Frozen case holdout, 0704_15–17 | 55 | 31 | 6 | 13 | 5 | 80.0% | 86.1% | 68.4% |
| All clear-frame proxies | 192 | 115 | 18 | 41 | 18 | 81.3% | 86.5% | 69.5% |

These numbers are deliberately called *proxy agreement*, not final visual
accuracy.  The prompt was not changed after calibration before either frozen
partition was run.

## Direct review of every disagreement

All 36 disagreements were rendered with both the original CAM4 scene and the
exact crop sent to the VLM, then visually reviewed.

The review confirms two distinct kinds of disagreement:

1. **Event label does not match the current frame's visible pose.**  For
   example, 0704_12 frame 986 and 0704_13 frame 975 are event-midpoint
   positives but the fixed crop does not show a readable open hand, so the
   VLM's `false` is visually plausible.  Conversely, several high-clearance
   negative controls visibly show an open palm, including 0704_15 frame 1179,
   0704_16 frames 876 and 1124, 0704_14 frame 1407, and 0704_9 frame 1546;
   the VLM's `true` is visually plausible even though the event proxy says
   negative.
2. **Likely model-side visual errors remain.**  Clear held-out palms such as
   0704_15 frames 257 and 1641 and 0704_16 frame 1271 were returned as
   `false`; several sleeve/field-only negative crops were returned as `true`.
   Occlusion, bright-field washout, and side-on/partial hand views remain the
   main ambiguity sources.

So the stricter sampling removes much of the old boundary artifact, but it
does **not** convert interaction-event intervals into ground truth for a
frame-level visible-open-hand classifier.  It provides evidence that V8 has
meaningful visual recognition ability, while also showing real room for
improvement.

## Artifacts

- Frozen sampling/protocol: `output/prompt_optimization/gesture_recognition/0704_all/20260819-clear-frame-v1/FROZEN_PROTOCOL.json`
- Manifest and data-quality coverage: `output/prompt_optimization/gesture_recognition/0704_all/20260819-clear-frame-v1/manifest.jsonl`, `coverage.json`
- Raw score report: `output/prompt_optimization/gesture_recognition/0704_all/20260819-clear-frame-v1/V8_CLEAR_FRAME_REPORT.json`
- All 36 visual disagreements: `output/prompt_optimization/gesture_recognition/0704_all/20260819-clear-frame-v1/v8-clear-frame-disagreement-review/REVIEW_INDEX.md`

No runtime ROS, handover, action, or robot-control code changed.
