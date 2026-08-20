# Top-right open-hand V8 evaluation — 2026-08-19

## Decision

`gesture-top-right-open-hand-v8` is the selected evaluation-only hand-pose
prompt. It receives one fixed upper-right CAM4 crop and returns only
`{"open_hand":true|false}`. It does not receive case, event, timestamp,
transcript, tool, procedure, or ground-truth data.

The prompt asks only:

> Look only at that surgeon's gloved hand. Is the hand visibly open and held
> out?

## Reference and measurement boundary

- Source: the pre-existing, read-only 528-sample 0704_6–17 manifest, built
  from historical human and authorized AI video-review intervals.
- No event interval, frame label, or negative label was added or edited.
- The visual-pose policy marks a visibly open, held-out hand as positive even
  when it is outside a semantic request interval.
- Therefore only the 263 existing confirmed `open_receive` samples were
  retained. The report measures **positive recall**, not accuracy, specificity,
  or false-positive rate.

The old pre/post interval controls are intentionally excluded: direct review
showed that 11 apparent V7 false positives still visibly had an open hand, so
they are policy-concordant rather than visual false positives.

## Frozen configuration

- Model: `qwen3.6-35b-a3b`
- Input: fixed `x=340..640, y=0..300` CAM4 crop, no label-dependent ROI
- Prompt/output: `gesture-top-right-open-hand-v8` / JSON boolean
- Selection lock: `output/prompt_optimization/gesture_recognition/0704_all/20260819-top-right-v7/FROZEN_V8_SELECTION.json`
- Lock integrity: prompt, manifest, protocol, and calibration report hashes all
  matched immediately before frozen evaluation.

## Results

| Partition | Existing positive samples | Detected | Positive recall | Transport / format failures |
| --- | ---: | ---: | ---: | ---: |
| Development calibration (0704_6–14 early) | 121 | 97 | 80.2% | 0 / 0 |
| Frozen temporal challenge (0704_6–14 late) | 70 | 50 | 71.4% | 0 / 0 |
| Frozen case holdout (0704_15–17) | 72 | 57 | 79.2% | 0 / 0 |

The V8 boolean output increased same-calibration positive recall from V7's
55.4% (67/121) to 80.2% (97/121). V7's `open_receive` output name could still
be interpreted as a semantic receiving action; V8 removes that term from the
model output.

## Direct failure review

Every remaining V8 failure was rendered from the exact fixed crop sent to the
model and inspected:

- Calibration: 24 FN samples
- Frozen temporal challenge: 20 FN samples
- Frozen holdout: 15 FN samples

The recurring failure modes are a side-on or fingers-together hand, motion
blur at the start of the interval, partial occlusion by another hand/instrument,
or a partly visible hand near the bright surgical field. No output-format or
transport failure was counted as a model decision.

Review artifacts:

- Calibration crop sheets:
  `output/prompt_optimization/gesture_recognition/0704_all/20260819-top-right-v7/v8-calibration-positive-only/failure_review_right_detail/`
- Temporal challenge crop sheets:
  `output/prompt_optimization/gesture_recognition/0704_all/20260819-top-right-v7/v8-frozen-temporal-challenge-positive-only/failure_review_right_detail/`
- Holdout crop sheets:
  `output/prompt_optimization/gesture_recognition/0704_all/20260819-top-right-v7/v8-frozen-case-holdout-positive-only/failure_review_right_detail/`

## Limitation and next measurement requirement

This result is not a 79.2% overall accuracy claim. Under the visual-open-hand
policy, the repository currently provides confirmed positive intervals but no
separate, policy-consistent visual-negative reference set. Creating negatives
would violate the instruction not to add labels. A future specificity or
accuracy metric requires an already-reviewed visual-negative label source from
the task owner; no such labels were created in this work.

All artifacts remain evaluation-only. No ROS topic, task-planner action, or
physical handover behavior was changed.
