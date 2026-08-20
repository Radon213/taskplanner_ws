# Proposed Mayo prompt v4 — calibration only

Status: **approved, implemented as the evaluation-only `optimized_v4` variant,
and evaluated once on the calibration split; it is now locked as the
evaluation-only frozen temporal-arrival variant in
[`FROZEN_V4_SELECTION.json`](FROZEN_V4_SELECTION.json).** The text
below remains the immutable proposal record. Its independent normalized
calibration run uses the same 14 calibration samples only; it must not be tuned
on or run against the frozen arrival challenge. See
[`CALIBRATION_V4_COMPARISON.md`](CALIBRATION_V4_COMPARISON.md) for the result.

## Evidence-to-change mapping

| Calibration observation | Proposed change | Intended safeguard |
| --- | --- | --- |
| 8 parseable outputs omitted `abstain` | Explicit required-key self-check | Raises operational acceptance without changing semantic labels. |
| Ringed target predicted as Adson; Allis/Kocher confused with mosquito | Ring/jaw/working-end decision rules plus abstain | Prevents a tweezer label where visible rings rule it out. |
| Crop 02 includes overlapping neighboring morphology | Explicit target-association rule | Avoids classifying a neighbor merely crossing the magenta crop. |
| Inventory added Bovie/Senn and missed parallel tools | Ordered strip scan and stronger positive morphology requirements | Reduces unsupported additions and count collapse. |
| Early arrival changes small/hand-occluded | Difference-first settled-object rule | Separates a newly resting object from a carried/fully hidden one. |

## Proposed unified diff

The following text is appended after the baseline visual-pixel policy and
before its mode-specific output contract in `mayo_prompt_eval.py` under
`optimized_v4`.

```diff
--- mayo-recognition-v3 baseline
+++ mayo-recognition-v4 proposed calibration prompt
@@
+Contract self-check before emitting: return every key shown in the mode's JSON
+contract, including `abstain`, and no other keys. If you name one or more
+tools, still include `"abstain":false`; if discriminative pixels are absent,
+use the contract's empty tool field/list and `"abstain":true`.
+
+For an outlined crop, associate the target with the instrument whose central
+body and working end are inside the magenta rectangle. Do not classify a
+neighbor merely because its shaft, ring, or cable crosses the rectangle. If
+two distinct instruments occupy the rectangle, or only ambiguous rings/shaft
+are visible, abstain.
+
+A target with visible circular finger rings cannot be Adson or bipolar forceps:
+those are tweezer-style instruments without finger rings. Distinguish a small
+fine mosquito clamp from Allis by visible jaw morphology: call Allis only when
+broad/serrated grasping jaws support it; otherwise abstain. A ring-handled
+Kocher/Middeldorpf retractor needs a substantial retractor working end, not
+just clamp-like rings; if that end is not visible, abstain rather than calling
+a forceps class.
+
+For inventory, scan the blue Mayo surface in fixed strips and count every
+separately visible handle, shaft, jaw, or retractor working end once. Do not
+collapse touching parallel tools, and do not count a cable as an instrument. A
+Bovie requires the recognizable insulated pencil/probe body resting on the
+cloth, not a loose cable, generic white rod, or unrelated black device. A
+Senn-Miller requires a visible narrow rake or blade end.
+
+For BEFORE/AFTER arrivals, first compare the persistent tray layout, then look
+for a distinct object newly supported by the Mayo cloth. A tool may be partly
+covered by a hand if its own body is visibly resting on the cloth; do not label
+an object that is entirely hand-held or has no visible discriminative body.
```

## Evaluation rule if approved

1. Run the new independent **calibration-only** normalized evaluation with the
   same 14 samples, batch size one, no retry, and score-only-if-complete gate.
2. Review source and normalized failure sheets after that run.
3. Only after selection is frozen may the pre-registered five arrival pairs be
   evaluated once; no calibration observation may then alter their prompt.
