# Mayo runtime probe findings (2026-08-18)

This is a label-free transport diagnostic, not an accuracy experiment. It
exists because the first fresh-worker calibration run lost the native vision
worker while processing one morphology crop. No reviewed label, target score,
or frozen frame is included in any probe request or in this report.

## Common safeguards

- One POST at most per probe, under `/tmp/taskplanner-ninfer-eval.lock`.
- Fresh `unload -> load` lifecycle before each POST, followed by manager and
  direct-worker readiness checks.
- Same model, task text, system prompt, request context, and closed vocabulary
  throughout. The original crop request hash must equal the input hash from
  the halted calibration run before a probe can send.
- No retry, calibration continuation, frozen evaluation, or metric calculation.

## Observations

| Probe | Image handling | Outcome | Runtime observation |
| --- | --- | --- | --- |
| P0 | Original JPEG, 267 x 154 | Halted | One POST returned 502; direct worker reset/refused and no worker process remained. |
| P1 | Same 267 x 154 pixels, deterministic JPEG Q95 re-encode, no crop/pad/resize | Halted | The original-request hash and non-image request hash checks passed; the same one-POST 502/worker-loss pattern recurred. |
| P2 | Aspect-preserving 512 x 512 JPEG letterbox with black padding | Completed | One POST completed; post-run manager and direct worker remained ready and the same worker PID was present before and after. |

The P2 artifact records source and transformed byte hashes, dimensions,
codec flags, scale, and exact padding. Its semantic response is retained only
in the ignored run artifact and is deliberately not scored here.

## Interpretation and gate

P0 and P1 make a metadata-only or JPEG-container-only explanation unlikely:
the same-size Q95 re-encode still loses the worker. P2 changes geometry and
pixel sampling together, so it establishes only a bounded runtime workaround
for this one request—not a causal explanation, recognition accuracy, or a
production preprocessing change.

The direct visual comparison for P2 shows the selected crop remains
aspect-preserved within the outlined ROI, with only black top/bottom padding.
All local evaluator and preprocessor tests pass (`24 passed`).

The runtime owner reviewed the P2 artifact and approved this exact transform
for one new independent calibration baseline only. That completed run is
documented in `CALIBRATION_NORMALIZED_BASELINE_FINDINGS.md`. This does not
authorize frozen samples or a production preprocessing change: a reviewed
calibration-only prompt selection still has to precede any frozen challenge.

## Artifact locations

- P0: `runs/mayo-crop03-repro-probe-fresh-20260818-v1_mayo_repro_probe/probe.json`
- P1: `runs/mayo-crop03-p1-reencode-fresh-20260818-v1_mayo_repro_probe/probe.json`
- P2: `runs/mayo-crop03-p2-letterbox-fresh-20260818-v1_mayo_repro_probe/probe.json`
- P2 visual comparison:
  `runs/mayo-crop03-p2-letterbox-fresh-20260818-v1_mayo_repro_probe/pixel_comparison.jpg`

The `runs/` tree is git-ignored because it can contain raw model text and
review images. The tracked scripts regenerate the evidence without persisting
base64 request bodies.
