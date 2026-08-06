#!/usr/bin/env python3
"""Create a compact, reproducible manifest for external replay assets."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "release" / "verification.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expand_shell_defaults(value: str, values: dict[str, str]) -> str:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

    def replace(match: re.Match[str]) -> str:
        key, fallback = match.group(1), match.group(2)
        return values.get(key) or os.environ.get(key) or (fallback or "")

    previous = ""
    while previous != value:
        previous = value
        value = pattern.sub(replace, value)
    return os.path.expanduser(value)


def load_environment(paths: list[Path]) -> dict[str, str]:
    values = dict(os.environ)
    for path in paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = _expand_shell_defaults(value.strip(), values)
    return values


def load_checksums(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if separator and len(digest) == 64:
            checksums[relative.lstrip("*")] = digest
    return checksums


def create_manifest(
    *,
    dataset_root: Path,
    annotation_root: Path,
    cases: list[str],
    verify_payloads: bool,
) -> dict[str, Any]:
    dataset_root = dataset_root.expanduser().resolve()
    package_root = dataset_root.parent if dataset_root.name == "bags" else dataset_root
    bags_root = dataset_root if dataset_root.name == "bags" else dataset_root / "bags"
    checksum_path = package_root / "checksums.sha256"
    checksums = load_checksums(checksum_path)
    missing: list[str] = []
    mismatches: list[str] = []
    case_rows: list[dict[str, Any]] = []

    for case_id in cases:
        case_root = bags_root / case_id
        metadata = case_root / "metadata.yaml"
        annotation = annotation_root / "cases" / case_id / "annotation_manifest.json"
        mcap_files = sorted(case_root.glob("*.mcap"))
        for required in (metadata, annotation):
            if not required.is_file():
                missing.append(str(required))
        if not mcap_files:
            missing.append(str(case_root / "*.mcap"))

        files: list[dict[str, Any]] = []
        for path in [metadata, *mcap_files]:
            if not path.is_file():
                continue
            relative = path.relative_to(package_root).as_posix()
            expected = checksums.get(relative, "")
            actual = sha256_file(path) if verify_payloads else ""
            if verify_payloads and expected and actual != expected:
                mismatches.append(relative)
            files.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "sha256": expected or actual,
                    "hash_source": "dataset_checksums" if expected else "computed",
                    "payload_verified": bool(verify_payloads),
                }
            )
        case_rows.append(
            {
                "case_id": case_id,
                "files": files,
                "annotation_manifest": str(annotation.resolve()),
                "annotation_sha256": sha256_file(annotation) if annotation.is_file() else "",
            }
        )

    return {
        "schema": "taskplanner.external_replay_assets.v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "complete" if not missing and not mismatches else "invalid",
        "dataset_root": str(dataset_root),
        "annotation_root": str(annotation_root.resolve()),
        "dataset_checksum_manifest": {
            "path": str(checksum_path),
            "sha256": sha256_file(checksum_path) if checksum_path.is_file() else "",
        },
        "payload_hashes_verified": verify_payloads,
        "missing": missing,
        "checksum_mismatches": mismatches,
        "cases": case_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset-root", type=Path)
    parser.add_argument("--annotation-root", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--verify-payloads", action="store_true")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    environment = load_environment([ROOT / ".env.example", ROOT / ".env"])
    dataset_root = args.dataset_root or Path(
        environment.get("SHADOW_DATASET_ROOT", "")
    )
    annotation_root = args.annotation_root or Path(
        environment.get(
            "TASKPLANNER_ANNOTATION_ROOT",
            str(ROOT / "annotations" / "observable_tool_events"),
        )
    )
    if not str(dataset_root):
        raise SystemExit("SHADOW_DATASET_ROOT is not configured")

    payload = create_manifest(
        dataset_root=dataset_root,
        annotation_root=annotation_root,
        cases=list(config["cases"]),
        verify_payloads=args.verify_payloads,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if payload["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
