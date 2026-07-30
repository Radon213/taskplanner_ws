#!/usr/bin/env python3
"""Create and validate a no-copy replay/evaluation asset map."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CASE_IDS = [f"0704_{number}" for number in range(5, 18)]
REVIEW_CASE_IDS = [f"0704_{number}" for number in range(6, 18)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize_tree(path: Path) -> tuple[int, int]:
    file_count = 0
    size_bytes = 0
    for root, _, filenames in os.walk(path):
        root_path = Path(root)
        for filename in filenames:
            candidate = root_path / filename
            try:
                size_bytes += candidate.stat().st_size
            except FileNotFoundError:
                continue
            file_count += 1
    return file_count, size_bytes


def find_checksum_manifest(path: Path) -> dict[str, str] | None:
    for filename in ("checksums.sha256", "CHECKSUMS.sha256"):
        candidate = path / filename
        if candidate.is_file():
            return {
                "path": filename,
                "sha256": sha256_file(candidate),
            }
    return None


def asset_record(
    *,
    name: str,
    path: Path,
    release_dir: Path,
    required_for: list[str],
    cases: list[str] | None = None,
    storage_mode: str = "referenced",
) -> dict[str, Any]:
    resolved = path.resolve()
    file_count, size_bytes = summarize_tree(resolved)
    payload: dict[str, Any] = {
        "name": name,
        "storage_mode": storage_mode,
        "path": str(resolved),
        "path_relative_to_release": os.path.relpath(resolved, release_dir),
        "required_for": required_for,
        "file_count": file_count,
        "size_bytes": size_bytes,
    }
    if cases is not None:
        payload["cases"] = cases
    checksum_manifest = find_checksum_manifest(resolved)
    if checksum_manifest is not None:
        payload["checksum_manifest"] = checksum_manifest
    return payload


def storage_mode_for(path: Path, release_dir: Path) -> str:
    try:
        path.resolve().relative_to(release_dir.resolve())
    except ValueError:
        return "referenced"
    return "bundled_source"


def require_file(path: Path, label: str, missing: list[str]) -> None:
    if not path.is_file():
        missing.append(f"{label}: {path}")


def validate_assets(args: argparse.Namespace) -> dict[str, Any]:
    missing: list[str] = []
    warnings: list[str] = []

    original_media = args.original_media.resolve()
    shadow_dataset = args.shadow_dataset.resolve()
    review_media = args.review_media.resolve()
    rfdetr = args.rfdetr.resolve()
    annotations = args.annotations.resolve()
    bag_root = (
        shadow_dataset / "bags"
        if (shadow_dataset / "bags").is_dir()
        else shadow_dataset
    )

    for case_id in CASE_IDS:
        for camera_number in range(1, 5):
            require_file(
                original_media
                / case_id
                / f"cam_{camera_number}"
                / "rgb.avi",
                f"{case_id} CAM{camera_number}",
                missing,
            )
        require_file(
            original_media / case_id / "flir" / "rgb.avi",
            f"{case_id} FLIR",
            missing,
        )
        require_file(
            bag_root / case_id / "metadata.yaml",
            f"{case_id} rosbag metadata",
            missing,
        )
        if not any((bag_root / case_id).glob("*.mcap")):
            missing.append(f"{case_id} rosbag MCAP: {bag_root / case_id}")
        require_file(
            annotations
            / "observable_tool_events"
            / "cases"
            / case_id
            / "annotation_manifest.json",
            f"{case_id} observable annotation",
            missing,
        )

    for case_id in REVIEW_CASE_IDS:
        for filename in (
            "review_cam2.mp4",
            "review_cam3.mp4",
            "review_cam4.mp4",
            "review_flir.mp4",
            "review_multiview.manifest.json",
        ):
            require_file(
                review_media / case_id / filename,
                f"{case_id} review media",
                missing,
            )
        require_file(
            annotations
            / "clinical_video"
            / "cases"
            / case_id
            / "clinical_manifest.v2.json",
            f"{case_id} clinical annotation",
            missing,
        )

    checkpoints = sorted(rfdetr.rglob("*.pth"))
    if not checkpoints:
        missing.append(f"RF-DETR checkpoints: {rfdetr}")

    if args.derived_bags is not None:
        derived_bags = args.derived_bags.resolve()
        if not any(derived_bags.rglob("*.mcap")):
            warnings.append(f"no derived MCAP files found: {derived_bags}")

    return {
        "schema": "taskplanner.replay_asset_map_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if not missing else "blocked_missing_required_files",
        "strict_replay_files_complete": not missing,
        "missing_required_files": missing,
        "warnings": warnings,
        "cases": CASE_IDS,
        "clinical_review_cases": REVIEW_CASE_IDS,
        "rfdetr_checkpoint_count": len(checkpoints),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--taskplanner-commit", required=True)
    parser.add_argument("--original-media", type=Path, required=True)
    parser.add_argument("--shadow-dataset", type=Path, required=True)
    parser.add_argument("--review-media", type=Path, required=True)
    parser.add_argument("--rfdetr", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--derived-bags", type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--keyframes", type=Path)
    parser.add_argument("--legacy-perception", type=Path)
    parser.add_argument("--legacy-detection", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    release_dir = args.release_dir.resolve()
    data_dir = release_dir / "data"
    if data_dir.exists():
        raise SystemExit(f"data package already exists: {data_dir}")

    required_paths = (
        args.original_media,
        args.shadow_dataset,
        args.review_media,
        args.rfdetr,
        args.annotations,
        args.reports,
    )
    missing_roots = [str(path) for path in required_paths if not path.is_dir()]
    if missing_roots:
        raise SystemExit(
            "required replay asset roots are missing:\n"
            + "\n".join(missing_roots)
        )

    validation = validate_assets(args)
    if not validation["strict_replay_files_complete"]:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return 1

    assets = [
        asset_record(
            name="original_media",
            path=args.original_media,
            release_dir=release_dir,
            required_for=["audit", "regeneration"],
            cases=CASE_IDS,
        ),
        asset_record(
            name="shadow_dataset",
            path=args.shadow_dataset,
            release_dir=release_dir,
            required_for=["replay", "evaluation"],
            cases=CASE_IDS,
        ),
        asset_record(
            name="review_media",
            path=args.review_media,
            release_dir=release_dir,
            required_for=["annotation_review"],
            cases=REVIEW_CASE_IDS,
        ),
        asset_record(
            name="rfdetr_assets",
            path=args.rfdetr,
            release_dir=release_dir,
            required_for=["live", "replay"],
        ),
        asset_record(
            name="annotations",
            path=args.annotations,
            release_dir=release_dir,
            required_for=["replay", "evaluation"],
            cases=CASE_IDS,
            storage_mode=storage_mode_for(args.annotations, release_dir),
        ),
        asset_record(
            name="evaluation_reports",
            path=args.reports,
            release_dir=release_dir,
            required_for=["audit"],
            storage_mode=storage_mode_for(args.reports, release_dir),
        ),
    ]

    optional_assets = (
        ("derived_bags", args.derived_bags, ["audit", "evaluation"]),
        ("source_audio", args.audio, ["audit", "regeneration"]),
        ("keyframes", args.keyframes, ["audit", "annotation_review"]),
        (
            "legacy_perception",
            args.legacy_perception,
            ["audit", "regeneration"],
        ),
        (
            "legacy_cam4_detection",
            args.legacy_detection,
            ["audit", "regeneration"],
        ),
    )
    for name, path, required_for in optional_assets:
        if path is None:
            continue
        if not path.is_dir():
            raise SystemExit(f"optional asset root is missing: {path}")
        assets.append(
            asset_record(
                name=name,
                path=path,
                release_dir=release_dir,
                required_for=required_for,
            )
        )

    data_dir.mkdir(parents=True)
    payload = {
        "schema": "taskplanner.replay_data_package.v2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "taskplanner_commit": args.taskplanner_commit,
        "storage_mode": "referenced",
        "copy_policy": (
            "Canonical large assets remain in one NAS location; this release "
            "contains validated paths and integrity metadata instead of a "
            "second physical copy."
        ),
        "cases": CASE_IDS,
        "clinical_review_cases": REVIEW_CASE_IDS,
        "assets": assets,
        "excluded": [
            "credentials and local environment files",
            "LM Studio, Unsloth Studio, vLLM, and NInfer model downloads",
            "Hugging Face and runtime caches",
            "transient taskplanner output and service logs",
        ],
        "handling": {
            "classification": "restricted clinical research data",
            "redistribution": "owner authorization required",
        },
    }
    (data_dir / "DATA_PACKAGE.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (data_dir / "DATA_VALIDATION.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (data_dir / "README.md").write_text(
        """# Taskplanner replay and evaluation assets

