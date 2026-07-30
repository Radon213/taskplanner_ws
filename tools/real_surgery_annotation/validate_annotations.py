#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .event_model import (
    HAND_LOCATIONS,
    HUMAN_HOLDERS,
    derive_action,
    load_jsonl,
    load_yaml,
    sha256_file,
    state_key,
    strip_internal_fields,
)


def _error(record: dict[str, Any] | None, message: str) -> str:
    if record and "_jsonl_line" in record:
        return f"line {record['_jsonl_line']}: {message}"
    return message


def validate_records(
    records: list[dict[str, Any]],
    *,
    schema: dict[str, Any],
    tool_catalog: dict[str, Any],
    case_id: str,
    duration_sec: float,
) -> list[str]:
    errors: list[str] = []
    schema_validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )
    tools = {item["id"]: item for item in tool_catalog["tools"]}
    ids: set[str] = set()
    last_time = -1.0
    states: dict[str, dict[str, str]] = {}

    for record in records:
        public_record = strip_internal_fields(record)
        for schema_error in sorted(
            schema_validator.iter_errors(public_record),
            key=lambda item: list(item.path),
        ):
            path = ".".join(str(part) for part in schema_error.path)
            errors.append(
                _error(record, f"schema {path or '<root>'}: {schema_error.message}")
            )
        if any(True for _ in schema_validator.iter_errors(public_record)):
            continue

        if record["case_id"] != case_id:
            errors.append(
                _error(
                    record,
                    f"case_id {record['case_id']!r} does not match {case_id!r}",
                )
            )
        if record["event_id"] in ids:
            errors.append(_error(record, f"duplicate event_id {record['event_id']}"))
        ids.add(record["event_id"])

        time_sec = float(record["time_sec"])
        if time_sec < last_time:
            errors.append(
                _error(record, f"time_sec {time_sec} is earlier than {last_time}")
            )
        last_time = max(last_time, time_sec)
        if time_sec > duration_sec:
            errors.append(
                _error(record, f"time_sec {time_sec} exceeds duration {duration_sec}")
            )
        start = record.get("candidate_start_sec")
        end = record.get("candidate_end_sec")
        if start is not None and end is not None:
            if float(start) > float(end):
                errors.append(_error(record, "candidate_start_sec exceeds candidate_end_sec"))
            if not float(start) <= time_sec <= float(end):
                errors.append(
                    _error(record, "time_sec is outside the candidate interval")
                )

        tool_id = record["tool"]["id"]
        if not tool_id.startswith("unknown_tool_"):
            if tool_id not in tools:
                errors.append(_error(record, f"tool.id {tool_id!r} is not canonical"))
            elif record["tool"]["name"] != tools[tool_id]["name"]:
                errors.append(
                    _error(
                        record,
                        f"tool.name must be canonical {tools[tool_id]['name']!r}",
                    )
                )

        expected_action = derive_action(record)
        if record["derived_action"] != expected_action:
            errors.append(
                _error(
                    record,
                    f"derived_action must be {expected_action!r}, "
                    f"got {record['derived_action']!r}",
                )
            )
        event_type = record["event_type"]
        if event_type == "initial_state" and time_sec != 0.0:
            errors.append(_error(record, "initial_state must be anchored at bag t=0"))
        if event_type == "place_on_mayo" and expected_action != "place_on_mayo":
            errors.append(_error(record, "place_on_mayo event has incompatible states"))
        if event_type == "pickup_from_mayo" and expected_action != "pickup_from_mayo":
            errors.append(_error(record, "pickup_from_mayo event has incompatible states"))
        if record["review_status"] == "confirmed":
            states_to_check = [record["to"]]
            if record["from"] is not None:
                states_to_check.append(record["from"])
            if tool_id.startswith("unknown_tool_") or any(
                state["holder"] == "unknown" or state["location"] == "unknown"
                for state in states_to_check
            ):
                errors.append(
                    _error(
                        record,
                        "confirmed events cannot contain unknown tool/holder/location; "
                        "use ambiguous until source review can resolve it",
                    )
                )
        states_to_check = [record["to"]]
        if record["from"] is not None:
            states_to_check.append(record["from"])
        for state in states_to_check:
            if (
                state["location"] in HAND_LOCATIONS
                and state["holder"] not in HUMAN_HOLDERS
            ):
                errors.append(
                    _error(
                        record,
                        "a hand location requires a known human holder",
                    )
                )

        # Rejected proposals do not participate in a physical state sequence.
        if record["review_status"] == "rejected":
            continue
        key = state_key(record)
        previous = states.get(key)
        if record["event_type"] == "initial_state":
            if previous is not None:
                errors.append(_error(record, f"duplicate initial state for {key}"))
        else:
            observed_from = record["from"]
            if previous is None:
                # A transition can be the first directly observed state for an
                # instance. Requiring a synthetic t=0 state or unknown/unknown
                # would discard a visible transfer merely because the tool was
                # previously occluded or entered the camera view later.
                pass
            elif observed_from != previous:
                errors.append(
                    _error(
                        record,
                        f"state discontinuity for {key}: expected from {previous}, "
                        f"got {observed_from}",
                    )
                )
            if (
                expected_action == "pickup_from_mayo"
                and previous is not None
                and previous
                != {
                    "holder": "none",
                    "location": "mayo_stand",
                }
            ):
                errors.append(
                    _error(record, f"{key} was not previously observed on the Mayo stand")
                )
        states[key] = record["to"]

    return errors


