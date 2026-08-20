# Next-tool forecast prompt experiment

This directory is an offline, task-specific experiment for NInfer
`qwen3.6-35b-a3b`. It does not change `real_vlm.py`, the ROS schema, the
digital twin, the behavior tree, or robot authority.

## Fixed task definition

At the final image (the causal cutoff), predict the **first confirmed new
`scrub_nurse -> surgeon` physical handover 2–8 seconds later**. The output is
strictly one object:

```json
{"decision":"handover","tool_id":"adson_forceps","confidence":0.82,"uncertainty":0.18}
```

- `decision`: `handover`, `none`, or `uncertain`.
- `tool_id`: an exact canonical observable-tool ID only for `handover`; empty
  otherwise.
- A tool already being held, merely visible, or named in a current request is
  not automatically the next additional handover.
- The unresolved `retractor_bundle_unresolved` observation is excluded from
  scoreable classes rather than converted into a guessed tool.

`optimized_v1` adds three accuracy-oriented constraints over `baseline_v0`:
chronological FLIR/CAM4 comparison, separation of current request/tool from a
future handover, and an explicit `none` decision for unsupported forecasts.
`optimized_v2_prior` keeps those constraints and adds a deliberately weak,
case-agnostic thyroidectomy functional prior as a tie-breaker; it contains no
case ID, timestamp, or exact handover sequence. Compare it on development data
before selecting either optimized variant.
`optimized_v3` is a calibration-only strict four-key hypothesis from direct
failure review and an ASR-coverage audit. It uses a different, causal input
contract: each public ASR item carries only a negative relative
`available_offset_sec` (never a case ID or absolute time). A fresh, explicit,
not-yet-fulfilled tool request can support a forecast by itself; CAM4 is used
to reject a request that already appears fulfilled and may corroborate, but a
receiving cue is not mandatory. Current/fulfilled tools and stale/history ASR
must not be copied forward. When no concrete new-transfer evidence exists, it
returns `none`. Its companion
`optimized_v3_diagnostic` adds `evidence_type` (`fresh_asr_visual`,
`visual_only`, or `none_or_ambiguous`) for calibration error analysis only;
the deployable variant remains exactly the four-key schema above.
The exact prompts and parser are in [prompt_contract.py](prompt_contract.py).
The exact timestamped-input/v3 proposal and comparison boundary are in
[V3_CALIBRATION_PROPOSAL.md](V3_CALIBRATION_PROPOSAL.md).

## Information boundary

`build_eval_manifest.py` creates separate files:

- `inputs.jsonl`: causal image-frame bindings and causally available public ASR
  only.
- `labels.jsonl`: future transfer target, event ID, regime, and score metadata.

The NInfer request builder consumes only `inputs.jsonl`; it never receives the
label file, target event ID, case ID, absolute timestamp, annotation provenance,
or GT. Unit tests assert this boundary. Ground truth is therefore used for
offline sampling and scoring only.

Each example has three chronological FLIR/CAM4 frame pairs ending at the cutoff
and public ASR whose `available_sec` is at or before it. A visual gap invalidates
the sample. Proxies are checked against the canonical CAM4 timeline; optional
full SHA-256 verification is available for a strict run.

## Coverage and leakage-free evaluation policy

The first step is an explicit audit of `0704_5`–`0704_17`, rather than assuming
that every recording is a usable negative source. `0704_5` has legacy event
files but no complete evaluation-only reference, causally bounded public ASR,
or paired proxy manifest. It is therefore excluded from both positives and
negatives. Only `0704_6`–`0704_17` can enter this benchmark.

The 0704 campaign is development data, not an external-generalization dataset.
The split policy is:

1. `0704_6`–`0704_14` are split at each case's 50% temporal boundary. A
   calibration cutoff plus its full 8-second target window must end at least
   4 seconds before the boundary. A challenge cutoff minus its 6-second image
   lookback must begin at least 4 seconds after it. The intervening windows are
   unscored. Thus calibration labels cannot overlap challenge image evidence.
