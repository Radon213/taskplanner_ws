# Mayo calibration findings (not a challenge score)

## Evidence used to change the prompt

Both probes used the same three **calibration-only** samples from the reviewed
0704_5 reference: the t=0 full Mayo inventory, a truth-localized scalpel crop,
and a truth-localized Allis crop.  They each completed three locked NInfer
requests with a loaded vision catalog before and after the batch.

| Calibration measure | Baseline | `optimized` v1 |
|---|---:|---:|
| Inventory JSON valid / contract-valid | 1 / 0 | 1 / 1 |
| Inventory instance precision | 0.667 (6 / 9) | 0.875 (7 / 8) |
| Inventory instance recall | 0.545 (6 / 11) | 0.636 (7 / 11) |
| Crop label accuracy | 1 / 2 | 1 / 2 |
| Crop contract-valid | 1 / 2 | 2 / 2 |
| Transport errors | 0 | 0 |

Source artifacts:

- `runs/mayo-calibration-baseline-probe-v3_calibration_baseline/result.json`
- `runs/mayo-calibration-optimized-probe-v3_calibration_optimized/result.json`
- `artifacts/review_baseline_probe_v3/review_manifest.json`
- `artifacts/review_baseline_probe_v4_failure_sheet/failure_review_sheet.jpg`

## Direct, post-inference frame review

The baseline full-Mayo response included one `bovie` even though the visible
evidence was an elongated cable/line rather than a clearly visible resting
electrosurgical handpiece.  It also over-counted `mosquito_forceps` as three,
missed `kocher_retractor`, and under-counted several duplicated tools.

For the outlined Allis crop, both baseline and `optimized` v1 returned
`adson_forceps`; the original crop visibly contains a ring-handled target,
which conflicts with the tweezer-style / no-ring morphology of Adson forceps.
This was a model error, not a contract parser artifact.

## Prompt change frozen before the challenge

`optimized_v2` adds only the following calibration-derived visual rules:

1. Scan a Mayo inventory in ordered strips and count independently visible
   parallel handles, shafts, or jaws; do not merge touching tools.
2. Require a recognizable Bovie pencil/probe body resting on the blue cloth;
   a cable alone is not a Bovie.
3. Do not assign `adson_forceps` or `bipolar_forceps` to a target with circular
   finger rings.  Assign `allis_forceps` only when clamp/jaw morphology also
   supports it; otherwise abstain.

No frozen arrival frame, label, or result was inspected while creating these
rules.  The pre-registered late temporal challenge remains five untouched
arrival pairs.  Its score must be reported separately and cannot be combined
with the calibration numbers above.
