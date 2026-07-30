# Causal surgical VLM SFT dataset

`build_causal_sft_dataset.py` converts the current 0704_6–0704_17
annotation manifests into a case-grouped, causal multimodal SFT dataset.
It never edits source annotations.

## Build

```bash
cd /home/arl/Documents/ARPA-H/taskplanner_ws
python3 -m tools.vlm_finetuning.build_causal_sft_dataset \
  --output-dir /home/arl/Documents/ARPA-H/taskplanner_ws/output/vlm_finetuning/qwen35_4b_causal_v1 \
  --seed 20260729 \
  --fold-count 4 \
  --held-out-fold 0
```

Image materialization is enabled by default. It fully decodes the
overlay-free, one-to-one `review_cam4.mp4` and `review_flir.mp4` proxies,
checks that each decoded frame count equals the canonical timeline, and
writes only requested frames:

```text
images/<case_id>/<view>/frame_<source_frame_idx>.jpg
```

Each image is deduplicated by `(case_id, view, source_frame_idx)`. The
Unsloth output contains absolute JPEG paths. Use `--no-materialize-images`
only for metadata/audit development; that mode intentionally omits
`unsloth_messages.jsonl`.

## Outputs

- `master.jsonl`: full provenance, causal cutoff, frame indices, source
  bindings, authority tier, target, and split for every example.
- `unsloth_messages.jsonl`: OpenAI-style vision `messages` ready for
  `UnslothVisionDataCollator`; every row retains `task_type`, `fold_id`,
  `split`, and authority.
- `folds.json`: deterministic case-group 4-fold assignments and every CV
  train/validation/test partition.
- `audit.json`: counts, current source hashes, leakage/gap/authority checks,
  proxy decode evidence, and output hashes.

The assistant response is a canonical compact JSON string. Configure the
training collator to supervise assistant tokens only
(`train_on_responses_only=True` or equivalent label masking).

## Task definitions and authority

- `tool_presence_at_transfer`: sparse positive supervision for the named
  physical transfer event. It is deliberately **not** a complete inventory
  of visible tools and contains no absence labels. ASR is removed from this
  task so the answer cannot be copied from a spoken tool name.
- `tool_presence_pseudo`: train-split-only RF-DETR pseudo-labels. An example
  requires confidence >= 0.90, the same spatial class in at least three of
  the trailing five frames, and no conflicting tool class on the same box.
  Identical `(case, view, frame)` media retain only the highest-confidence
  target so one SFT input cannot have contradictory single-tool answers.
  Samples are capped per tool and temporally separated. Clean proxy frames,
  never box overlays, are supplied to the VLM. Authority remains `pseudo`;
  these rows are forbidden from validation/test and scoring.
- `request_intent`: strict implicit open-palm request intervals. The eventual
  transfer tool is never backfilled into the request target.
- `current_phase`: two interior samples per P03–P06 interval plus observed
  transitions. These remain provisional ambiguous context and are not
  promoted to scoring ground truth.
- `next_physical_tool`: the first physical
  `scrub_nurse -> surgeon` transfer strictly after the cutoff and within five
  seconds, or `none`. Input media and ASR stop at the cutoff.
- `clinical_observation_interpretation`: the declared FLIR evidence window,
  ending at the causal cutoff. These targets remain AI drafts requiring
  surgeon review.

All frames, camera views, crops, prompt variants, and targets from one case
remain in one split. Because 0704_6–0704_17 are one development/calibration
campaign, the generated test fold is an internal stress fold, not an
external generalization test.