def validate_case(
    case_dir: Path,
    schema_path: Path,
    tools_path: Path,
) -> dict[str, Any]:
    manifest_path = case_dir / "annotation_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    case_id = case_dir.name
    events_path = case_dir / manifest["event_file"]
    candidate_path = case_dir / manifest["candidate_file"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    tools = load_yaml(tools_path)
    records = load_jsonl(events_path)
    candidates = load_jsonl(candidate_path)

    errors: list[str] = []
    if manifest.get("case_id") != case_id:
        errors.append("manifest case_id does not match directory")
    if manifest.get("event_schema") != schema.get("$id"):
        errors.append("manifest event_schema does not match JSON Schema $id")
    for label, actual_path, expected in (
        ("schema", schema_path, manifest.get("schema_sha256")),
        ("tool catalog", tools_path, manifest.get("tool_catalog_sha256")),
    ):
        actual = sha256_file(actual_path)
        if expected != actual:
            errors.append(f"{label} sha256 mismatch: manifest={expected} actual={actual}")

    errors.extend(
        validate_records(
            records,
            schema=schema,
            tool_catalog=tools,
            case_id=case_id,
            duration_sec=float(manifest["duration_sec"]),
        )
    )
    errors.extend(
        f"candidate {message}"
        for message in validate_records(
            candidates,
            schema=schema,
            tool_catalog=tools,
            case_id=case_id,
            duration_sec=float(manifest["duration_sec"]),
        )
    )

    status_counts = Counter(record["review_status"] for record in records + candidates)
    expected_counts = manifest.get("review_status_counts", {})
    for status in ("proposed", "confirmed", "ambiguous", "rejected"):
        if int(expected_counts.get(status, 0)) != status_counts[status]:
            errors.append(
                f"manifest count for {status} is {expected_counts.get(status, 0)}, "
                f"actual {status_counts[status]}"
            )
    confirmed_records = [
        record for record in records if record.get("review_status") == "confirmed"
    ]
    confirmed_origin_counts = Counter(
        record["label_origin"] for record in confirmed_records
    )
    confirmed_reviewer_kind_counts = Counter(
        record["review"]["reviewer_kind"]
        for record in confirmed_records
        if record.get("review")
    )
    adjudication = manifest.get("annotation_adjudication")
    has_assistant_confirmation = bool(
        confirmed_origin_counts["assistant_video_adjudication"]
    )
    if has_assistant_confirmation and not adjudication:
        errors.append(
            "manifest annotation_adjudication is required for "
            "assistant-confirmed records"
        )
    if adjudication:
        if int(adjudication.get("confirmed_event_count", -1)) != len(
            confirmed_records
        ):
            errors.append(
                "manifest annotation_adjudication.confirmed_event_count "
                f"is {adjudication.get('confirmed_event_count')}, "
                f"actual {len(confirmed_records)}"
            )
        if dict(adjudication.get("confirmed_origin_counts", {})) != dict(
            confirmed_origin_counts
        ):
            errors.append(
                "manifest annotation_adjudication.confirmed_origin_counts "
                f"is {adjudication.get('confirmed_origin_counts', {})}, "
                f"actual {dict(confirmed_origin_counts)}"
            )
        if dict(adjudication.get("confirmed_reviewer_kind_counts", {})) != dict(
            confirmed_reviewer_kind_counts
        ):
            errors.append(
                "manifest annotation_adjudication."
                "confirmed_reviewer_kind_counts "
                f"is {adjudication.get('confirmed_reviewer_kind_counts', {})}, "
                f"actual {dict(confirmed_reviewer_kind_counts)}"
            )
        if has_assistant_confirmation and not adjudication.get("authorized_by"):
            errors.append(
                "manifest annotation_adjudication.authorized_by is required "
                "for assistant-confirmed records"
            )
    return {
        "case_id": case_id,
        "ok": not errors,
        "event_count": len(records),
        "candidate_count": len(candidates),
        "review_status_counts": dict(status_counts),
        "confirmed_origin_counts": dict(confirmed_origin_counts),
        "confirmed_reviewer_kind_counts": dict(
            confirmed_reviewer_kind_counts
        ),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = validate_case(args.case_dir, args.schema, args.tools)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
