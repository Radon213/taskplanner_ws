#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


FORBIDDEN_RUNTIME_REFERENCES = {
    "ground_truth_topic": "/evaluation/ground_truth",
    "flat_case_root": "annotations/observable_tool_events/cases",
    "observed_final": "interaction_events.observed.final",
    "dt_reference_final": "interaction_events.dt_reference.final",
    "phase_reference_final": "phase_events.provisional.final",
    "evaluation_masks": "evaluation_masks.v1",
}
DEFAULT_RUNTIME_ROOTS = ("src", "webapp", "config", "docker")
RUNTIME_SCAN_EXCLUDED_PARTS = {
    "test",
    "tests",
    "__pycache__",
    "node_modules",
    "build",
    "install",
    "dist",
}
# This node is the evaluation display adapter: it may read reviewed timelines
# only to publish the operator-facing shadow ground-truth panel. Production
# decision nodes remain covered by the normal deny rule and runtime graph audit.
EVALUATION_DISPLAY_REFERENCE_ALLOWLIST = {
    "src/shadow_evaluation/shadow_evaluation/interactive_replay_controller.py": {
        "flat_case_root",
        "observed_final",
        "phase_reference_final",
    },
}
TEXT_SUFFIXES = {
    ".py",
    ".cpp",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".yaml",
    ".yml",
    ".json",
    ".xml",
    ".launch",
}


def check_boundary(repo_root: Path) -> dict:
    checked = 0
    violations: list[dict[str, object]] = []
    evaluation_display_references: list[dict[str, object]] = []
    for relative_root in DEFAULT_RUNTIME_ROOTS:
        root = repo_root / relative_root
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            relative_path = path.relative_to(repo_root)
            if any(
                part in RUNTIME_SCAN_EXCLUDED_PARTS
                for part in relative_path.parts
            ):
                continue
            checked += 1
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for line_number, line in enumerate(lines, 1):
                matches = [
                    {
                        "reference_kind": reference_kind,
                        "reference": reference,
                    }
                    for reference_kind, reference in (
                        FORBIDDEN_RUNTIME_REFERENCES.items()
                    )
                    if reference in line
                ]
                if matches:
                    allowed_reference_kinds = (
                        EVALUATION_DISPLAY_REFERENCE_ALLOWLIST.get(
                            str(relative_path),
                            set(),
                        )
                    )
                    allowed_matches = [
                        match
                        for match in matches
                        if match["reference_kind"] in allowed_reference_kinds
                    ]
                    matches = [
                        match
                        for match in matches
                        if match["reference_kind"]
                        not in allowed_reference_kinds
                    ]
                    if allowed_matches:
                        evaluation_display_references.append(
                            {
                                "path": str(relative_path),
                                "line": line_number,
                                "text": line.strip(),
                                "matches": allowed_matches,
                            }
                        )
                    if not matches:
                        continue
                    violations.append(
                        {
                            "path": str(relative_path),
                            "line": line_number,
                            "text": line.strip(),
                            "matches": matches,
                        }
                    )
    return {
        "schema": "taskplanner.ground_truth_information_boundary_check.v2",
        "ok": not violations,
        "runtime_roots": list(DEFAULT_RUNTIME_ROOTS),
        "checked_file_count": checked,
        "forbidden_runtime_references": FORBIDDEN_RUNTIME_REFERENCES,
        "evaluation_display_reference_allowlist": {
            path: sorted(reference_kinds)
            for path, reference_kinds in (
                EVALUATION_DISPLAY_REFERENCE_ALLOWLIST.items()
            )
        },
        "evaluation_display_references": evaluation_display_references,
        "violations": violations,
        "policy": (
            "No VLM, reducer, BT, skill, runtime config, or production UI "
            "consumer may resolve evaluation-only references. Offline "
            "evaluation and annotation tools live outside these runtime roots. "
            "The allow-listed shadow display adapter may read reviewed "
            "timelines only for the operator-facing ground-truth panel; "
            "runtime graph auditing protects that topic from decision nodes."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = check_boundary(args.repo.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
