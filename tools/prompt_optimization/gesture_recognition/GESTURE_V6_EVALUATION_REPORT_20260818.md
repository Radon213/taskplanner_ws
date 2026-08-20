# Gesture full-frame v6 evaluation

## Decision

`gesture-full-frame-v6` was selected on the 0704_6–14 development
calibration split and frozen before either evaluation split was opened.  The
frozen configuration is recorded in
`output/prompt_optimization/gesture_recognition/0704_all/20260818-multicase-v1/FROZEN_V6_SELECTION.json`.

The selected input is one current full CAM4 frame.  It uses Qwen
`qwen3.6-35b-a3b`, temperature 0, top-p 1, max tokens 96, a fixed threshold of
0.95, batch size 1, a fresh worker lifecycle per request, and no retries.  All
three runs completed with zero transport failures and no format failures.

## Coverage and split

| Partition | Cases | Samples | Status |
| --- | --- | ---: | --- |
| Calibration | 0704_6–14 early temporal portions | 245 | Used to choose v6 and threshold only |
| Frozen temporal challenge | 0704_6–14 later temporal portions | 140 | Opened once after selection |
| Frozen case holdout | 0704_15–17 | 143 | Opened once after selection |

`0704_5` was audited but excluded because it lacks a complete confirmed
gesture-target reference; it was never converted into negative examples.

## Metrics

| Partition | TP / FP / TN / FN | Balanced accuracy | F1 | Onset recall |
| --- | --- | ---: | ---: | ---: |
| Calibration | 75 / 45 / 79 / 46 | 62.85% | 62.24% | 39.34% |
| Temporal challenge | 42 / 36 / 34 / 28 | 54.29% | 56.76% | 51.43% |
| Case holdout | 50 / 34 / 37 / 22 | 60.78% | 64.10% | 61.11% |

The controlled full-frame calibration comparison selected v6 over v4:

| Prompt | Input | Balanced accuracy | F1 | FP | FN |
| --- | --- | ---: | ---: | ---: |
| `gesture-pose-only-v4` | full CAM4 | 59.25% | 60.94% | 57 | 43 |
| `gesture-full-frame-v6` | full CAM4 | 62.85% | 62.24% | 45 | 46 |

The prior v5 result uses a different causal detail input and is therefore not
included in this controlled v4/v6 comparison.

## Direct failure review

The primary agent reviewed every rendered FP/FN frame:

| Partition | Reviewed failures | Pages | Recurrent observation |
| --- | ---: | ---: | --- |
| Calibration | 91 | 23 | 37 onset FNs; most show an instrument grip, tissue manipulation, an occlusion, or no readable palm. |
| Temporal challenge | 64 | 16 | 17 onset FNs; same frame-to-event alignment failure recurs. |
| Case holdout | 56 | 14 | 14 onset FNs; FPs again mostly visibly open empty palms in event-boundary negatives. |

The model's visual evidence is usually consistent with the supplied current
frame.  In many purported FNs, the labeled tool-request event occurs while the
frame visibly shows manipulation or a non-palmar hand.  In many purported FPs,
the frame visibly contains an empty open palm even though it lies just before
or after the source event boundary.  This makes the remaining score primarily
a **source-event-to-visible-pose alignment diagnostic**, not independent
human-adjudicated pure-CAM4 pose accuracy.

Failure artifacts:

- Calibration: `output/prompt_optimization/gesture_recognition/0704_all/20260818-multicase-v1/v6-full-frame-calibration/failure_review_full/failures.json`
- Temporal challenge: `output/prompt_optimization/gesture_recognition/0704_all/20260818-multicase-v1/v6-full-frame-frozen-temporal-challenge/failure_review_full/failures.json`
- Case holdout: `output/prompt_optimization/gesture_recognition/0704_all/20260818-multicase-v1/v6-full-frame-frozen-case-holdout/failure_review_full/failures.json`

## Implication for the next improvement

Do not claim deployment-grade hand-gesture accuracy from these event-derived
labels.  The next meaningful improvement is not another longer prompt: create
a small independently human-adjudicated **visible-pose** set with a target-hand
identity and a narrow timestamp window, then evaluate a short temporal frame
sequence or a target-hand crop against that new authority.  The frozen results
above must not be reused to alter v6 or select a new prompt.

All artifacts are evaluation-only.  No output was sent to ROS, a tool
dispatcher, or a physical controller.
