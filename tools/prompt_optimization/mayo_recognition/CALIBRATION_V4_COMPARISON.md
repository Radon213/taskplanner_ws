# Mayo normalized calibration: v3 baseline vs v4 (2026-08-18)

## Valid comparison boundary

Both artifacts use the same 14 `0704_5` calibration requests, the same
evaluation-only 512 x 512 black-letterbox/Q95 normalization, the same model,
and the same one-POST fresh-worker lifecycle. Each completed all 14 requests
without retry or transport error, and each records 16 source/normalized image
manifests with passing integrity checks. Neither contains a frozen sample or
the prior P2 transport probe.

This comparison is calibration evidence only, not a held-out estimate. The
runtime owner subsequently selected and locked v4 only for the pre-registered
within-case temporal-arrival frozen challenge; the lock explicitly retains the
crop semantic regression below and makes no cross-case claim.

| Run | Prompt metadata | Result artifact |
| --- | --- | --- |
| Baseline | `mayo-recognition-v3`, `baseline` | `runs/mayo-calibration-baseline-letterbox-fresh-20260818-v1_calibration_baseline/result.json` |
| v4 | `mayo-recognition-v4`, `optimized_v4` | `runs/mayo-calibration-optimized-v4-letterbox-fresh-20260818-v1_calibration_optimized_v4/result.json` |

## Semantic versus strict-contract metrics

| Mode / metric | v3 baseline | v4 | Change |
| --- | ---:|---:|---:|
| Inventory semantic precision | 0.625 | 0.750 | +0.125 |
| Inventory semantic recall | 0.455 | 0.545 | +0.091 |
| Inventory exact / strict accepted exact | 0/1 / 0/1 | 0/1 / 0/1 | unchanged |
| Crop semantic correct | 7/11 (63.6%) | 5/11 (45.5%) | -2/11 |
| Crop strict accepted-correct | 2/11 (18.2%) | 5/11 (45.5%) | +3/11 |
| Arrival semantic target recall / exact | 0/2 / 0/2 | 1/2 / 1/2 | +1/2 |
| Arrival strict accepted recall / exact | 0/2 / 0/2 | 1/2 / 1/2 | +1/2 |
| Arrival false-positive tools | 1 | 0 | -1 |
| Valid strict contracts, all modes | 6/14 | 14/14 | +8 |
| Failure-sheet records | 12 | 8 | -4 |

The v4 contract self-check fixed the eight v3 `abstain` omissions. It did not
turn every weak visual case into an abstention, so contract validity should not
be mistaken for morphology accuracy.

## Direct source/normalized review

I reviewed both v4 sheets after inference:

- source sheet:
  `artifacts/review_calibration_optimized_v4_letterbox_fresh_source/failure_review_sheet.jpg`
- normalized model-input sheet:
  `artifacts/review_calibration_optimized_v4_letterbox_fresh_normalized/failure_review_sheet.jpg`

The normalized renderer reproduced the recorded per-image manifest before
rendering. The relevant visual findings are:

1. **Intended safe abstention:** arrival `E0012` is a small, partly
   hand-occluded difference. v4 returned an empty arrival list with
   `abstain:true`, rather than forcing a class from insufficient visible
   morphology. It remains a recall miss against the evaluation reference.
2. **Safety instruction not reliably followed:** crop 02 still selected the
   overlapping scalpel rather than abstaining on the ambiguous crop; crop 03
   still called the ringed target mosquito; crops 10–11 still called weak
   ringed retractor views mosquito rather than abstaining. v4 removed the
   impossible Adson-with-rings error in crop 11 but did not solve the class.
3. **New semantic regressions:** crop 07 changed from correct bipolar to
   Bovie, and crop 09 changed from correct Army-Navy to Bovie. Both are strict
   JSON now, but are morphology regressions; the stronger Bovie wording did not
   eliminate those false positives.
4. **Positive arrival gain:** v4 identified the first early arrival (`E0008`)
   as Bovie with a valid contract, replacing v3's bipolar false positive.
5. **Inventory gain remains incomplete:** v4 improved instance precision and
   recall, but still adds Bovie/Senn-Miller and misses reviewed instances, so
   exact inventory remains zero.

## Selection implication

There is no automatic calibration winner: v4 substantially improves strict
contract behavior and the early arrival task, but reduces crop semantic
accuracy and does not consistently abstain on visually weak/overlapping crop
cases. The runtime owner selected `optimized_v4` for the temporal-arrival
objective only: its 0/2 to 1/2 arrival gain and FP 1 to 0 reduction outweighed
the documented crop regression. The resulting immutable frozen lock prohibits
additional prompt, normalizer, or threshold edits after frozen review.
