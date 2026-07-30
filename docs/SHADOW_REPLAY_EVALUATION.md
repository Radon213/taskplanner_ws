# Taskplanner Shadow Replay Evaluation

> The model names recorded in completed run artifacts are historical evidence.
> New runs default to `unsloth/gemma-4-E4B-it-NVFP4` through the local vLLM
> manager; old Qwen run attribution is intentionally preserved.

## Purpose

Shadow replay evaluates the current Taskplanner against recorded surgery
without controlling a physical robot or changing the source video. The test is
designed to answer four separate questions:

1. What did the VLM propose from public evidence?
2. What did the digital-twin reducer accept?
3. What action did the BT select?
4. What command would have reached skill execution?

It does not treat the recorded scrub-nurse trajectory as the only clinically
valid strategy. Exact handover matching, physical-state conflicts, unmatched
commands, and recovery-policy concerns are reported separately.

## Strict Information Boundary

The strict runtime can receive:

- normalized public FLIR and CAM4 images at
  `/surgery/images/flir/compressed` and
  `/surgery/images/cam4/compressed`;
- the exact bounded model image at
  `/surgery/images/vlm/composite/compressed`, composed from the FLIR frame and
  a detection-guided CAM4 crop with source-stamp alignment;
- bounded CAM4 detector summaries at
  `/surgery/perception/cam4/tools/bboxes/json` and
  `/surgery/perception/cam4/tools/segmentation/json`;
- `/surgery/transcript`, admitted through the public speech adapter;
- public digital-twin events produced by the system itself;
- counterfactual skill completion generated from an admitted shadow command.

The `0704_6` source bag contains public detector products at
`/surgery/cam4/tools/bboxes/json` and
`/surgery/cam4/tools/segmentation/json`. The replay worker normalizes these
topics before the VLM consumes them. Prompt and trace context retain bounded
class, track, normalized box, mask-area, and centroid summaries only. Full COCO
RLE `counts`, composite masks, overlays, annotation labels, and
`/evaluation/ground_truth/*` are not supplied to the VLM.

It cannot receive:

- `/evaluation/ground_truth/*`;
- annotation JSONL or annotated-MCAP labels;
- hidden LLM-surgeon state;
- future reference events.

The runner performs a static launch/source audit and a runtime ROS graph audit.
The manifest records `ground_truth_runtime_visible=false`, both audit reports,
topic allowlists, source and reference hashes, and hashes of the execution and
post-processing code.

## Modes

| Mode | Reference visible to runtime | Purpose |
|---|---:|---|
| `strict` | No | End-to-end public-evidence result |
| `reconciled` | Only after event time | Diagnose accumulated state drift |
| `oracle` | At event time, downstream only | Reducer/BT upper-bound baseline |

Results from these modes must not be pooled.

## Scoring Contract

- Confirmed `scrub_nurse -> operative_recipient` transfers are handover targets.
- A prediction must precede the target and fall inside the configured lead
  window.
- One semantic prediction episode can match at most one handover.
- Positive `request_generation` values keep repeated same-tool requests
  distinct.
- Proposed, ambiguous, and rejected annotations are excluded.
- Dataset tool IDs and procedure IDs are normalized with the hashed catalog.
- Handover actions and Mayo recovery actions use separate denominators.
- Phase is evaluated only from independently confirmed phase intervals.

The default settings are:

| Setting | Value |
|---|---:|
| Handover lead window | 10 s |
| Stable prediction duration | 3 s |
| Maximum continuous-prediction age | 3 s |
| Same-episode gap | 2.5 s |
| Recovery reuse warning window | 15 s |

## Frozen 0704_5 Strict Baseline

The verified artifact is:

```text
output/shadow_runs/0704_5-strict-live-015
```

Source and model:

- source MCAP SHA-256:
  `cc3162c2b944639c9d4c03186f49bdf02c8d34dbf60732c8a17819d83222f1ea`;
- VLM: `qwen3.6-35b-a3b-mtp@q2_k_xl` through LM Studio;
- 2,139 / 2,139 CAM4 frames recorded;
- 22 / 22 source transcripts recorded and admitted;
- strict static and runtime boundary audits passed;
- 0 trace-contract errors;
- 0 commands after completion;
- 0 physical executions.

### Tool handover results

| Layer | Exact / 14 | Wrong | Missed | Handover FP | Request-backed |
|---|---:|---:|---:|---:|---:|
| VLM raw | 0 | 1 | 13 | 0 | 0 |
| Reducer fused | 14 | 0 | 0 | 1 | 14 |
| BT decision | 14 | 0 | 0 | 1 | 14 |
| Skill command | 14 | 0 | 0 | 1 | 14 |

The reducer/BT/skill result is driven by public speech requests in this case; it
must not be presented as visual next-tool prediction accuracy. The unmatched
handover command is a public Adson request for which no confirmed completion is
present in the observable reference. It remains a false positive against this
reference and also a human-adjudication candidate.

### VLM runtime

| Metric | Result |
|---|---:|
| Results during source input / total | 84 / 89 |
| Effective result rate during input | 0.558 Hz |
| Median latency | 1.809 s |
| p95 latency | 1.885 s |
| Parse retries | 0 |
| Unhealthy samples during input | 0 |
| Unhealthy samples after input | 3 |

The three post-input samples are expected stale-image health reports during
post-roll, not inference failures during source replay. The effective rate is
below the configured 1 Hz request period because one synchronous inference
takes about 1.8 seconds.

### Recovery audit

| Time | Tool | Next confirmed handover | Audit |
|---:|---|---:|---|
| 97.005 s | Bipolar forceps | 11.598 s later | `suspicious` |
| 114.005 s | Bovie | 25.446 s later | `review` |
| 129.405 s | Army-Navy retractor | None observed | `info` |

The first recovery falls inside the 15-second reuse warning window. The second
has later observable reuse and needs policy review. The third has no later
observable reuse, which does not prove that recovery was clinically optimal.

## Reference Limitations

- The reference contains 52 confirmed records with mixed authority: 11 human
  seed confirmations and 41 authorized assistant video adjudications.
- It contains 14 confirmed handovers but no confirmed phase intervals.
- Phase accuracy is therefore intentionally reported as unavailable.
- Tool instance continuity is partly observational and should not be treated as
  a complete instrument-count inventory.
- This is one recorded thyroidectomy case, not a clinical efficacy result.

## Verification Artifacts

- `run_manifest.json`: source, reference, procedure, model, commands, code
  hashes, boundary status, and artifact hashes;
- `shadow_trace.v1.jsonl`: append-only public input and decision trace;
- `shadow_evaluation.v2.json`: full layered metrics and event-level outcomes;
- `shadow_layers.csv`: compact layer table;
- `shadow_report.md`: reader-facing report;
- `shadow_timeline.svg` and `shadow_timeline.png`: slide-readable timeline;
- `static_boundary.json` and `runtime_boundary.json`: leakage audits;
- `model_preflight.json`: selected-model availability.

The deterministic integration pair is:

```text
output/shadow_runs/synthetic-strict-023
output/shadow_runs/synthetic-strict-024
output/shadow_runs/synthetic-determinism-023-024.json
```

All public input, VLM, reducer, BT, skill, shadow-sink, and evaluation semantic
digests match across those two runs. Their manifests also match every recorded
artifact hash.

The reviewed `0704_5` derived MCAP was revalidated with ROS 2 Jazzy. All 30,999
source records remain byte-identical and in the original replay order; only one
annotation manifest and 53 eligible ground-truth records are added.
