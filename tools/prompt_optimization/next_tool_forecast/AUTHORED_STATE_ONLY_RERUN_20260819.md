# Authored-state-only next-tool rerun — 2026-08-19

## Question tested

Can Qwen 3.6 35B A3B predict the next tool handover using only information we
supply, with no FLIR, CAM4, ASR, visual availability inference, or learned
cross-case transition distribution?

The fixed model-visible input contained only:

1. the causally clipped current functional phase;
2. event-sourced, last-known surgeon-held tool counts and recent incoming
   tools; and
3. the authored open-thyroidectomy exchange paths and phase-conditioned
   transitions.

The model received no image, no audio/transcript, no case ID, no timestamp,
no frame ID, no target label, and no calibration transition counts.  This is
an evaluation-only state-information ablation: phase and tool state originate
from offline reviewed annotations, so it is not an online VLM or deployment
measurement.

## Fixed prompt policy

The prompt explicitly told the model that no visual/audio confirmation would
arrive and asked it to choose the phase-conditioned next tool or the next
coherent tool in an authored exchange path.  It was told to return `none` only
when the supplied state made a near-term additional handover unsupported.
The prompt was frozen before either run and was unchanged between calibration
and challenge.

## Results

| partition | examples | overall exact accuracy | exact handover recall | true-`none` specificity | error pattern |
| --- | ---: | ---: | ---: | ---: | --- |
| `0704_6`–`0704_14` calibration | 65 | 15/65 (23.1%) | 15/44 (34.1%) | 0/21 (0%) | 29 wrong-tool, 21 false handovers |
| same cases, temporally embargoed challenge | 56 | 23/56 (41.1%) | 23/43 (53.5%) | 0/13 (0%) | 20 wrong-tool, 13 false handovers |

All 121 responses satisfied the four-key JSON contract; no transport failure
or retry occurred.

The model predicted a handover for **every** example in both partitions.  In
calibration it was particularly biased to `bovie`; in challenge it was biased
to `adson_forceps` and `bipolar_forceps`.  It therefore recognized some
protocol regularity, but could not identify whether a new handover would occur
in the next 2–8 seconds.

## Direct review of errors

Rendered source-frame bundles were inspected after scoring only; those images
were never supplied to the model in this condition.

- [Calibration montage](runs/state_context_v3_authored_state_only_calibration_0704_6_14_20260819_failure_review/all_failures_montage.jpg)
- [Calibration error index](runs/state_context_v3_authored_state_only_calibration_0704_6_14_20260819_failure_review/failure_index.json)
- [Challenge montage](runs/state_context_v3_authored_state_only_challenge_0704_6_14_20260819_failure_review/all_failures_montage.jpg)
- [Challenge error index](runs/state_context_v3_authored_state_only_challenge_0704_6_14_20260819_failure_review/failure_index.json)

The principal failure is not image recognition: after removing images and ASR,
the state contains no timing/intent signal that separates a clean-negative
window from an imminent transfer.  The authored sequence describes the likely
tool class but not when that exchange happens.  For that reason, this condition
is rejected as a standalone predictor and no final-holdout run was performed.

## Reproducibility

- [Evaluator](state_context_eval.py), variant
  `procedure_pattern_v3_authored_state_only`
- [Calibration run](runs/state_context_v3_authored_state_only_calibration_0704_6_14_20260819/run.json)
- [Challenge run](runs/state_context_v3_authored_state_only_challenge_0704_6_14_20260819/run.json)
- Prompt/request boundary and evaluator tests: `29 passed`

The useful implication is to retain a separately observable trigger for
“handover now” (e.g., reviewed open-hand request, ASR request, or a reliable
state transition) and use the supplied surgical phase/pattern only to rank the
tool after that trigger is present.
