# Taskplanner 0.1.0 Release Notes

Release date: 2026-06-23

## Scope

This release fixes the current development snapshot as the first integrated
task-planning baseline for surgical tool handover and Mayo-stand recovery.

## Highlights

- Procedure bundles are now driven by compact `vlm_procedure_prompt.yaml` files.
- The dashboard updates surgery stage, instruments, and procedure flow directly
  from the selected procedure bundle.
- VLM mode is the default integration path, with selectable local LM Studio
  models and structured JSON parsing support.
- The LLM surgeon actor can drive randomized test scenarios without exposing
  hidden actor state to the VLM path.
- The synthetic VLM input frame only shows externally visible cues such as
  speech, hand extension, field interrupt, and visible Mayo-stand instruments.
- Mayo-based recovery is the normal flow; direct hand recovery remains only as
  a legacy/manual path.
- BT recovery supports returning unused prepositioned right-hand tools during
  procedure cleanup.
- Thyroidectomy, nephrectomy, and inguinal hernia repair procedure bundles are
  available for cross-scenario validation.

## Validation Target

- ROS workspace build for modified taskplanner packages.
- Web dashboard production build.
- Multi-bundle runtime probe across thyroidectomy, nephrectomy, and inguinal
  hernia repair.
