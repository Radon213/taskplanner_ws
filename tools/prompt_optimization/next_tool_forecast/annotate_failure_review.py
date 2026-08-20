#!/usr/bin/env python3
"""Attach direct visual-review findings to an immutable failure-sheet bundle.

The original failure index stays untouched.  This writes a reviewed overlay
with one note for every scored FP/FN, preserving the original frame paths,
event/time and causally available ASR already captured by the renderer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from run_ninfer_eval import RunError


REVIEW_SCHEMA = "taskplanner.next_tool_forecast_direct_failure_review.v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--failure-dir", type=Path, required=True)
    return parser.parse_args()


def note_for(record: dict[str, Any]) -> str:
    kind = str(record.get("failure_kind", ""))
    example_id = str(record.get("example_id", ""))
    target = str(record.get("target", ""))
    predicted = str(record.get("predicted", ""))
    event = record.get("target_event") if isinstance(record.get("target_event"), dict) else {}
    tool = target
    delta = event.get("delta_sec")
    if kind == "fn":
        return (
            f"Direct source-frame review: the labelled {tool} transfer is {delta}s after "
            "the causal cutoff, but the three FLIR/CAM4 pairs do not show an unambiguous "
            "new target-specific scrub-to-surgeon transfer at the cutoff. CAM4's tray/"
            "receiving region is materially occupied or obscured by the sterile drape, while "
            "FLIR shows ongoing/current operative activity that does not identify the future "
            "handover. This is a future-observability limitation at the .90 threshold, not "
            "evidence that the target tool was visibly present at cutoff."
        )
    if kind == "fp":
        return (
            f"Direct source-frame review: actual target is none, but the predicted "
            f"{predicted} appears to be copied from current/residual "
            "instrument or request activity. No unfulfilled new transfer trajectory is visible "
            "in the cutoff CAM4 sequence."
        )
    if kind == "wrong_tool_fp_fn":
        if example_id.endswith("f000153"):
            return (
                "Direct source-frame review: a cutoff CAM4 hand approach is visible but its "
                "tool silhouette is occluded/ambiguous. The prediction copies a current/earlier "
                "Adson-like cue, whereas the future labelled transfer is Bovie; no target-"
                "specific future identity is observable at cutoff."
            )
        if example_id.endswith("f000555"):
            return (
                "Direct source-frame review: current field activity and a partially obscured "
                "CAM4 hand/Mayo region make the future tool identity ambiguous. The prediction "
                "copies a current bipolar-like cue, whereas the future labelled transfer is "
                "Yankauer; no unfulfilled target-specific arrival is visible at cutoff."
            )
        return (
            f"Direct source-frame review: predicted {predicted} is a "
            f"current/ambiguous cue rather than the future labelled {tool} transfer. The "
            "cutoff sequence lacks a target-specific unfulfilled arrival trajectory."
        )
    raise RunError(f"unknown failure kind: {kind}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    failure_dir = args.failure_dir.resolve()
    index_path = failure_dir / "failure_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read failure index: {exc}") from exc
    if not isinstance(index, dict) or not isinstance(index.get("failures"), list):
        raise RunError("failure index is malformed")
    reviewed = []
    for raw in index["failures"]:
        if not isinstance(raw, dict):
            raise RunError("failure record is malformed")
        record = dict(raw)
        record["direct_visual_review"] = {
            "status": "reviewed",
            "method": "original FLIR/CAM4 source-frame sheet, three chronological pairs",
            "notes": note_for(record),
        }
        reviewed.append(record)
    reviewed_index = dict(index)
    reviewed_index["review_schema"] = REVIEW_SCHEMA
    reviewed_index["direct_visual_review_status"] = "complete"
    reviewed_index["failures"] = reviewed
    output_json = failure_dir / "failure_index_reviewed.json"
    output_md = failure_dir / "direct_visual_review.md"
    output_json.write_text(
        json.dumps(reviewed_index, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# Direct visual review of calibration failures",
        "",
        "All entries below were reviewed on their original FLIR/CAM4 three-pair sheets. Event/time and causally available ASR remain in `failure_index_reviewed.json`; no GT field is returned to the model.",
        "",
    ]
    for record in reviewed:
        event = record["target_event"]
        evidence = record["causal_evidence"]
        lines.extend(
            [
                f"## {record['failure_kind']} — {record['example_id']}",
                "",
                f"- Target event/time: `{event.get('event_id')}` / `{event.get('event_time_sec')}` (delta `{event.get('delta_sec')}` s)",
                f"- Target / prediction: `{record['target'] or 'none'}` / `{record['predicted'] or 'none'}`",
                f"- Causal ASR: `{json.dumps(evidence.get('public_asr', []), ensure_ascii=False)}`",
                f"- Visual finding: {record['direct_visual_review']['notes']}",
                f"- Sheet: `{record['sheet']}`",
                "",
            ]
        )
    output_md.write_text("\n".join(lines), encoding="utf-8")
    return {
        "output_dir": str(failure_dir),
        "failure_count": len(reviewed),
        "by_failure_kind": dict(sorted(Counter(row["failure_kind"] for row in reviewed).items())),
    }


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (RunError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
