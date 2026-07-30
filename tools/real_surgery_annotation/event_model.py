from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml


HUMAN_HOLDERS = {
    "surgeon",
    "operative_recipient",
    "scrub_nurse",
    "assistant",
    "circulating_nurse",
}
HAND_LOCATIONS = {
    "left_hand",
    "right_hand",
    "both_hands",
    "hand_unspecified",
}
AUTHORITATIVE_STATUSES = {"confirmed", "ambiguous"}
FLAT_ENDPOINT_STATES = {
    "mayo_stand": {"holder": "none", "location": "mayo_stand"},
    "scrub_nurse": {
        "holder": "scrub_nurse",
        "location": "hand_unspecified",
    },
    "surgeon": {"holder": "surgeon", "location": "hand_unspecified"},
}


def canonical_json(value: Any) -> str:
    """Return deterministic compact JSON suitable for std_msgs/String."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: JSONL record must be an object")
            record["_jsonl_line"] = line_number
            records.append(record)
    return records


def strip_internal_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return value


def event_tool_id(event: dict[str, Any]) -> str:
    """Return a canonical tool-type id from legacy or minimal events."""

    tool = event.get("tool")
    if isinstance(tool, dict):
        return str(tool.get("id", "") or "").strip()
    if isinstance(tool, str):
        return tool.strip()
    return ""


def event_tool_instance_id(event: dict[str, Any]) -> str:
    """Return a physical instance id when the reference actually supplies one."""

    tool = event.get("tool")
    if isinstance(tool, dict):
        return str(tool.get("instance_id", "") or "").strip()
    return str(event.get("instance_id", "") or "").strip()


def event_endpoint(event: dict[str, Any], key: str) -> dict[str, str]:
    """Normalize nested legacy states and flat minimal endpoint enums."""

    raw = event.get(key)
    if isinstance(raw, dict):
        return {
            "holder": str(raw.get("holder", "") or "").strip(),
            "location": str(raw.get("location", "") or "").strip(),
        }
    if isinstance(raw, str):
        mapped = FLAT_ENDPOINT_STATES.get(raw.strip())
        return dict(mapped) if mapped is not None else {}
    return {}


def event_endpoint_key(event: dict[str, Any], key: str) -> str:
    """Return a stable coarse state key for continuity audits."""

    raw = event.get(key)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    normalized = event_endpoint(event, key)
    holder = normalized.get("holder", "")
    location = normalized.get("location", "")
    return f"{holder}@{location}" if holder or location else ""


def state_key(event: dict[str, Any]) -> str:
    instance_id = event_tool_instance_id(event)
    tool_id = event_tool_id(event)
    if instance_id:
        return instance_id
    if tool_id:
        return f"type:{tool_id}"
    raise KeyError("event does not contain a tool id")


def derive_action(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type", "") or "").strip()
    if event_type == "initial_state":
        return "initial_state"
    if event_type == "phase_start":
        return "phase_start"
    if event_type == "implicit_tool_request":
        return "implicit_tool_request"

    source = event_endpoint(event, "from")
    target = event_endpoint(event, "to")
    from_holder = source.get("holder")
    to_holder = target.get("holder")
    from_location = source.get("location")
    to_location = target.get("location")

    if (
        from_holder == "scrub_nurse"
        and from_location in HAND_LOCATIONS
        and to_holder in {"surgeon", "operative_recipient"}
        and to_location in HAND_LOCATIONS
    ):
        return "handover"
    if (
        from_holder in {"surgeon", "operative_recipient"}
        and from_location in HAND_LOCATIONS
        and to_holder == "scrub_nurse"
        and to_location in HAND_LOCATIONS
    ):
        return "observed_direct_return"
    if (
        from_holder in HUMAN_HOLDERS
        and from_location in HAND_LOCATIONS
        and to_holder == "none"
        and to_location == "mayo_stand"
    ):
        return "place_on_mayo"
    if (
        from_holder == "none"
        and from_location == "mayo_stand"
        and to_holder in HUMAN_HOLDERS
        and to_location in HAND_LOCATIONS
    ):
        return "pickup_from_mayo"
    return "relocate"


def event_stamp_ns(event: dict[str, Any]) -> int:
    return round(float(event["time_sec"]) * 1_000_000_000)


def records_for_injection(
    records: Iterable[dict[str, Any]],
    include_statuses: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return evaluation records only; proposals never enter GT by default."""
    allowed = include_statuses or AUTHORITATIVE_STATUSES
    return [
        strip_internal_fields(record)
        for record in records
        if record.get("review_status") in allowed
    ]
