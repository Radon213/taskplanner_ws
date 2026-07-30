#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .event_model import canonical_json, load_jsonl, load_yaml
from .validate_annotations import validate_records


def normalize_candidates(
    *,
    raw_records: list[dict[str, Any]],
    query_spec: dict[str, Any],
    case_id: str,
    duration_sec: float,
    start_index: int = 1000,
) -> list[dict[str, Any]]:
    queries = {item["id"]: item for item in query_spec["queries"]}
    output: list[dict[str, Any]] = []
    for offset, raw in enumerate(raw_records):
        query_id = raw.get("query_id")
        if query_id not in queries:
            raise ValueError(f"unknown TimeLens2 query_id: {query_id!r}")
        start = float(raw["candidate_start_sec"])
        end = float(raw["candidate_end_sec"])
        confidence_value = raw.get("confidence")
        confidence = (
            None if confidence_value is None else float(confidence_value)
        )
        if start < 0 or end < start or end > duration_sec:
            raise ValueError(
                f"invalid interval for {query_id}: [{start}, {end}] "
                f"outside [0, {duration_sec}]"
            )
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError(f"confidence outside [0,1]: {confidence}")
        index = start_index + offset
        # Temporal grounding proposes only a time interval and action phrase.
        # It does not establish physical holder/location or tool identity.
        # Keep those fields unknown until a person reviews synchronized video.
        proposal = {
            "generator": "timelens2_temporal_grounding",
            "query": queries[query_id]["text"],
            "model_version": str(raw.get("model_version", "unspecified")),
        }
        if confidence is not None:
            proposal["confidence"] = confidence
        output.append(
            {
                "schema": "taskplanner.observable_tool_event.v1",
                "case_id": case_id,
                "event_id": f"{case_id}-P{index:04d}",
                "event_type": "tool_transfer",
                "time_sec": end,
                "candidate_start_sec": start,
                "candidate_end_sec": end,
                "tool": {
                    "id": f"unknown_tool_{index:02d}",
                    "name": "Unidentified surgical instrument",
                    "instance_id": f"{case_id}-tool-timelens2_{index:04d}",
                },
                "from": {"holder": "unknown", "location": "unknown"},
                "to": {"holder": "unknown", "location": "unknown"},
                "derived_action": "relocate",
                "source_views": raw.get("source_views", ["cam4", "flir"]),
                "visibility": "partial",
                "review_status": "proposed",
                "label_origin": "temporal_grounding_model",
                "proposal": proposal,
                "notes": (
                    f"Query suggests {queries[query_id]['suggested_action']}; "
                    "event type, exact completion time, tool, holder, and location "
                    "must be resolved by human CAM4/FLIR review."
                ),
            }
        )
    output.sort(
        key=lambda item: (
            float(item["candidate_start_sec"]),
            float(item["candidate_end_sec"]),
            item["event_id"],
        )
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize TimeLens2 intervals into non-GT proposed events."
    )
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--start-index", type=int, default=1000)
    args = parser.parse_args()

    records = normalize_candidates(
        raw_records=load_jsonl(args.raw),
        query_spec=load_yaml(args.queries),
        case_id=args.case_id,
        duration_sec=args.duration_sec,
        start_index=args.start_index,
    )
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    tools = load_yaml(args.tools)
    errors = validate_records(
        records,
        schema=schema,
        tool_catalog=tools,
        case_id=args.case_id,
        duration_sec=args.duration_sec,
    )
    report = {
        "schema": "taskplanner.timelens2_candidate_generation_report.v1",
        "case_id": args.case_id,
        "ok": not errors,
        "raw_interval_count": len(records),
        "proposed_candidate_count": len(records),
        "ground_truth_event_count": 0,
        "human_confirmation_required": True,
        "errors": errors,
    }
    if errors:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(canonical_json(item) + "\n" for item in records),
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
