from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from tools.real_surgery_annotation import bind_phase_generalization_catalog
from tools.real_surgery_annotation.finalize_interaction_review import (
    encode_jsonl,
)
from tools.real_surgery_annotation.publish_assistant_case_reference import (
    encode_json,
)


ROOT = Path(__file__).resolve().parents[2]
REAL_COMMON_CATALOG = (
    ROOT
    / "annotations/observable_tool_events/"
    "procedure_phases.cross_case_provisional.v3.yaml"
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _phase(case_id: str, phase_id: str, ordinal: int, frame: int) -> dict:
    return {
        "schema": "taskplanner.observable_interaction_point.v1",
        "case_id": case_id,
        "event_id": f"{case_id}-PH{ordinal:04d}",
        "event_type": "phase_start",
        "phase_id": phase_id,
        "time_sec": frame / 10.0,
        "source_frame_idx": frame,
        "source_views": ["cam4", "flir"],
        "phase_boundary_kind": (
            "clip_initial_state" if frame == 0 else "observed_transition"
        ),
        "review_status": "ambiguous",
        "label_origin": "assistant_video_adjudication",
        "review": {
            "reviewer_kind": "ai_assistant",
            "reviewer_id": "codex-gpt-5.6-sol",
            "reviewed_at": "2026-07-29T00:00:00+09:00",
            "authorized_by": "task_owner_user_request_2026-07-29",
            "notes": f"{phase_id} exact visible functional-state onset",
        },
    }


def _fixture(tmp_path: Path, *, case_id: str = "0704_99") -> tuple[
    Path,
    Path,
    Path,
    bytes,
]:
    annotation_root = tmp_path / "annotations/observable_tool_events"
    case_dir = annotation_root / "cases" / case_id
    case_dir.mkdir(parents=True)
    common_path = (
        annotation_root
        / bind_phase_generalization_catalog.COMMON_CATALOG_NAME
    )
    common_path.write_bytes(REAL_COMMON_CATALOG.read_bytes())
    phase_path = case_dir / "phase_events.provisional.final.v9.jsonl"
    phase_data = encode_jsonl(
        [
            _phase(case_id, "P03", 1, 0),
            _phase(case_id, "P04", 2, 100),
            _phase(case_id, "P05", 3, 200),
            _phase(case_id, "P06", 4, 300),
        ]
    )
    phase_path.write_bytes(phase_data)
    old_catalog = case_dir / "procedure_phases.ai_review.v1.yaml"
    old_catalog.write_text("schema: old-case-catalog\n", encoding="utf-8")
    manifest = {
        "schema": "taskplanner.observable_annotation_manifest.v1",
        "case_id": case_id,
        "source_bag": {
            "path": "/offline/source.mcap",
            "sha256": "bag-sha",
        },
        "evaluation_reference": {
            "observed_reference": {
                "file": "observed.jsonl",
                "sha256": "observed-sha",
            },
            "dt_reference": {
                "file": "dt.jsonl",
                "sha256": "dt-sha",
            },
            "phase_reference": {
                "file": phase_path.name,
                "sha256": _sha256_bytes(phase_data),
                "event_count": 4,
                "event_type_counts": {"phase_start": 4},
                "review_status_counts": {"ambiguous": 4},
                "scoring_role": "context_only_not_ground_truth",
            },
            "projection_report_file": "../../reports/projection.v9.json",
            "projection_report_sha256": "projection-report-sha",
        },
        "phase_annotation": {
            "authority": (
                "user_authorized_ai_assistant_video_adjudication_"
                "provisional_context_not_scoring_ground_truth"
            ),
            "complete": True,
            "event_count": 4,
            "procedure_catalog_file": old_catalog.name,
            "procedure_catalog_sha256": hashlib.sha256(
                old_catalog.read_bytes()
            ).hexdigest(),
            "procedure_catalog_runtime_status": (
                "evaluation_only_draft_not_frozen"
            ),
            "provisional_reference_file": phase_path.name,
            "provisional_reference_sha256": _sha256_bytes(phase_data),
            "review_complete": True,
            "review_status_counts": {
                "ambiguous": 4,
                "confirmed": 0,
                "rejected": 0,
            },
            "scoring_reference_ready": False,
        },
        "unrelated_extension": {
            "clinical": "clinical_annotations.final.v2.jsonl",
            "detector": "rfdetr",
        },
    }
    manifest_data = encode_json(manifest)
    manifest_path = case_dir / "annotation_manifest.json"
    manifest_path.write_bytes(manifest_data)
    return case_dir, common_path, phase_path, manifest_data


def test_binding_derives_functional_catalog_and_preserves_manifest(
    tmp_path: Path,
) -> None:
    case_dir, common_path, phase_path, manifest_before = _fixture(tmp_path)
    phase_before = phase_path.read_bytes()
    original = json.loads(manifest_before)

    result = bind_phase_generalization_catalog.bind_case(
        case_dir=case_dir,
        common_catalog_path=common_path,
        bound_at="2026-07-29T12:00:00+00:00",
    )

    assert result["ok"] is True
    assert result["already_bound"] is False
    assert phase_path.read_bytes() == phase_before

    archive_path = (
        case_dir / bind_phase_generalization_catalog.ARCHIVE_RELATIVE
    )
    assert archive_path.read_bytes() == manifest_before
    updated = json.loads(
        (case_dir / "annotation_manifest.json").read_text(encoding="utf-8")
    )
    assert (
        bind_phase_generalization_catalog._without_catalog_bindings(updated)
        == bind_phase_generalization_catalog._without_catalog_bindings(
            original
        )
    )
    assert updated["evaluation_reference"]["projection_report_file"] == (
        original["evaluation_reference"]["projection_report_file"]
    )
    assert updated["evaluation_reference"]["projection_report_sha256"] == (
        original["evaluation_reference"]["projection_report_sha256"]
    )
    assert updated["evaluation_reference"]["phase_reference"]["file"] == (
        phase_path.name
    )
    assert updated["evaluation_reference"]["phase_reference"]["sha256"] == (
        _sha256_bytes(phase_before)
    )

    catalog_path = (
        case_dir / bind_phase_generalization_catalog.LOCAL_CATALOG_NAME
    )
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    assert catalog["source_phase_reference"] == {
        "file": phase_path.name,
        "sha256": _sha256_bytes(phase_before),
        "event_count": 4,
        "annotation_frames": {
            "P03": 0,
            "P04": 100,
            "P05": 200,
            "P06": 300,
        },
    }
    phases = {row["phase_id"]: row for row in catalog["phases"]}
    assert list(phases) == ["P03", "P04", "P05", "P06"]
    for phase_id, frame in zip(phases, (0, 100, 200, 300)):
        assert phases[phase_id]["case_observation"][
            "annotation_frame"
        ] == frame
        assert phases[phase_id]["case_observation"][
            "observed_key_frame"
        ] == frame
        assert phases[phase_id]["annotation_note"].startswith(phase_id)
    assert phases["P06"]["name"] == (
        "focal_target_control_and_treatment"
    )
    assert "neither required nor sufficient" in phases["P06"][
        "tool_pattern"
    ]

    ontology = updated["evaluation_reference"]["phase_reference"]
    assert ontology["ontology_file"] == (
        "../../procedure_phases.cross_case_provisional.v3.yaml"
    )
    assert ontology["ontology_sha256"] == hashlib.sha256(
        common_path.read_bytes()
    ).hexdigest()
    assert updated["phase_annotation"]["procedure_catalog_file"] == (
        bind_phase_generalization_catalog.LOCAL_CATALOG_NAME
    )
    assert updated["phase_annotation"]["procedure_catalog_sha256"] == (
        hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    )

    audit = json.loads(
        (
            case_dir / bind_phase_generalization_catalog.AUDIT_NAME
        ).read_text(encoding="utf-8")
    )
    assert audit["canonical_phase_reference"]["annotation_frames"] == {
        "P03": 0,
        "P04": 100,
        "P05": 200,
        "P06": 300,
    }
    assert audit["preservation"] == {
        "canonical_phase_file_unchanged": True,
        "canonical_phase_sha256_unchanged": True,
        "projection_report_descriptor_unchanged": True,
        "all_non_catalog_manifest_values_unchanged": True,
    }


def test_binding_is_idempotent_after_success(tmp_path: Path) -> None:
    case_dir, common_path, _phase_path, _manifest_before = _fixture(tmp_path)
    first = bind_phase_generalization_catalog.bind_case(
        case_dir=case_dir,
        common_catalog_path=common_path,
        bound_at="2026-07-29T12:00:00+00:00",
    )
    manifest_after = (case_dir / "annotation_manifest.json").read_bytes()
    catalog_after = (
        case_dir / bind_phase_generalization_catalog.LOCAL_CATALOG_NAME
    ).read_bytes()

    second = bind_phase_generalization_catalog.bind_case(
        case_dir=case_dir,
        common_catalog_path=common_path,
        bound_at="2099-01-01T00:00:00+00:00",
    )

    assert first["already_bound"] is False
    assert second["already_bound"] is True
    assert (case_dir / "annotation_manifest.json").read_bytes() == (
        manifest_after
    )
    assert (
        case_dir / bind_phase_generalization_catalog.LOCAL_CATALOG_NAME
    ).read_bytes() == catalog_after


def test_binding_refuses_case_0704_6(tmp_path: Path) -> None:
    case_dir, common_path, _phase_path, _manifest_before = _fixture(
        tmp_path,
        case_id="0704_6",
    )
    with pytest.raises(
        bind_phase_generalization_catalog.CatalogBindingError,
        match="outside this batch",
    ):
        bind_phase_generalization_catalog.bind_case(
            case_dir=case_dir,
            common_catalog_path=common_path,
        )


def test_versioned_rebinding_tracks_promoted_phase_without_overwriting_v3(
    tmp_path: Path,
) -> None:
    case_dir, common_path, _phase_path, _manifest_before = _fixture(tmp_path)
    bind_phase_generalization_catalog.bind_case(
        case_dir=case_dir,
        common_catalog_path=common_path,
        bound_at="2026-07-29T12:00:00+00:00",
    )
    original_catalog_path = (
        case_dir / bind_phase_generalization_catalog.LOCAL_CATALOG_NAME
    )
    original_audit_path = (
        case_dir / bind_phase_generalization_catalog.AUDIT_NAME
    )
    original_archive_path = (
        case_dir / bind_phase_generalization_catalog.ARCHIVE_RELATIVE
    )
    original_catalog = original_catalog_path.read_bytes()
    original_audit = original_audit_path.read_bytes()
    original_archive = original_archive_path.read_bytes()

    promoted_path = case_dir / "phase_events.provisional.final.v10.jsonl"
    promoted_data = encode_jsonl(
        [
            _phase(case_dir.name, "P03", 1, 0),
            _phase(case_dir.name, "P04", 2, 101),
            _phase(case_dir.name, "P05", 3, 201),
            _phase(case_dir.name, "P06", 4, 301),
        ]
    )
    promoted_path.write_bytes(promoted_data)
    promoted_sha256 = _sha256_bytes(promoted_data)
    manifest_path = case_dir / "annotation_manifest.json"
    promoted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    promoted_manifest["evaluation_reference"]["phase_reference"].update(
        {
            "file": promoted_path.name,
            "sha256": promoted_sha256,
        }
    )
    promoted_manifest["phase_annotation"].update(
        {
            "provisional_reference_file": promoted_path.name,
            "provisional_reference_sha256": promoted_sha256,
        }
    )
    manifest_path.write_bytes(encode_json(promoted_manifest))

    versioned_catalog = (
        "catalog_versions/"
        "procedure_phases.generalization.v3.phase-v10.yaml"
    )
    versioned_audit = (
        "audit_versions/"
        "phase_catalog_generalization.audit.phase-v10.json"
    )
    versioned_archive = (
        "audit_archive/phase_catalog_generalization_phase_v10/"
        "annotation_manifest.before_catalog_phase_v10.json"
    )
    first = bind_phase_generalization_catalog.bind_case(
        case_dir=case_dir,
        common_catalog_path=common_path,
        catalog_relative=versioned_catalog,
        audit_relative=versioned_audit,
        archive_relative=versioned_archive,
        bound_at="2026-07-29T13:00:00+00:00",
    )
    second = bind_phase_generalization_catalog.bind_case(
        case_dir=case_dir,
        common_catalog_path=common_path,
        catalog_relative=versioned_catalog,
        audit_relative=versioned_audit,
        archive_relative=versioned_archive,
        bound_at="2099-01-01T00:00:00+00:00",
    )

    assert first["already_bound"] is False
    assert second["already_bound"] is True
    assert original_catalog_path.read_bytes() == original_catalog
    assert original_audit_path.read_bytes() == original_audit
    assert original_archive_path.read_bytes() == original_archive
    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert updated["phase_annotation"]["procedure_catalog_file"] == (
        versioned_catalog
    )
    catalog = yaml.safe_load(
        (case_dir / versioned_catalog).read_text(encoding="utf-8")
    )
    assert catalog["source_phase_reference"] == {
        "file": promoted_path.name,
        "sha256": promoted_sha256,
        "event_count": 4,
        "annotation_frames": {
            "P03": 0,
            "P04": 101,
            "P05": 201,
            "P06": 301,
        },
    }
    audit = json.loads(
        (case_dir / versioned_audit).read_text(encoding="utf-8")
    )
    assert audit["archived_manifest_file"] == versioned_archive
    assert audit["manifest_bindings_before"][
        "procedure_catalog_file"
    ] == bind_phase_generalization_catalog.LOCAL_CATALOG_NAME
    assert audit["manifest_bindings_after"][
        "procedure_catalog_file"
    ] == versioned_catalog


@pytest.mark.parametrize(
    ("argument", "unsafe_value"),
    [
        ("catalog_relative", "../escaped.yaml"),
        ("audit_relative", "/tmp/escaped.json"),
        (
            "archive_relative",
            "audit_archive/../../escaped.json",
        ),
    ],
)
def test_versioned_binding_rejects_path_escape(
    tmp_path: Path,
    argument: str,
    unsafe_value: str,
) -> None:
    case_dir, common_path, _phase_path, _manifest_before = _fixture(tmp_path)
    arguments = {
        "case_dir": case_dir,
        "common_catalog_path": common_path,
        argument: unsafe_value,
    }
    with pytest.raises(
        bind_phase_generalization_catalog.CatalogBindingError,
        match="case-local relative path",
    ):
        bind_phase_generalization_catalog.bind_case(**arguments)
    assert not (
        case_dir / bind_phase_generalization_catalog.LOCAL_CATALOG_NAME
    ).exists()


def test_versioned_binding_refuses_existing_unbound_output(
    tmp_path: Path,
) -> None:
    case_dir, common_path, _phase_path, manifest_before = _fixture(tmp_path)
    catalog_relative = "procedure_phases.generalization.v3.phase-v10.yaml"
    (case_dir / catalog_relative).write_text(
        "schema: conflicting-create-only-output\n",
        encoding="utf-8",
    )

    with pytest.raises(
        bind_phase_generalization_catalog.CatalogBindingError,
        match="create-only binding target exists",
    ):
        bind_phase_generalization_catalog.bind_case(
            case_dir=case_dir,
            common_catalog_path=common_path,
            catalog_relative=catalog_relative,
            audit_relative=(
                "phase_catalog_generalization.audit.phase-v10.json"
            ),
            archive_relative=(
                "audit_archive/phase_catalog_generalization_phase_v10/"
                "annotation_manifest.before_catalog_phase_v10.json"
            ),
        )
    assert (case_dir / "annotation_manifest.json").read_bytes() == (
        manifest_before
    )


def test_update_changes_only_the_four_catalog_binding_fields() -> None:
    manifest = {
        "phase_annotation": {
            "procedure_catalog_file": "old.yaml",
            "procedure_catalog_sha256": "old",
            "other": copy.deepcopy({"nested": [1, 2, 3]}),
        },
        "evaluation_reference": {
            "phase_reference": {
                "file": "phase.jsonl",
                "sha256": "phase-sha",
            },
            "projection_report_sha256": "report-sha",
        },
        "top": {"untouched": True},
    }
    updated = bind_phase_generalization_catalog.updated_manifest(
        manifest=manifest,
        catalog_file="catalog_versions/generalization.phase-v10.yaml",
        catalog_sha256="a" * 64,
        ontology_file="../../ontology.yaml",
        ontology_sha256="b" * 64,
    )

    assert manifest["phase_annotation"]["procedure_catalog_file"] == "old.yaml"
    assert (
        bind_phase_generalization_catalog._without_catalog_bindings(updated)
        == bind_phase_generalization_catalog._without_catalog_bindings(
            manifest
        )
    )
    assert updated["phase_annotation"]["procedure_catalog_file"] == (
        "catalog_versions/generalization.phase-v10.yaml"
    )
    assert updated["evaluation_reference"]["phase_reference"][
        "ontology_file"
    ] == "../../ontology.yaml"