2. Baseline, v1, and v2 use the same frozen challenge IDs. Their comparison
   report rejects a run if any source hash, selected IDs, model, generation
   setting, or threshold differs. After calibration locks a final candidate,
   v1/v2 challenge rows are explicitly **exploratory only** and cannot replace
   that candidate for the final holdout.
3. Challenge failures may be used for qualitative v3 hypothesis generation,
   but then the challenge is diagnostic rather than a final claim. The only
   final confirmation remains the case-disjoint `0704_15`–`0704_17` holdout.
   The timestamped-ASR v3 variants and their
   `baseline_v0_timestamped_asr` control are hard-blocked from challenge and
   holdout in normal use; they can be run only on `development_calibration`
   and cannot alter an already immutable calibration lock. A separate,
   hash-pinned `failed_candidate_diagnostic` exception may authorize exactly
   one batch-one/no-retry evaluation of an already failed strict v3 candidate
   on predeclared challenge and holdout manifests. It is explicitly
   non-deployable, cannot reselect a prompt, and must retain that status in
   every result. Because their model-visible input contract differs, compare
   v3 only with that timestamped-v0 control, never claim a direct strict
   comparison with historical plain-ASR v0.
4. Report overall results plus `anticipatory`, `request_context`,
   `voice_context`, and `clean_negative` subsets. The primary prompt-quality
   result is the anticipatory subset.

Overall accuracy is never enough to call a candidate useful: a `none`-heavy
output can score well while missing almost all tool handovers. The calibration
selector therefore records a separate primary non-degeneracy gate: exact-tool
recall ≥ 0.10, exact-tool F1 ≥ 0.10, and actual-`none` specificity ≥ 0.90 must
all pass before a candidate may be described as suitable. This is a minimum
research screen, not a clinical deployment claim. An immutable historical
accuracy lock remains an evaluation candidate even if it fails this separate
suitability status.

All generated manifests and raw model responses must live beneath this
directory's ignored `runs/` folder. Both commands reject an output path outside
that location.

## Commands

Audit actual source coverage and bindings first:

```bash
python3 tools/prompt_optimization/next_tool_forecast/audit_0704_coverage.py \
  --verify-proxy-sha256 \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/coverage_0704
```

Build the three leakage-controlled manifests:

```bash
python3 tools/prompt_optimization/next_tool_forecast/build_eval_manifest.py \
  --partition development_calibration --verify-proxy-sha256 \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/calibration_manifest

python3 tools/prompt_optimization/next_tool_forecast/build_eval_manifest.py \
  --partition development_challenge --verify-proxy-sha256 \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_manifest

python3 tools/prompt_optimization/next_tool_forecast/build_eval_manifest.py \
  --partition final_holdout --verify-proxy-sha256 \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/final_holdout_manifest
```

Build a separate calibration-only timestamped-ASR manifest for the v3/control
experiment; it has the same frozen IDs and labels but a different input hash:

```bash
python3 tools/prompt_optimization/next_tool_forecast/build_eval_manifest.py \
  --partition development_calibration --asr-input-format timestamped_relative \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/calibration_timestamped_asr_manifest
```

If an already failed v3 candidate is evaluated as a predeclared diagnostic,
build matching timestamped-ASR manifests for the frozen challenge and holdout
as well. Their labels must be byte-identical to the ordinary frozen manifests;
only the model-visible causal ASR serialization changes:

```bash
python3 tools/prompt_optimization/next_tool_forecast/build_eval_manifest.py \
  --partition development_challenge --asr-input-format timestamped_relative \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_timestamped_asr_manifest

python3 tools/prompt_optimization/next_tool_forecast/build_eval_manifest.py \
  --partition final_holdout --asr-input-format timestamped_relative \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/holdout_timestamped_asr_manifest
```

Before attributing a v3 result to ASR, audit whether each GT tool name was even
present in the causal transcript. This joins labels offline only and produces
no model input:

```bash
python3 tools/prompt_optimization/next_tool_forecast/audit_asr_target_coverage.py \
  --benchmark-dir tools/prompt_optimization/next_tool_forecast/runs/calibration_timestamped_asr_manifest \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/calibration_timestamped_asr_coverage
```

