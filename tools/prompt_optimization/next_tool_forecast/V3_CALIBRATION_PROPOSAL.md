# Calibration-only v3 proposal: timestamped ASR, not a replacement lock

The immutable plain-ASR calibration evaluation lock remains `baseline_v0` at
threshold `0.90`. This proposal does not modify it, does not read challenge or
holdout, and is not a production prompt change.

## Exact input-contract change

Historical plain input serializes ASR as a text list:

```json
{"asr":["Adson","Adson 하나 더."]}
```

The new timestamped calibration input serializes only the same causal public
text plus a negative, relative availability offset:

```json
{"asr":[
  {"text":"Adson","available_offset_sec":-1.238},
  {"text":"Adson 하나 더.","available_offset_sec":-0.449}
]}
```

`available_offset_sec` is computed as `available_sec - cutoff_sec` while the
manifest is built. The model sees neither source `available_sec`, case ID,
absolute cutoff time, target, event ID, nor annotation metadata. Values nearer
zero are newer and every value is in `[-8, 0]`.

## Exact v3 decision change

The prior v3 hypothesis required both a fresh request and a CAM4 receiving
cue. The revised rule is:

> Treat an explicit ASR tool request as forecast evidence when it is the
> latest relevant, unfulfilled explicit request close to the causal cutoff. A
> fresh unfulfilled request may support a handover even if a CAM4 receiving cue
> is not yet visible. Use CAM4 to check whether that request already appears
> fulfilled; an arrival cue can corroborate but is not required.

It also explicitly rejects a current/fulfilled tool, stale/history ASR,
current FLIR activity, generic hand motion, or a tool merely held/visible as a
future handover. If no concrete new-transfer evidence remains, output `none`.

## Output contracts

`optimized_v3` remains deployable-shape JSON only:

```json
{"decision":"handover","tool_id":"adson_forceps","confidence":0.82,"uncertainty":0.18}
```

`optimized_v3_diagnostic` is calibration-only and adds exactly one field:

```json
{"decision":"handover","tool_id":"adson_forceps","confidence":0.82,"uncertainty":0.18,"evidence_type":"fresh_asr_visual"}
```

Allowed diagnostic evidence types are `fresh_asr_visual`, `visual_only`, and
`none_or_ambiguous`. `fresh_asr_visual` now means a fresh unfulfilled ASR item
after visual fulfillment checking; a visible arrival is optional rather than a
hard requirement.

## Required comparison

Because the model-visible input changed, use a same-input control:

1. `baseline_v0_timestamped_asr` on the timestamped calibration manifest.
2. `optimized_v3` on that exact manifest, with identical model/generation and
   frozen selected IDs.
3. Optionally run `optimized_v3_diagnostic` only to inspect evidence-type
   behavior; do not compare its five-key contract as deployable accuracy.

All three are hard-blocked to `development_calibration` in normal use. The
only exception is a separately generated `failed_candidate_diagnostic` lock:
after the strict v3 calibration candidate has explicitly failed the
non-degeneracy gate, it may pin the same strict prompt/config and authorize one
batch-one/no-retry run on each predeclared timestamped challenge and holdout
manifest. That exception is non-deployable, cannot revise the existing lock,
and is reported only as a failed-candidate diagnostic. The evaluator records
`input_contract`, and the comparator rejects a plain-ASR versus
timestamped-ASR comparison.

## Why this remains a narrow hypothesis

The accompanying offline availability audit finds a target-name alias in
causal ASR for only 4 of 44 positives, all Adson. Thus the timestamped prompt
is expected to test recency/fulfillment handling for a small supported slice;
it cannot recover Bovie, Bipolar, or Yankauer identities when their causal
inputs contain no target-name ASR signal.
