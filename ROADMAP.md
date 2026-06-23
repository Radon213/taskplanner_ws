# Taskplanner Roadmap

Last updated: 2026-06-23 KST

## Current Release

`0.1.0` is the first integrated baseline for the surgical-assistance task
planner.

It establishes:

- YAML-driven procedure prompts.
- Real VLM default path through a local OpenAI-compatible LM Studio endpoint.
- LLM surgeon actor for randomized validation stimuli.
- Public-evidence-only VLM input discipline.
- Authoritative OR digital twin reducer.
- Behavior Tree skill dispatch.
- Mock ROS action server for deterministic robot execution.
- Mayo-stand based recovery.
- Dashboard support for procedure selection, start phase selection, VLM model
  selection, LLM actor control, VLM input preview, and decision observability.
- Multi-bundle validation across thyroidectomy, nephrectomy, and inguinal hernia
  repair.

## Engineering Priorities

1. Real input integration
   Replace the no-image camera with the video team's actual surgical-field feed
   while preserving the same public-evidence boundary.

2. VLM robustness
   Improve phase and next-tool prediction across procedures without overfitting
   to one scripted actor trajectory. Track raw VLM proposal quality separately
   from reducer/system-fused accuracy.

3. Surgeon actor diversity
   Keep the LLM actor procedure-agnostic. It should adapt to arbitrary
   `vlm_procedure_prompt.yaml` bundles, vary timing and speech, trigger
   interrupt events naturally, and respect tool occupancy constraints.

4. Digital twin invariants
   Continue enforcing fail-closed state updates: no duplicate tool locations, no
   overfull hands, no contaminated rack returns, no action on stale or invalid
   VLM output.

5. Robot adapter boundary
   Preserve `/skill/execute` as the integration boundary for a future real
   humanoid action server. The mock server should remain deterministic for
   regression testing.

6. Operator workflow
   Keep procedure switching, mid-procedure start, model selection, reset/stop,
   and observability fast enough for repeated test runs.

## Completed in `0.1.0`

- Converted procedure loading from the old split-file spec layout to compact
  `vlm_procedure_prompt.yaml` bundles.
- Added thyroidectomy, open nephrectomy, and inguinal hernia repair bundles with
  distinct phase and tool definitions.
- Made dashboard procedure selection update stage, instruments, and flow from
  the selected bundle.
- Changed default runtime to real VLM mode and LLM surgeon actor mode.
- Added model selection UI for VLM and LLM actor.
- Added start-phase selection for mid-procedure insertion.
- Added no-image camera overlay for public visual cues only.
- Removed hidden event-tool hints from the VLM input overlay.
- Added Mayo stand display and Mayo-based recovery semantics.
- Kept bleeding/hemostasis as an interrupt event rather than a normal sequential
  phase.
- Added return of unused prepositioned right-hand tools during cleanup.
- Added VLM/system phase and tool scoreboards with
  `correct / proposed / evaluable` semantics.
- Added multi-bundle runtime probe and focused LLM/prediction probes.
- Published GitHub release `v0.1.0`.

## Next Work

### 1. Real Video Feed

- Define the exact ROS image topic and compression format from the video team.
- Replace or run alongside `no_image_camera`.
- Verify the dashboard VLM input preview uses the same frame stream that VLM
  consumes.
- Confirm no hidden simulation-only state is added to the image or prompt.

### 2. VLM Evaluation Harness

- Save per-frame VLM inputs, outputs, reducer decisions, and ground-truth actor
  labels for offline analysis.
- Add confusion reports by procedure, phase, and tool.
- Track latency distribution and JSON repair/retry counts.
- Separate "VLM proposed correctly" from "system fused correctly".

### 3. Procedure Authoring

- Add a schema validator for `vlm_procedure_prompt.yaml`.
- Add a lightweight preview command that prints phases, tools, transitions, and
  expected tool sequences before launching ROS.
- Document the minimum fields required for a new procedure.

### 4. Real Robot Integration

- Implement a real `/skill/execute` action server adapter.
- Preserve the existing action names:
  `direct_handover`, `pick_up_and_handover`, `put_down_and_handover`,
  `retrieve_from_mayo`, `return_unused_preposition`, and `predict_tool`.
- Keep the mock action server available as the default regression backend.

### 5. Dashboard Hardening

- Add automated browser checks for procedure switching, phase start selection,
  VLM model selection, LLM actor toggle, Mayo overflow layout, and interrupt
  event popups.
- Keep dense runtime states readable without shrinking tool labels too early.

### 6. Release Discipline

- Keep `main` as the current stable development branch.
- Use feature branches for nontrivial changes.
- Tag the next functional baseline as `v0.2.0`.
- Avoid moving existing release tags unless a release was created on the wrong
  commit.