Run each authorized variant sequentially. The evaluator takes an advisory
`flock` on `/tmp/taskplanner-ninfer-eval.lock` for the complete batch,
explicitly unloads then reloads the manager worker, verifies manager
`loaded`+vision and direct worker `/v1/models`, and makes at most three POSTs
per fresh-worker batch. It verifies both catalog endpoints after the batch. A
transport/HTTP failure stops the batch immediately, preserves raw partial
evidence, emits no partial metric, and never retries automatically. For the
frozen challenge and holdout recovery protocol, invoke it with `--batch-size
1` so every request receives a fresh worker.

```bash
python3 tools/prompt_optimization/next_tool_forecast/run_ninfer_eval.py \
  --benchmark-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_manifest \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_baseline \
  --variant baseline_v0 --split development_challenge
```

Audit, without changing, an immutable calibration lock's suitability status:

```bash
python3 tools/prompt_optimization/next_tool_forecast/audit_selection_suitability.py \
  --selection-lock tools/prompt_optimization/next_tool_forecast/runs/calibration_selection/calibration_selection.json \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/calibration_selection_suitability
```

### Frozen failed-candidate diagnostic only

This is deliberately not a route to deploy v3 or replace the calibration
selection. It is available only after `optimized_v3` at `0.65` has failed the
predeclared suitability gate. The lock embeds the exact prompt and generation
hashes, timestamped input/label hashes, selected IDs, batch-one policy, and one
otherwise-empty output directory per split. The evaluator refuses all other
challenge/holdout v3 calls.

```bash
python3 tools/prompt_optimization/next_tool_forecast/freeze_failed_candidate_diagnostic.py \
  --source-run-dir tools/prompt_optimization/next_tool_forecast/runs/calibration_v3 \
  --challenge-manifest-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_timestamped_asr_manifest \
  --holdout-manifest-dir tools/prompt_optimization/next_tool_forecast/runs/holdout_timestamped_asr_manifest \
  --challenge-output-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_v3_failed_diagnostic \
  --holdout-output-dir tools/prompt_optimization/next_tool_forecast/runs/holdout_v3_failed_diagnostic \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/v3_failed_diagnostic_lock

python3 tools/prompt_optimization/next_tool_forecast/run_ninfer_eval.py \
  --benchmark-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_timestamped_asr_manifest \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_v3_failed_diagnostic \
  --variant optimized_v3 --split development_challenge --threshold 0.65 --batch-size 1 \
  --frozen-candidate-lock tools/prompt_optimization/next_tool_forecast/runs/v3_failed_diagnostic_lock/failed_candidate_diagnostic.json
```

Run the predeclared holdout command once with the corresponding frozen manifest
and output directory. Then validate both result directories offline:

```bash
python3 tools/prompt_optimization/next_tool_forecast/report_failed_candidate_diagnostic.py \
  --lock tools/prompt_optimization/next_tool_forecast/runs/v3_failed_diagnostic_lock/failed_candidate_diagnostic.json \
  --challenge-run-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_v3_failed_diagnostic \
  --holdout-run-dir tools/prompt_optimization/next_tool_forecast/runs/holdout_v3_failed_diagnostic \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/v3_failed_diagnostic_report
```

After all three frozen runs complete, compare and render every scored error
against the original frames:

```bash
python3 tools/prompt_optimization/next_tool_forecast/compare_runs.py \
  --run-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_baseline \
  --run-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_v1 \
  --run-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_v2 \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_comparison

python3 tools/prompt_optimization/next_tool_forecast/render_failure_sheets.py \
  --benchmark-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_manifest \
  --run-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_v1 \
  --output-dir tools/prompt_optimization/next_tool_forecast/runs/challenge_v1_failures
```

`run.json` records a sanitized model catalog entry, source hashes, prompt hashes,
generation parameters, threshold sweep, scores, and a pointer to the local
raw-response file. The score uses exact top-1 tool correctness: a handover is
correct only if its tool and thresholded decision both match. It separately
reports false positives on `none` windows, exact accuracy, balanced accuracy,
wrong-tool errors, per-tool recall, and an expected-tool → predicted-tool
confusion matrix. A wrong-tool prediction is a top-1 miss but is not counted as
a false positive on an actual `none` window.
