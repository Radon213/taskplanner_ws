# Mayo selected v4 frozen temporal-arrival report

## Scope and frozen lock

- Selected prompt: `mayo-recognition-v4` / `optimized_v4`.
- Locked selection artifact: `tools/prompt_optimization/mayo_recognition/FROZEN_V4_SELECTION.json` (ID `394854bb2855334b204352e7a6f60c17445a9cecd2600b9245ecf59c05563bc9`).
- Frozen result: `tools/prompt_optimization/mayo_recognition/runs/mayo-frozen-arrival-optimized-v4-letterbox-fresh-20260818-v1_frozen_arrival_optimized_v4/result.json`.
- Accuracy-eligible cases: `0704_5`.
- This is a pre-registered, within-case, time-separated arrival challenge; it makes **no cross-case or clinical-generalization claim**.
- Unlabelled 0704 cases are excluded from every accuracy, FP, FN, and negative denominator.
- Ground truth was attached after inference only; request bodies contain no event id, label, timestamp, bbox, phase, speech, or DT state.
- Prompt, letterbox preprocessor, and confidence-threshold policy were locked before the first frozen POST. No post-frozen change or rerun is valid for this split.

## Selection caveat retained

- Crop semantic calibration regression was explicitly accepted for the temporal-arrival objective: `7/11` to `5/11` correct.

## Semantic and strict-contract metrics

| Metric | Value |
|---|---:|
| attempted | 5 |
| model_outputs | 5 |
| transport_errors | 0 |
| valid_json | 5 |
| contract_valid | 5 |
| target_recall | 0.4 |
| exact_match | 0.4 |
| accepted_target_recall | 0.4 |
| accepted_exact_match | 0.4 |
| false_positive_total | 0 |

## Post-inference visual review artifacts

- Source failure sheet: `tools/prompt_optimization/mayo_recognition/artifacts/review_frozen_arrival_optimized_v4_letterbox_fresh_source/failure_review_sheet.jpg`
- Exact normalized model-input failure sheet: `tools/prompt_optimization/mayo_recognition/artifacts/review_frozen_arrival_optimized_v4_letterbox_fresh_normalized/failure_review_sheet.jpg`

## Per-sample results

| Frozen sample | Reference (evaluation-only) | Prediction | Tags |
|---|---|---|---|
| 0704_5-challenge-arrival-0704_5-E0016 | "bovie" | ["bovie"] | exact-TP |
| 0704_5-challenge-arrival-0704_5-E0020 | "bovie" | [] | FN |
| 0704_5-challenge-arrival-0704_5-E0031 | "bipolar_forceps" | [] | FN |
| 0704_5-challenge-arrival-0704_5-E0037 | "bipolar_forceps" | [] | FN |
| 0704_5-challenge-arrival-0704_5-E0041 | "bovie" | ["bovie"] | exact-TP |

## Interpretation guardrail

This frozen report can describe only this locked temporal challenge. Frame review may explain failures, but cannot alter v4, normalization, thresholds, or the five-sample result. A new labelled case or separately pre-registered partition is required for any next prompt experiment.
