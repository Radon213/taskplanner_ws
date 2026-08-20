# State-pattern + visual fusion evaluation — 2026-08-19

## Verdict

Adding the thyroidectomy phase/current-tool state and five case-disjoint similar-state examples made the VLM substantially better than the earlier visual-prior prompt, but it did **not** improve on the deterministic state-retrieval baseline.  The useful result is therefore a data/architecture finding, not a deployable VLM win: in these 2–8 s forecasts, the state prior carries most of the predictive signal and the VLM's visual override is currently harmful more often than helpful.

This is an evaluation-only, **oracle-state visual fusion** experiment.  Phase and current-tool-state are offline event-derived annotations; they are not runtime perception outputs.  It is neither a pure-VLM result nor a clinical/generalization claim.

## Frozen input contract

- Model: `qwen3.6-35b-a3b`, temperature 0, seed 0, batch size 1, no retry.
- Images: three chronological FLIR/CAM4 pairs ending at the causal cutoff.
- Supplied state: provisional functional phase, event-sourced last-known surgeon tool state, authored thyroidectomy-demo exchange paths, and five anonymous nearest examples from **other cases only**.
- Omitted: ASR, case ID, absolute time, event ID, target label, and annotation provenance.
- Retrieval used the full 0704_6–14 development corpus with whole-case leave-one-case-out exclusion.  The phase support was P03=70, P04=10, P05=21, P06=20 (121 windows total).

## Development: 0704_6–14, case leave-one-case-out

| Condition | Correct / total | Accuracy | Exact handover recall | `none` specificity |
| --- | ---: | ---: | ---: | ---: |
| Visual/state prior v1 | 46 / 121 | 38.0% | 20.7% | 82.4% |
| Visual/state retrieval v2 | 74 / 121 | 61.2% | 54 / 87 = 62.1% | 20 / 34 = 58.8% |
| Deterministic retrieval only | 86 / 121 | 71.1% | 67 / 87 = 77.0% | 19 / 34 = 55.9% |

The v2 policy explicitly told the model to use the leading retrieval vote as the default and to change it only for unmistakable contradictory visual evidence.  It nevertheless changed the retrieval decision in 24 windows: 15 correct retrieval decisions became wrong, 3 wrong retrieval decisions became correct, and 6 remained wrong.  Thus the visual override lost 12 net windows.

The v2 per-class result was strong only for `adson_forceps` (21/22) and `bipolar_forceps` (18/20).  It missed all seven `yankauer_suction` and all five `army_navy_retractor` targets; `bovie` was 13/24 and `mosquito_forceps` 2/8.  These classes must not be represented as reliable from this evidence.

## Direct original-frame review

I inspected every scored error at the exact original FLIR/CAM4 frames sent to the model: 47 development errors (13 direct false negatives, 14 false positives on `none`, 20 wrong-tool errors) and 26 post-hoc errors (11 direct false negatives, 6 false positives on `none`, 9 wrong-tool errors).

The recurring causes were:

1. **Future tool is not yet visible.** Most target transfers begin about 3–4 s after the cutoff, while the three pre-cutoff frames show ongoing dissection or a hand already occupied with another tool.  There is no unambiguous visual identity cue for the future instrument.
2. **Fine tool-shape ambiguity.** Bovie was often changed to bipolar, and mosquito to bipolar; Army-Navy, Yankauer, and Kocher/Allis were not reliably distinguishable before their approach is visible.
3. **Active work mistaken for a new transfer.** In true-`none` windows, a moving/held instrument or a hand near the field caused unsupported handover predictions.
4. **The model does not reliably obey the retrieval-default instruction.** This explains why the visual fusion underperformed a deterministic use of exactly the same case-disjoint state prior.

Review bundles (each sheet shows ground truth and VLM output plus the exact three FLIR/CAM4 pairs):

- Development: `runs/state_visual_fusion_v2_development_case_loo_0704_6_14_20260819_failure_review/`
- Post-hoc 15–17: `runs/state_visual_fusion_v2_posthoc_0704_15_17_20260819_failure_review/`

## Post-hoc diagnostic: 0704_15–17

This partition had already been inspected in earlier work, so it was evaluated once only after the v2 configuration and IDs were locked in `runs/state_visual_fusion_v2_posthoc_selection_20260819.json`.  It is a **post-hoc diagnostic**, not an independent final holdout.

| Condition | Correct / total | Accuracy | Exact handover recall | `none` specificity |
| --- | ---: | ---: | ---: | ---: |
| Visual/state retrieval v2 | 28 / 54 | 51.9% | 22 / 42 = 52.4% | 6 / 12 = 50.0% |
| Deterministic retrieval only | 30 / 54 | 55.6% | 24 / 42 = 57.1% | 6 / 12 = 50.0% |

All 54 responses passed the strict JSON contract, and all 54 fresh-worker lifecycle requests completed without transport failure.  Again, the VLM did not improve the state retrieval: it changed eight decisions, with three degradations, one recovery, and four unchanged errors.

## Recommended next architecture

Use a case-disjoint state-transition model as the forecast decision path, and treat VLM vision as a separately validated **non-automatic evidence/guard channel**.  Do not let the VLM overwrite the state forecast until it can show positive net benefit on a predeclared case-disjoint test, particularly for the Bovie/bipolar/mosquito and retractor/suction distinctions.  A runtime system would also need phase and current-tool state produced by real sensors/perception rather than these offline event annotations.
