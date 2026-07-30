#!/usr/bin/env python3
"""Validate the portable Shadow Replay data companion without changing labels."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def validate_package(data_root: Path) -> dict[str, Any]:
    data_root = data_root.resolve()
    observable_root = (
        data_root / "annotations" / "observable_tool_events"
    )
    clinical_root = data_root / "annotations" / "clinical_video"
    bag_root = data_root / "shadow_dataset" / "bags"
    original_root = data_root / "original_media"
    review_root = data_root / "review_media"
    missing: list[str] = []
    warnings: list[dict[str, str]] = []
    cases: dict[str, dict[str, Any]] = {}

    def require(path: Path) -> None:
        if not path.is_file():
            missing.append(str(path.relative_to(data_root)))

    for number in range(5, 18):
        case_id = f"0704_{number}"
        case_dir = observable_root / "cases" / case_id
        manifest_path = case_dir / "annotation_manifest.json"
        require(manifest_path)
        require(bag_root / case_id / "metadata.yaml")
        for camera_number in range(1, 5):
            require(
                original_root
                / case_id
                / f"cam_{camera_number}"
                / "rgb.avi"
            )
        require(original_root / case_id / "flir" / "rgb.avi")

        mcap_files = sorted((bag_root / case_id).glob("*.mcap"))
        if not mcap_files:
            missing.append(f"shadow_dataset/bags/{case_id}/*.mcap")

        if number >= 6:
            require(
                clinical_root
                / "cases"
                / case_id
                / "clinical_manifest.v2.json"
            )
            for filename in (
                "review_cam2.mp4",
                "review_cam3.mp4",
                "review_cam4.mp4",
                "review_flir.mp4",
                "review_multiview.manifest.json",
            ):
                require(review_root / case_id / filename)

        annotation_status = "missing"
        if manifest_path.is_file():
            manifest = load_json(manifest_path)
            annotation_status = "present"
            for label, path_key, hash_key in (
                ("schema", "schema_path", "schema_sha256"),
                (
                    "tool_catalog",
                    "tool_catalog_path",
                    "tool_catalog_sha256",
                ),
            ):
                relative = str(manifest.get(path_key, "")).strip()
                expected = str(manifest.get(hash_key, "")).strip()
                if not relative:
                    continue
                resolved = (case_dir / relative).resolve()
                try:
                    resolved.relative_to(observable_root.resolve())
                except ValueError:
                    warnings.append(
                        {
                            "case_id": case_id,
                            "kind": f"{label}_path_outside_root",
                            "detail": relative,
                        }
                    )
                    continue
                if not resolved.is_file():
                    missing.append(
                        str(resolved.relative_to(data_root))
                    )
                    continue
                actual = sha256_file(resolved)
                if expected and expected != actual:
                    warnings.append(
                        {
                            "case_id": case_id,
                            "kind": f"{label}_sha256_mismatch",
                            "detail": (
                                f"manifest={expected} actual={actual}"
                            ),
                        }
                    )
                    annotation_status = "present_with_warning"

        cases[case_id] = {
            "mcap_count": len(mcap_files),
            "annotation_status": annotation_status,
            "clinical_review_expected": number >= 6,
        }

    status = (
        "blocked_missing_required_files"
        if missing
        else (
            "complete_with_annotation_warnings"
            if warnings
            else "complete"
        )
    )
    return {
        "schema": "taskplanner.replay_data_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "strict_replay_files_complete": not missing,
        "missing_required_files": sorted(set(missing)),
        "annotation_integrity_warnings": warnings,
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_root", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = validate_package(args.data_root)
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report is None:
        print(output, end="")
    else:
        args.report.write_text(output, encoding="utf-8")
    return 0 if report["strict_replay_files_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
