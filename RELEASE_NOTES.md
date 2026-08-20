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

## Post-0.1.0 Integration Note

The current development branch uses the institution-agreed retraction-only
contract:

- direct teach, retraction, adjustment, Tool Change, and stop use the single
  `/surgery/retraction/command` (`ExecuteRetractionCommand`) Service;
- its response reports request admission only, not physical completion,
  controller state, cancellation, or tool attachment;
- controller-owned retraction state arrives on
  `/external/bed_robot_arms/status`;
- bed-mounted suction-arm command and status paths are removed, while clinical
  suction instruments and surgeon speech about suction remain supported.

This note describes unreleased development after tag `v0.1.0`; it does not
retroactively change the tagged artifact.
