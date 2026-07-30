from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from tools.real_surgery_annotation import revise_provisional_phase_reference
from tools.real_surgery_annotation.finalize_interaction_review import (
    encode_jsonl,
)


ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _phase(phase_id: str, ordinal: int, frame: int) -> dict:
    return {
        "schema": "taskplanner.observable_interaction_point.v1",
        "case_id": "0704_99",
        "event_id": f"0704_99-PH{ordinal:04d}",
        "event_type": "phase_start",
        "phase_id": phase_id,
        "time_sec": float(frame),
        "source_frame_idx": frame,
        "source_views": ["cam4"],
        "phase_boundary_kind": (
            "clip_initial_state" if frame == 0 else "observed_transition"
        ),
        "review_status": "ambiguous",
        "label_origin": "assistant_video_adjudication",
        "review": {
            "reviewer_kind": "ai_assistant",
            "reviewer_id": "codex-gpt-5.6-sol",
            "reviewed_at": "2026-07-28T00:00:00+09:00",
            "authorized_by": "task-owner",
            "notes": "source",
        },
    }


def test_phase_remediation_is_create_only_and_archives_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    case_dir = tmp_path / "annotations/observable_tool_events/cases/0704_99"
    reports = tmp_path / "annotations/observable_tool_events/reports"
    schema_dir = tmp_path / "annotations/observable_tool_events/schema"
    case_dir.mkdir(parents=True)
    reports.mkdir(parents=True)
    schema_dir.mkdir(parents=True)

    timeline = {
        "case_id": "0704_99",
        "frame_count": 5,
        "timestamps_sec": [0.0, 1.0, 2.0, 3.0, 4.0],
        "gaps": [],
    }
    (case_dir / "cam4_frame_timeline.v1.json").write_text(
        json.dumps(timeline),
        encoding="utf-8",
    )
    source_phases = [
        _phase("P03", 1, 0),
        _phase("P04", 2, 1),
        _phase("P05", 3, 2),
        _phase("P06", 4, 4),
    ]
    source_phase_path = (
        case_dir / "phase_events.provisional.final.v1.jsonl"
    )
    source_phase_path.write_bytes(encode_jsonl(source_phases))
    source_report_path = reports / "0704_99_projection.v1.json"
    source_report_path.write_text(
        json.dumps(
            {
                "case_id": "0704_99",
                "inputs": {},
                "outputs": {},
                "manifest_handoff": {
                    "evaluation_reference": {"phase_reference": {}}
                },
            }
        ),
        encoding="utf-8",
    )
    point_schema_path = (
        schema_dir / "observable_interaction_point.v1.schema.json"
    )
    point_schema_path.write_bytes(
        (
            ROOT
            / "annotations/observable_tool_events/schema/"
            "observable_interaction_point.v1.schema.json"
        ).read_bytes()
    )
    information_boundary_path = reports / "information_boundary.json"
    information_boundary_path.write_text(
        json.dumps({"ok": True, "violations": []}),
        encoding="utf-8",
    )
    reviewed_at = datetime.now().astimezone().isoformat()
    spec_path = case_dir / "phase_boundary_remediation.spec.v2.json"
    spec_path.write_text(
        json.dumps(
            {
                "schema": "taskplanner.phase_boundary_remediation_spec.v1",
                "case_id": "0704_99",
                "reviewed_at": reviewed_at,
                "corrections": [
                    {
                        "phase_id": "P05",
                        "new_source_frame_idx": 3,
                        "review_notes": "Stable target engagement begins.",
                        "evidence": {"before": "approach", "after": "sustained"},
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
        assert kwargs["phase_reference_path"].is_file()
        assert kwargs["report_path"].is_file()
        return {
            "case_id": "0704_99",
            "phase_file": kwargs["phase_reference_path"].name,
        }

    monkeypatch.setattr(
        revise_provisional_phase_reference,
        "build_manifest",
        fake_build_manifest,
    )
    target_phase_path = (
        case_dir / "phase_events.provisional.final.v2.jsonl"
    )
    target_report_path = reports / "0704_99_projection.v2.json"
    audit_path = case_dir / "phase_boundary_remediation.audit.v2.json"
    archive_path = (
        case_dir
        / "audit_archive/phase_boundary_remediation_v2/"
        "annotation_manifest.before_phase_v2.json"
    )

    result = revise_provisional_phase_reference.remediate(
        case_dir=case_dir,
        spec_path=spec_path,
        source_phase_path=source_phase_path,
        target_phase_path=target_phase_path,
        source_report_path=source_report_path,
        target_report_path=target_report_path,
        information_boundary_report_path=information_boundary_path,
        point_schema_path=point_schema_path,
        audit_output_path=audit_path,
        manifest_path=manifest_path,
        archive_manifest_path=archive_path,
    )

    assert result["ok"] is True
    assert source_phase_path.read_bytes() == encode_jsonl(source_phases)
    revised = [
        json.loads(line)
        for line in target_phase_path.read_text(encoding="utf-8").splitlines()
    ]
    assert revised[2]["source_frame_idx"] == 3
    assert revised[2]["time_sec"] == 3.0
    assert revised[2]["review"]["reviewed_at"] == reviewed_at
    assert archive_path.read_bytes() == original_manifest
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "case_id": "0704_99",
        "phase_file": "phase_events.provisional.final.v2.jsonl",
    }
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["corrections"][0]["old_source_frame_idx"] == 2
    assert audit["corrections"][0]["new_source_frame_idx"] == 3


def _offline_manifest(
    *,
    case_dir: Path,
    source_phase_path: Path,
    source_report_path: Path,
) -> dict:
    return {
        "schema": "taskplanner.observable_annotation_manifest.v1",
        "case_id": case_dir.name,
        "source_bag": {
            "path": "/offline/immutable/source.mcap",
            "sha256": "source-bag-sha",
            "message_count": 1234,
        },
        "event_file": "observed_interactions.final.v1.jsonl",
        "evaluation_reference": {
            "observed_reference": {
                "file": "observed_interactions.final.v1.jsonl",
                "sha256": "observed-sha",
            },
            "dt_reference": {
                "file": "dt_interactions.final.v1.jsonl",
                "sha256": "dt-sha",
            },
            "phase_reference": {
                "file": source_phase_path.name,
                "sha256": _sha256(source_phase_path),
                "event_count": 4,
                "event_type_counts": {"phase_start": 4},
                "review_status_counts": {"ambiguous": 4},
            },
            "projection_report_file": os.path.relpath(
                source_report_path,
                case_dir,
            ),
            "projection_report_sha256": _sha256(source_report_path),
            "information_boundary": (
                "evaluation_only_never_vlm_reducer_bt_runtime_input"
            ),
        },
        "minimal_interaction_annotation": {
            "final_observed_reference_file": (
                "observed_interactions.final.v1.jsonl"
            ),
            "final_observed_reference_sha256": "observed-sha",
            "provisional_phase_reference_file": source_phase_path.name,
            "provisional_phase_reference_sha256": _sha256(source_phase_path),
        },
        "phase_annotation": {
            "authority": "assistant_video_adjudication",
            "provisional_reference_file": source_phase_path.name,
            "provisional_reference_sha256": _sha256(source_phase_path),
            "event_count": 4,
            "review_status_counts": {
                "ambiguous": 4,
                "confirmed": 0,
                "rejected": 0,
            },
            "scoring_reference_ready": False,
        },
        "notes": ["existing note"],
        "unrelated_extension": {
            "mask": "evaluation_masks.v2.json",
            "speech": "source_voice_timeline.v1.jsonl",
        },
    }


def _write_explicit_override_artifacts(case_dir: Path) -> dict:
    annotation_root = case_dir.parents[1]
    proposal_path = case_dir / "phase_events.generalization.proposed.v3.jsonl"
    catalog_path = case_dir / "procedure_phases.generalization.v3.yaml"
    ontology_path = (
        annotation_root / "procedure_phases.cross_case_provisional.v3.yaml"
    )
    proposal_path.write_text('{"proposal":true}\n', encoding="utf-8")
    catalog_path.write_text("schema: case-local-catalog\n", encoding="utf-8")
    ontology_path.write_text("schema: cross-case-ontology\n", encoding="utf-8")
    return {
        "authority": "explicit_user_override",
        "authorized_by": "task-owner",
        "reviewer_kind": "ai_assistant",
        "reviewer_id": "codex-gpt-5.6-sol",
        "label_origin": "assistant_video_adjudication",
        "source_proposal_file": proposal_path.name,
        "source_proposal_sha256": _sha256(proposal_path),
        "procedure_catalog_file": catalog_path.name,
        "procedure_catalog_sha256": _sha256(catalog_path),
        "ontology_file": os.path.relpath(ontology_path, case_dir),
        "ontology_sha256": _sha256(ontology_path),
        "phase_review_notes": {
            phase_id: f"{phase_id} assistant functional-state review"
            for phase_id in ("P03", "P04", "P05", "P06")
        },
    }


def test_offline_manifest_reuse_rebinds_only_phase_descriptors(
    tmp_path: Path,
) -> None:
    case_dir = tmp_path / "annotations/observable_tool_events/cases/0704_99"
    reports = tmp_path / "annotations/observable_tool_events/reports"
    case_dir.mkdir(parents=True)
    reports.mkdir(parents=True)
    source_phase_path = (
        case_dir / "phase_events.provisional.final.v1.jsonl"
    )
    source_phase_path.write_bytes(
        encode_jsonl(
            [
                _phase("P03", 1, 0),
                _phase("P04", 2, 1),
                _phase("P05", 3, 2),
                _phase("P06", 4, 4),
            ]
        )
    )
    source_report_path = reports / "0704_99_projection.v1.json"
    source_report_path.write_text('{"case_id":"0704_99"}\n', encoding="utf-8")
    target_phase_path = (
        case_dir / "phase_events.provisional.final.v2.jsonl"
    )
    target_report_path = reports / "0704_99_projection.v2.json"
    phases = [
        _phase("P03", 1, 0),
        _phase("P04", 2, 1),
        _phase("P05", 3, 3),
        _phase("P06", 4, 4),
    ]
    phases[2]["review_status"] = "confirmed"
    target_phase_sha256 = "a" * 64
    target_report_sha256 = "b" * 64
    current = _offline_manifest(
        case_dir=case_dir,
        source_phase_path=source_phase_path,
        source_report_path=source_report_path,
    )
    original = copy.deepcopy(current)

    updated = (
        revise_provisional_phase_reference._updated_manifest_from_current(
            current_manifest=current,
            case_dir=case_dir,
            source_phase_path=source_phase_path,
            target_phase_path=target_phase_path,
            target_phase_sha256=target_phase_sha256,
            phases=phases,
            source_report_path=source_report_path,
            target_report_path=target_report_path,
            target_report_sha256=target_report_sha256,
            promotion=None,
        )
    )

    # The helper must not mutate its caller-owned manifest.
    assert current == original
    # Immutable/offline inputs and all unrelated annotation descriptors survive
    # byte-for-byte at the JSON-value level.
    assert updated["source_bag"] == original["source_bag"]
    assert updated["event_file"] == original["event_file"]
    assert (
        updated["evaluation_reference"]["observed_reference"]
        == original["evaluation_reference"]["observed_reference"]
    )
    assert (
        updated["evaluation_reference"]["dt_reference"]
        == original["evaluation_reference"]["dt_reference"]
    )
    assert (
        updated["minimal_interaction_annotation"][
            "final_observed_reference_file"
        ]
        == original["minimal_interaction_annotation"][
            "final_observed_reference_file"
        ]
    )
    assert updated["unrelated_extension"] == original["unrelated_extension"]

    phase_reference = updated["evaluation_reference"]["phase_reference"]
    assert phase_reference == {
        "file": target_phase_path.name,
        "sha256": target_phase_sha256,
        "event_count": 4,
        "event_type_counts": {"phase_start": 4},
        "review_status_counts": {"ambiguous": 3, "confirmed": 1},
    }
    assert (
        updated["evaluation_reference"]["projection_report_file"]
        == os.path.relpath(target_report_path, case_dir)
    )
    assert (
        updated["evaluation_reference"]["projection_report_sha256"]
        == target_report_sha256
    )
    assert (
        updated["minimal_interaction_annotation"][
            "provisional_phase_reference_file"
        ]
        == target_phase_path.name
    )
    assert updated["phase_annotation"]["review_status_counts"] == {
        "ambiguous": 3,
        "confirmed": 1,
        "rejected": 0,
    }
    assert updated["phase_reference_history"] == [
        {
            "phase_reference": original["evaluation_reference"][
                "phase_reference"
            ],
            "phase_annotation": original["phase_annotation"],
            "reason": (
                "Superseded by a create-only cross-case functional-state "
                "Phase remediation; retained for provenance."
            ),
        }
    ]
    assert updated["notes"][0] == "existing note"
    assert len(updated["notes"]) == 2


def test_explicit_user_override_rebinds_phase_authority_and_preserves_history(
    tmp_path: Path,
) -> None:
    case_dir = tmp_path / "annotations/observable_tool_events/cases/0704_99"
    reports = tmp_path / "annotations/observable_tool_events/reports"
    case_dir.mkdir(parents=True)
    reports.mkdir(parents=True)
    source_phase_path = (
        case_dir / "phase_events.provisional.final.v2.jsonl"
    )
    source_phase_path.write_bytes(
        encode_jsonl(
            [
                _phase("P03", 1, 0),
                _phase("P04", 2, 1),
                _phase("P05", 3, 2),
                _phase("P06", 4, 4),
            ]
        )
    )
    source_report_path = reports / "0704_99_projection.v2.json"
    source_report_path.write_text('{"case_id":"0704_99"}\n', encoding="utf-8")
    raw_promotion = _write_explicit_override_artifacts(case_dir)
    promotion = (
        revise_provisional_phase_reference._validated_explicit_user_override(
            spec={"promotion": raw_promotion},
            case_dir=case_dir,
        )
    )
    assert promotion is not None
    assert promotion["authority"] == "explicit_user_override"
    assert promotion["source_proposal_path"].parent == case_dir
    assert promotion["procedure_catalog_path"].parent == case_dir
    assert promotion["ontology_path"].parent == case_dir.parents[1]

    current = _offline_manifest(
        case_dir=case_dir,
        source_phase_path=source_phase_path,
        source_report_path=source_report_path,
    )
    current["phase_annotation"].update(
        {
            "authority": "human_review_timeline",
            "candidate_file": "phase_candidates.ai_review.v1.jsonl",
            "candidate_sha256": "candidate-sha",
            "effective_review_status_counts": {"confirmed": 4},
            "human_decision_file": "human_review_decisions.v1.jsonl",
            "human_decision_sha256": "human-sha",
        }
    )
    current["minimal_interaction_annotation"].update(
        {
            # These descriptors also cover non-Phase interaction decisions,
            # so a Phase override must not erase them.
            "human_decision_file": "human_review_decisions.v1.jsonl",
            "human_decision_sha256": "human-sha",
        }
    )
    original = copy.deepcopy(current)
    target_phase_path = (
        case_dir / "phase_events.provisional.final.v3.jsonl"
    )
    target_report_path = reports / "0704_99_projection.v3.json"

    updated = (
        revise_provisional_phase_reference._updated_manifest_from_current(
            current_manifest=current,
            case_dir=case_dir,
            source_phase_path=source_phase_path,
            target_phase_path=target_phase_path,
            target_phase_sha256="a" * 64,
            phases=[
                _phase("P03", 1, 0),
                _phase("P04", 2, 2),
                _phase("P05", 3, 3),
                _phase("P06", 4, 4),
            ],
            source_report_path=source_report_path,
            target_report_path=target_report_path,
            target_report_sha256="b" * 64,
            promotion=promotion,
        )
    )

    assert current == original
    phase_annotation = updated["phase_annotation"]
    assert phase_annotation["authority"] == (
        revise_provisional_phase_reference.ASSISTANT_PHASE_CONTEXT_AUTHORITY
    )
    assert phase_annotation["review_authority"] == {
        "authority": "explicit_user_override",
        "authorized_by": "task-owner",
        "reviewer_ids": ["codex-gpt-5.6-sol"],
        "reviewer_kind": "ai_assistant",
    }
    for removed_field in (
        "candidate_file",
        "candidate_sha256",
        "effective_review_status_counts",
        "human_decision_file",
        "human_decision_sha256",
    ):
        assert removed_field not in phase_annotation
    assert updated["evaluation_reference"]["phase_reference"][
        "promotion_authority"
    ] == "explicit_user_override"
    assert updated["evaluation_reference"]["phase_reference"][
        "ontology_sha256"
    ] == raw_promotion["ontology_sha256"]
    assert updated["phase_reference_history"][-1]["phase_annotation"] == (
        original["phase_annotation"]
    )
    assert "explicit task-owner override" in updated[
        "phase_reference_history"
    ][-1]["reason"]
    assert updated["source_bag"] == original["source_bag"]
    assert (
        updated["minimal_interaction_annotation"]["human_decision_sha256"]
        == "human-sha"
    )


def test_explicit_user_override_rejects_case_local_path_escape(
    tmp_path: Path,
) -> None:
    case_dir = tmp_path / "annotations/observable_tool_events/cases/0704_99"
    case_dir.mkdir(parents=True)
    raw_promotion = _write_explicit_override_artifacts(case_dir)
    escaped_catalog = case_dir.parent / "escaped.yaml"
    escaped_catalog.write_text("schema: escaped\n", encoding="utf-8")
    raw_promotion["procedure_catalog_file"] = os.path.relpath(
        escaped_catalog,
        case_dir,
    )
    raw_promotion["procedure_catalog_sha256"] = _sha256(escaped_catalog)

    with pytest.raises(
        revise_provisional_phase_reference.RemediationError,
        match="path escapes its allowed root",
    ):
        revise_provisional_phase_reference._validated_explicit_user_override(
            spec={"promotion": raw_promotion},
            case_dir=case_dir,
        )


@pytest.mark.parametrize(
    "binding",
    [
        "evaluation_reference.phase_reference",
        "phase_annotation",
        "minimal_interaction_annotation",
        "evaluation_reference.projection_report",
    ],
)
def test_offline_manifest_reuse_refuses_unbound_sources(
    tmp_path: Path,
    binding: str,
) -> None:
    case_dir = tmp_path / binding.replace(".", "_") / "0704_99"
    reports = tmp_path / binding.replace(".", "_") / "reports"
    case_dir.mkdir(parents=True)
    reports.mkdir(parents=True)
    source_phase_path = (
        case_dir / "phase_events.provisional.final.v1.jsonl"
    )
    source_phase_path.write_bytes(
        encode_jsonl(
            [
                _phase("P03", 1, 0),
                _phase("P04", 2, 1),
                _phase("P05", 3, 2),
                _phase("P06", 4, 4),
            ]
        )
    )
    source_report_path = reports / "0704_99_projection.v1.json"
    source_report_path.write_text('{"case_id":"0704_99"}\n', encoding="utf-8")
    current = _offline_manifest(
        case_dir=case_dir,
        source_phase_path=source_phase_path,
        source_report_path=source_report_path,
    )
    if binding == "evaluation_reference.phase_reference":
        current["evaluation_reference"]["phase_reference"]["sha256"] = "wrong"
    elif binding == "phase_annotation":
        current["phase_annotation"]["provisional_reference_sha256"] = "wrong"
    elif binding == "minimal_interaction_annotation":
        current["minimal_interaction_annotation"][
            "provisional_phase_reference_sha256"
        ] = "wrong"
    else:
        current["evaluation_reference"]["projection_report_sha256"] = "wrong"

    with pytest.raises(
        revise_provisional_phase_reference.RemediationError,
        match="not .*requested source|not bound to the source",
    ):
        revise_provisional_phase_reference._updated_manifest_from_current(
            current_manifest=current,
            case_dir=case_dir,
            source_phase_path=source_phase_path,
            target_phase_path=(
                case_dir / "phase_events.provisional.final.v2.jsonl"
            ),
            target_phase_sha256="a" * 64,
            phases=[
                _phase("P03", 1, 0),
                _phase("P04", 2, 1),
                _phase("P05", 3, 3),
                _phase("P06", 4, 4),
            ],
            source_report_path=source_report_path,
            target_report_path=reports / "0704_99_projection.v2.json",
            target_report_sha256="b" * 64,
            promotion=None,
        )
