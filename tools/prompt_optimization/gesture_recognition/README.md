# CAM4 open-receive gesture prompt experiment

This is an offline, single-task prompt evaluation for the visible open-palm
instrument-receiving posture. It is intentionally separate from `real_vlm.py`,
the digital twin, BT dispatch, Actions, and robot control.

## Input and output contract

- Input: `gesture-full-frame-v6` uses one current **full CAM4** still image and
  scans every visible hand. The reviewed `review_corrected.mp4` media is a
  two-panel video; the evaluator extracts its left CAM4 panel. The optional
  `right_detail_only` and `full_plus_right_detail` variants use the same frame's
  fixed camera-layout crop (`x=340..640, y=0..300`) enlarged onto a padded
  640x360 canvas. It is not a GT- or detector-selected ROI. The two-image
  variant is retained for experimentation only; do not use it if the active
  NInfer worker cannot reliably accept multiple images.
- `causal_right_detail_pair` keeps one 640x360 input image, with a fixed
  right-side crop 12 CAM4 frames earlier on the left and the current crop on the
  right. It is causal: the target remains the current/right panel and no future
  frame is used.
- `gesture-top-right-open-hand-v7` uses only that fixed right-side crop. Its
  prompt asks whether the upper-right surgeon's hand is visibly open and held
  out, and its output is the binary JSON field `gesture` only.
- No input includes a case ID, timestamp, event ID, transcript, tool list,
  phase, procedure context, or ground-truth label.
- Output: exactly one JSON object with `gesture` (`open_receive`,
  `not_open_receive`, or `uncertain`), `confidence`, and short visible-only
  evidence. The model must not name a tool or infer an action.
- NInfer requests use `temperature=0`, `reasoning_effort=none`, and
  `enable_thinking=false`.

## Evaluation protocol

The availability audit covers `0704_5`–`0704_17`.

- `0704_5` is excluded because it lacks a complete gesture-target reference;
  missing data is never silently treated as a negative.
- `0704_6`–`0704_14` are development cases. The earlier 60% of confirmed
  gesture events in each case form calibration and later events form the frozen
  temporal challenge.
- `0704_15`–`0704_17` are held-out cases. Their early/late temporal split is
  reported, but neither half is used to select a prompt or threshold.

The current manifest has 528 samples: 245 development-calibration, 140
development temporal-challenge, and 143 case-holdout samples. Each target
generates a scorable onset when available, an interior frame, and local
pre/post boundary controls. Labels remain evaluator-only and never reach
NInfer.

The source reference combines historical human review and authorized assistant
video review. Its labels are read-only in this experiment. The 12-frame
boundary controls are deterministic samples derived from the existing intervals,
not new annotations. Treat the score as offline CAM4-frame agreement, not a
deployable handover or clinical-safety claim.

For a visual-pose policy in which any visibly open, held-out hand is positive,
do not score those boundary controls as visual negatives: the pose can remain
open outside a semantic request interval. Create a read-only positive-only
manifest with `filter-confirmed-positive-manifest` and report its positive
recall with `score-confirmed-positive`. This uses only existing confirmed
open-hand samples; it does not create labels or claim specificity/accuracy.

## Run

Use a new ignored output directory for each prompt version/run:

```bash
run_root=output/prompt_optimization/gesture_recognition/0704_all/gesture-v6
python3 -m tools.prompt_optimization.gesture_recognition.gesture_prompt_eval \
  build-multicase-manifest \
  --output "$run_root/manifest.jsonl" \
  --coverage-report "$run_root/coverage.json"

python3 -m tools.prompt_optimization.gesture_recognition.run_batched_eval \
  --manifest "$run_root/manifest.jsonl" \
  --video-root /home/arl/.cache/taskplanner_annotation \
  --output-root "$run_root/v6-calibration" \
  --split calibration \
  --prompt-version gesture-full-frame-v6 \
  --input-variant full_cam4
```

`run_batched_eval` holds the shared NInfer lock and reloads the worker before
each batch of at most three calls. A transport failure is stored as failed
evidence and never silently converted into a score. Do not select a prompt or
threshold after opening the temporal challenge or held-out cases.
