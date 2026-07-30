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
    for relative_root in DEFAULT_RUNTIME_ROOTS:
        root = repo_root / relative_root
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in {"node_modules", "build", "install", "dist"} for part in path.parts):
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
                    violations.append(
                        {
                            "path": str(path.relative_to(repo_root)),
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
        "violations": violations,
        "policy": (
            "No VLM, reducer, BT, skill, runtime config, or production UI "
            "consumer may resolve evaluation-only references. Offline "
            "evaluation and annotation tools live outside these runtime roots."
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
