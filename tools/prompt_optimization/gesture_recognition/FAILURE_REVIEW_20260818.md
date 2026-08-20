# v5 calibration failure review — 2026-08-18

Scope: `0704_6`–`0704_14` development calibration, 245 samples, prompt
`gesture-causal-v5`, causal fixed right-side crop, threshold `0.95`.

This is a human-readable inspection ledger for the rendered full-CAM4 sheets
under the ignored evaluation run. It is not a replacement label set and must
not be passed to a VLM.

## What was inspected

All 71 threshold errors were opened as full-current-CAM4 contact sheets and
compared with the causal-detail image supplied to the model:

- 15 false positives
- 41 onset false negatives
- 15 interior false negatives

The original source intervals were user-authorized `assistant_video_adjudication`
records. This review therefore reports a CAM4-input alignment issue; it does
not relabel the source annotation or claim independently human adjudication.

## Direct findings

1. **All 15 false positives are visibly empty open palms.** They are nominal
   negatives only because they were sampled 12 frames before/after a source
   interval. The local boundary rule is therefore too close for a valid visual
   negative. No prompt should be made less sensitive merely to suppress these
   cases.

   - `0704_10`: f190, f551, f660
   - `0704_11`: f379, f794
   - `0704_12`: f190, f604, f800
   - `0704_13`: f161, f371, f497, f601, f652, f746
   - `0704_7`: f758

2. **All 41 onset false negatives lack a readable strict empty open palm in
   the current CAM4 frame.** The visible evidence is instead a grasp, tissue
   contact/manipulation, a transition, blur, or occlusion. The source event
   boundary may be valid in its original multi-view review context, but it is
   not a reliable current-CAM4 pose target. Do not make the pose prompt call
   those images positive.

3. **Six interior false negatives do contain a visible empty palm:**
   `0704_11` f346, `0704_12` f139/f203, `0704_14` f1097, `0704_7` f1042,
   and `0704_9` f534. The causal fixed-right-crop prompt repeatedly treated a
   palm resting lightly on skin as a negative or did not scan the whole frame.
   This is the prompt/input failure that v6 changes.

4. **The other nine interior false negatives are not reliable current-CAM4
   visual-pose targets:** `0704_10` f146/f517, `0704_12` f922/f986,
   `0704_13` f357/f598, `0704_7` f193/f985, and `0704_8` f1226. They show
   no readable palm, an object grasp, a pinch/manipulation, or insufficient
   visibility at that exact frame.

5. **One output-contract failure** (`0704_12` f433) was semantically a
   negative but exceeded the 24-word evidence cap. v6 requests at most
   12 evidence words while retaining the same JSON keys.

## Changes justified by this review

- v6 moves from a prior/current fixed right-side crop to the current full CAM4
  frame, so the model must scan all hands before returning negative.
- v6 explicitly accepts an empty relaxed palm that is near or lightly resting
  on skin/drape; location alone is not a negative cue.
- v6 preserves `not_open_receive` for actual grips, pinches, tissue
  manipulation, unreadable dorsal/edge views, and absent hands.
- The original event-alignment report remains intact for provenance. Future
  pure-CAM4 pose accuracy requires a separately frozen visual re-adjudication
  and explicitly verified negatives; it cannot be inferred from a better
  source-event score alone.
