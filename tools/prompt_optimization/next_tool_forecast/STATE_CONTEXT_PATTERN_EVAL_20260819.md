# Thyroidectomy-demo procedure-pattern state ablation

## What was tested

This is an evaluation-only **oracle-state** condition, requested to test whether the thyroidectomy-demo exchange pattern helps even without image or ASR input. The model received no image and no ASR. Its text input contained:

- a supplied provisional procedure phase (`P03`--`P06`);
- a causally clipped, event-sourced last-known surgeon-owned inventory and incoming-tool history;
- the authored thyroidectomy-demo exchange paths and phase-conditioned transitions; and, in v2,
- a cross-case transition distribution trained only from 0704_6--14 labels.

The phase source is explicitly `context_only_not_ground_truth`, and the tool inventory is a replay of offline confirmed transfer events rather than a visual observation. This measures the stated input condition, not pure VLM performance or a deployable runtime configuration.

## Fixed evaluation protocol

| Partition | Cases | N | Pattern training boundary |
| --- | --- | ---: | --- |
| Calibration | 0704_6--14 early temporal partition | 65 | For each example, its entire case was excluded from the learned transition table. |
| Final holdout | 0704_15--17 | 54 | Pattern table used only all 65 calibration examples; no 15--17 label was sent to the model. |

Every live request used a fresh manager/worker lifecycle, batch size 1, no retry, and the shared NInfer lock. The final run completed 54/54 requests, with 54/54 valid four-key JSON responses and no transport errors.

## Results

| Condition | Partition | Exact top-1 accuracy | Exact tool recall | None specificity |
| --- | --- | ---: | ---: | ---: |
| v1: authored protocol/state only | Calibration | 17/65 (26.2%) | 17/44 (38.6%) | 0/21 (0.0%) |
| v2: authored protocol + cross-case transition table | Calibration | 39/65 (60.0%) | 22/44 (50.0%) | 17/21 (81.0%) |
| Deterministic argmax of the same v2 table | Calibration | 43/65 (66.2%) | 30/44 (68.2%) | 13/21 (61.9%) |
| v2, frozen final holdout | 0704_15--17 | 17/54 (31.5%) | 5/42 (11.9%) | 12/12 (100.0%) |
| Deterministic argmax of the same v2 table | 0704_15--17 | 19/54 (35.2%) | 8/42 (19.0%) | 11/12 (91.7%) |

The earlier image+ASR-only failed-candidate diagnostic scored 8/54 (14.8%) on this same final partition. That is a different input condition, so 31.5% is an ablation comparison rather than an additive model improvement claim.

## Direct failure review

I rendered and inspected all 37 v2 scoring errors on their original three-pair FLIR/CAM4 sheets. The model did not receive those frames in this state-only test.

- 32 were false negatives (`none` for a later transfer); 5 were wrong-tool predictions.
- 23/54 holdout states had no matching phase/history key in the 65-row calibration table and fell back to the global distribution. Its top outcome was `none` for all 23; 16 were actual positive transfers and none was correct as a positive-tool prediction.
- Calibration contained only `P03` examples. Final holdout included 7 each from `P04`, `P05`, and `P06`; therefore 21/54 final states had no phase-matched learned support.
- 12/42 positive final transfers were tools never represented as a calibration target: Army-Navy 6, Mosquito 3, Kocher/thyroid retractor 2, and Allis 1. v2 got none of those 12 exactly right.
- The raw image sheets show why the state-only condition is fundamentally incomplete: hand/request posture, scrub movement, tray interaction, and operative-field change are often present at the cutoff but cannot be reconstructed from only a phase and past tool names.

The model followed the state-table argmax after thresholding on 47/54 examples. On the remaining seven it emitted a low-confidence handover (confidence 0.40--0.57), which was scored as `none` at the frozen 0.65 threshold. Thus the main limitation is not JSON/worker reliability; it is missing phase/tool-pattern coverage and ambiguity of the state representation.

## Implication for the next iteration

Do not treat v2 as deployable. The defensible next development step is to build a separate, case-disjoint state-pattern corpus that contains `P03`--`P06` and all target tools, then fuse the structured prior with visual handover evidence. Reusing 0704_15--17 to select another prompt would make this holdout post-hoc rather than independent.

Artifacts:

- Calibration v1: `runs/state_context_calibration_0704_6_14_procedure_pattern_v1_20260819/run.json`
- Calibration v2: `runs/state_context_calibration_0704_6_14_procedure_pattern_v2_20260819/run.json`
- Frozen selection: `runs/state_context_selection_procedure_pattern_v2_20260819.json`
- Final v2: `runs/state_context_holdout_0704_15_17_procedure_pattern_v2_20260819/run.json`
- Direct visual failure sheets: `runs/state_context_holdout_0704_15_17_procedure_pattern_v2_failure_review_20260819/`
