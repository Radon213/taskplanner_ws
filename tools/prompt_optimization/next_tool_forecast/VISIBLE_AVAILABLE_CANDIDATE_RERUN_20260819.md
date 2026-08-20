# Visible-available candidate rerun — 2026-08-19

## Result

The requested candidate rule was tested on the complete `0704_6`–`0704_14`
development set (121 causally separated examples).  The model was Qwen 3.6
35B A3B, with one fresh worker and one inference request per example.  All
121 calls completed with a valid output contract and no transport failure.

| condition | overall exact accuracy | correct handovers / positive handovers | handover recall | actual-`none` specificity | actual-`none` false positives |
| --- | ---: | ---: | ---: | ---: | ---: |
| state + visual retrieval v1 | 46/121 (38.0%) | 18/87 | 20.7% | 82.4% | 6 |
| state + visual retrieval v2 policy | 74/121 (61.2%) | 54/87 | 62.1% | 58.8% | 14 |
| **v3 visible-available candidate gate** | **35/121 (28.9%)** | **3/87** | **3.45%** | **94.1%** | **2** |

The v3 condition predicted `none` 110 times and a handover 11 times.  Its 11
handover predictions contained 3 exact tools, 6 wrong tools, and 2 predictions
in true-`none` windows.  It is rejected as a next-tool candidate because it
suppresses nearly every real upcoming handover.

## Exact candidate policy tested

The model received the same three chronological FLIR/CAM4 pairs, causal
event-sourced surgeon-held tool state, supplied functional phase, and authored
thyroidectomy exchange pattern as the prior state+visual experiment.  The new
prompt required all of the following for a `handover` prediction:

1. A distinct instance of the predicted tool must be visibly resting on the
   Mayo or instrument stand in the latest CAM4 image.
2. A tool held by the surgeon or carried by the scrub nurse is not a candidate.
3. A type that appears in the surgeon-held state remains eligible only if a
   separate physical instance of that type is visibly present on the stand.
4. If no visible available candidate can be established, return `none` or
   `uncertain` rather than infer inventory from an absent state entry.

This deliberately avoids the invalid type-level shortcut “a type in surgeon
state can never be handed over again”; duplicate instances are common.

## Direct original-frame review

The 86 scored error windows were rendered from exactly the source proxy frames
sent to the model: 78 direct false negatives, 6 wrong-tool errors, and 2
false-positive windows.  Inspection of the montage and individual early Adson
false negatives shows that the CAM4 frame commonly contains drape, hands, and
the red camera fixture rather than an unobstructed Mayo/instrument stand.
For example, both early `0704_10` Adson windows show no identifiable tray
inventory at the cutoff:

- [0704_10 f000044 source sheet](runs/state_visual_fusion_v3_visible_available_development_case_loo_0704_6_14_20260819_failure_review/sheets/001_fn_ntf_0704_10_cutoff_f000044.jpg)
- [0704_10 f000105 source sheet](runs/state_visual_fusion_v3_visible_available_development_case_loo_0704_6_14_20260819_failure_review/sheets/002_fn_ntf_0704_10_cutoff_f000105.jpg)
- [All 86 error montage](runs/state_visual_fusion_v3_visible_available_development_case_loo_0704_6_14_20260819_failure_review/all_failures_montage.jpg)

The exact error index, including target, model prediction, causal ASR for
review only, and original-frame locations, is
[failure_index.json](runs/state_visual_fusion_v3_visible_available_development_case_loo_0704_6_14_20260819_failure_review/failure_index.json).

## Interpretation and required input for a real availability gate

This is a valid test of a *visible-candidate prompt constraint*, not a verified
physical-inventory evaluation.  The `0704_6`–`0704_17` event references do not
provide initial Mayo/stand inventory or instrument-instance identities; their
evaluation masks mark physical/reuse state as unavailable.  In the benchmark,
the selected CAM4 frames also often do not expose the stand.  Therefore an
accurate hard gate cannot be derived from the present inputs alone.

To enforce the requested constraint operationally without collapsing recall,
the runtime needs either:

1. a timestamp-aligned, unobstructed Mayo/instrument-stand image plus a
   calibrated per-instance inventory detector; or
2. a causal DT inventory stream with `{instance_id, tool_id, location,
   availability}` at every cutoff.

Until one of those is available, retain the higher-recall next-tool prompt and
report physical availability separately instead of using this v3 rule as a
hard output filter.

## Reproducibility

- [Evaluator](state_visual_fusion_eval.py), variant
  `state_visual_retrieval_v3_visible_available`
- [Completed run](runs/state_visual_fusion_v3_visible_available_development_case_loo_0704_6_14_20260819/run.json)
- [Predictions](runs/state_visual_fusion_v3_visible_available_development_case_loo_0704_6_14_20260819/predictions.jsonl)
- Prompt evaluator tests: `28 passed`

This remains a within-campaign development result, not an external or clinical
generalization claim.
