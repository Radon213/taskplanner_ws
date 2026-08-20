# Normalized Mayo calibration-baseline findings (2026-08-18)

## Scope and validity

This is one independent, calibration-only baseline run for `0704_5`. It uses
the baseline prompt unchanged and applies the approved evaluation-only image
normalizer to **every** input image:

- 512 x 512 aspect-preserving letterbox;
- black BGR padding `(0, 0, 0)`;
- deterministic OpenCV JPEG quality 95;
- source/normalized byte hashes, decoded dimensions, geometry, codec flags,
  and per-image integrity checks retained in the result artifact.

The run contains 14 selected calibration requests: one t=0 inventory, 11
truth-localized morphology crops, and two early arrival pairs (16 input images
in total). It contains no frozen samples. The earlier single P2 transport probe
is not a calibration sample and is explicitly excluded from this result.

## Execution and integrity

| Check | Result |
| --- | --- |
| Selected / completed requests | 14 / 14 |
| Model POSTs | 14 |
| Fresh lifecycle batches | 14, one sample and one POST each |
| Post-batch manager + direct worker readiness | true for all 14 |
| Retries / transport errors | 0 / 0 |
| Normalized request images | 16 / 16 with passing runtime integrity checks |
| Embedded normalizer unit fixture | passed |
| Embedded normalizer pytest | 4 passed |
| Prior P2 probe in score denominator | excluded |

The evaluator withholds all metrics unless the complete selected suite finishes;
therefore these numbers are not a partial-run score.

## Calibration-only metrics

| Mode | Semantic result | Strict-contract accepted result |
| --- | --- | --- |
| Inventory (1 frame) | precision 0.625; recall 0.455; exact 0/1 | exact 0/1 |
| Crop morphology (11) | 7/11 correct (63.6%) | 2/11 accepted-correct (18.2%) |
| Arrival (2 pairs) | target recall 0/2; exact 0/2; 1 false-positive tool | accepted target recall 0/2; accepted exact 0/2 |

All 14 responses were parseable JSON. Eight otherwise parseable responses
omitted the mandatory `abstain` key (seven crops and one arrival), so semantic
recognition must remain separate from deployable contract acceptance.

## Direct visual review

I reviewed both post-inference sheets below, not just model text:

- source images:
  `artifacts/review_calibration_baseline_letterbox_fresh_source/failure_review_sheet.jpg`
- exact normalized model images:
  `artifacts/review_calibration_baseline_letterbox_fresh_normalized/failure_review_sheet.jpg`

The normalized renderer reconstructs every image from the immutable source and
refuses the review if its source/normalized manifest differs from the recorded
request manifest. Both representations produced the same 12 failed-sample
review set.

Observed failure mechanisms, stated only as calibration findings:

1. **Contract:** the baseline repeatedly emitted valid JSON without `abstain`.
2. **Target association / ring morphology:** crop 02 visually contains an
   overlapping scalpel and a ringed neighboring instrument; crop 03 was called
   a mosquito instead of the reviewed Allis; crops 10–11 confuse ringed
   retractors with forceps, including one impossible Adson prediction for a
   ringed target.
3. **Inventory:** the tray scan under-counted reviewed Allis/Army-Navy/Kocher
   instances and added unsupported Bovie/Senn-Miller instances.
4. **Arrival:** the early differences are small and partly hand-occluded. The
   baseline called the first change bipolar rather than the reviewed Bovie and
   abstained on the second reviewed bipolar arrival.

These observations motivated the proposal in
[`PROMPT_V4_PROPOSAL.md`](PROMPT_V4_PROPOSAL.md). The baseline itself was
unchanged; its observations did not authorize use of the frozen split.

The approved implementation and its same-split calibration comparison are now
recorded in [`CALIBRATION_V4_COMPARISON.md`](CALIBRATION_V4_COMPARISON.md). That
comparison likewise does not authorize frozen evaluation.

## Primary artifact

`runs/mayo-calibration-baseline-letterbox-fresh-20260818-v1_calibration_baseline/result.json`

The ignored run artifact contains raw model text and the complete per-image
normalization manifest; no base64 request body is stored.