This release uses a no-copy asset map. Large clinical videos, rosbag data,
review media, and perception assets remain in their canonical NAS locations.
`DATA_PACKAGE.json` records validated absolute and release-relative paths,
file counts, byte sizes, and available checksum manifests.

This avoids duplicating the same restricted dataset through an rclone VFS
cache. Deployments should set `SHADOW_DATASET_ROOT`,
`TASKPLANNER_ANNOTATION_ROOT`, `TASKPLANNER_ANNOTATION_CACHE`, and
`RFDETR_MODEL_ROOT` from the corresponding asset records.

This is restricted clinical research data. Do not redistribute it without
explicit authorization from the data owner.
""",
        encoding="utf-8",
    )

    external_assets = {
        "schema": "taskplanner.external_assets.v2",
        "storage_mode": "referenced",
        "asset_map": "data/DATA_PACKAGE.json",
        "assets": [
            {
                "name": asset["name"],
                "required_for": asset["required_for"],
                "path": asset["path"],
                "path_relative_to_release": asset[
                    "path_relative_to_release"
                ],
                "storage_mode": asset["storage_mode"],
            }
            for asset in assets
        ],
    }
    (release_dir / "manifests" / "EXTERNAL_ASSETS.json").write_text(
        json.dumps(external_assets, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
