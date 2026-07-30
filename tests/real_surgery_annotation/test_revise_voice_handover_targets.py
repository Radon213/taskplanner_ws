from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from tools.real_surgery_annotation import revise_voice_handover_targets
from tools.real_surgery_annotation.finalize_interaction_review import (
    FinalizationError,
    encode_jsonl,
    publish_create_only,
)


ROOT = Path(__file__).resolve().parents[2]
METRIC_KEYS = (
    "action",
    "latency",
    "state",
    "physical",
    "reuse",
    "gesture_presence",
    "gesture_onset",
    "phase_accuracy",
    "actor_identity",
)


def _eligibility(**enabled: bool) -> dict[str, bool]:
    value = {key: False for key in METRIC_KEYS}
    value.update(enabled)
    return value


def test_voice_role_remediation_preserves_source_and_archives_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "annotations/observable_tool_events"
    case_dir = root / "cases/0704_99"
    reports = root / "reports"
    schema_dir = root / "schema"
    case_dir.mkdir(parents=True)
    reports.mkdir(parents=True)
    schema_dir.mkdir(parents=True)
    timeline = {
        "case_id": "0704_99",
        "frame_count": 5,
        "timestamps_sec": [0.0, 1.0, 2.0, 3.0, 4.0],
        "start_sec": 0.0,
        "end_sec": 4.0,
        "gaps": [],
    }
    (case_dir / "cam4_frame_timeline.v1.json").write_text(
        json.dumps(timeline),
        encoding="utf-8",
    )
    observed = [
        {
            "event_id": "0704_99-T0001",
            "event_type": "tool_transfer",
            "tool": "bovie",
        }
    ]
    (case_dir / "interaction_events.observed.final.v1.jsonl").write_bytes(
        encode_jsonl(observed)
    )
    phases = [
        {
            "event_id": f"0704_99-PH{index:04d}",
            "event_type": "phase_start",
        }
        for index in range(1, 5)
    ]
    phase_path = case_dir / "phase_events.provisional.final.v1.jsonl"
    phase_path.write_bytes(encode_jsonl(phases))
    voice = [
        {
            "event_id": "0704_99-V0001",
            "end_sec": 1.0,
            "available_sec": 1.0,
        }
    ]
    (case_dir / "voice_events.source.v2.jsonl").write_bytes(
        encode_jsonl(voice)
    )
    masks = {
        "schema": "taskplanner.evaluation_masks.v1",
        "case_id": "0704_99",
        "evaluation_scope": {
            "classification": "development_calibration",
            "held_out_eligible": False,
            "reason": "calibration",
        },
        "default_metric_eligibility": _eligibility(),
        "event_roles": [
            {
                "event_id": "0704_99-T0001",
                "role": "state_observation_only",
                "metric_eligibility": _eligibility(),
                "reason": "source role",
            },
            *[
                {
                    "event_id": phase["event_id"],
                    "role": "context_only_not_ground_truth",
                    "metric_eligibility": _eligibility(),
                    "reason": "Phase context",
                }
                for phase in phases
            ],
        ],
        "interval_masks": [
            {
                "mask_id": "voice_availability_V0001",
                "start_sec": 2.0,
                "end_sec": 3.0,
                "metric_eligibility": _eligibility(
                    gesture_presence=True,
                    gesture_onset=True,
                ),
                "reason": "Old interval vetoes visual action.",
            }
        ],
        "cutoffs": {
            "action_and_next_tool_end_sec": 4.0,
            "state_audit_end_sec": 4.0,
            "visual_end_sec": 4.0,
            "voice_context_end_sec": 1.0,
        },
        "tool_metric_scopes": [
            {
                "tool": "bovie",
                "instance_resolution": "initial_inventory_unavailable",
                "state": False,
                "physical": False,
                "reuse": False,
                "reason": "inventory unavailable",
            }
        ],
        "voice_context_roles": [
            {
                "event_id": "0704_99-V0001",
                "role": "tool_request_context",
                "handover_target": False,
                "reason": "generic source context",
            }
        ],
    }
    source_masks = case_dir / "evaluation_masks.v1.json"
    source_masks.write_text(json.dumps(masks), encoding="utf-8")
    mask_schema = schema_dir / "evaluation_masks.v1.schema.json"
    mask_schema.write_bytes(
        (
            ROOT
            / "annotations/observable_tool_events/schema/"
            "evaluation_masks.v1.schema.json"
        ).read_bytes()
    )
    report_path = reports / "0704_99_projection.v1.json"
    report_path.write_text(json.dumps({"case_id": "0704_99"}), encoding="utf-8")
    boundary_path = reports / "boundary.json"
    boundary_path.write_text(
        json.dumps({"ok": True, "violations": []}),
        encoding="utf-8",
    )
    reviewed_at = datetime.now().astimezone().isoformat()
    spec_path = case_dir / "voice_handover_target_remediation.spec.v2.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": (
                    "taskplanner.voice_handover_target_remediation_spec.v1"
                ),
                "case_id": "0704_99",
                "reviewed_at": reviewed_at,
                "corrections": [
                    {
                        "event_id": "0704_99-V0001",
                        "handover_target": True,
                        "reason": "Visible T0001 follows causally.",
                        "evidence": {"target_event_ids": ["0704_99-T0001"]},
                    }
                ],
                "event_role_corrections": [
                    {
                        "event_id": "0704_99-T0001",
                        "role": "action_target",
                        "metric_eligibility": _eligibility(
                            action=True,
                            latency=True,
                        ),
                        "reason": "Visible anticipatory handover target.",
                        "evidence": {"source_frame_idx": 3},
                    }
                ],
                "interval_mask_corrections": [
                    {
                        "mask_id": "voice_availability_V0001",
                        "metric_eligibility": _eligibility(
                            action=True,
                            latency=True,
                            gesture_presence=True,
                            gesture_onset=True,
                        ),
                        "reason": (
                            "Voice availability does not veto the independently "
                            "confirmed visual target."
                        ),
                        "evidence": {"target_event_ids": ["0704_99-T0001"]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = case_dir / "annotation_manifest.json"
    original_manifest = b'{"case_id":"0704_99","old":true}\n'
    manifest_path.write_bytes(original_manifest)

    def fake_build_manifest(**kwargs):
        assert kwargs["evaluation_masks_path"].is_file()
        return {
            "case_id": "0704_99",
            "masks_file": kwargs["evaluation_masks_path"].name,
        }

    monkeypatch.setattr(
        revise_voice_handover_targets,
        "build_manifest",
        fake_build_manifest,
    )
    target_masks = case_dir / "evaluation_masks.v2.json"
    audit_path = (
        case_dir / "voice_handover_target_remediation.audit.v2.json"
    )
    archive_path = (
        case_dir
        / "audit_archive/voice_handover_target_remediation_v2/"
        "annotation_manifest.before_voice_role_v2.json"
    )

    result = revise_voice_handover_targets.remediate(
        case_dir=case_dir,
        spec_path=spec_path,
        source_masks_path=source_masks,
        target_masks_path=target_masks,
        audit_output_path=audit_path,
        manifest_path=manifest_path,
        archive_manifest_path=archive_path,
        report_path=report_path,
        information_boundary_report_path=boundary_path,
        phase_reference_path=phase_path,
        mask_schema_path=mask_schema,
    )

    assert result["ok"] is True
    assert json.loads(source_masks.read_text())["voice_context_roles"][0][
        "handover_target"
    ] is False
    revised = json.loads(target_masks.read_text())
    assert revised["voice_context_roles"][0]["handover_target"] is True
    assert revised["event_roles"][0]["role"] == "action_target"
    assert revised["event_roles"][0]["metric_eligibility"]["action"] is True
    assert revised["interval_masks"][0]["metric_eligibility"]["action"] is True
    assert revised["interval_masks"][0]["metric_eligibility"]["latency"] is True
    assert archive_path.read_bytes() == original_manifest
    assert json.loads(manifest_path.read_text()) == {
        "case_id": "0704_99",
        "masks_file": "evaluation_masks.v2.json",
    }
    audit = json.loads(audit_path.read_text())
    assert audit["corrections"][0]["new_handover_target"] is True
    assert audit["interval_mask_corrections"][0]["mask_id"] == (
        "voice_availability_V0001"
    )


def test_manifest_publish_failure_restores_original_without_link_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_dir = tmp_path / "cases/0704_99"
    case_dir.mkdir(parents=True)
    spec_path = case_dir / "spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": (
                    "taskplanner.voice_handover_target_remediation_spec.v1"
                ),
                "case_id": "0704_99",
                "reviewed_at": datetime.now().astimezone().isoformat(),
                "corrections": [
                    {
                        "event_id": "0704_99-V0001",
                        "handover_target": True,
                        "reason": "Confirmed target.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    source_masks_path = case_dir / "evaluation_masks.v1.json"
    source_masks_path.write_text(
        json.dumps(
            {
                "case_id": "0704_99",
                "voice_context_roles": [
                    {
                        "event_id": "0704_99-V0001",
                        "handover_target": False,
                        "reason": "Source role.",
                    }
                ],
                "event_roles": [],
                "interval_masks": [],
            }
        ),
        encoding="utf-8",
    )
    (case_dir / "cam4_frame_timeline.v1.json").write_text(
        json.dumps({"case_id": "0704_99"}),
        encoding="utf-8",
    )
    (case_dir / "interaction_events.observed.final.v1.jsonl").write_text(
        "",
        encoding="utf-8",
    )
    phase_path = case_dir / "phase.jsonl"
    phase_path.write_text("", encoding="utf-8")
    (case_dir / "voice_events.source.v2.jsonl").write_bytes(
        encode_jsonl([{"event_id": "0704_99-V0001"}])
    )
    mask_schema_path = case_dir / "mask.schema.json"
    mask_schema_path.write_text("{}", encoding="utf-8")
    manifest_path = case_dir / "annotation_manifest.json"
    original_manifest = b'{"case_id":"0704_99","old":true}\n'
    manifest_path.write_bytes(original_manifest)
    target_masks_path = case_dir / "evaluation_masks.v2.json"
    audit_output_path = case_dir / "remediation.audit.json"
    archive_manifest_path = case_dir / "archive/manifest.before.json"

    monkeypatch.setattr(
        revise_voice_handover_targets,
        "validate_evaluation_masks",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        revise_voice_handover_targets,
        "build_manifest",
        lambda **kwargs: {
            "case_id": "0704_99",
            "masks_file": kwargs["evaluation_masks_path"].name,
        },
    )
    publish_calls = 0

    def fail_final_publish(outputs: dict[Path, bytes]) -> None:
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 3:
            [(path, data)] = outputs.items()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            raise FinalizationError("injected final manifest publish failure")
        publish_create_only(outputs)

    monkeypatch.setattr(
        revise_voice_handover_targets,
        "publish_create_only",
        fail_final_publish,
    )

    with pytest.raises(
        FinalizationError,
        match="injected final manifest publish failure",
    ):
        revise_voice_handover_targets.remediate(
            case_dir=case_dir,
            spec_path=spec_path,
            source_masks_path=source_masks_path,
            target_masks_path=target_masks_path,
            audit_output_path=audit_output_path,
            manifest_path=manifest_path,
            archive_manifest_path=archive_manifest_path,
            report_path=case_dir / "unused-report.json",
            information_boundary_report_path=case_dir / "unused-boundary.json",
            phase_reference_path=phase_path,
            mask_schema_path=mask_schema_path,
        )

    assert publish_calls == 3
    assert manifest_path.read_bytes() == original_manifest
    assert archive_manifest_path.read_bytes() == original_manifest
