#!/usr/bin/env python3
"""Evaluate Taskplanner shadow traces without exposing reference labels at runtime."""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import csv
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable
import unicodedata

import yaml

from .event_model import (
    derive_action,
    event_endpoint,
    event_endpoint_key,
    event_tool_id,
    event_tool_instance_id,
    load_jsonl,
    strip_internal_fields,
)
from .shadow_contract import (
    BEHAVIOR_QUALITY_SCHEMA,
    RUN_MODES,
    load_jsonl as load_trace_jsonl,
    resolve_case_evaluation_mask,
    resolve_case_phase_context,
    resolve_case_reference,
    resolve_case_tool_catalog,
    sha256_file as shadow_sha256_file,
    validate_behavior_quality_report,
    validate_trace_records,
)


EVALUATION_SCHEMA = "taskplanner.observable_shadow_evaluation.v2"
PREDICTION_LAYERS = (
    "vlm_model_raw",
    "vlm_raw",
    "reducer_fused",
    "bt_decision",
    "skill_command",
)
UNSAFE_STATUSES = {"unsafe", "physically_impossible", "blocked_invariant"}
HANDOVER_ACTIONS = {
    "handover",
    "direct_handover",
    "pick_up_and_handover",
    "pick_up_from_mayo_and_handover",
    "put_down_and_handover",
    "predict_tool",
    "prepare_tool",
}
RECOVERY_ACTIONS = {"retrieve_from_mayo", "recover", "recovery"}
PREPARATION_ACTIONS = {"predict_tool", "prepare_tool"}
UNUSED_PREPOSITION_RETURN_ACTIONS = {"return_unused_preposition"}
# Confirmed request boundaries come from a different observer than runtime
# speech/vision evidence. Admit only a narrow early match so a response at the
# boundary is not mislabeled as missed, while preserving the signed offset.
REQUEST_HANDOVER_EARLY_MATCH_TOLERANCE_SEC = 1.5
REQUEST_EVENT_TYPES = {
    "explicit_tool_request",
    "implicit_tool_request",
    "request_tool",
    "surgeon_tool_request",
    "tool_request",
    "voice_tool_request",
}
BED_ROBOT_GROUP_LAYERS = {
    "bed_robot_arm_group_status",
    "bed_robot_arm_group_request",
    "bed_robot_arm_group_command",
}
RETRACTION_GROUP_ID = "retraction"
RETRACTION_TOOL_CHANGE_OPERATIONS = {
    "change_end_effector",
    "tool_change",
}
EVALUATION_MASK_SCHEMAS = {
    "taskplanner.evaluation_masks.v1",
    "taskplanner.evaluation_masks.v2",
}
METRIC_ELIGIBILITY_KEYS = (
    "action",
    "latency",
    "state",
    "physical",
    "reuse",
)
SCORING_ROLE_DEFAULTS = {
    "action_target": {
        "action": True,
        "latency": True,
        "state": False,
        "physical": False,
        "reuse": False,
    },
    "compound_action_substep": {
        "action": False,
        "latency": False,
        "state": False,
        "physical": False,
        "reuse": False,
    },
    "state_observation_only": {
        "action": False,
        "latency": False,
        "state": True,
        "physical": False,
        "reuse": False,
    },
    "gesture_target": {
        "action": False,
        "latency": False,
        "state": False,
        "physical": False,
        "reuse": False,
    },
    "context_only_not_ground_truth": {
        "action": False,
        "latency": False,
        "state": False,
        "physical": False,
        "reuse": False,
    },
    "not_scorable": {
        "action": False,
        "latency": False,
        "state": False,
        "physical": False,
        "reuse": False,
    },
}
SCORING_ROLE_ALIASES: dict[str, str] = {}
SCORING_ROLE_ALLOWED_METRICS = {
    "action_target": {"action", "latency"},
    "compound_action_substep": set(),
    "state_observation_only": {"state", "physical", "reuse"},
    "gesture_target": set(),
    "context_only_not_ground_truth": set(),
    "not_scorable": set(),
}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _metric_eligibility(
    payload: Any,
    *,
    base: dict[str, bool],
    location: str,
) -> dict[str, bool]:
    if payload is None:
        return dict(base)
    if not isinstance(payload, dict):
        raise ValueError(f"{location} must be an object")
    result = dict(base)
    for key in METRIC_ELIGIBILITY_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, bool):
            raise ValueError(f"{location}.{key} must be boolean")
        result[key] = value
    return result


class EvaluationMask:
    """Validated, fail-closed scoring metadata for a reference timeline."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload
        self.present = payload is not None
        self.case_id = ""
        self.evaluation_scope: dict[str, Any] = {}
        self.issues: list[str] = []
        self.event_roles: dict[str, dict[str, Any]] = {}
        self.interval_masks: list[dict[str, Any]] = []
        self.cutoffs: dict[str, float] = {}
        self.tool_metric_scopes: dict[str, dict[str, Any]] = {}

        if payload is None:
            self.default_metric_eligibility = {
                "action": True,
                "latency": True,
                "state": True,
                "physical": True,
                "reuse": True,
            }
            return
        if not isinstance(payload, dict):
            raise ValueError("evaluation mask must be an object")
        if payload.get("schema") not in EVALUATION_MASK_SCHEMAS:
            raise ValueError(
                "evaluation mask schema must be one of "
                + ", ".join(sorted(EVALUATION_MASK_SCHEMAS))
            )
        self.case_id = _clean(payload.get("case_id"))
        raw_scope = payload.get("evaluation_scope")
        if raw_scope is not None:
            if not isinstance(raw_scope, dict):
                raise ValueError("evaluation_scope must be an object")
            held_out_eligible = raw_scope.get("held_out_eligible")
            if (
                held_out_eligible is not None
                and not isinstance(held_out_eligible, bool)
            ):
                raise ValueError(
                    "evaluation_scope.held_out_eligible must be boolean"
                )
            self.evaluation_scope = {
                "classification": _clean(
                    raw_scope.get("classification")
                )
                or None,
                "held_out_eligible": held_out_eligible,
                "reason": _clean(raw_scope.get("reason")) or None,
            }
        self.default_metric_eligibility = _metric_eligibility(
            payload.get("default_metric_eligibility"),
            base={
                "action": True,
                "latency": True,
                "state": False,
                "physical": False,
                "reuse": False,
            },
            location="default_metric_eligibility",
        )
        self._load_event_roles(payload.get("event_roles", []))
        self._load_interval_masks(payload.get("interval_masks", []))
        self._load_cutoffs(payload.get("cutoffs", {}))
        self._load_tool_metric_scopes(payload.get("tool_metric_scopes", []))

    def _load_event_roles(self, value: Any) -> None:
        if isinstance(value, dict):
            rows: list[dict[str, Any]] = []
            for event_id, descriptor in value.items():
                if isinstance(descriptor, str):
                    rows.append({"event_id": event_id, "role": descriptor})
                elif isinstance(descriptor, dict):
                    rows.append({**descriptor, "event_id": event_id})
                else:
                    raise ValueError(
                        f"event_roles.{event_id} must be a role string or object"
                    )
        elif isinstance(value, list):
            rows = value
        else:
            raise ValueError("event_roles must be an array or object")

        for index, row in enumerate(rows):
            location = f"event_roles[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{location} must be an object")
            event_id = _clean(row.get("event_id"))
            if not event_id:
                raise ValueError(f"{location}.event_id is required")
            if event_id in self.event_roles:
                raise ValueError(f"duplicate event role for {event_id}")
            raw_role = _clean(row.get("role"))
            role = SCORING_ROLE_ALIASES.get(raw_role, raw_role)
            if role not in SCORING_ROLE_DEFAULTS:
                self.issues.append(
                    f"{event_id}: unknown scoring role {raw_role!r}; failed closed"
                )
                role = "not_scorable"
            eligibility = _metric_eligibility(
                row.get("metric_eligibility"),
                base=SCORING_ROLE_DEFAULTS[role],
                location=f"{location}.metric_eligibility",
            )
            allowed_metrics = SCORING_ROLE_ALLOWED_METRICS[role]
            eligibility = {
                key: bool(value and key in allowed_metrics)
                for key, value in eligibility.items()
            }
            self.event_roles[event_id] = {
                "role": role,
                "metric_eligibility": eligibility,
                "reason": _clean(row.get("reason")),
            }

    def _load_interval_masks(self, value: Any) -> None:
        if not isinstance(value, list):
            raise ValueError("interval_masks must be an array")
        for index, row in enumerate(value):
            location = f"interval_masks[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{location} must be an object")
            try:
                start_sec = float(row["start_sec"])
                end_sec = float(row["end_sec"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"{location}.start_sec/end_sec must be numeric"
                ) from exc
            if (
                not math.isfinite(start_sec)
                or not math.isfinite(end_sec)
                or start_sec < 0.0
                or end_sec < start_sec
            ):
                raise ValueError(f"{location} has an invalid interval")
            eligibility = _metric_eligibility(
                row.get("metric_eligibility"),
                base={key: True for key in METRIC_ELIGIBILITY_KEYS},
                location=f"{location}.metric_eligibility",
            )
            self.interval_masks.append(
                {
                    "mask_id": _clean(row.get("mask_id")) or f"mask-{index + 1}",
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "metric_eligibility": eligibility,
                    "reason": _clean(row.get("reason")),
                }
            )

    def _load_cutoffs(self, value: Any) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError("cutoffs must be an object")
        for key in (
            "action_and_next_tool_end_sec",
            "state_audit_end_sec",
            "visual_end_sec",
        ):
            if key not in value:
                continue
            try:
                cutoff = float(value[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"cutoffs.{key} must be numeric") from exc
            if not math.isfinite(cutoff) or cutoff < 0.0:
                raise ValueError(f"cutoffs.{key} must be finite and non-negative")
            self.cutoffs[key] = cutoff

    def _load_tool_metric_scopes(self, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            rows = [
                {**descriptor, "tool": tool_id}
                for tool_id, descriptor in value.items()
                if isinstance(descriptor, dict)
            ]
            if len(rows) != len(value):
                raise ValueError(
                    "tool_metric_scopes values must be objects"
                )
        elif isinstance(value, list):
            rows = value
        else:
            raise ValueError("tool_metric_scopes must be an array or object")

        allowed_resolutions = {
            "resolved",
            "unresolved_multiple_instances",
            "initial_inventory_unavailable",
        }
        for index, row in enumerate(rows):
            location = f"tool_metric_scopes[{index}]"
            if not isinstance(row, dict):
                raise ValueError(f"{location} must be an object")
            tool_id = _clean(row.get("tool"))
            if not tool_id:
                raise ValueError(f"{location}.tool is required")
            if tool_id in self.tool_metric_scopes:
                raise ValueError(f"duplicate tool metric scope for {tool_id}")
            resolution = _clean(row.get("instance_resolution"))
            if resolution not in allowed_resolutions:
                self.issues.append(
                    f"{tool_id}: unknown instance resolution "
                    f"{resolution!r}; physical metrics failed closed"
                )
                resolution = "initial_inventory_unavailable"
            eligibility: dict[str, bool] = {}
            for metric in ("state", "physical", "reuse"):
                raw_value = row.get(metric, False)
                if not isinstance(raw_value, bool):
                    raise ValueError(f"{location}.{metric} must be boolean")
                eligibility[metric] = raw_value
            mask_after_sec: float | None = None
            if row.get("mask_after_sec") is not None:
                try:
                    mask_after_sec = float(row["mask_after_sec"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{location}.mask_after_sec must be numeric"
                    ) from exc
                if (
                    not math.isfinite(mask_after_sec)
                    or mask_after_sec < 0.0
                ):
                    raise ValueError(
                        f"{location}.mask_after_sec must be finite "
                        "and non-negative"
                    )
            self.tool_metric_scopes[tool_id] = {
                "instance_resolution": resolution,
                "metric_eligibility": eligibility,
                "mask_after_sec": mask_after_sec,
                "reason": _clean(row.get("reason")),
            }

    def tool_metric_policy(
        self,
        tool_id: str,
        time_sec: float,
    ) -> dict[str, Any]:
        descriptor = self.tool_metric_scopes.get(_clean(tool_id))
        if descriptor is None:
            return {
                "instance_resolution": None,
                "metric_eligibility": {
                    "state": True,
                    "physical": True,
                    "reuse": True,
                },
                "reason": "",
            }
        eligibility = dict(descriptor["metric_eligibility"])
        mask_after_sec = descriptor.get("mask_after_sec")
        if mask_after_sec is not None and float(time_sec) > mask_after_sec:
            for metric in ("state", "physical", "reuse"):
                eligibility[metric] = False
        return {
            "instance_resolution": descriptor["instance_resolution"],
            "metric_eligibility": eligibility,
            "mask_after_sec": mask_after_sec,
            "reason": descriptor["reason"],
        }

    def _time_eligibility(
        self,
        time_sec: float,
        *,
        include_default: bool = True,
    ) -> dict[str, bool]:
        eligibility = (
            dict(self.default_metric_eligibility)
            if include_default
            else {key: True for key in METRIC_ELIGIBILITY_KEYS}
        )
        action_cutoff = self.cutoffs.get("action_and_next_tool_end_sec")
        if action_cutoff is not None and time_sec > action_cutoff:
            eligibility["action"] = False
            eligibility["latency"] = False
        state_cutoff = self.cutoffs.get("state_audit_end_sec")
        if state_cutoff is not None and time_sec > state_cutoff:
            for key in ("state", "physical", "reuse"):
                eligibility[key] = False
        visual_cutoff = self.cutoffs.get("visual_end_sec")
        if visual_cutoff is not None and time_sec > visual_cutoff:
            for key in METRIC_ELIGIBILITY_KEYS:
                eligibility[key] = False
        for mask in self.interval_masks:
            if mask["start_sec"] <= time_sec <= mask["end_sec"]:
                for key, enabled in mask["metric_eligibility"].items():
                    eligibility[key] = bool(eligibility[key] and enabled)
        return eligibility

    def metric_enabled_at(self, metric: str, time_sec: float) -> bool:
        if metric not in METRIC_ELIGIBILITY_KEYS:
            raise KeyError(metric)
        return self._time_eligibility(
            float(time_sec),
            include_default=False,
        )[metric]

    def event_policy(self, event: dict[str, Any]) -> dict[str, Any]:
        event_id = _clean(event.get("event_id"))
        descriptor = self.event_roles.get(event_id)
        raw_direct_role = _clean(event.get("scoring_role"))
        if descriptor is None and raw_direct_role:
            role = SCORING_ROLE_ALIASES.get(raw_direct_role, raw_direct_role)
            if role not in SCORING_ROLE_DEFAULTS:
                self.issues.append(
                    f"{event_id}: unknown direct scoring role "
                    f"{raw_direct_role!r}; failed closed"
                )
                role = "not_scorable"
            descriptor = {
                "role": role,
                "metric_eligibility": SCORING_ROLE_DEFAULTS[role],
                "reason": "",
            }

        if descriptor is None:
            role = "legacy_default" if not self.present else "mask_default"
            eligibility = dict(self.default_metric_eligibility)
            if self.present:
                for key in ("state", "physical", "reuse"):
                    eligibility[key] = False
        else:
            role = descriptor["role"]
            eligibility = dict(descriptor["metric_eligibility"])

        event_type = _clean(event.get("event_type"))
        action = derive_action(event)
        if action != "handover":
            eligibility["action"] = False
            eligibility["latency"] = False
        if event_type not in {"initial_state", "tool_transfer"}:
            for key in ("state", "physical", "reuse"):
                eligibility[key] = False

        event_time = _float(event.get("time_sec"))
        tool_policy = self.tool_metric_policy(
            event_tool_id(event),
            event_time,
        )
        for key in ("state", "physical", "reuse"):
            eligibility[key] = bool(
                eligibility[key]
                and tool_policy["metric_eligibility"][key]
            )

        time_eligibility = self._time_eligibility(
            event_time,
            include_default=False,
        )
        for key in METRIC_ELIGIBILITY_KEYS:
            eligibility[key] = bool(eligibility[key] and time_eligibility[key])
        return {
            "role": role,
            "metric_eligibility": eligibility,
            "reason": descriptor.get("reason", "") if descriptor else "",
        }

    def report(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "schema": (
                self.payload.get("schema")
                if isinstance(self.payload, dict)
                else None
            ),
            "case_id": self.case_id or None,
            "evaluation_scope": copy.deepcopy(self.evaluation_scope),
            "default_metric_eligibility": dict(
                self.default_metric_eligibility
            ),
            "event_role_count": len(self.event_roles),
            "interval_mask_count": len(self.interval_masks),
            "tool_metric_scope_count": len(self.tool_metric_scopes),
            "tool_metric_scopes": copy.deepcopy(self.tool_metric_scopes),
            "cutoffs": dict(self.cutoffs),
            "issues": list(dict.fromkeys(self.issues)),
        }


def _identity_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _clean(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def load_tool_identity_map(path: Path | None) -> dict[str, str]:
    """Map procedure refs, names, and aliases to dataset-canonical tool ids."""

    if path is None:
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = payload.get("tools", []) if isinstance(payload, dict) else []
    identity_map: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        canonical = _clean(row.get("id"))
        if not canonical:
            continue
        values = [
            canonical,
            row.get("name"),
            *(row.get("procedure_refs", []) or []),
            *(row.get("aliases", []) or []),
        ]
        for value in values:
            key = _identity_key(value)
            if key:
                identity_map[key] = canonical
    return identity_map


def normalize_tool_id(value: Any, identity_map: dict[str, str]) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    return identity_map.get(_identity_key(raw), raw)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(
        0,
        min(
            len(ordered) - 1,
            math.ceil((percentile / 100.0) * len(ordered)) - 1,
        ),
    )
    return round(float(ordered[index]), 6)


def _distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    rows = [float(value) for value in values if math.isfinite(float(value))]
    return {
        "count": len(rows),
        "mean": round(statistics.fmean(rows), 6) if rows else None,
        "median": round(statistics.median(rows), 6) if rows else None,
        "p95": _percentile(rows, 95.0),
        "max": round(max(rows), 6) if rows else None,
    }


def _trace_time(record: dict[str, Any]) -> float:
    return _float(record.get("ros_time_sec", record.get("time_sec", 0.0)))


def _wall_time(record: dict[str, Any]) -> float | None:
    value = record.get("wall_time_sec")
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0.0:
        return None
    return result


def _prediction_time(record: dict[str, Any], layer: str) -> float:
    if layer == "vlm_model_raw":
        source_stamp = _float(record.get("source_stamp_sec"), default=-1.0)
        if source_stamp >= 0.0:
            return source_stamp
    return _trace_time(record)


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else record


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_ranked_id(rows: Any) -> tuple[str, float]:
    if not isinstance(rows, list):
        return ("", 0.0)
    for row in rows:
        if isinstance(row, list) and len(row) >= 2 and _clean(row[0]):
            return (_clean(row[0]), _float(row[1]))
        if isinstance(row, dict):
            candidate = _clean(
                row.get("id")
                or row.get("tool_id")
                or row.get("phase_id")
            )
            if candidate:
                return (
                    candidate,
                    _float(row.get("confidence", row.get("score", 0.0))),
                )
    return ("", 0.0)


def _vlm_prediction(record: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(record)
    raw = _parse_json_object(payload.get("raw_json"))
    if not raw and any(key in payload for key in ("tool", "phase", "intent", "ph")):
        raw = payload

    tool_id, tool_confidence = _first_ranked_id(raw.get("tool"))
    phase_id, phase_confidence = _first_ranked_id(raw.get("phase"))
    action = "predict_tool" if tool_id else ""
    prediction_source = "predicted_tool" if tool_id else ""
    intent = raw.get("intent")
    if isinstance(intent, list):
        intent_action = _clean(intent[0]) if intent else ""
        intent_tool_id = _clean(intent[1]) if len(intent) >= 2 else ""
        if not tool_id and intent:
            action = intent_action
        if not tool_id and len(intent) >= 2:
            tool_id = intent_tool_id
            if len(intent) >= 3:
                tool_confidence = _float(intent[2])
            prediction_source = (
                "explicit_request"
                if intent_action in HANDOVER_ACTIONS
                and intent_action not in {"predict_tool", "prepare_tool"}
                else "predicted_tool"
            )
        elif (
            tool_id
            and intent_tool_id == tool_id
            and intent_action in HANDOVER_ACTIONS
            and intent_action not in {"predict_tool", "prepare_tool"}
        ):
            # Preserve a public handover request as a distinct episode from an
            # ongoing forecast for the same tool. This lets repeated requests
            # for one tool be evaluated independently without trusting an
            # intent that names a different ranked tool.
            action = intent_action
            prediction_source = "explicit_request"
    if tool_id and action.lower() in {"", "none", "hold", "wait"}:
        action = "predict_tool"
        prediction_source = "predicted_tool"

    if not phase_id:
        phase_ids = payload.get("phase_ids")
        confidences = payload.get("phase_confidences")
        if isinstance(phase_ids, list) and phase_ids:
            phase_id = _clean(phase_ids[0])
            if isinstance(confidences, list) and confidences:
                phase_confidence = _float(confidences[0])
    if not tool_id:
        tool_id = _clean(
            payload.get("predicted_tool_id")
            or payload.get("gesture_requested_tool")
        )
        tool_confidence = _float(
            payload.get(
                "predicted_tool_confidence",
                payload.get("gesture_confidence", 0.0),
            )
        )

    return {
        "tool_id": tool_id,
        "tool_confidence": tool_confidence,
        "action": action,
        "prediction_source": prediction_source,
        "phase_id": phase_id,
        "phase_confidence": phase_confidence,
    }


def _prediction_from_record(
    layer: str,
    record: dict[str, Any],
    *,
    tool_identity_map: dict[str, str],
) -> dict[str, Any] | None:
    payload = _payload(record)
    if layer in {"vlm_model_raw", "vlm_raw"}:
        values = _vlm_prediction(record)
    elif layer == "reducer_fused":
        predicted_tool = _clean(
            payload.get("predicted_tool")
            or payload.get("predicted_tool_id")
        )
        request_tool = _clean(
            payload.get("explicit_request_tool")
            or payload.get("surgeon_request_tool")
        )
        values = {
            "tool_id": predicted_tool or request_tool,
            "tool_confidence": _float(
                payload.get(
                    "predicted_tool_confidence",
                    1.0 if request_tool else payload.get("confidence", 0.0),
                )
            ),
            "action": "predict_tool" if predicted_tool else "handover",
            "phase_id": _clean(
                payload.get("filtered_phase")
                or payload.get("phase_id")
            ),
            "phase_confidence": _float(
                payload.get(
                    "phase_confidence",
                    payload.get("confidence", 0.0),
                )
            ),
            "prediction_source": (
                "predicted_tool" if predicted_tool else "explicit_request"
            ),
        }
    elif layer == "bt_decision":
        values = {
            "tool_id": _clean(
                payload.get("selected_tool")
                or payload.get("predicted_tool_id")
            ),
            "tool_confidence": _float(payload.get("confidence", 0.0)),
            "action": _clean(
                payload.get("action")
                or payload.get("predicted_action")
                or payload.get("decision")
            ),
            "phase_id": _clean(payload.get("filtered_phase")),
            "phase_confidence": _float(payload.get("phase_confidence", 0.0)),
            "prediction_source": "bt_decision",
        }
    elif layer == "skill_command":
        values = {
            "tool_id": _clean(
                payload.get("instrument_id")
                or payload.get("predicted_tool_id")
            ),
            "tool_confidence": _float(payload.get("confidence", 0.0)),
            "action": _clean(
                payload.get("action")
                or payload.get("predicted_action")
            ),
            "phase_id": "",
            "phase_confidence": 0.0,
            "prediction_source": "skill_command",
        }
    else:
        return None

    if not values["tool_id"] and not values["phase_id"]:
        return None
    raw_tool_id = values["tool_id"]
    values["tool_id"] = normalize_tool_id(raw_tool_id, tool_identity_map)
    sequence = int(record.get("sequence", record.get("_jsonl_line", 0)))
    request_generation = int(
        payload.get(
            (
                "surgeon_request_generation"
                if layer == "reducer_fused"
                else "request_generation"
            ),
            0,
        )
        or 0
    )
    return {
        "prediction_id": f"{layer}:{sequence}",
        "layer": layer,
        "sequence": sequence,
        "time_sec": _prediction_time(record, layer),
        "publication_time_sec": _trace_time(record),
        "tool_id": values["tool_id"],
        "raw_tool_id": raw_tool_id,
        "prediction_source": values.get(
            "prediction_source",
            "vlm_raw",
        ),
        "request_generation": request_generation,
        "tool_confidence": round(float(values["tool_confidence"]), 6),
        "action": values["action"],
        "phase_id": values["phase_id"],
        "phase_confidence": round(float(values["phase_confidence"]), 6),
        "safety_status": _clean(
            payload.get("safety_status")
            or payload.get("blocking_guard")
        ),
        "decision": _clean(payload.get("decision")),
        "payload": payload,
    }


def extract_predictions(
    records: list[dict[str, Any]],
    *,
    tool_identity_map: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    identity_map = tool_identity_map or {}
    by_layer: dict[str, list[dict[str, Any]]] = {
        layer: [] for layer in PREDICTION_LAYERS
    }
    has_trace_layers = any(record.get("layer") in PREDICTION_LAYERS for record in records)
    if not has_trace_layers:
        # Backwards-compatible decision JSONL used by the annotation pilot.
        for index, record in enumerate(records):
            legacy = dict(record)
            legacy.setdefault("sequence", index)
            prediction = _prediction_from_record(
                "bt_decision",
                legacy,
                tool_identity_map=identity_map,
            )
            if prediction:
                by_layer["bt_decision"].append(prediction)
        return by_layer

    for record in records:
        layer = _clean(record.get("layer"))
        if layer not in by_layer:
            continue
        prediction = _prediction_from_record(
            layer,
            record,
            tool_identity_map=identity_map,
        )
        if prediction:
            by_layer[layer].append(prediction)
    for predictions in by_layer.values():
        predictions.sort(key=lambda item: (item["time_sec"], item["sequence"]))
    return by_layer


def collapse_prediction_episodes(
    predictions: list[dict[str, Any]],
    *,
    episode_gap_sec: float,
) -> list[dict[str, Any]]:
    episodes: list[dict[str, Any]] = []
    for prediction in predictions:
        if not prediction.get("tool_id"):
            continue
        semantic_key = (
            prediction["tool_id"],
            prediction.get("action", ""),
            (
                prediction.get("request_generation", 0)
                if prediction.get("request_generation", 0) > 0
                else None
            ),
        )
        previous = episodes[-1] if episodes else None
        if (
            previous is not None
            and previous["semantic_key"] == semantic_key
            and prediction["time_sec"] - previous["last_time_sec"]
            <= episode_gap_sec
        ):
            previous["last_time_sec"] = prediction["time_sec"]
            previous["record_count"] += 1
            previous["prediction_ids"].append(prediction["prediction_id"])
            previous["record_times_sec"].append(prediction["time_sec"])
            previous["max_confidence"] = max(
                previous["max_confidence"],
                prediction["tool_confidence"],
            )
            previous["last_prediction"] = prediction
            if prediction.get("safety_status"):
                previous["safety_status"] = prediction["safety_status"]
            continue
        episodes.append(
            {
                "episode_id": f"{prediction['layer']}:episode:{len(episodes)}",
                "layer": prediction["layer"],
                "semantic_key": semantic_key,
                "tool_id": prediction["tool_id"],
                "action": prediction.get("action", ""),
                "request_generation": int(
                    prediction.get("request_generation", 0) or 0
                ),
                "first_time_sec": prediction["time_sec"],
                "last_time_sec": prediction["time_sec"],
                "record_count": 1,
                "prediction_ids": [prediction["prediction_id"]],
                "record_times_sec": [prediction["time_sec"]],
                "max_confidence": prediction["tool_confidence"],
                "safety_status": prediction.get("safety_status", ""),
                "first_prediction": prediction,
                "last_prediction": prediction,
            }
        )
    return episodes


class ReferenceTimeline:
    def __init__(
        self,
        ground_truth: list[dict[str, Any]],
        *,
        tool_identity_map: dict[str, str] | None = None,
        evaluation_mask: EvaluationMask | None = None,
    ) -> None:
        identity_map = tool_identity_map or {}
        self.evaluation_mask = evaluation_mask or EvaluationMask()
        self.events = sorted(
            (
                copy.deepcopy(strip_internal_fields(item))
                for item in ground_truth
                if item.get("review_status") == "confirmed"
            ),
            key=lambda item: (float(item.get("time_sec", 0.0)), item.get("event_id", "")),
        )
        for event in self.events:
            tool = event.get("tool")
            if isinstance(tool, dict):
                tool["id"] = normalize_tool_id(tool.get("id"), identity_map)
            elif isinstance(tool, str):
                event["tool"] = normalize_tool_id(tool, identity_map)
        self.canonical_tools = {
            event_tool_id(item)
            for item in self.events
            if event_tool_id(item)
        }
        self.canonical_tools.update(identity_map.values())
        self.duplicate_type_instances: dict[str, list[dict[str, Any]]] = {}
        self.type_instance_assumptions: list[dict[str, Any]] = []
        self._audit_type_continuity()
        self._apply_tool_metric_scope_assumptions()

    def _audit_type_continuity(self) -> None:
        state_by_tool: dict[str, str] = {}
        for event in self.events:
            if (
                event.get("event_type") != "tool_transfer"
                or not event_tool_id(event)
                or event_tool_instance_id(event)
            ):
                continue
            tool_id = event_tool_id(event)
            source = event_endpoint_key(event, "from")
            target = event_endpoint_key(event, "to")
            expected = state_by_tool.get(tool_id)
            if expected and source and source != expected:
                self.duplicate_type_instances.setdefault(tool_id, []).append(
                    {
                        "event_id": _clean(event.get("event_id")),
                        "time_sec": _float(event.get("time_sec")),
                        "expected_from": expected,
                        "observed_from": source,
                        "interpretation": (
                            "multiple physical instances or an unobserved "
                            "transition are required"
                        ),
                    }
                )
            if target:
                state_by_tool[tool_id] = target

    def _apply_tool_metric_scope_assumptions(self) -> None:
        for tool_id, descriptor in (
            self.evaluation_mask.tool_metric_scopes.items()
        ):
            resolution = descriptor["instance_resolution"]
            if resolution == "resolved":
                continue
            assumption = {
                "tool_id": tool_id,
                "instance_resolution": resolution,
                "source": "evaluation_mask.tool_metric_scopes",
                "reason": descriptor["reason"],
            }
            self.type_instance_assumptions.append(assumption)
            if resolution == "unresolved_multiple_instances":
                self.duplicate_type_instances.setdefault(tool_id, []).append(
                    {
                        "event_id": None,
                        "time_sec": None,
                        "expected_from": None,
                        "observed_from": None,
                        "interpretation": descriptor["reason"]
                        or "evaluation mask declares unresolved instances",
                        "source": "evaluation_mask.tool_metric_scopes",
                    }
                )

    def state_at(self, time_sec: float) -> dict[str, dict[str, Any]]:
        state: dict[str, dict[str, Any]] = {}
        for event in self.events:
            if float(event.get("time_sec", 0.0)) > time_sec:
                break
            policy = self.evaluation_mask.event_policy(event)
            if not policy["metric_eligibility"]["state"]:
                continue
            tool_id = event_tool_id(event)
            if not tool_id:
                continue
            instance_id = event_tool_instance_id(event) or f"type:{tool_id}"
            target = event_endpoint(event, "to")
            state[instance_id] = {
                "tool_id": tool_id,
                "holder": _clean(target.get("holder")),
                "location": _clean(target.get("location")),
                "event_id": event.get("event_id"),
                "time_sec": event.get("time_sec"),
                "instance_level": bool(event_tool_instance_id(event)),
                "physical_scorable": bool(
                    policy["metric_eligibility"]["physical"]
                    and tool_id not in self.duplicate_type_instances
                ),
                "reuse_scorable": bool(
                    policy["metric_eligibility"]["reuse"]
                    and tool_id not in self.duplicate_type_instances
                ),
            }
        return state

    def state_audit(self) -> dict[str, Any]:
        state_event_count = sum(
            self.evaluation_mask.event_policy(event)["metric_eligibility"][
                "state"
            ]
            for event in self.events
        )
        physical_event_count = sum(
            self.evaluation_mask.event_policy(event)["metric_eligibility"][
                "physical"
            ]
            for event in self.events
        )
        physical_scorable_event_count = sum(
            self.evaluation_mask.event_policy(event)["metric_eligibility"][
                "physical"
            ]
            and event_tool_id(event) not in self.duplicate_type_instances
            for event in self.events
        )
        reuse_scorable_event_count = sum(
            self.evaluation_mask.event_policy(event)["metric_eligibility"][
                "reuse"
            ]
            and event_tool_id(event) not in self.duplicate_type_instances
            for event in self.events
        )
        return {
            "status": (
                "not_scorable_type_instance_assumption"
                if self.duplicate_type_instances
                else "scorable"
                if physical_event_count
                else "not_scorable_no_physical_reference"
            ),
            "state_event_count": state_event_count,
            "physical_event_count": physical_event_count,
            "physical_scorable_event_count": physical_scorable_event_count,
            "reuse_scorable_event_count": reuse_scorable_event_count,
            "duplicate_type_instance_tools": sorted(
                self.duplicate_type_instances
            ),
            "duplicate_type_instance_events": copy.deepcopy(
                self.duplicate_type_instances
            ),
            "type_instance_assumptions": copy.deepcopy(
                self.type_instance_assumptions
            ),
        }

    def feasibility(
        self,
        episode: dict[str, Any],
    ) -> tuple[str, str]:
        safety_status = _clean(episode.get("safety_status")).lower()
        if safety_status in UNSAFE_STATUSES:
            return ("unsafe", f"runtime declared {safety_status}")
        tool_id = _clean(episode.get("tool_id"))
        if tool_id not in self.canonical_tools:
            return ("impossible", "tool is absent from the confirmed reference catalog")
        if tool_id in self.duplicate_type_instances:
            return (
                "not_scorable",
                "canonical tool type requires multiple instances or an "
                "unobserved transition",
            )
        action = _clean(episode.get("action")).lower()
        state = self.state_at(float(episode["last_time_sec"]))
        instances = [item for item in state.values() if item["tool_id"] == tool_id]
        if action in RECOVERY_ACTIONS:
            physical_instances = [
                item for item in instances if item["physical_scorable"]
            ]
            if not physical_instances:
                return (
                    "not_scorable",
                    "no instance-level physical reference is eligible at "
                    "command time",
                )
            if any(
                item["location"] == "mayo_stand"
                for item in physical_instances
            ):
                return ("possible", "confirmed instance is on the Mayo stand")
            if physical_instances:
                return ("impossible", "no confirmed instance is on the Mayo stand")
        return (
            "unknown",
            "observable event labels do not prove clinical acceptability",
        )

    def reuse_scorable(self, tool_id: str, time_sec: float) -> bool:
        if tool_id in self.duplicate_type_instances:
            return False
        return any(
            item["tool_id"] == tool_id and item["reuse_scorable"]
            for item in self.state_at(time_sec).values()
        )

    def handover_is_reuse_eligible(self, event: dict[str, Any]) -> bool:
        return bool(
            self.evaluation_mask.event_policy(event)["metric_eligibility"][
                "reuse"
            ]
        )


def _target_handovers(
    ground_truth: list[dict[str, Any]],
    *,
    tool_identity_map: dict[str, str] | None = None,
    evaluation_mask: EvaluationMask | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    identity_map = tool_identity_map or {}
    mask = evaluation_mask or EvaluationMask()
    confirmed = [
        copy.deepcopy(strip_internal_fields(item))
        for item in ground_truth
        if item.get("review_status") == "confirmed"
    ]
    for item in confirmed:
        tool = item.get("tool")
        if isinstance(tool, dict):
            tool["id"] = normalize_tool_id(tool.get("id"), identity_map)
        elif isinstance(tool, str):
            item["tool"] = normalize_tool_id(tool, identity_map)
    confirmed_handovers = [
        item for item in confirmed if derive_action(item) == "handover"
    ]
    targets = [
        item
        for item in confirmed_handovers
        if mask.event_policy(item)["metric_eligibility"]["action"]
    ]
    excluded = {
        status: sum(
            1 for item in ground_truth if item.get("review_status") == status
        )
        for status in ("proposed", "ambiguous", "rejected")
    }
    origin_counts = Counter(
        item.get("label_origin", "unspecified") for item in confirmed
    )
    reviewer_counts = Counter(
        item["review"]["reviewer_kind"]
        for item in confirmed
        if isinstance(item.get("review"), dict)
        and item["review"].get("reviewer_kind")
    )
    scoring_role_counts = Counter(
        mask.event_policy(item)["role"] for item in confirmed
    )
    if len(origin_counts) == 1:
        authority = next(iter(origin_counts))
    elif origin_counts:
        authority = "mixed"
    else:
        authority = "none"
    reference = {
        "confirmed_ground_truth_count": len(confirmed),
        "confirmed_handover_count": len(targets),
        "confirmed_handover_before_mask_count": len(confirmed_handovers),
        "masked_confirmed_handover_count": (
            len(confirmed_handovers) - len(targets)
        ),
        "confirmed_phase_start_count": sum(
            item.get("event_type") == "phase_start" for item in confirmed
        ),
        "excluded_ground_truth_counts": excluded,
        "confirmed_label_origin_counts": dict(origin_counts),
        "confirmed_reviewer_kind_counts": dict(reviewer_counts),
        "confirmed_scoring_role_counts": dict(scoring_role_counts),
        "reference_authority": authority,
    }
    return targets, excluded, reference


def _semantic_identifier_tokens(value: Any) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", _clean(value)).casefold()
    characters = [
        character if character.isalnum() else " "
        for character in normalized
    ]
    return frozenset("".join(characters).split())


def _capability_match_rank(
    capability_name: str,
    tool_id: str,
    *,
    tool_identity_map: dict[str, str],
) -> int:
    normalized_capability = normalize_tool_id(
        capability_name,
        tool_identity_map,
    )
    if normalized_capability == tool_id:
        return 3
    capability_tokens = _semantic_identifier_tokens(normalized_capability)
    tool_tokens = _semantic_identifier_tokens(tool_id)
    if not capability_tokens or not tool_tokens:
        return 0
    if capability_tokens < tool_tokens:
        return 2
    if tool_tokens < capability_tokens:
        return 1
    return 0


def _discover_bed_robot_group_capabilities(
    records: list[dict[str, Any]],
    *,
    canonical_tool_ids: set[str],
    tool_identity_map: dict[str, str],
) -> dict[str, Any]:
    descriptors: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if _clean(record.get("layer")) not in BED_ROBOT_GROUP_LAYERS:
            continue
        payload = _payload(record)
        group_id = _clean(payload.get("group_id"))
        if group_id != RETRACTION_GROUP_ID:
            continue
        profile = _clean(
            payload.get("target_tool_id")
            or payload.get("end_effector_profile")
        )
        if not group_id or not profile:
            continue
        descriptor = descriptors.setdefault(
            (group_id, profile),
            {
                "group_id": group_id,
                "end_effector_profile": profile,
                "source_layers": set(),
                "record_count": 0,
            },
        )
        descriptor["source_layers"].add(_clean(record.get("layer")))
        descriptor["record_count"] += 1

    resolved: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for descriptor in descriptors.values():
        ranked = [
            (
                _capability_match_rank(
                    descriptor["end_effector_profile"],
                    tool_id,
                    tool_identity_map=tool_identity_map,
                ),
                tool_id,
            )
            for tool_id in sorted(canonical_tool_ids)
        ]
        best_rank = max((rank for rank, _tool_id in ranked), default=0)
        candidates = [
            tool_id
            for rank, tool_id in ranked
            if rank == best_rank and rank > 0
        ]
        row = {
            **descriptor,
            "source_layers": sorted(descriptor["source_layers"]),
            "match_rank": best_rank,
        }
        if len(candidates) == 1:
            row["tool_id"] = candidates[0]
            resolved.append(row)
        elif candidates:
            row["candidate_tool_ids"] = candidates
            ambiguous.append(row)
        else:
            unresolved.append(row)

    resolved.sort(
        key=lambda row: (
            row["group_id"],
            row["end_effector_profile"],
        )
    )
    ambiguous.sort(
        key=lambda row: (
            row["group_id"],
            row["end_effector_profile"],
        )
    )
    unresolved.sort(
        key=lambda row: (
            row["group_id"],
            row["end_effector_profile"],
        )
    )
    return {
        "resolved": resolved,
        "ambiguous": ambiguous,
        "unresolved": unresolved,
    }


def _is_bed_robot_activation(
    *,
    operation: str,
    group_id: str,
    end_effector_profile: str,
) -> bool:
    if group_id != RETRACTION_GROUP_ID:
        return False
    del end_effector_profile
    return operation in RETRACTION_TOOL_CHANGE_OPERATIONS


def _bed_robot_command_id(record: dict[str, Any]) -> str:
    payload = _payload(record)
    return _clean(
        payload.get("command_id")
        or record.get("correlation_id")
    )


def _evaluate_specialized_group_actions(
    *,
    records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    timeline: ReferenceTimeline,
    lead_window_sec: float,
    tool_identity_map: dict[str, str],
) -> tuple[dict[str, Any], set[str]]:
    capabilities = _discover_bed_robot_group_capabilities(
        records,
        canonical_tool_ids=timeline.canonical_tools,
        tool_identity_map=tool_identity_map,
    )
    capabilities_by_descriptor = {
        (
            row["group_id"],
            row["end_effector_profile"],
        ): row
        for row in capabilities["resolved"]
    }
    capabilities_by_group: dict[str, list[dict[str, Any]]] = {}
    for row in capabilities["resolved"]:
        capabilities_by_group.setdefault(row["group_id"], []).append(row)

    target_capabilities: dict[str, list[dict[str, Any]]] = {}
    for row in capabilities["resolved"]:
        target_capabilities.setdefault(row["tool_id"], []).append(row)

    specialized_targets = [
        target
        for target in targets
        if event_tool_id(target) in target_capabilities
    ]
    specialized_target_ids = {
        _clean(target.get("event_id"))
        for target in specialized_targets
    }

    commands: list[dict[str, Any]] = []
    non_activation_commands: list[dict[str, Any]] = []
    unmapped_commands: list[dict[str, Any]] = []
    for record in records:
        if _clean(record.get("layer")) != "bed_robot_arm_group_command":
            continue
        payload = _payload(record)
        group_id = _clean(payload.get("group_id"))
        if group_id != RETRACTION_GROUP_ID:
            continue
        profile = _clean(
            payload.get("target_tool_id")
            or payload.get("end_effector_profile")
        )
        operation = _clean(payload.get("operation"))
        sequence = int(
            record.get("sequence", record.get("_jsonl_line", 0))
        )
        capability = capabilities_by_descriptor.get((group_id, profile))
        if capability is None:
            group_capabilities = capabilities_by_group.get(group_id, [])
            if len(group_capabilities) == 1:
                capability = group_capabilities[0]
        command = {
            "command_id": (
                _bed_robot_command_id(record)
                or f"bed-group-command:{sequence}"
            ),
            "request_id": _clean(payload.get("request_id")),
            "sequence": sequence,
            "time_sec": _trace_time(record),
            "group_id": group_id,
            "end_effector_profile": profile,
            "operation": operation,
            "tool_id": (
                capability["tool_id"] if capability is not None else ""
            ),
        }
        if capability is None:
            unmapped_commands.append(command)
            continue
        if _is_bed_robot_activation(
            operation=operation,
            group_id=group_id,
            end_effector_profile=profile,
        ):
            commands.append(command)
        else:
            non_activation_commands.append(command)

    commands.sort(key=lambda row: (row["time_sec"], row["sequence"]))
    terminal_status_by_command: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if _clean(record.get("layer")) != "bed_robot_arm_group_status":
            continue
        payload = _payload(record)
        if _clean(payload.get("group_id")) != RETRACTION_GROUP_ID:
            continue
        command_id = _bed_robot_command_id(record)
        if not command_id or not bool(payload.get("terminal")):
            continue
        terminal_status_by_command.setdefault(command_id, []).append(
            {
                "time_sec": _trace_time(record),
                "success": bool(payload.get("success")),
                "state": _clean(payload.get("state")),
                "outcome": _clean(payload.get("outcome")),
            }
        )

    sink_command_ids = {
        _bed_robot_command_id(record)
        for record in records
        if _clean(record.get("layer"))
        == "shadow_bed_robot_arm_group_sink"
        and _clean(_payload(record).get("group_id"))
        == RETRACTION_GROUP_ID
        and _bed_robot_command_id(record)
    }

    consumed_commands: set[str] = set()
    event_results: list[dict[str, Any]] = []
    for target in specialized_targets:
        target_time = float(target["time_sec"])
        target_tool_id = event_tool_id(target)
        target_policy = timeline.evaluation_mask.event_policy(target)
        candidates = [
            command
            for command in commands
            if command["command_id"] not in consumed_commands
            and command["tool_id"] == target_tool_id
            and target_time - lead_window_sec
            <= float(command["time_sec"])
            < target_time
            and timeline.evaluation_mask.metric_enabled_at(
                "action",
                float(command["time_sec"]),
            )
        ]
        selected = max(
            candidates,
            key=lambda row: (row["time_sec"], row["sequence"]),
            default=None,
        )
        if selected is not None:
            consumed_commands.add(selected["command_id"])
        terminal_rows = (
            terminal_status_by_command.get(selected["command_id"], [])
            if selected is not None
            else []
        )
        terminal = max(
            terminal_rows,
            key=lambda row: row["time_sec"],
            default=None,
        )
        if terminal is not None and terminal["success"]:
            execution_outcome = "terminal_success"
        elif terminal is not None:
            execution_outcome = "terminal_failure"
        elif (
            selected is not None
            and selected["command_id"] in sink_command_ids
        ):
            execution_outcome = "dispatched_without_terminal_status"
        elif selected is not None:
            execution_outcome = "no_execution_evidence"
        else:
            execution_outcome = "not_commanded"
        event_results.append(
            {
                "event_id": _clean(target.get("event_id")),
                "time_sec": target_time,
                "target_tool_id": target_tool_id,
                "target_label_origin": target.get("label_origin"),
                "target_scoring_role": target_policy["role"],
                "outcome": (
                    "exact_match"
                    if selected is not None
                    else "missed_opportunity"
                ),
                "selected_command_id": (
                    selected["command_id"]
                    if selected is not None
                    else None
                ),
                "group_id": (
                    selected["group_id"]
                    if selected is not None
                    else target_capabilities[target_tool_id][0]["group_id"]
                ),
                "end_effector_profile": (
                    selected["end_effector_profile"]
                    if selected is not None
                    else target_capabilities[target_tool_id][0][
                        "end_effector_profile"
                    ]
                ),
                "operation": (
                    selected["operation"] if selected is not None else None
                ),
                "lead_time_sec": (
                    round(target_time - float(selected["time_sec"]), 6)
                    if selected is not None
                    and target_policy["metric_eligibility"]["latency"]
                    else None
                ),
                "sink_observed": bool(
                    selected is not None
                    and selected["command_id"] in sink_command_ids
                ),
                "execution_outcome": execution_outcome,
                "terminal_state": (
                    terminal["state"] if terminal is not None else None
                ),
                "terminal_outcome": (
                    terminal["outcome"] if terminal is not None else None
                ),
            }
        )

    unmatched_commands = [
        command
        for command in commands
        if command["command_id"] not in consumed_commands
    ]
    exact_count = sum(
        row["outcome"] == "exact_match" for row in event_results
    )
    terminal_success_count = sum(
        row["execution_outcome"] == "terminal_success"
        for row in event_results
    )
    target_count = len(event_results)
    reference_gap_commands = (
        unmatched_commands
        if commands and not target_count and capabilities["resolved"]
        else []
    )
    false_positive_commands = (
        []
        if reference_gap_commands
        else unmatched_commands
    )
    return (
        {
            "schema": "taskplanner.specialized_group_action_evaluation.v1",
            "status": (
                "complete"
                if target_count
                else (
                    "ambiguous_capabilities"
                    if capabilities["ambiguous"]
                    else (
                        "unscorable_reference_gap"
                        if reference_gap_commands
                        else (
                            "no_mapped_targets"
                            if capabilities["resolved"]
                            else "no_declared_capabilities"
                        )
                    )
                )
            ),
            "reference_quality": (
                (
                    "no confirmed specialized-action reference targets; "
                    "activation commands are audited but not classified"
                )
                if reference_gap_commands
                else (
                    "confirmed handover targets mapped to uniquely declared "
                    "retraction-arm end-effector capabilities"
                )
            ),
            "capabilities": capabilities,
            "target_count": target_count,
            "exact_match_count": exact_count,
            "missed_opportunity_count": target_count - exact_count,
            "command_recall": (
                exact_count / target_count if target_count else None
            ),
            "activation_command_count": len(commands),
            "false_positive_command_count": len(false_positive_commands),
            "unscorable_activation_command_count": len(
                reference_gap_commands
            ),
            "non_activation_command_count": len(non_activation_commands),
            "unmapped_command_count": len(unmapped_commands),
            "terminal_success_count": terminal_success_count,
            "terminal_failure_count": sum(
                row["execution_outcome"] == "terminal_failure"
                for row in event_results
            ),
            "execution_fulfillment_rate": (
                terminal_success_count / exact_count if exact_count else None
            ),
            "events": event_results,
            "unmatched_activation_commands": false_positive_commands,
            "unscorable_activation_commands": reference_gap_commands,
            "non_activation_commands": non_activation_commands,
            "unmapped_commands": unmapped_commands,
            "notes": [
                (
                    "Capability-to-tool mapping is derived offline from "
                    "declared end-effector profiles and confirmed tool "
                    "identities; ambiguous mappings remain ordinary "
                    "handover targets."
                ),
                (
                    "Reference labels are never emitted to VLM, reducer, "
                    "BT, or runtime command topics."
                ),
                (
                    "Lifecycle stop/release commands are reported but "
                    "excluded from activation false-positive counts."
                ),
                (
                    "When no confirmed specialized-action target exists, "
                    "activation commands are marked unscorable instead of "
                    "being inferred as false positives."
                ),
            ],
        },
        specialized_target_ids,
    )


def _eligible_episode_view(
    episode: dict[str, Any],
    *,
    target_time: float,
    lead_window_sec: float,
    max_prediction_age_sec: float,
    evaluation_mask: EvaluationMask,
    allow_request_reaction: bool = False,
    request_reaction_window_sec: float = 0.0,
) -> dict[str, Any] | None:
    eligible_times = [
        float(value)
        for value in episode.get("record_times_sec", [])
        if evaluation_mask.metric_enabled_at("action", float(value))
    ]
    prior_times = [
        value for value in eligible_times if value < target_time
    ]
    if not prior_times:
        if not allow_request_reaction:
            return None
        reaction_times = [
            value
            for value in eligible_times
            if target_time <= value <= target_time + request_reaction_window_sec
        ]
        if not reaction_times:
            return None
        first_time = min(reaction_times)
        last_time = max(reaction_times)
        view = dict(episode)
        view["first_time_sec"] = first_time
        view["last_time_sec"] = last_time
        view["record_times_sec"] = reaction_times
        view["match_timing"] = "request_reactive"
        view["reaction_lag_sec"] = first_time - target_time
        return view
    first_time = min(prior_times)
    last_time = max(prior_times)
    if first_time < target_time - lead_window_sec:
        # The semantic episode began before the scoring window, but a fresh
        # repetition inside the window is still a valid current prediction.
        first_time = min(
            value
            for value in prior_times
            if value >= target_time - lead_window_sec
        ) if any(
            value >= target_time - lead_window_sec for value in prior_times
        ) else first_time
    if not (
        target_time - lead_window_sec <= first_time < target_time
        and last_time >= target_time - max_prediction_age_sec
    ):
        return None
    view = dict(episode)
    view["first_time_sec"] = first_time
    view["last_time_sec"] = last_time
    view["record_times_sec"] = prior_times
    view["match_timing"] = "pre_event"
    view["reaction_lag_sec"] = None
    return view


def _episode_metric_view(
    episode: dict[str, Any],
    *,
    evaluation_mask: EvaluationMask,
    metric: str,
) -> dict[str, Any] | None:
    times = [
        float(value)
        for value in episode.get("record_times_sec", [])
        if evaluation_mask.metric_enabled_at(metric, float(value))
    ]
    if not times:
        return None
    view = dict(episode)
    view["first_time_sec"] = min(times)
    view["last_time_sec"] = max(times)
    view["record_times_sec"] = times
    view["eligible_record_count"] = len(times)
    return view


def _evaluate_layer(
    *,
    layer: str,
    episodes: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    request_time_by_target_id: dict[str, float],
    timeline: ReferenceTimeline,
    lead_window_sec: float,
    stable_sec: float,
    max_prediction_age_sec: float,
    request_reaction_window_sec: float,
) -> dict[str, Any]:
    evaluation_mask = timeline.evaluation_mask
    handover_episodes = [
        view
        for episode in episodes
        for view in [
            _episode_metric_view(
                episode,
                evaluation_mask=evaluation_mask,
                metric="action",
            )
        ]
        if view is not None
        if _clean(episode.get("action")).lower() in HANDOVER_ACTIONS
    ]
    non_handover_episodes = [
        view
        for episode in episodes
        for view in [
            _episode_metric_view(
                episode,
                evaluation_mask=evaluation_mask,
                metric="action",
            )
        ]
        if view is not None
        if _clean(episode.get("action")).lower()
        not in HANDOVER_ACTIONS | {"", "hold", "none", "wait"}
    ]
    consumed: set[str] = set()
    results: list[dict[str, Any]] = []

    for target in targets:
        target_time = float(target["time_sec"])
        target_event_id = _clean(target.get("event_id"))
        confirmed_request_time = request_time_by_target_id.get(
            target_event_id
        )
        target_tool = event_tool_id(target)
        target_policy = evaluation_mask.event_policy(target)
        eligible = []
        for episode in handover_episodes:
            if episode["episode_id"] in consumed:
                continue
            prediction_sources = {
                _clean(
                    prediction.get("prediction_source")
                )
                for prediction in (
                    episode.get("first_prediction", {}),
                    episode.get("last_prediction", {}),
                )
            }
            request_backed = (
                layer in {
                    "reducer_fused",
                    "bt_decision",
                    "skill_command",
                }
                and (
                    int(episode.get("request_generation", 0)) > 0
                    or "explicit_request" in prediction_sources
                )
            )
            discrete_request = (
                layer == "reducer_fused"
                and "explicit_request" in prediction_sources
            )
            effective_max_age_sec = (
                lead_window_sec
                if layer in {"bt_decision", "skill_command"}
                or discrete_request
                else max_prediction_age_sec
            )
            view = _eligible_episode_view(
                episode,
                target_time=target_time,
                lead_window_sec=lead_window_sec,
                max_prediction_age_sec=effective_max_age_sec,
                evaluation_mask=evaluation_mask,
                allow_request_reaction=request_backed,
                request_reaction_window_sec=request_reaction_window_sec,
            )
            if view is not None:
                eligible.append(view)
        exact_candidates = [
            episode for episode in eligible if episode["tool_id"] == target_tool
        ]
        pre_event_candidates = [
            episode
            for episode in eligible
            if episode.get("match_timing") == "pre_event"
        ]
        selected: dict[str, Any] | None
        if exact_candidates:
            selected = max(
                exact_candidates,
                key=lambda item: (
                    item.get("match_timing") == "pre_event",
                    (
                        float(item["last_time_sec"])
                        if item.get("match_timing") == "pre_event"
                        else -float(item["first_time_sec"])
                    ),
                    (
                        float(item["first_time_sec"])
                        if item.get("match_timing") == "pre_event"
                        else -float(item["last_time_sec"])
                    ),
                ),
            )
            outcome = "exact_match"
        elif pre_event_candidates:
            selected = max(
                pre_event_candidates,
                key=lambda item: (
                    item.get("match_timing") == "pre_event",
                    (
                        float(item["last_time_sec"])
                        if item.get("match_timing") == "pre_event"
                        else -float(item["first_time_sec"])
                    ),
                    (
                        float(item["first_time_sec"])
                        if item.get("match_timing") == "pre_event"
                        else -float(item["last_time_sec"])
                    ),
                ),
            )
            feasibility, _reason = timeline.feasibility(selected)
            if feasibility in {"unsafe", "impossible"}:
                outcome = "unsafe_or_impossible"
            elif layer in {"bt_decision", "skill_command"}:
                outcome = "needs_human_adjudication"
            else:
                outcome = "wrong_prediction"
        else:
            selected = None
            outcome = "missed_opportunity"

        first_lead = None
        stable_lead = None
        last_lead = None
        reaction_lag = None
        feasibility = "not_applicable"
        feasibility_reason = ""
        first_prediction_time = None
        decision_timing = None
        if selected is not None:
            consumed.add(selected["episode_id"])
            first_prediction_time = float(selected["first_time_sec"])
            latency_times = [
                float(value)
                for value in selected.get("record_times_sec", [])
                if evaluation_mask.metric_enabled_at("latency", float(value))
            ]
            if (
                target_policy["metric_eligibility"]["latency"]
                and latency_times
            ):
                latency_first = min(latency_times)
                latency_last = max(latency_times)
                if selected.get("match_timing") == "request_reactive":
                    reaction_lag = latency_first - target_time
                else:
                    first_lead = target_time - latency_first
                    last_lead = target_time - latency_last
                    if (
                        selected["tool_id"] == target_tool
                        and target_time - latency_first >= stable_sec
                        and latency_last >= latency_first + stable_sec
                    ):
                        stable_lead = (
                            target_time
                            - latency_first
                            - stable_sec
                        )
            feasibility, feasibility_reason = timeline.feasibility(selected)
            prediction_source = _clean(
                selected.get("first_prediction", {}).get(
                    "prediction_source"
                )
            )
            request_backed = (
                prediction_source == "explicit_request"
                or int(selected.get("request_generation", 0)) > 0
            )
            if outcome == "exact_match" and request_backed:
                decision_timing = "request_backed"
            elif (
                outcome == "exact_match"
                and prediction_source == "predicted_tool"
            ):
                if confirmed_request_time is None:
                    decision_timing = "unrequested_pre_handover"
                elif first_prediction_time < confirmed_request_time:
                    decision_timing = "pre_request_proactive"
                else:
                    decision_timing = "post_request_visual"

        results.append(
            {
                "event_id": target["event_id"],
                "time_sec": target_time,
                "confirmed_request_time_sec": (
                    round(confirmed_request_time, 6)
                    if confirmed_request_time is not None
                    else None
                ),
                "target_tool_id": target_tool,
                "target_label_origin": target.get("label_origin"),
                "target_visibility": target.get("visibility"),
                "target_scoring_role": target_policy["role"],
                "latency_eligible": target_policy["metric_eligibility"][
                    "latency"
                ],
                "outcome": outcome,
                "selected_episode_id": (
                    selected["episode_id"] if selected is not None else None
                ),
                "predicted_tool_id": (
                    selected["tool_id"] if selected is not None else None
                ),
                "predicted_raw_tool_id": (
                    selected.get("first_prediction", {}).get("raw_tool_id")
                    if selected is not None
                    else None
                ),
                "predicted_action": (
                    selected["action"] if selected is not None else None
                ),
                "prediction_source": (
                    selected.get("first_prediction", {}).get(
                        "prediction_source"
                    )
                    if selected is not None
                    else None
                ),
                "request_generation": (
                    int(selected.get("request_generation", 0))
                    if selected is not None
                    else 0
                ),
                "match_timing": (
                    selected.get("match_timing")
                    if selected is not None
                    else None
                ),
                "first_prediction_time_sec": (
                    round(first_prediction_time, 6)
                    if first_prediction_time is not None
                    else None
                ),
                "decision_timing": decision_timing,
                "first_correct_lead_sec": (
                    round(first_lead, 6)
                    if outcome == "exact_match" and first_lead is not None
                    else None
                ),
                "lead_time_sec": (
                    round(first_lead, 6)
                    if outcome == "exact_match" and first_lead is not None
                    else None
                ),
                "stable_correct_lead_sec": (
                    round(stable_lead, 6)
                    if stable_lead is not None
                    else None
                ),
                "last_prediction_lead_sec": (
                    round(last_lead, 6) if last_lead is not None else None
                ),
                "request_reaction_lag_sec": (
                    round(reaction_lag, 6)
                    if outcome == "exact_match" and reaction_lag is not None
                    else None
                ),
                "feasibility": feasibility,
                "feasibility_reason": feasibility_reason,
            }
        )

    unmatched = [
        episode
        for episode in handover_episodes
        if episode["episode_id"] not in consumed
    ]
    counts = Counter(result["outcome"] for result in results)
    feasibility_counts = Counter(
        result["feasibility"] for result in results
    )
    exact = counts["exact_match"]
    request_backed_exact = sum(
        result["outcome"] == "exact_match"
        and result.get("decision_timing") == "request_backed"
        for result in results
    )
    pre_request_proactive_exact = sum(
        result["outcome"] == "exact_match"
        and result.get("decision_timing") == "pre_request_proactive"
        for result in results
    )
    unrequested_pre_handover_exact = sum(
        result["outcome"] == "exact_match"
        and result.get("decision_timing") == "unrequested_pre_handover"
        for result in results
    )
    proactive_exact = (
        pre_request_proactive_exact
        + unrequested_pre_handover_exact
    )
    post_request_visual_exact = sum(
        result["outcome"] == "exact_match"
        and result.get("decision_timing") == "post_request_visual"
        for result in results
    )
    request_reactive_exact = sum(
        result["outcome"] == "exact_match"
        and result.get("match_timing") == "request_reactive"
        for result in results
    )
    target_count = len(results)
    first_leads = [
        float(result["first_correct_lead_sec"])
        for result in results
        if result["first_correct_lead_sec"] is not None
    ]
    stable_leads = [
        float(result["stable_correct_lead_sec"])
        for result in results
        if result["stable_correct_lead_sec"] is not None
    ]
    request_reaction_lags = [
        float(result["request_reaction_lag_sec"])
        for result in results
        if result["request_reaction_lag_sec"] is not None
    ]
    precision_denominator = (
        exact
        + counts["wrong_prediction"]
        + counts["unsafe_or_impossible"]
        + counts["needs_human_adjudication"]
        + len(unmatched)
    )
    return {
        "layer": layer,
        "prediction_record_count": sum(
            int(episode["record_count"]) for episode in episodes
        ),
        "scoring_eligible_prediction_record_count": sum(
            int(episode.get("eligible_record_count", 0))
            for episode in handover_episodes + non_handover_episodes
        ),
        "all_action_episode_count": len(episodes),
        "prediction_episode_count": len(handover_episodes),
        "non_handover_action_episode_count": len(non_handover_episodes),
        "target_count": target_count,
        "outcomes": {
            "exact_match": exact,
            "wrong_prediction": counts["wrong_prediction"],
            "missed_opportunity": counts["missed_opportunity"],
            "unsafe_or_impossible": counts["unsafe_or_impossible"],
            "needs_human_adjudication": counts["needs_human_adjudication"],
            "not_evaluable": counts["not_evaluable"],
        },
        "top1_exact_rate": exact / target_count if target_count else None,
        "stable_exact_count": len(stable_leads),
        "request_backed_exact_count": request_backed_exact,
        "request_reactive_exact_count": request_reactive_exact,
        "proactive_exact_count": proactive_exact,
        "pre_request_proactive_exact_count": (
            pre_request_proactive_exact
        ),
        "unrequested_pre_handover_exact_count": (
            unrequested_pre_handover_exact
        ),
        "post_request_visual_exact_count": post_request_visual_exact,
        # Compatibility alias. Unlike the previous implementation, this now
        # excludes visual predictions that first appeared after a confirmed
        # request.
        "anticipatory_exact_count": proactive_exact,
        "stable_exact_rate": len(stable_leads) / target_count if target_count else None,
        "precision_including_false_positives": (
            exact / precision_denominator if precision_denominator else None
        ),
        "recall": exact / target_count if target_count else None,
        "false_positive_count": len(unmatched),
        "physical_feasibility_counts": dict(feasibility_counts),
        "first_correct_lead_sec": _distribution(first_leads),
        "stable_correct_lead_sec": _distribution(stable_leads),
        "request_reaction_lag_sec": _distribution(request_reaction_lags),
        "events": results,
        "unmatched_prediction_episodes": [
            {
                key: value
                for key, value in episode.items()
                if key
                not in {
                    "semantic_key",
                    "first_prediction",
                    "last_prediction",
                }
            }
            for episode in unmatched
        ],
        "non_handover_action_episodes": [
            {
                key: value
                for key, value in episode.items()
                if key
                not in {
                    "semantic_key",
                    "first_prediction",
                    "last_prediction",
                }
            }
            for episode in non_handover_episodes
        ],
    }


def _recovery_action_audit(
    *,
    episodes: list[dict[str, Any]],
    timeline: ReferenceTimeline,
    reuse_warning_sec: float,
) -> dict[str, Any]:
    recovery_episodes = [
        episode
        for episode in episodes
        if _clean(episode.get("action")).lower() in RECOVERY_ACTIONS
    ]
    rows: list[dict[str, Any]] = []
    for episode in recovery_episodes:
        command_time = float(episode["first_time_sec"])
        tool_id = _clean(episode.get("tool_id"))
        feasibility, feasibility_reason = timeline.feasibility(episode)
        reuse_scorable = timeline.reuse_scorable(tool_id, command_time)
        next_handover = next(
            (
                event
                for event in timeline.events
                if float(event.get("time_sec", 0.0)) > command_time
                and event_tool_id(event) == tool_id
                and derive_action(event) == "handover"
                and timeline.handover_is_reuse_eligible(event)
            ),
            None,
        )
        next_handover_after_sec = (
            round(float(next_handover["time_sec"]) - command_time, 6)
            if next_handover is not None
            else None
        )

        if feasibility == "not_scorable" or not reuse_scorable:
            severity = "not_scorable"
            outcome = "not_scorable_reference"
        elif feasibility in {"unsafe", "impossible"}:
            severity = "blocker"
            outcome = "observable_state_conflict"
        elif (
            next_handover_after_sec is not None
            and next_handover_after_sec <= reuse_warning_sec
        ):
            severity = "suspicious"
            outcome = "reuse_observed_within_guard_window"
        elif next_handover_after_sec is not None:
            severity = "review"
            outcome = "later_reuse_observed"
        else:
            severity = "info"
            outcome = "no_later_reuse_observed"

        rows.append(
            {
                "episode_id": episode["episode_id"],
                "time_sec": command_time,
                "tool_id": tool_id,
                "action": _clean(episode.get("action")),
                "runtime_safety_status": _clean(
                    episode.get("safety_status")
                ),
                "observable_feasibility": feasibility,
                "observable_feasibility_reason": feasibility_reason,
                "reuse_scorable": reuse_scorable,
                "next_confirmed_handover_event_id": (
                    next_handover.get("event_id")
                    if next_handover is not None
                    else None
                ),
                "next_confirmed_handover_after_sec": (
                    next_handover_after_sec
                ),
                "severity": severity,
                "outcome": outcome,
                "interpretation": (
                    "Instance-level physical/reuse reference is unavailable or "
                    "masked; no success or failure is assigned."
                    if severity == "not_scorable"
                    else "Observable labels contradict Mayo recovery at command time."
                    if severity == "blocker"
                    else (
                        "The same tool is observably handed over again inside "
                        "the configured recovery warning window."
                        if severity == "suspicious"
                        else (
                            "A later reuse is observed; clinical suitability "
                            "requires human review."
                            if severity == "review"
                            else (
                                "No later reuse is visible in the confirmed "
                                "reference; this does not prove clinical "
                                "correctness."
                            )
                        )
                    )
                ),
            }
        )

    severity_counts = Counter(row["severity"] for row in rows)
    outcome_counts = Counter(row["outcome"] for row in rows)
    return {
        "status": (
            "complete"
            if rows and not all(
                row["severity"] == "not_scorable" for row in rows
            )
            else "not_scorable"
            if rows
            else "not_observed"
        ),
        "source_layer": "skill_command",
        "recovery_action_count": len(rows),
        "reuse_warning_sec": reuse_warning_sec,
        "severity_counts": dict(severity_counts),
        "outcome_counts": dict(outcome_counts),
        "actions": rows,
        "notes": [
            "Recovery actions are audited separately from handover accuracy.",
            "The audit uses confirmed observable state only; it is not a clinical correctness label.",
        ],
    }


def _load_phase_intervals(
    phase_ground_truth: list[dict[str, Any]] | None,
    *,
    allow_provisional: bool = False,
) -> list[dict[str, Any]]:
    if not phase_ground_truth:
        return []
    intervals: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    for item in phase_ground_truth:
        review_status = _clean(
            item.get("review_status", "confirmed")
        ).lower()
        phase_id = _clean(item.get("phase_id"))
        if not phase_id:
            continue
        if review_status == "confirmed":
            start_sec = _float(item.get("start_sec"), default=-1.0)
            end_sec = _float(item.get("end_sec"), default=-1.0)
            if start_sec >= 0.0 and end_sec > start_sec:
                intervals.append(
                    {
                        **item,
                        "phase_id": phase_id,
                        "start_sec": start_sec,
                        "end_sec": end_sec,
                        "reference_quality": "confirmed",
                    }
                )
                continue
        if (
            allow_provisional
            and review_status == "ambiguous"
            and _clean(item.get("event_type")) == "phase_start"
        ):
            start_sec = _float(item.get("time_sec"), default=-1.0)
            if start_sec >= 0.0:
                boundaries.append(
                    {
                        **item,
                        "phase_id": phase_id,
                        "start_sec": start_sec,
                        "end_sec": None,
                        "reference_quality": "provisional",
                    }
                )

    boundaries.sort(key=lambda item: float(item["start_sec"]))
    for index, boundary in enumerate(boundaries):
        if index + 1 < len(boundaries):
            boundary["end_sec"] = boundaries[index + 1]["start_sec"]
    intervals.extend(boundaries)
    return sorted(intervals, key=lambda item: float(item["start_sec"]))


def _phase_at(
    intervals: list[dict[str, Any]],
    time_sec: float,
) -> str:
    for interval in intervals:
        end_sec = interval.get("end_sec")
        if (
            _float(interval.get("start_sec"))
            <= time_sec
            and (
                end_sec is None
                or time_sec < _float(end_sec)
            )
        ):
            return _clean(interval.get("phase_id"))
    return ""


def _evaluate_phase(
    predictions_by_layer: dict[str, list[dict[str, Any]]],
    phase_ground_truth: list[dict[str, Any]] | None,
    *,
    allow_provisional: bool = False,
) -> dict[str, Any]:
    intervals = _load_phase_intervals(
        phase_ground_truth,
        allow_provisional=allow_provisional,
    )
    if not intervals:
        return {
            "status": "not_available",
            "reason": "No confirmed phase interval ground truth was supplied.",
            "layers": {},
        }
    provisional = any(
        interval.get("reference_quality") == "provisional"
        for interval in intervals
    )
    layers: dict[str, Any] = {}
    for layer in ("vlm_model_raw", "vlm_raw", "reducer_fused"):
        if (
            layer == "vlm_model_raw"
            and not predictions_by_layer.get(layer)
        ):
            continue
        rows = []
        for prediction in predictions_by_layer[layer]:
            truth = _phase_at(intervals, float(prediction["time_sec"]))
            if not truth:
                continue
            predicted = _clean(prediction.get("phase_id"))
            rows.append((truth, predicted))
        correct = sum(truth == predicted for truth, predicted in rows)
        confusion = Counter(f"{truth}->{predicted or '<empty>'}" for truth, predicted in rows)
        layers[layer] = {
            "evaluated_count": len(rows),
            "correct_count": correct,
            "accuracy": correct / len(rows) if rows else None,
            "confusion": dict(sorted(confusion.items())),
        }
    return {
        "status": "complete_provisional" if provisional else "complete",
        "scoring_ready": True,
        "reference_quality": (
            "provisional_ambiguous" if provisional else "confirmed"
        ),
        "interpretation": (
            "Development-only score using the current provisional Phase labels."
            if provisional
            else "Score using confirmed Phase interval ground truth."
        ),
        "interval_count": len(intervals),
        "intervals": [
            {
                "phase_id": interval["phase_id"],
                "start_sec": interval["start_sec"],
                "end_sec": interval.get("end_sec"),
                "reference_quality": interval.get("reference_quality"),
            }
            for interval in intervals
        ],
        "layers": layers,
    }


def _provisional_phase_context_report(
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    boundaries: list[dict[str, Any]] = []
    previous_time = -1.0
    for index, event in enumerate(events, 1):
        if event.get("event_type") != "phase_start":
            raise ValueError(
                f"provisional phase context line {index}: "
                "event_type must be phase_start"
            )
        if event.get("review_status") != "ambiguous":
            raise ValueError(
                f"provisional phase context line {index}: "
                "review_status must be ambiguous"
            )
        phase_id = _clean(event.get("phase_id"))
        event_id = _clean(event.get("event_id"))
        time_sec = _float(event.get("time_sec"), default=-1.0)
        frame_idx = event.get("source_frame_idx")
        if (
            not phase_id
            or not event_id
            or time_sec < 0
            or time_sec < previous_time
            or not isinstance(frame_idx, int)
            or frame_idx < 0
        ):
            raise ValueError(
                f"provisional phase context line {index}: invalid boundary"
            )
        previous_time = time_sec
        boundaries.append(
            {
                "event_id": event_id,
                "phase_id": phase_id,
                "source_frame_idx": frame_idx,
                "start_sec": time_sec,
                "end_sec": None,
                "review_status": "ambiguous",
            }
        )
    for index in range(len(boundaries) - 1):
        boundaries[index]["end_sec"] = boundaries[index + 1]["start_sec"]
    return {
        "status": "provisional_context_only",
        "scoring_ready": False,
        "reason": (
            "Provisional human-reviewed Phase boundaries are included for "
            "timeline context only; no Phase accuracy is scored."
        ),
        "event_count": len(boundaries),
        "review_status_counts": {"ambiguous": len(boundaries)},
        "boundaries": boundaries,
        "layers": {},
    }


def _runtime_metrics(
    trace_records: list[dict[str, Any]],
    *,
    trace_errors: list[str],
) -> dict[str, Any]:
    health_records = [
        record
        for record in trace_records
        if record.get("layer") == "vlm_health"
    ]
    health_payloads = [_payload(record) for record in health_records]
    latency = [
        _float(payload.get("latency_sec"))
        for payload in health_payloads
        if _float(payload.get("latency_sec")) > 0.0
    ]
    image_times = [
        _trace_time(record)
        for record in trace_records
        if record.get("layer") == "input_image"
    ]
    image_gaps = [
        current - previous
        for previous, current in zip(image_times, image_times[1:])
        if current >= previous
    ]
    first_image_time = min(image_times) if image_times else None
    last_image_time = max(image_times) if image_times else None
    input_image_duration_sec = (
        last_image_time - first_image_time
        if first_image_time is not None and last_image_time is not None
        else None
    )
    vlm_result_times = [
        _trace_time(record)
        for record in trace_records
        if record.get("layer") == "vlm_raw"
    ]
    vlm_result_during_input_count = sum(
        last_image_time is not None and time_sec <= last_image_time
        for time_sec in vlm_result_times
    )
    unhealthy_during_input_count = sum(
        not bool(_payload(record).get("healthy"))
        and last_image_time is not None
        and _trace_time(record) <= last_image_time
        for record in health_records
    )
    unhealthy_post_input_count = sum(
        not bool(_payload(record).get("healthy"))
        and last_image_time is not None
        and _trace_time(record) > last_image_time
        for record in health_records
    )
    completion_times = [
        _trace_time(record)
        for record in trace_records
        if record.get("layer") == "runtime_state"
        and _clean(_payload(record).get("execution_state"))
        in {"completed", "halted"}
    ]
    completion_time = min(completion_times) if completion_times else None
    commands_after_completion = [
        record
        for record in trace_records
        if record.get("layer") == "skill_command"
        and completion_time is not None
        and _trace_time(record) > completion_time
    ]
    sink_status_counts = Counter(
        _clean(_payload(record).get("status")) or "unknown"
        for record in trace_records
        if record.get("layer") == "shadow_sink"
    )
    skill_command_count = sum(
        record.get("layer") == "skill_command"
        for record in trace_records
    )
    semantic_admission_count = (
        sink_status_counts.get("admissible", 0)
        + sink_status_counts.get("instance_resolution_assumed", 0)
    )
    duplicate_suppressed_count = sink_status_counts.get(
        "duplicate_suppressed",
        0,
    )
    semantic_attempt_count = (
        semantic_admission_count + duplicate_suppressed_count
    )
    shadow_assumptions = [
        _parse_json_object(_payload(record).get("detail_json"))
        for record in trace_records
        if record.get("layer") == "reducer_event"
        and _clean(_payload(record).get("input_type"))
        == "shadow_state_assumption"
    ]
    shadow_assumption_counts = Counter(
        _clean(payload.get("event_type")) or "unknown"
        for payload in shadow_assumptions
    )
    replay_records = sorted(
        [
            record
            for record in trace_records
            if record.get("layer") == "shadow_replay_state"
        ],
        key=lambda record: _float(record.get("wall_time_sec")),
    )
    completed_replay_record = next(
        (
            record
            for record in replay_records
            if _clean(_payload(record).get("state")) == "completed"
        ),
        None,
    )
    final_replay_record = completed_replay_record or (
        replay_records[-1] if replay_records else None
    )
    final_replay_payload = (
        _payload(final_replay_record)
        if final_replay_record is not None
        else {}
    )
    replay_source_sec = _float(
        final_replay_payload.get("source_time_sec"),
    )
    replay_wall_sec = _float(
        final_replay_payload.get("wall_elapsed_sec"),
    )
    replay_hold_breakdown: Counter[str] = Counter()
    for current, following in zip(replay_records, replay_records[1:]):
        if (
            completed_replay_record is not None
            and _float(current.get("wall_time_sec"))
            >= _float(completed_replay_record.get("wall_time_sec"))
        ):
            break
        reason = _clean(_payload(current).get("hold_reason"))
        if not reason:
            continue
        delta = max(
            0.0,
            min(
                1.0,
                _float(following.get("wall_time_sec"))
                - _float(current.get("wall_time_sec")),
            ),
        )
        replay_hold_breakdown[reason] += delta
    return {
        "trace_record_count": len(trace_records),
        "trace_contract_error_count": len(trace_errors),
        "trace_contract_errors": trace_errors,
        "input_image_count": len(image_times),
        "input_image_duration_sec": (
            round(input_image_duration_sec, 6)
            if input_image_duration_sec is not None
            else None
        ),
        "input_transcript_count": sum(
            record.get("layer") == "input_transcript"
            for record in trace_records
        ),
        "source_transcript_count": sum(
            record.get("layer") == "input_transcript"
            and record.get("topic") == "/surgery/transcript"
            for record in trace_records
        ),
        "admitted_speech_count": sum(
            record.get("layer") == "input_transcript"
            and record.get("topic") == "/surgery/audio/request_text"
            for record in trace_records
        ),
        "vlm_result_count": len(vlm_result_times),
        "vlm_result_during_input_count": vlm_result_during_input_count,
        "vlm_effective_rate_hz": (
            round(vlm_result_during_input_count / input_image_duration_sec, 6)
            if input_image_duration_sec is not None
            and input_image_duration_sec > 0.0
            else None
        ),
        "vlm_health_count": len(health_records),
        "vlm_unhealthy_count": sum(
            not bool(payload.get("healthy"))
            for payload in health_payloads
        ),
        "vlm_unhealthy_during_input_count": unhealthy_during_input_count,
        "vlm_unhealthy_post_input_count": unhealthy_post_input_count,
        "vlm_parse_retry_count": sum(
            int(payload.get("parse_retry_count", 0))
            for payload in health_payloads
        ),
        "vlm_latency_sec": _distribution(latency),
        "input_image_gap_sec": _distribution(image_gaps),
        "replay_source_duration_sec": (
            round(replay_source_sec, 6)
            if replay_records
            else None
        ),
        "replay_wall_elapsed_sec": (
            round(replay_wall_sec, 6)
            if replay_records
            else None
        ),
        "replay_realtime_factor": (
            round(replay_source_sec / replay_wall_sec, 6)
            if replay_records and replay_wall_sec > 0.0
            else None
        ),
        "replay_elastic_hold_sec": (
            round(
                _float(final_replay_payload.get("elastic_hold_sec")),
                6,
            )
            if replay_records
            else None
        ),
        "replay_hold_breakdown_sec": {
            reason: round(duration, 3)
            for reason, duration in sorted(replay_hold_breakdown.items())
        },
        "completion_time_sec": completion_time,
        "skill_command_count": skill_command_count,
        "skill_command_semantic_admission_count": semantic_admission_count,
        "skill_command_duplicate_suppressed_count": duplicate_suppressed_count,
        "skill_command_instance_resolution_assumed_count": (
            sink_status_counts.get("instance_resolution_assumed", 0)
        ),
        "shadow_state_assumption_count": len(shadow_assumptions),
        "shadow_state_assumption_counts": dict(shadow_assumption_counts),
        "shadow_state_assumption_ground_truth_use_count": sum(
            bool(payload.get("ground_truth_used"))
            for payload in shadow_assumptions
        ),
        "skill_command_duplicate_rate": (
            duplicate_suppressed_count / semantic_attempt_count
            if semantic_attempt_count
            else None
        ),
        "counterfactual_skill_event_count": sum(
            record.get("layer") == "skill_event"
            and _clean(_payload(record).get("mode"))
            == "shadow_counterfactual"
            for record in trace_records
        ),
        "counterfactual_success_status_count": sum(
            record.get("layer") == "skill_status"
            and _clean(_payload(record).get("mode"))
            == "shadow_counterfactual"
            and _clean(_payload(record).get("state")) == "completed"
            and bool(_payload(record).get("success"))
            for record in trace_records
        ),
        "skill_command_after_completion_count": len(commands_after_completion),
        "shadow_sink_status_counts": dict(sink_status_counts),
    }


def load_tool_inventory(
    path: Path | None,
    identity_map: dict[str, str],
) -> dict[str, int]:
    if path is None:
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: procedure prompt must be an object")
    raw_inventory = payload.get("tool_inventory", {})
    if not isinstance(raw_inventory, dict):
        raise ValueError(f"{path}: tool_inventory must be an object")
    inventory: dict[str, int] = {}
    for raw_tool_id, raw_count in raw_inventory.items():
        tool_id = normalize_tool_id(raw_tool_id, identity_map)
        if not tool_id:
            raise ValueError(f"{path}: empty tool id in tool_inventory")
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count <= 0
        ):
            raise ValueError(
                f"{path}: tool_inventory.{raw_tool_id} must be a "
                "positive integer"
            )
        inventory[tool_id] = raw_count
    return inventory


def _raw_vlm_intent(
    record: dict[str, Any],
    *,
    layer: str,
) -> tuple[str, float]:
    if record.get("layer") != layer:
        return "", 0.0
    payload = _payload(record)
    raw = _parse_json_object(payload.get("raw_json"))
    intent = raw.get("intent")
    if not isinstance(intent, list) or not intent:
        return "", 0.0
    action = _clean(intent[0]).lower()
    confidence = _float(intent[2]) if len(intent) >= 3 else 0.0
    return action, confidence


def _evaluate_vlm_intent(
    trace_records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    *,
    lead_window_sec: float,
    evaluation_mask: EvaluationMask,
    layer: str = "vlm_raw",
) -> dict[str, Any]:
    layer_record_count = sum(
        record.get("layer") == layer for record in trace_records
    )
    if layer_record_count == 0:
        return {
            "status": "not_available",
            "reference": "confirmed handover target",
            "evaluated_count": 0,
            "correct_count": 0,
            "accuracy": None,
            "precision": None,
            "recall": None,
            "f1": None,
            "positive_record_count": 0,
            "false_positive_record_count": 0,
            "positive_episode_count": 0,
            "false_positive_episode_count": 0,
            "false_negative_count": 0,
            "events": [],
            "notes": [
                f"No {layer} records were captured in this trace."
            ],
        }
    intents = sorted(
        [
            {
                "time_sec": _prediction_time(record, layer),
                "publication_time_sec": _trace_time(record),
                "action": action,
                "confidence": confidence,
            }
            for record in trace_records
            for action, confidence in [
                _raw_vlm_intent(record, layer=layer)
            ]
            if action
        ],
        key=lambda item: float(item["time_sec"]),
    )
    positive_intents = [
        intent
        for intent in intents
        if (
            _clean(intent.get("action")) in HANDOVER_ACTIONS
            and evaluation_mask.metric_enabled_at(
                "action",
                float(intent["time_sec"]),
            )
        )
    ]
    intent_episodes: list[dict[str, Any]] = []
    for intent in positive_intents:
        observed_at = float(intent["time_sec"])
        if (
            not intent_episodes
            or observed_at - float(intent_episodes[-1]["end_sec"]) > 1.5
        ):
            intent_episodes.append(
                {
                    "start_sec": observed_at,
                    "end_sec": observed_at,
                    "samples": [intent],
                    "max_confidence": float(intent["confidence"]),
                }
            )
            continue
        intent_episodes[-1]["end_sec"] = observed_at
        intent_episodes[-1]["samples"].append(intent)
        intent_episodes[-1]["max_confidence"] = max(
            float(intent_episodes[-1]["max_confidence"]),
            float(intent["confidence"]),
        )

    matched_episode_indexes: set[int] = set()
    rows: list[dict[str, Any]] = []
    for target in targets:
        target_time = float(target["time_sec"])
        candidates = [
            (episode_index, sample)
            for episode_index, episode in enumerate(intent_episodes)
            if episode_index not in matched_episode_indexes
            for sample in episode["samples"]
            if (
                target_time - lead_window_sec
                <= float(sample["time_sec"])
                <= target_time
            )
        ]
        selected_pair = (
            max(candidates, key=lambda item: float(item[1]["time_sec"]))
            if candidates
            else None
        )
        if selected_pair is not None:
            matched_episode_indexes.add(selected_pair[0])
        selected = selected_pair[1] if selected_pair is not None else None
        action = _clean(selected.get("action")) if selected else ""
        correct = selected is not None
        rows.append(
            {
                "event_id": target.get("event_id"),
                "time_sec": target_time,
                "expected_intent": "handover",
                "predicted_intent": action or None,
                "confidence": (
                    selected.get("confidence") if selected is not None else None
                ),
                "correct": correct,
            }
        )
    correct_count = sum(bool(row["correct"]) for row in rows)
    false_positive_records = [
        intent
        for intent in positive_intents
        if not any(
            float(intent["time_sec"]) <= float(target["time_sec"])
            <= float(intent["time_sec"]) + lead_window_sec
            for target in targets
        )
    ]
    true_positive_count = correct_count
    false_positive_episode_count = (
        len(intent_episodes) - len(matched_episode_indexes)
    )
    false_negative_count = len(rows) - true_positive_count
    precision = (
        true_positive_count
        / (true_positive_count + false_positive_episode_count)
        if true_positive_count + false_positive_episode_count
        else None
    )
    recall = (
        true_positive_count
        / (true_positive_count + false_negative_count)
        if true_positive_count + false_negative_count
        else None
    )
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision is not None
        and recall is not None
        and precision + recall > 0.0
        else None
    )
    return {
        "status": "complete" if rows else "not_available",
        "reference": "confirmed handover target",
        "evaluated_count": len(rows),
        "correct_count": correct_count,
        "accuracy": recall,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "positive_record_count": len(positive_intents),
        "false_positive_record_count": len(false_positive_records),
        "positive_episode_count": len(intent_episodes),
        "false_positive_episode_count": false_positive_episode_count,
        "false_negative_count": false_negative_count,
        "events": rows,
        "notes": [
            "Intent scoring evaluates handover recognition independently of tool identity.",
            "Consecutive 1 Hz positive records are collapsed into intent episodes before one-to-one target matching.",
            "The compatibility accuracy field equals target recall; use precision, recall, and F1 for the balanced result.",
            "This clip currently supplies handover intent targets only; other intent classes are not represented in the denominator.",
            (
                "Model-raw intent uses the frozen input observation timestamp; "
                "operational intent uses result publication time."
            ),
        ],
    }


def _instrument_matches_endpoint(
    instrument: dict[str, Any],
    endpoint: dict[str, str],
) -> bool:
    holder = _clean(endpoint.get("holder")).lower()
    location = _clean(endpoint.get("location")).lower()
    owner = _clean(instrument.get("owner")).lower()
    lifecycle = _clean(instrument.get("lifecycle_stage")).lower()
    location_type = _clean(instrument.get("location_type")).lower()
    location_id = _clean(instrument.get("location_id")).lower()
    if holder in {"surgeon", "operative_recipient"}:
        return (
            owner == "surgeon"
            or lifecycle == "surgeon_owned"
            or "surgeon" in location_type
            or "surgeon" in location_id
        )
    if holder == "none" and location == "mayo_stand":
        return (
            lifecycle.startswith("mayo_")
            or "mayo" in location_type
            or "mayo" in location_id
        )
    return False


def _evaluate_dt_tool_endpoints(
    trace_records: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    *,
    identity_map: dict[str, str],
    evaluation_mask: EvaluationMask,
    tool_inventory: dict[str, int],
    settle_window_sec: float = 0.75,
) -> dict[str, Any]:
    snapshots = sorted(
        [
            (
                _trace_time(record),
                int(record.get("sequence", 0)),
                _payload(record),
            )
            for record in trace_records
            if record.get("layer") == "reducer_fused"
        ],
        key=lambda item: (item[0], item[1]),
    )
    rows: list[dict[str, Any]] = []
    skipped = Counter()
    state_cutoff = evaluation_mask.cutoffs.get("state_audit_end_sec")
    for event in ground_truth:
        if (
            _clean(event.get("review_status", "confirmed")) != "confirmed"
            or _clean(event.get("event_type")) != "tool_transfer"
        ):
            continue
        if not evaluation_mask.event_policy(event)[
            "metric_eligibility"
        ]["state"]:
            skipped["event_masked_state"] += 1
            continue
        time_sec = _float(event.get("time_sec"), default=-1.0)
        if time_sec < 0.0 or (
            state_cutoff is not None and time_sec > state_cutoff
        ):
            skipped["outside_state_window"] += 1
            continue
        endpoint = event_endpoint(event, "to")
        if not (
            endpoint.get("holder") in {"surgeon", "operative_recipient"}
            or (
                endpoint.get("holder") == "none"
                and endpoint.get("location") == "mayo_stand"
            )
        ):
            skipped["endpoint_not_represented_by_dt"] += 1
            continue
        tool_id = normalize_tool_id(event_tool_id(event), identity_map)
        if not tool_id:
            skipped["missing_tool_id"] += 1
            continue
        eligible_snapshots = [
            snapshot
            for snapshot in snapshots
            if snapshot[0] <= time_sec + settle_window_sec
        ]
        snapshot = eligible_snapshots[-1] if eligible_snapshots else None
        if snapshot is None:
            rows.append(
                {
                    "event_id": event.get("event_id"),
                    "time_sec": time_sec,
                    "tool_id": tool_id,
                    "expected_endpoint": event_endpoint_key(event, "to"),
                    "observed_endpoint": None,
                    "correct": False,
                    "reason": "no digital-twin snapshot available",
                }
            )
            continue
        instruments = [
            item
            for item in snapshot[2].get("instrument_states", [])
            if normalize_tool_id(
                item.get("instrument_id"),
                identity_map,
            )
            == tool_id
        ]
        matching_instrument = next(
            (
                item
                for item in instruments
                if _instrument_matches_endpoint(item, endpoint)
            ),
            None,
        )
        instrument = matching_instrument or (
            instruments[0] if instruments else None
        )
        correct = matching_instrument is not None
        rows.append(
            {
                "event_id": event.get("event_id"),
                "time_sec": time_sec,
                "sample_time_sec": snapshot[0],
                "tool_id": tool_id,
                "expected_endpoint": event_endpoint_key(event, "to"),
                "observed_endpoint": (
                    {
                        "owner": instrument.get("owner"),
                        "instance_id": instrument.get("instance_id"),
                        "location_type": instrument.get("location_type"),
                        "location_id": instrument.get("location_id"),
                        "lifecycle_stage": instrument.get("lifecycle_stage"),
                    }
                    if instrument is not None
                    else None
                ),
                "correct": correct,
                "reason": (
                    "type-level endpoint matched an instrument instance"
                    if correct
                    else "type-level endpoint did not match"
                ),
            }
        )
    correct_count = sum(bool(row["correct"]) for row in rows)
    inventory_total = sum(tool_inventory.values())
    duplicate_type_count = sum(
        count > 1 for count in tool_inventory.values()
    )
    latest_instruments = (
        list(snapshots[-1][2].get("instrument_states", []))
        if snapshots
        else []
    )
    observed_inventory = Counter(
        normalize_tool_id(item.get("instrument_id"), identity_map)
        for item in latest_instruments
        if normalize_tool_id(item.get("instrument_id"), identity_map)
    )
    instance_ids = [
        _clean(item.get("instance_id"))
        for item in latest_instruments
    ]
    missing_instance_id_count = sum(
        not instance_id for instance_id in instance_ids
    )
    duplicate_instance_ids = sorted(
        instance_id
        for instance_id, count in Counter(instance_ids).items()
        if instance_id and count > 1
    )
    inventory_keys = set(tool_inventory) | set(observed_inventory)
    count_mismatches = {
        tool_id: {
            "expected": int(tool_inventory.get(tool_id, 0)),
            "observed": int(observed_inventory.get(tool_id, 0)),
        }
        for tool_id in sorted(inventory_keys)
        if int(tool_inventory.get(tool_id, 0))
        != int(observed_inventory.get(tool_id, 0))
    }
    inventory_union_count = sum(
        max(
            int(tool_inventory.get(tool_id, 0)),
            int(observed_inventory.get(tool_id, 0)),
        )
        for tool_id in inventory_keys
    )
    inventory_match_count = sum(
        min(
            int(tool_inventory.get(tool_id, 0)),
            int(observed_inventory.get(tool_id, 0)),
        )
        for tool_id in inventory_keys
    )
    inventory_scorable = bool(
        tool_inventory
        and latest_instruments
        and not missing_instance_id_count
    )
    inventory_accuracy = (
        inventory_match_count / inventory_union_count
        if inventory_scorable and inventory_union_count
        else None
    )
    inventory_status = (
        "complete_declared_inventory_conservation"
        if inventory_scorable
        else (
            "not_scorable_missing_declared_inventory"
            if not tool_inventory
            else (
                "not_scorable_no_runtime_snapshot"
                if not latest_instruments
                else "not_scorable_runtime_state_has_no_instance_ids"
            )
        )
    )
    endpoint_status = (
        "complete_type_level_development"
        if rows
        else (
            "inventory_conservation_only"
            if inventory_scorable
            else "not_available"
        )
    )
    return {
        "status": (
            endpoint_status
        ),
        "reference_quality": "confirmed transfer endpoints",
        "evaluated_count": len(rows),
        "correct_count": correct_count,
        "endpoint_accuracy": correct_count / len(rows) if rows else None,
        "settle_window_sec": settle_window_sec,
        "skipped_counts": dict(skipped),
        "inventory_contract": {
            "tool_type_count": len(tool_inventory),
            "physical_instance_count": inventory_total,
            "duplicate_tool_type_count": duplicate_type_count,
            "counts": dict(sorted(tool_inventory.items())),
            "observed_physical_instance_count": len(latest_instruments),
            "observed_counts": dict(sorted(observed_inventory.items())),
            "missing_instance_id_count": missing_instance_id_count,
            "duplicate_instance_ids": duplicate_instance_ids,
            "count_mismatches": count_mismatches,
        },
        "instance_inventory_accuracy": inventory_accuracy,
        "instance_inventory_status": inventory_status,
        "events": rows,
        "notes": [
            "Endpoint accuracy compares the DT state with observed destination holders at confirmed transfer times.",
            "Endpoint labels remain type-level; a match means at least one tracked instance of that type reached the observed endpoint.",
            "Instance inventory accuracy measures conservation against the declared procedure inventory, not physical identity against video labels.",
        ],
    }


def _expected_terminal_skill_events(action: str) -> set[str]:
    if action in PREPARATION_ACTIONS:
        return {"ToolPrepared"}
    if action in HANDOVER_ACTIONS:
        return {
            "ToolHandoverCompleted",
            "ShadowAdditionalToolHandoverCompleted",
        }
    if action in RECOVERY_ACTIONS:
        return {"ToolReturnedToTray"}
    if action == "return_unused_preposition":
        return {
            "PredictedToolReturnedToRack",
            "UnusedPrepositionReturned",
        }
    return set()


def _reference_event_time(
    event: dict[str, Any],
    *,
    prefer_start: bool = False,
) -> float:
    if prefer_start and event.get("start_sec") is not None:
        return _float(event.get("start_sec"))
    return _float(event.get("time_sec", event.get("start_sec", 0.0)))


def _is_request_reference(event: dict[str, Any]) -> bool:
    event_type = _clean(event.get("event_type")).lower()
    return (
        event_type in REQUEST_EVENT_TYPES
        or event_type.endswith("_tool_request")
    )


def _request_tool_id(
    event: dict[str, Any],
    identity_map: dict[str, str],
) -> str:
    candidate = (
        event.get("requested_tool")
        or event.get("instrument_id")
        or event_tool_id(event)
    )
    if isinstance(candidate, dict):
        candidate = candidate.get("id") or candidate.get("tool_id")
    return normalize_tool_id(candidate, identity_map)


def _linked_reference_ids(event: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        event.get("handover_event_id"),
        event.get("target_event_id"),
        event.get("linked_event_id"),
        event.get("related_event_id"),
    ]
    links = event.get("links")
    if isinstance(links, dict):
        values.extend(links.values())
    elif isinstance(links, list):
        values.extend(links)
    return {_clean(value) for value in values if _clean(value)}


def _request_activation_wall_times(
    trace_records: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    activations: dict[str, dict[str, float]] = {}
    for record in sorted(
        trace_records,
        key=lambda row: (
            _wall_time(row)
            if _wall_time(row) is not None
            else math.inf,
            int(row.get("sequence", 0) or 0),
        ),
    ):
        if record.get("layer") != "evaluation_ground_truth":
            continue
        payload = _payload(record)
        if payload.get("evaluation_only") is not True:
            continue
        request = payload.get("implicit_tool_request")
        if not isinstance(request, dict) or request.get("active") is not True:
            continue
        event_id = _clean(request.get("event_id"))
        wall_time = _wall_time(record)
        if not event_id or wall_time is None:
            continue
        activations.setdefault(
            event_id,
            {
                "wall_time_sec": wall_time,
                "source_time_sec": _float(
                    payload.get("source_time_sec"),
                    default=_trace_time(record),
                ),
            },
        )
    return activations


def _runtime_tool_matches(
    value: Any,
    target_tool_id: str,
    identity_map: dict[str, str],
) -> bool:
    candidate = normalize_tool_id(value, identity_map)
    if candidate == target_tool_id:
        return True
    if "#" in candidate:
        return (
            normalize_tool_id(candidate.split("#", 1)[0], identity_map)
            == target_tool_id
        )
    return False


def _world_request_fact_signatures(
    payload: dict[str, Any],
    identity_map: dict[str, str],
) -> set[tuple[str, str, int]]:
    signatures: set[tuple[str, str, int]] = set()

    explicit_tool = normalize_tool_id(
        payload.get("explicit_request_tool")
        or payload.get("surgeon_request_tool"),
        identity_map,
    )
    if explicit_tool:
        signatures.add(
            (
                "explicit_request",
                explicit_tool,
                int(payload.get("surgeon_request_generation", 0) or 0),
            )
        )

    if payload.get("implicit_request_visible") is not True:
        return signatures
    hand_pose = _clean(payload.get("implicit_request_hand_pose")).lower()
    if hand_pose and hand_pose != "open_receive":
        return signatures
    implicit_tool = normalize_tool_id(
        payload.get("implicit_request_tool"),
        identity_map,
    )
    predicted_tool = normalize_tool_id(
        payload.get("predicted_tool")
        or payload.get("predicted_tool_id"),
        identity_map,
    )
    if implicit_tool and predicted_tool and implicit_tool != predicted_tool:
        return signatures
    visual_target = implicit_tool or predicted_tool
    if visual_target:
        signatures.add(
            (
                "visual_implicit_request",
                visual_target,
                int(payload.get("implicit_request_generation", 0) or 0),
            )
        )
    return signatures


def _reducer_request_fact_signatures(
    record: dict[str, Any],
    identity_map: dict[str, str],
) -> set[tuple[str, str, int]]:
    if record.get("layer") != "reducer_fused":
        return set()
    return _world_request_fact_signatures(_payload(record), identity_map)


def _request_pipeline_activations(
    *,
    trace_records: list[dict[str, Any]],
    requested_pairs: list[dict[str, Any]],
    identity_map: dict[str, str],
) -> dict[str, dict[str, dict[str, Any] | None]]:
    ordered_records = sorted(
        trace_records,
        key=lambda row: (
            _trace_time(row),
            _wall_time(row)
            if _wall_time(row) is not None
            else math.inf,
            int(row.get("sequence", 0) or 0),
        ),
    )

    fact_episodes: list[dict[str, Any]] = []
    active_fact_signatures: set[tuple[str, str, int]] = set()
    for record in ordered_records:
        if record.get("layer") != "reducer_fused":
            continue
        signatures = _reducer_request_fact_signatures(
            record,
            identity_map,
        )
        for source, tool_id, generation in sorted(
            signatures - active_fact_signatures
        ):
            fact_episodes.append(
                {
                    "record_id": (
                        "reducer:"
                        + str(record.get("sequence", len(fact_episodes)))
                    ),
                    "source": source,
                    "tool_id": tool_id,
                    "generation": generation,
                    "source_time_sec": _trace_time(record),
                    "wall_time_sec": _wall_time(record),
                }
            )
        active_fact_signatures = signatures

    bt_ingress_episodes: list[dict[str, Any]] = []
    for record in ordered_records:
        if record.get("layer") != "bt_context_ingress":
            continue
        for source, tool_id, generation in sorted(
            _world_request_fact_signatures(
                _payload(record),
                identity_map,
            )
        ):
            bt_ingress_episodes.append(
                {
                    "record_id": (
                        "bt-ingress:"
                        + str(record.get("sequence", len(bt_ingress_episodes)))
                        + f":{source}:{generation}"
                    ),
                    "source": source,
                    "tool_id": tool_id,
                    "generation": generation,
                    "source_time_sec": _trace_time(record),
                    "wall_time_sec": _wall_time(record),
                }
            )

    bt_episodes: list[dict[str, Any]] = []
    for record in ordered_records:
        if record.get("layer") != "bt_decision":
            continue
        payload = _payload(record)
        action = _clean(payload.get("action")).lower()
        decision = _clean(payload.get("decision")).lower()
        request_generation = int(
            payload.get("request_generation", 0) or 0
        )
        is_acceptance = (
            action in HANDOVER_ACTIONS - PREPARATION_ACTIONS
            and (
                decision in {"explicit_request", "implicit_request"}
                or request_generation > 0
            )
        )
        if request_generation <= 0 and decision != "implicit_request":
            continue
        bt_episodes.append(
            {
                "record_id": (
                    "bt:"
                    + str(record.get("sequence", len(bt_episodes)))
                ),
                "source": decision or "request_backed",
                "decision": decision,
                "action": action,
                "generation": request_generation,
                "is_acceptance": is_acceptance,
                "tool_id": normalize_tool_id(
                    payload.get("selected_tool")
                    or payload.get("instrument_id"),
                    identity_map,
                ),
                "source_time_sec": _trace_time(record),
                "wall_time_sec": _wall_time(record),
            }
        )

    results: dict[str, dict[str, dict[str, Any] | None]] = {}
    used_fact_ids: set[str] = set()
    used_bt_ingress_ids: set[str] = set()
    used_bt_evaluation_ids: set[str] = set()
    used_bt_acceptance_ids: set[str] = set()
    for index, pair in enumerate(requested_pairs):
        event_id = _clean(pair.get("request_event_id"))
        if not event_id:
            continue
        request_time = float(pair["request_time_sec"])
        previous_request_time = (
            float(requested_pairs[index - 1]["request_time_sec"])
            if index > 0
            else None
        )
        next_request_time = (
            float(requested_pairs[index + 1]["request_time_sec"])
            if index + 1 < len(requested_pairs)
            else math.inf
        )
        earliest = (
            request_time - REQUEST_HANDOVER_EARLY_MATCH_TOLERANCE_SEC
        )
        if previous_request_time is not None:
            earliest = max(earliest, previous_request_time)

        fact = next(
            (
                row
                for row in fact_episodes
                if row["record_id"] not in used_fact_ids
                and earliest <= row["source_time_sec"] < next_request_time
                and _runtime_tool_matches(
                    row["tool_id"],
                    pair["tool_id"],
                    identity_map,
                )
            ),
            None,
        )
        if fact is not None:
            used_fact_ids.add(fact["record_id"])

        def bt_matches_fact(row: dict[str, Any]) -> bool:
            if fact is None:
                return False
            if row["source_time_sec"] < fact["source_time_sec"]:
                return False
            if fact["source"] == "explicit_request":
                generation = int(fact.get("generation", 0) or 0)
                if generation > 0:
                    return row["generation"] == generation
                return (
                    row["source"] == "explicit_request"
                    and _runtime_tool_matches(
                        row["tool_id"],
                        fact["tool_id"],
                        identity_map,
                    )
                )
            return (
                row["source"] == "implicit_request"
                and _runtime_tool_matches(
                    row["tool_id"],
                    fact["tool_id"],
                    identity_map,
                )
            )

        def ingress_matches_fact(row: dict[str, Any]) -> bool:
            if fact is None:
                return False
            if row["source_time_sec"] < fact["source_time_sec"]:
                return False
            if row["source"] != fact["source"]:
                return False
            if not _runtime_tool_matches(
                row["tool_id"],
                fact["tool_id"],
                identity_map,
            ):
                return False
            generation = int(fact.get("generation", 0) or 0)
            return generation <= 0 or row["generation"] == generation

        bt_ingress = next(
            (
                row
                for row in bt_ingress_episodes
                if row["record_id"] not in used_bt_ingress_ids
                and row["source_time_sec"] < next_request_time
                and ingress_matches_fact(row)
            ),
            None,
        )
        if bt_ingress is not None:
            used_bt_ingress_ids.add(bt_ingress["record_id"])

        bt_evaluation = next(
            (
                row
                for row in bt_episodes
                if row["record_id"] not in used_bt_evaluation_ids
                and row["source_time_sec"] < next_request_time
                and bt_matches_fact(row)
            ),
            None,
        )
        if bt_evaluation is not None:
            used_bt_evaluation_ids.add(bt_evaluation["record_id"])

        bt_acceptance = next(
            (
                row
                for row in bt_episodes
                if row["record_id"] not in used_bt_acceptance_ids
                and row["is_acceptance"]
                and row["source_time_sec"] < next_request_time
                and bt_matches_fact(row)
            ),
            None,
        )
        if bt_acceptance is not None:
            used_bt_acceptance_ids.add(bt_acceptance["record_id"])
        results[event_id] = {
            "dt_request_fact": fact,
            "bt_context_ingress": bt_ingress,
            "bt_request_evaluation": bt_evaluation,
            "bt_request_acceptance": bt_acceptance,
        }
    return results


def _non_negative_elapsed(
    *,
    start_sec: float | None,
    end_sec: float | None,
) -> float | None:
    if start_sec is None or end_sec is None:
        return None
    elapsed = end_sec - start_sec
    if elapsed < -1e-6:
        return None
    return round(max(0.0, elapsed), 6)


def _pair_requests_to_handovers(
    *,
    ground_truth: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    identity_map: dict[str, str],
    evaluation_mask: EvaluationMask,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []
    for event in ground_truth:
        if (
            event.get("review_status") != "confirmed"
            or not _is_request_reference(event)
        ):
            continue
        request_time = _reference_event_time(event, prefer_start=True)
        if not evaluation_mask.metric_enabled_at("latency", request_time):
            continue
        requests.append(
            {
                "event": strip_internal_fields(event),
                "event_id": _clean(event.get("event_id")),
                "time_sec": request_time,
                "end_sec": _float(
                    event.get("end_sec"),
                    default=request_time,
                ),
                "tool_id": _request_tool_id(event, identity_map),
                "linked_ids": _linked_reference_ids(event),
            }
        )
    requests.sort(key=lambda row: (row["time_sec"], row["event_id"]))

    used_request_indices: set[int] = set()
    pairs: list[dict[str, Any]] = []
    for target in sorted(
        targets,
        key=lambda row: (
            _reference_event_time(row),
            _clean(row.get("event_id")),
        ),
    ):
        target_time = _reference_event_time(target)
        target_id = _clean(target.get("event_id"))
        target_tool = normalize_tool_id(
            event_tool_id(target),
            identity_map,
        )
        candidates: list[tuple[int, dict[str, Any]]] = []
        for index, request in enumerate(requests):
            if index in used_request_indices:
                continue
            if request["time_sec"] > target_time:
                continue
            if request["tool_id"] and request["tool_id"] != target_tool:
                continue
            candidates.append((index, request))

        linked = [
            candidate
            for candidate in candidates
            if target_id in candidate[1]["linked_ids"]
        ]
        selected = max(
            linked or candidates,
            key=lambda candidate: (
                candidate[1]["time_sec"],
                candidate[1]["event_id"],
            ),
            default=None,
        )
        request: dict[str, Any] | None = None
        if selected is not None:
            used_request_indices.add(selected[0])
            request = selected[1]
        pairs.append(
            {
                "target": target,
                "target_event_id": target_id,
                "target_time_sec": target_time,
                "tool_id": target_tool,
                "request": request,
                "request_event_id": (
                    request["event_id"] if request is not None else None
                ),
                "request_time_sec": (
                    request["time_sec"] if request is not None else None
                ),
            }
        )
    unmatched_requests = [
        request
        for index, request in enumerate(requests)
        if index not in used_request_indices
    ]
    return pairs, unmatched_requests


def _skill_action_outcomes(
    trace_records: list[dict[str, Any]],
    *,
    identity_map: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    commands: dict[str, dict[str, Any]] = {}
    for record in trace_records:
        if record.get("layer") != "skill_command":
            continue
        payload = _payload(record)
        command_id = _clean(payload.get("command_id"))
        if not command_id:
            command_id = f"skill-command:{record.get('sequence', 0)}"
        commands.setdefault(
            command_id,
            {
                "command_id": command_id,
                "action": _clean(payload.get("action")).lower(),
                "tool_id": normalize_tool_id(
                    payload.get("instrument_id"),
                    identity_map,
                ),
                "instance_id": _clean(
                    payload.get("instrument_instance_id")
                ),
                "arm": _clean(payload.get("arm")).lower(),
                "command_time_sec": _trace_time(record),
                "command_wall_time_sec": _wall_time(record),
                "command_sequence": int(
                    record.get("sequence", 0) or 0
                ),
            },
        )

    completed_status: dict[str, dict[str, Any]] = {}
    for record in trace_records:
        if record.get("layer") != "skill_status":
            continue
        payload = _payload(record)
        command_id = _clean(payload.get("command_id"))
        if (
            command_id
            and _clean(payload.get("state")).lower() == "completed"
            and bool(payload.get("success"))
        ):
            completed_status.setdefault(
                command_id,
                {
                    "time_sec": _trace_time(record),
                    "wall_time_sec": _wall_time(record),
                    "sequence": int(record.get("sequence", 0) or 0),
                    "payload": payload,
                },
            )

    events_by_command: dict[str, list[dict[str, Any]]] = {}
    event_only: list[dict[str, Any]] = []
    for record in trace_records:
        if record.get("layer") != "skill_event":
            continue
        payload = _payload(record)
        detail = _parse_json_object(payload.get("detail_json"))
        command_id = _clean(
            payload.get("command_id") or detail.get("command_id")
        )
        row = {
            "command_id": command_id,
            "event_type": _clean(payload.get("event_type")),
            "tool_id": normalize_tool_id(
                payload.get("instrument_id")
                or detail.get("instrument_id"),
                identity_map,
            ),
            "instance_id": _clean(
                payload.get("instance_id")
                or payload.get("instrument_instance_id")
                or detail.get("instrument_instance_id")
                or detail.get("instance_id")
            ),
            "arm": _clean(
                payload.get("arm") or detail.get("arm")
            ).lower(),
            "time_sec": _trace_time(record),
            "wall_time_sec": _wall_time(record),
            "sequence": int(record.get("sequence", 0) or 0),
        }
        if command_id:
            events_by_command.setdefault(command_id, []).append(row)
        else:
            event_only.append(row)

    outcomes: dict[str, list[dict[str, Any]]] = {
        "preparations": [],
        "handovers": [],
        "returns": [],
    }
    observed_keys: set[tuple[str, str]] = set()

    def append_outcome(
        kind: str,
        *,
        command_id: str,
        action: str,
        tool_id: str,
        instance_id: str,
        arm: str,
        command_time_sec: float,
        completion_time_sec: float,
        command_wall_time_sec: float | None,
        completion_wall_time_sec: float | None,
        completion_sequence: int,
        source: str,
    ) -> None:
        key = (kind, command_id)
        if key in observed_keys or not tool_id:
            return
        observed_keys.add(key)
        outcomes[kind].append(
            {
                "outcome_id": f"{kind}:{command_id}",
                "command_id": command_id,
                "action": action,
                "tool_id": tool_id,
                "instance_id": instance_id or None,
                "arm": arm or None,
                "command_time_sec": command_time_sec,
                "completion_time_sec": completion_time_sec,
                "command_wall_time_sec": command_wall_time_sec,
                "completion_wall_time_sec": completion_wall_time_sec,
                "completion_sequence": completion_sequence,
                "source": source,
            }
        )

    handover_events = {
        "ToolHandoverCompleted",
        "ShadowAdditionalToolHandoverCompleted",
    }
    for command_id, command in commands.items():
        event_rows = events_by_command.get(command_id, [])
        action = command["action"]
        tool_id = command["tool_id"]
        status = completed_status.get(command_id)
        if not tool_id and status is not None:
            tool_id = normalize_tool_id(
                status["payload"].get("instrument_id"),
                identity_map,
            )

        prepared = next(
            (
                row
                for row in event_rows
                if row["event_type"] == "ToolPrepared"
            ),
            None,
        )
        if action in PREPARATION_ACTIONS and (prepared or status):
            append_outcome(
                "preparations",
                command_id=command_id,
                action=action,
                tool_id=tool_id or (prepared or {}).get("tool_id", ""),
                instance_id=(
                    (prepared or {}).get("instance_id")
                    or command["instance_id"]
                ),
                arm=(
                    (prepared or {}).get("arm")
                    or command["arm"]
                ),
                command_time_sec=command["command_time_sec"],
                completion_time_sec=(
                    prepared["time_sec"]
                    if prepared is not None
                    else status["time_sec"]
                ),
                command_wall_time_sec=command["command_wall_time_sec"],
                completion_wall_time_sec=(
                    prepared["wall_time_sec"]
                    if prepared is not None
                    else status["wall_time_sec"]
                ),
                completion_sequence=(
                    prepared["sequence"]
                    if prepared is not None
                    else status["sequence"]
                ),
                source=(
                    "ToolPrepared"
                    if prepared is not None
                    else "successful_skill_status"
                ),
            )

        handed_over = next(
            (
                row
                for row in event_rows
                if row["event_type"] in handover_events
            ),
            None,
        )
        if (
            action in HANDOVER_ACTIONS - PREPARATION_ACTIONS
            and (handed_over or status)
        ):
            append_outcome(
                "handovers",
                command_id=command_id,
                action=action,
                tool_id=tool_id or (handed_over or {}).get("tool_id", ""),
                instance_id=(
                    (handed_over or {}).get("instance_id")
                    or command["instance_id"]
                ),
                arm=(
                    (handed_over or {}).get("arm")
                    or command["arm"]
                ),
                command_time_sec=command["command_time_sec"],
                completion_time_sec=(
                    handed_over["time_sec"]
                    if handed_over is not None
                    else status["time_sec"]
                ),
                command_wall_time_sec=command["command_wall_time_sec"],
                completion_wall_time_sec=(
                    handed_over["wall_time_sec"]
                    if handed_over is not None
                    else status["wall_time_sec"]
                ),
                completion_sequence=(
                    handed_over["sequence"]
                    if handed_over is not None
                    else status["sequence"]
                ),
                source=(
                    handed_over["event_type"]
                    if handed_over is not None
                    else "successful_skill_status"
                ),
            )

        returned = next(
            (
                row
                for row in event_rows
                if row["event_type"]
                in {
                    "PredictedToolReturnedToRack",
                    "UnusedPrepositionReturned",
                }
            ),
            None,
        )
        if returned is not None or (
            action in UNUSED_PREPOSITION_RETURN_ACTIONS
            and status is not None
        ):
            append_outcome(
                "returns",
                command_id=command_id,
                action=action,
                tool_id=(
                    returned.get("tool_id", "")
                    if returned is not None
                    else tool_id
                ),
                instance_id=(
                    returned.get("instance_id")
                    if returned is not None
                    else command["instance_id"]
                ),
                arm=(
                    returned.get("arm")
                    if returned is not None
                    else command["arm"]
                ),
                command_time_sec=command["command_time_sec"],
                completion_time_sec=(
                    returned["time_sec"]
                    if returned is not None
                    else status["time_sec"]
                ),
                command_wall_time_sec=command["command_wall_time_sec"],
                completion_wall_time_sec=(
                    returned["wall_time_sec"]
                    if returned is not None
                    else status["wall_time_sec"]
                ),
                completion_sequence=(
                    returned["sequence"]
                    if returned is not None
                    else status["sequence"]
                ),
                source=(
                    returned["event_type"]
                    if returned is not None
                    else "successful_skill_status"
                ),
            )

    event_kind = {
        "ToolPrepared": "preparations",
        "ToolHandoverCompleted": "handovers",
        "ShadowAdditionalToolHandoverCompleted": "handovers",
        "PredictedToolReturnedToRack": "returns",
        "UnusedPrepositionReturned": "returns",
    }
    for command_id, rows in events_by_command.items():
        if command_id not in commands:
            event_only.extend(rows)
    for row in event_only:
        kind = event_kind.get(row["event_type"])
        if not kind:
            continue
        append_outcome(
            kind,
            command_id=f"event:{row['sequence']}",
            action={
                "preparations": "prepare_tool",
                "handovers": "handover",
                "returns": "return_unused_preposition",
            }[kind],
            tool_id=row["tool_id"],
            instance_id=row["instance_id"],
            arm=row["arm"],
            command_time_sec=row["time_sec"],
            completion_time_sec=row["time_sec"],
            command_wall_time_sec=row["wall_time_sec"],
            completion_wall_time_sec=row["wall_time_sec"],
            completion_sequence=row["sequence"],
            source=row["event_type"],
        )

    for rows in outcomes.values():
        rows.sort(
            key=lambda row: (
                row["completion_time_sec"],
                row["outcome_id"],
            )
        )
    return outcomes


def _same_physical_outcome(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    if left.get("tool_id") != right.get("tool_id"):
        return False
    left_instance = _clean(left.get("instance_id"))
    right_instance = _clean(right.get("instance_id"))
    if left_instance and right_instance:
        return left_instance == right_instance
    return True


def _outcome_order_key(
    outcome: dict[str, Any],
) -> tuple[float, int]:
    return (
        _float(outcome.get("completion_time_sec")),
        int(outcome.get("completion_sequence", 0) or 0),
    )


def _same_robot_arm(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    left_arm = _clean(left.get("arm")).lower()
    right_arm = _clean(right.get("arm")).lower()
    return bool(left_arm and right_arm and left_arm == right_arm)


def _preparation_invalidation_at_cutoff(
    preparation: dict[str, Any],
    *,
    cutoff_sec: float,
    preparations: list[dict[str, Any]],
    handovers: list[dict[str, Any]],
    returns: list[dict[str, Any]],
) -> dict[str, Any] | None:
    prepared_order = _outcome_order_key(preparation)
    invalidations: list[dict[str, Any]] = []

    def add_if_after(
        outcome: dict[str, Any],
        reason: str,
    ) -> None:
        outcome_order = _outcome_order_key(outcome)
        if (
            outcome_order <= prepared_order
            or outcome_order[0] > cutoff_sec
        ):
            return
        invalidations.append(
            {
                "reason": reason,
                "outcome_id": outcome["outcome_id"],
                "time_sec": outcome_order[0],
                "sequence": outcome_order[1],
            }
        )

    for outcome in returns:
        if _same_physical_outcome(outcome, preparation):
            add_if_after(outcome, "returned")
    for outcome in handovers:
        if _same_physical_outcome(outcome, preparation):
            add_if_after(outcome, "consumed")
        elif _same_robot_arm(outcome, preparation):
            add_if_after(outcome, "displaced")
    for outcome in preparations:
        if (
            outcome["outcome_id"] != preparation["outcome_id"]
            and _same_robot_arm(outcome, preparation)
        ):
            add_if_after(outcome, "superseded")

    return min(
        invalidations,
        key=lambda row: (row["time_sec"], row["sequence"]),
        default=None,
    )


def _invariant_violation_audit(
    trace_records: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    active_bt_signature = ""
    for record in sorted(
        trace_records,
        key=lambda row: (
            _trace_time(row),
            int(row.get("sequence", 0) or 0),
        ),
    ):
        layer = _clean(record.get("layer"))
        if layer not in {
            "bt_decision",
            "reducer_event",
            "shadow_sink",
            "skill_event",
        }:
            continue
        payload = _payload(record)
        direct_values = {
            "event_type": _clean(payload.get("event_type")),
            "input_type": _clean(payload.get("input_type")),
            "reason": _clean(
                payload.get("reason")
                or payload.get("decision_reason")
            ),
            "guard": _clean(
                payload.get("blocking_guard")
                or payload.get("safety_status")
            ),
        }
        signature = "|".join(
            value.casefold() for value in direct_values.values() if value
        )
        is_violation = (
            "invariant" in signature
            or "blocked_invariant" in signature
        )
        if layer == "bt_decision":
            if not is_violation:
                active_bt_signature = ""
                continue
            if signature == active_bt_signature:
                continue
            active_bt_signature = signature
        elif not is_violation:
            continue

        event_id = _clean(
            payload.get("event_id")
            or payload.get("input_id")
            or payload.get("command_id")
            or record.get("correlation_id")
        )
        dedupe_id = (
            f"{layer}:{event_id}"
            if event_id
            else f"{layer}:{record.get('sequence', len(rows))}"
        )
        if dedupe_id in seen_ids:
            continue
        seen_ids.add(dedupe_id)
        rows.append(
            {
                "source_layer": layer,
                "time_sec": _trace_time(record),
                "event_id": event_id or None,
                **direct_values,
            }
        )
    return {
        "count": len(rows),
        "by_source_layer": dict(
            Counter(row["source_layer"] for row in rows)
        ),
        "events": rows,
    }


def _evaluate_behavior_quality(
    *,
    trace_records: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    identity_map: dict[str, str],
    evaluation_mask: EvaluationMask,
) -> dict[str, Any]:
    pairs, unmatched_requests = _pair_requests_to_handovers(
        ground_truth=ground_truth,
        targets=targets,
        identity_map=identity_map,
        evaluation_mask=evaluation_mask,
    )
    outcomes = _skill_action_outcomes(
        trace_records,
        identity_map=identity_map,
    )
    preparations = outcomes["preparations"]
    handovers = outcomes["handovers"]
    returns = outcomes["returns"]
    request_wall_activations = _request_activation_wall_times(
        trace_records
    )

    used_preparations: set[str] = set()
    coverage_rows: list[dict[str, Any]] = []
    for pair in pairs:
        cutoff = (
            pair["request_time_sec"]
            if pair["request_time_sec"] is not None
            else pair["target_time_sec"]
        )
        candidates: list[dict[str, Any]] = []
        stale_preparations: list[dict[str, Any]] = []
        for preparation in preparations:
            if (
                preparation["outcome_id"] in used_preparations
                or preparation["tool_id"] != pair["tool_id"]
                or preparation["completion_time_sec"] > cutoff
            ):
                continue
            invalidation = _preparation_invalidation_at_cutoff(
                preparation,
                cutoff_sec=cutoff,
                preparations=preparations,
                handovers=handovers,
                returns=returns,
            )
            if invalidation is None:
                candidates.append(preparation)
            else:
                stale_preparations.append(
                    {
                        "preparation_outcome_id": preparation[
                            "outcome_id"
                        ],
                        "instance_id": preparation.get("instance_id"),
                        "invalidated_by_outcome_id": invalidation[
                            "outcome_id"
                        ],
                        "invalidation_reason": invalidation["reason"],
                        "invalidation_time_sec": invalidation["time_sec"],
                    }
                )
        selected = max(
            candidates,
            key=_outcome_order_key,
            default=None,
        )
        if selected is not None:
            used_preparations.add(selected["outcome_id"])
        coverage_rows.append(
            {
                "target_event_id": pair["target_event_id"],
                "request_event_id": pair["request_event_id"],
                "tool_id": pair["tool_id"],
                "readiness_cutoff_sec": cutoff,
                "cutoff_basis": (
                    "confirmed_request"
                    if pair["request_time_sec"] is not None
                    else "confirmed_handover"
                ),
                "prepared": selected is not None,
                "preparation_outcome_id": (
                    selected["outcome_id"]
                    if selected is not None
                    else None
                ),
                "preparation_instance_id": (
                    selected.get("instance_id")
                    if selected is not None
                    else None
                ),
                "preparation_lead_sec": (
                    round(
                        cutoff - selected["completion_time_sec"],
                        6,
                    )
                    if selected is not None
                    else None
                ),
                "stale_preparation_count": len(stale_preparations),
                "stale_preparations": stale_preparations,
            }
        )
    prepared_target_count = sum(row["prepared"] for row in coverage_rows)
    preparation_coverage = (
        prepared_target_count / len(coverage_rows)
        if coverage_rows
        else None
    )

    used_handover_outcomes: set[str] = set()
    request_rows: list[dict[str, Any]] = []
    requested_pairs = [
        pair for pair in pairs if pair["request_time_sec"] is not None
    ]
    requested_pairs.sort(
        key=lambda row: (
            row["request_time_sec"],
            row["target_event_id"],
        )
    )
    request_pipeline_activations = _request_pipeline_activations(
        trace_records=trace_records,
        requested_pairs=requested_pairs,
        identity_map=identity_map,
    )
    for index, pair in enumerate(requested_pairs):
        previous_request_time = (
            requested_pairs[index - 1]["request_time_sec"]
            if index > 0
            else None
        )
        next_request_time = (
            requested_pairs[index + 1]["request_time_sec"]
            if index + 1 < len(requested_pairs)
            else None
        )
        earliest_match_time = (
            pair["request_time_sec"]
            - REQUEST_HANDOVER_EARLY_MATCH_TOLERANCE_SEC
        )
        if previous_request_time is not None:
            earliest_match_time = max(
                earliest_match_time,
                previous_request_time,
            )
        candidates = [
            outcome
            for outcome in handovers
            if outcome["outcome_id"] not in used_handover_outcomes
            and outcome["tool_id"] == pair["tool_id"]
            and outcome["completion_time_sec"] >= earliest_match_time
            and (
                next_request_time is None
                or outcome["completion_time_sec"] < next_request_time
            )
        ]
        selected = min(
            candidates,
            key=lambda row: row["completion_time_sec"],
            default=None,
        )
        if selected is not None:
            used_handover_outcomes.add(selected["outcome_id"])
        request_activation = request_wall_activations.get(
            _clean(pair["request_event_id"])
        )
        request_wall_time = (
            request_activation["wall_time_sec"]
            if request_activation is not None
            else None
        )
        handover_wall_time = (
            selected.get("completion_wall_time_sec")
            if selected is not None
            else None
        )
        wall_clock_latency = _non_negative_elapsed(
            start_sec=request_wall_time,
            end_sec=handover_wall_time,
        )
        source_offset = (
            round(
                selected["completion_time_sec"]
                - pair["request_time_sec"],
                6,
            )
            if selected is not None
            else None
        )
        pipeline = request_pipeline_activations.get(
            _clean(pair["request_event_id"]),
            {},
        )
        dt_fact = pipeline.get("dt_request_fact")
        bt_ingress = pipeline.get("bt_context_ingress")
        bt_evaluation = pipeline.get("bt_request_evaluation")
        bt_acceptance = pipeline.get("bt_request_acceptance")
        dt_fact_time = (
            float(dt_fact["source_time_sec"])
            if isinstance(dt_fact, dict)
            else None
        )
        dt_fact_wall_time = (
            dt_fact.get("wall_time_sec")
            if isinstance(dt_fact, dict)
            else None
        )
        bt_ingress_time = (
            float(bt_ingress["source_time_sec"])
            if isinstance(bt_ingress, dict)
            else None
        )
        bt_ingress_wall_time = (
            bt_ingress.get("wall_time_sec")
            if isinstance(bt_ingress, dict)
            else None
        )
        bt_evaluation_time = (
            float(bt_evaluation["source_time_sec"])
            if isinstance(bt_evaluation, dict)
            else None
        )
        bt_evaluation_wall_time = (
            bt_evaluation.get("wall_time_sec")
            if isinstance(bt_evaluation, dict)
            else None
        )
        bt_acceptance_time = (
            float(bt_acceptance["source_time_sec"])
            if isinstance(bt_acceptance, dict)
            else None
        )
        bt_acceptance_wall_time = (
            bt_acceptance.get("wall_time_sec")
            if isinstance(bt_acceptance, dict)
            else None
        )
        gt_to_dt_offset = (
            round(dt_fact_time - pair["request_time_sec"], 6)
            if dt_fact_time is not None
            else None
        )
        gt_to_dt_wall_offset = (
            round(dt_fact_wall_time - request_wall_time, 6)
            if dt_fact_wall_time is not None
            and request_wall_time is not None
            else None
        )
        request_rows.append(
            {
                "request_event_id": pair["request_event_id"],
                "target_event_id": pair["target_event_id"],
                "tool_id": pair["tool_id"],
                "request_time_sec": pair["request_time_sec"],
                "reference_handover_time_sec": pair["target_time_sec"],
                "system_handover_outcome_id": (
                    selected["outcome_id"]
                    if selected is not None
                    else None
                ),
                "system_handover_time_sec": (
                    selected["completion_time_sec"]
                    if selected is not None
                    else None
                ),
                "latency_sec": (
                    max(0.0, source_offset)
                    if selected is not None
                    else None
                ),
                "response_offset_sec": source_offset,
                "early_match": bool(
                    source_offset is not None and source_offset < 0.0
                ),
                "early_lead_sec": (
                    round(-source_offset, 6)
                    if source_offset is not None and source_offset < 0.0
                    else None
                ),
                "request_wall_time_sec": request_wall_time,
                "system_handover_wall_time_sec": handover_wall_time,
                "wall_clock_latency_sec": wall_clock_latency,
                "dt_request_fact_source": (
                    dt_fact.get("source")
                    if isinstance(dt_fact, dict)
                    else None
                ),
                "dt_request_fact_time_sec": dt_fact_time,
                "dt_request_fact_wall_time_sec": dt_fact_wall_time,
                "ground_truth_to_dt_request_fact_offset_sec": (
                    gt_to_dt_offset
                ),
                "ground_truth_to_dt_request_fact_latency_sec": (
                    max(0.0, gt_to_dt_offset)
                    if gt_to_dt_offset is not None
                    else None
                ),
                "ground_truth_to_dt_request_fact_wall_clock_offset_sec": (
                    gt_to_dt_wall_offset
                ),
                "ground_truth_to_dt_request_fact_wall_clock_latency_sec": (
                    max(0.0, gt_to_dt_wall_offset)
                    if gt_to_dt_wall_offset is not None
                    else None
                ),
                "bt_context_ingress_time_sec": bt_ingress_time,
                "bt_context_ingress_wall_time_sec": bt_ingress_wall_time,
                "dt_request_fact_to_bt_ingress_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=dt_fact_time,
                        end_sec=bt_ingress_time,
                    )
                ),
                "dt_request_fact_to_bt_ingress_wall_clock_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=dt_fact_wall_time,
                        end_sec=bt_ingress_wall_time,
                    )
                ),
                "bt_request_evaluation_source": (
                    bt_evaluation.get("source")
                    if isinstance(bt_evaluation, dict)
                    else None
                ),
                "bt_request_evaluation_decision": (
                    bt_evaluation.get("decision")
                    if isinstance(bt_evaluation, dict)
                    else None
                ),
                "bt_request_evaluation_time_sec": bt_evaluation_time,
                "bt_request_evaluation_wall_time_sec": (
                    bt_evaluation_wall_time
                ),
                "dt_request_fact_to_bt_evaluation_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=dt_fact_time,
                        end_sec=bt_evaluation_time,
                    )
                ),
                "dt_request_fact_to_bt_evaluation_wall_clock_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=dt_fact_wall_time,
                        end_sec=bt_evaluation_wall_time,
                    )
                ),
                "bt_request_acceptance_source": (
                    bt_acceptance.get("source")
                    if isinstance(bt_acceptance, dict)
                    else None
                ),
                "bt_request_acceptance_time_sec": bt_acceptance_time,
                "bt_request_acceptance_wall_time_sec": (
                    bt_acceptance_wall_time
                ),
                "dt_request_fact_to_bt_acceptance_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=dt_fact_time,
                        end_sec=bt_acceptance_time,
                    )
                ),
                "dt_request_fact_to_bt_acceptance_wall_clock_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=dt_fact_wall_time,
                        end_sec=bt_acceptance_wall_time,
                    )
                ),
                "bt_acceptance_to_handover_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=bt_acceptance_time,
                        end_sec=(
                            selected["completion_time_sec"]
                            if selected is not None
                            else None
                        ),
                    )
                ),
                "bt_acceptance_to_handover_wall_clock_latency_sec": (
                    _non_negative_elapsed(
                        start_sec=bt_acceptance_wall_time,
                        end_sec=handover_wall_time,
                    )
                ),
                "completed": selected is not None,
            }
        )
    request_latencies = [
        row["latency_sec"]
        for row in request_rows
        if row["latency_sec"] is not None
    ]
    request_wall_clock_latencies = [
        row["wall_clock_latency_sec"]
        for row in request_rows
        if row["wall_clock_latency_sec"] is not None
    ]
    request_pipeline_latency = {
        "ground_truth_to_dt_request_fact_latency_sec": _distribution(
            row["ground_truth_to_dt_request_fact_latency_sec"]
            for row in request_rows
            if row["ground_truth_to_dt_request_fact_latency_sec"]
            is not None
        ),
        "ground_truth_to_dt_request_fact_wall_clock_latency_sec": (
            _distribution(
                row[
                    "ground_truth_to_dt_request_fact_wall_clock_latency_sec"
                ]
                for row in request_rows
                if row[
                    "ground_truth_to_dt_request_fact_wall_clock_latency_sec"
                ]
                is not None
            )
        ),
        "dt_request_fact_to_bt_acceptance_latency_sec": _distribution(
            row["dt_request_fact_to_bt_acceptance_latency_sec"]
            for row in request_rows
            if row["dt_request_fact_to_bt_acceptance_latency_sec"]
            is not None
        ),
        "dt_request_fact_to_bt_acceptance_wall_clock_latency_sec": (
            _distribution(
                row[
                    "dt_request_fact_to_bt_acceptance_wall_clock_latency_sec"
                ]
                for row in request_rows
                if row[
                    "dt_request_fact_to_bt_acceptance_wall_clock_latency_sec"
                ]
                is not None
            )
        ),
        "dt_request_fact_to_bt_ingress_latency_sec": _distribution(
            row["dt_request_fact_to_bt_ingress_latency_sec"]
            for row in request_rows
            if row["dt_request_fact_to_bt_ingress_latency_sec"]
            is not None
        ),
        "dt_request_fact_to_bt_ingress_wall_clock_latency_sec": (
            _distribution(
                row["dt_request_fact_to_bt_ingress_wall_clock_latency_sec"]
                for row in request_rows
                if row[
                    "dt_request_fact_to_bt_ingress_wall_clock_latency_sec"
                ]
                is not None
            )
        ),
        "dt_request_fact_to_bt_evaluation_latency_sec": _distribution(
            row["dt_request_fact_to_bt_evaluation_latency_sec"]
            for row in request_rows
            if row["dt_request_fact_to_bt_evaluation_latency_sec"]
            is not None
        ),
        "dt_request_fact_to_bt_evaluation_wall_clock_latency_sec": (
            _distribution(
                row[
                    "dt_request_fact_to_bt_evaluation_wall_clock_latency_sec"
                ]
                for row in request_rows
                if row[
                    "dt_request_fact_to_bt_evaluation_wall_clock_latency_sec"
                ]
                is not None
            )
        ),
        "bt_acceptance_to_handover_latency_sec": _distribution(
            row["bt_acceptance_to_handover_latency_sec"]
            for row in request_rows
            if row["bt_acceptance_to_handover_latency_sec"] is not None
        ),
        "bt_acceptance_to_handover_wall_clock_latency_sec": (
            _distribution(
                row[
                    "bt_acceptance_to_handover_wall_clock_latency_sec"
                ]
                for row in request_rows
                if row[
                    "bt_acceptance_to_handover_wall_clock_latency_sec"
                ]
                is not None
            )
        ),
        "dt_request_fact_observed_count": sum(
            row["dt_request_fact_time_sec"] is not None
            for row in request_rows
        ),
        "bt_request_acceptance_count": sum(
            row["bt_request_acceptance_time_sec"] is not None
            for row in request_rows
        ),
        "early_dt_request_fact_count": sum(
            (
                row["ground_truth_to_dt_request_fact_offset_sec"]
                is not None
                and row["ground_truth_to_dt_request_fact_offset_sec"] < 0.0
            )
            for row in request_rows
        ),
    }

    coverage_by_target = {
        row["target_event_id"]: row for row in coverage_rows
    }
    request_readiness_rows: list[dict[str, Any]] = []
    for request_row in request_rows:
        coverage_row = coverage_by_target.get(
            request_row["target_event_id"],
            {},
        )
        prepared_at_request = bool(coverage_row.get("prepared"))
        early_handover = bool(request_row.get("early_match"))
        request_readiness_rows.append(
            {
                "request_event_id": request_row["request_event_id"],
                "target_event_id": request_row["target_event_id"],
                "tool_id": request_row["tool_id"],
                "ready": prepared_at_request or early_handover,
                "readiness_mode": (
                    "prepared"
                    if prepared_at_request
                    else "early_handover"
                    if early_handover
                    else None
                ),
                "prepared_at_request": prepared_at_request,
                "preparation_outcome_id": coverage_row.get(
                    "preparation_outcome_id"
                ),
                "preparation_lead_sec": coverage_row.get(
                    "preparation_lead_sec"
                ),
                "early_handover": early_handover,
                "early_handover_lead_sec": request_row.get(
                    "early_lead_sec"
                ),
                "handover_outcome_id": request_row.get(
                    "system_handover_outcome_id"
                ),
            }
        )
    request_ready_count = sum(
        row["ready"] for row in request_readiness_rows
    )
    request_readiness_coverage = (
        request_ready_count / len(request_readiness_rows)
        if request_readiness_rows
        else None
    )

    unnecessary_rows: list[dict[str, Any]] = []
    wrong_rows: list[dict[str, Any]] = []
    used_returns: set[str] = set()
    for preparation in preparations:
        prepared_time = preparation["completion_time_sec"]
        next_pair = next(
            (
                pair
                for pair in pairs
                if pair["target_time_sec"] >= prepared_time
            ),
            None,
        )
        cutoff = (
            (
                next_pair["request_time_sec"]
                if next_pair["request_time_sec"] is not None
                else next_pair["target_time_sec"]
            )
            if next_pair is not None
            else None
        )
        first_return = next(
            (
                row
                for row in returns
                if _same_physical_outcome(row, preparation)
                and row["completion_time_sec"] >= prepared_time
            ),
            None,
        )
        useful = bool(
            next_pair is not None
            and next_pair["tool_id"] == preparation["tool_id"]
            and (
                first_return is None
                or first_return["completion_time_sec"] > cutoff
            )
        )
        unnecessary_rows.append(
            {
                "preparation_outcome_id": preparation["outcome_id"],
                "tool_id": preparation["tool_id"],
                "instance_id": preparation.get("instance_id"),
                "prepared_time_sec": prepared_time,
                "next_target_event_id": (
                    next_pair["target_event_id"]
                    if next_pair is not None
                    else None
                ),
                "next_target_tool_id": (
                    next_pair["tool_id"]
                    if next_pair is not None
                    else None
                ),
                "classification": "useful" if useful else "unnecessary",
                "returned_before_use": bool(
                    first_return is not None
                    and (
                        cutoff is None
                        or first_return["completion_time_sec"] <= cutoff
                    )
                ),
            }
        )

        if (
            next_pair is None
            or next_pair["tool_id"] == preparation["tool_id"]
        ):
            continue
        next_request_activation = request_wall_activations.get(
            _clean(next_pair["request_event_id"])
        )
        contradiction_time = max(
            prepared_time,
            (
                next_pair["request_time_sec"]
                if next_pair["request_time_sec"] is not None
                else next_pair["target_time_sec"]
            ),
        )
        if (
            first_return is not None
            and first_return["completion_time_sec"] < contradiction_time
        ):
            continue
        consumed_before_contradiction = any(
            _same_physical_outcome(outcome, preparation)
            and prepared_time
            <= outcome["completion_time_sec"]
            < contradiction_time
            for outcome in handovers
        )
        if consumed_before_contradiction:
            continue
        release = next(
            (
                row
                for row in returns
                if row["outcome_id"] not in used_returns
                and _same_physical_outcome(row, preparation)
                and row["completion_time_sec"] >= contradiction_time
            ),
            None,
        )
        if release is not None:
            used_returns.add(release["outcome_id"])
        preparation_wall_time = preparation.get(
            "completion_wall_time_sec"
        )
        request_wall_time = (
            next_request_activation["wall_time_sec"]
            if next_request_activation is not None
            else None
        )
        contradiction_wall_time = (
            max(preparation_wall_time, request_wall_time)
            if preparation_wall_time is not None
            and request_wall_time is not None
            else None
        )
        release_wall_time = (
            release.get("completion_wall_time_sec")
            if release is not None
            else None
        )
        wall_clock_release_latency = _non_negative_elapsed(
            start_sec=contradiction_wall_time,
            end_sec=release_wall_time,
        )
        wrong_rows.append(
            {
                "preparation_outcome_id": preparation["outcome_id"],
                "prepared_tool_id": preparation["tool_id"],
                "prepared_instance_id": preparation.get("instance_id"),
                "contradicting_target_event_id": next_pair[
                    "target_event_id"
                ],
                "requested_tool_id": next_pair["tool_id"],
                "contradiction_time_sec": contradiction_time,
                "release_outcome_id": (
                    release["outcome_id"] if release is not None else None
                ),
                "release_time_sec": (
                    release["completion_time_sec"]
                    if release is not None
                    else None
                ),
                "release_latency_sec": (
                    round(
                        release["completion_time_sec"]
                        - contradiction_time,
                        6,
                    )
                    if release is not None
                    else None
                ),
                "contradiction_wall_time_sec": contradiction_wall_time,
                "release_wall_time_sec": release_wall_time,
                "wall_clock_release_latency_sec": (
                    wall_clock_release_latency
                ),
                "released": release is not None,
            }
        )

    abandoned_rows: list[dict[str, Any]] = []
    paired_abandoned_preparations: set[str] = set()
    for release in returns:
        candidates = []
        for preparation in preparations:
            if (
                preparation["outcome_id"] in paired_abandoned_preparations
                or preparation["completion_time_sec"]
                > release["completion_time_sec"]
                or not _same_physical_outcome(release, preparation)
            ):
                continue
            consumed_before_release = any(
                _same_physical_outcome(outcome, preparation)
                and preparation["completion_time_sec"]
                <= outcome["completion_time_sec"]
                <= release["completion_time_sec"]
                for outcome in handovers
            )
            if not consumed_before_release:
                candidates.append(preparation)
        preparation = max(
            candidates,
            key=_outcome_order_key,
            default=None,
        )
        if preparation is None:
            continue
        paired_abandoned_preparations.add(preparation["outcome_id"])
        source_hold_duration = max(
            0.0,
            release["completion_time_sec"]
            - preparation["completion_time_sec"],
        )
        wall_hold_duration = _non_negative_elapsed(
            start_sec=preparation.get("completion_wall_time_sec"),
            end_sec=release.get("completion_wall_time_sec"),
        )
        abandoned_rows.append(
            {
                "preparation_outcome_id": preparation["outcome_id"],
                "prepared_tool_id": preparation["tool_id"],
                "prepared_instance_id": preparation.get("instance_id"),
                "preparation_time_sec": preparation["completion_time_sec"],
                "release_outcome_id": release["outcome_id"],
                "release_time_sec": release["completion_time_sec"],
                "hold_duration_sec": round(source_hold_duration, 6),
                "preparation_wall_time_sec": preparation.get(
                    "completion_wall_time_sec"
                ),
                "release_wall_time_sec": release.get(
                    "completion_wall_time_sec"
                ),
                "wall_clock_hold_duration_sec": wall_hold_duration,
                "instance_identity_assumed": not bool(
                    _clean(preparation.get("instance_id"))
                    and _clean(release.get("instance_id"))
                ),
            }
        )

    wrong_release_latencies = [
        row["release_latency_sec"]
        for row in wrong_rows
        if row["release_latency_sec"] is not None
    ]
    wrong_release_wall_clock_latencies = [
        row["wall_clock_release_latency_sec"]
        for row in wrong_rows
        if row["wall_clock_release_latency_sec"] is not None
    ]
    abandoned_hold_durations = [
        row["hold_duration_sec"] for row in abandoned_rows
    ]
    abandoned_wall_clock_hold_durations = [
        row["wall_clock_hold_duration_sec"]
        for row in abandoned_rows
        if row["wall_clock_hold_duration_sec"] is not None
    ]
    unnecessary_count = sum(
        row["classification"] == "unnecessary"
        for row in unnecessary_rows
    )
    invariant_audit = _invariant_violation_audit(trace_records)
    summary = {
        "preparation_coverage": preparation_coverage,
        "request_readiness_coverage": request_readiness_coverage,
        "request_to_handover_latency_sec": _distribution(
            request_latencies
        ),
        "request_to_handover_wall_clock_latency_sec": _distribution(
            request_wall_clock_latencies
        ),
        "wrong_preposition_release_latency_sec": _distribution(
            wrong_release_latencies
        ),
        "wrong_preposition_release_wall_clock_latency_sec": _distribution(
            wrong_release_wall_clock_latencies
        ),
        "abandoned_preposition_hold_duration_sec": _distribution(
            abandoned_hold_durations
        ),
        "abandoned_preposition_wall_clock_hold_duration_sec": _distribution(
            abandoned_wall_clock_hold_durations
        ),
        "abandoned_preposition_count": len(abandoned_rows),
        "request_pipeline_latency": request_pipeline_latency,
        "unnecessary_preparation_count": unnecessary_count,
        "unnecessary_preparation_rate": (
            unnecessary_count / len(preparations)
            if preparations
            else None
        ),
        "invariant_violation_count": invariant_audit["count"],
    }
    report = {
        "schema": BEHAVIOR_QUALITY_SCHEMA,
        "status": (
            "complete"
            if targets or preparations or trace_records
            else "not_available"
        ),
        "reference_quality": (
            "confirmed evaluation-only request and handover events"
        ),
        "latency_clock_semantics": {
            "source_clock": (
                "Replay source time; elastic skill holds can pause this clock."
            ),
            "wall_clock": (
                "Elapsed trace wall time from the first active confirmed "
                "request record to the observed skill completion."
            ),
            "pipeline": (
                "Offline-only decomposition: confirmed reference request to "
                "the first matching DT request fact, DT fact to BT request "
                "acceptance, and BT acceptance to observed handover "
                "completion."
            ),
        },
        "summary": summary,
        "preparation_coverage": {
            "eligible_handover_count": len(coverage_rows),
            "prepared_before_request_count": prepared_target_count,
            "missed_preparation_count": (
                len(coverage_rows) - prepared_target_count
            ),
            "coverage": preparation_coverage,
            "targets": coverage_rows,
        },
        "request_readiness": {
            "evaluable_request_count": len(request_readiness_rows),
            "ready_before_request_count": request_ready_count,
            "prepared_at_request_count": sum(
                row["prepared_at_request"]
                for row in request_readiness_rows
            ),
            "early_handover_count": sum(
                row["early_handover"] for row in request_readiness_rows
            ),
            "coverage": request_readiness_coverage,
            "requests": request_readiness_rows,
        },
        "request_to_handover": {
            "paired_request_count": len(request_rows),
            "completed_count": sum(row["completed"] for row in request_rows),
            "missed_count": sum(
                not row["completed"] for row in request_rows
            ),
            "unmatched_confirmed_request_count": len(unmatched_requests),
            "latency_sec": summary[
                "request_to_handover_latency_sec"
            ],
            "wall_clock_latency_sec": summary[
                "request_to_handover_wall_clock_latency_sec"
            ],
            "episodes": request_rows,
        },
        "request_pipeline_latency": request_pipeline_latency,
        "wrong_preposition_release": {
            "wrong_preposition_count": len(wrong_rows),
            "released_count": sum(row["released"] for row in wrong_rows),
            "unreleased_count": sum(
                not row["released"] for row in wrong_rows
            ),
            "latency_sec": summary[
                "wrong_preposition_release_latency_sec"
            ],
            "wall_clock_latency_sec": summary[
                "wrong_preposition_release_wall_clock_latency_sec"
            ],
            "episodes": wrong_rows,
        },
        "abandoned_preposition": {
            "returned_before_use_count": len(abandoned_rows),
            "hold_duration_sec": summary[
                "abandoned_preposition_hold_duration_sec"
            ],
            "wall_clock_hold_duration_sec": summary[
                "abandoned_preposition_wall_clock_hold_duration_sec"
            ],
            "episodes": abandoned_rows,
        },
        "unnecessary_preparation": {
            "completed_preparation_count": len(preparations),
            "useful_preparation_count": (
                len(preparations) - unnecessary_count
            ),
            "unnecessary_preparation_count": unnecessary_count,
            "unnecessary_preparation_rate": summary[
                "unnecessary_preparation_rate"
            ],
            "preparations": unnecessary_rows,
        },
        "invariant_violations": invariant_audit,
        "notes": [
            (
                "Reference labels are read only by this offline evaluator and "
                "are never emitted as runtime VLM, reducer, or BT inputs."
            ),
            (
                "Preparation coverage requires a completed preparation before "
                "the paired confirmed request that remains active at the "
                "cutoff; consumed, returned, displaced, or superseded "
                "preparations are stale. When no request label exists, the "
                "confirmed handover time is the conservative cutoff."
            ),
            (
                "Request readiness counts a requested tool as ready when a "
                "matching preparation is still held at the request boundary "
                "or when a matching handover completed within the explicit "
                "early-match tolerance. It measures operational readiness, "
                "not forecast-label accuracy."
            ),
            (
                "A preparation is useful only for the next confirmed ordinary "
                "handover target; a later same-tool target does not "
                "retroactively justify blocking the robot hand."
            ),
            (
                "Wrong-preposition release latency starts when the next "
                "confirmed request or handover establishes a different "
                "immediate tool target."
            ),
            (
                "Abandoned-preposition hold duration measures every completed "
                "preparation that is returned before a matching handover, from "
                "preparation completion to source-return completion. It captures "
                "evidence withdrawal and prediction churn even when the next "
                "confirmed target is the same tool or is not yet labelled."
            ),
            (
                "Source-clock latency preserves replay-video timing. "
                "Wall-clock latency starts at the first evaluation-only "
                "ground-truth request activation and ends at the observed "
                "skill event or successful terminal status, so elastic replay "
                "holds remain visible."
            ),
            (
                "Wall-clock latency is unavailable when a confirmed request "
                "has no matching active evaluation-ground-truth trace record; "
                "the evaluator does not infer wall time from source time."
            ),
            (
                "Pipeline latency uses only emitted reducer and BT records. "
                "The runtime never receives the reference request; reference "
                "timestamps are joined only in this offline evaluator."
            ),
            (
                "A same-tool handover up to "
                f"{REQUEST_HANDOVER_EARLY_MATCH_TOLERANCE_SEC:.1f}s before a "
                "confirmed request boundary is retained as an explicit "
                "early_match. Source latency is floored at zero and the "
                "signed response_offset_sec remains available for audit."
            ),
            (
                "Invariant counts use explicit reducer, BT guard, shadow "
                "admission, or skill event markers and ignore repeated "
                "runtime-state snapshots."
            ),
        ],
    }
    errors = validate_behavior_quality_report(report)
    if errors:
        raise ValueError(
            "internal behavior-quality contract failure: "
            + "; ".join(errors)
        )
    return report


def _evaluate_command_fulfillment(
    trace_records: list[dict[str, Any]],
) -> dict[str, Any]:
    commands: dict[str, dict[str, Any]] = {}
    for record in trace_records:
        if record.get("layer") != "skill_command":
            continue
        payload = _payload(record)
        command_id = _clean(payload.get("command_id"))
        if command_id:
            commands.setdefault(command_id, payload)

    terminal_status: dict[str, dict[str, Any]] = {}
    for record in trace_records:
        if record.get("layer") != "skill_status":
            continue
        payload = _payload(record)
        command_id = _clean(payload.get("command_id"))
        if (
            command_id
            and _clean(payload.get("state")).lower()
            in {"completed", "failed", "cancelled", "rejected"}
        ):
            terminal_status[command_id] = payload

    admission_outcomes: dict[str, dict[str, Any]] = {}
    for record in trace_records:
        if record.get("layer") != "shadow_sink":
            continue
        payload = _payload(record)
        command_id = _clean(payload.get("command_id"))
        if command_id:
            admission_outcomes[command_id] = payload

    event_types: dict[str, set[str]] = {}
    for record in trace_records:
        if record.get("layer") != "skill_event":
            continue
        payload = _payload(record)
        detail = _parse_json_object(payload.get("detail_json"))
        command_id = _clean(detail.get("command_id"))
        if command_id:
            event_types.setdefault(command_id, set()).add(
                _clean(payload.get("event_type"))
            )

    rows: list[dict[str, Any]] = []
    for command_id, command in commands.items():
        action = _clean(command.get("action")).lower()
        status = terminal_status.get(command_id)
        admission = admission_outcomes.get(command_id, {})
        admission_status = _clean(admission.get("status")).lower()
        fulfillment_eligible = (
            not admission_status
            or admission_status
            in {"admissible", "instance_resolution_assumed"}
        )
        expected_events = _expected_terminal_skill_events(action)
        observed_events = event_types.get(command_id, set())
        terminal_event_ok = (
            not expected_events
            or bool(expected_events & observed_events)
        )
        fulfilled = bool(
            status
            and _clean(status.get("state")).lower() == "completed"
            and bool(status.get("success"))
            and terminal_event_ok
        )
        rows.append(
            {
                "command_id": command_id,
                "action": action,
                "tool_id": _clean(command.get("instrument_id")) or None,
                "terminal_state": (
                    _clean(status.get("state")) if status else None
                ),
                "admission_status": admission_status or None,
                "admission_reason": (
                    _clean(admission.get("reason")) or None
                ),
                "included_in_fulfillment": fulfillment_eligible,
                "success": bool(status.get("success")) if status else False,
                "expected_terminal_events": sorted(expected_events),
                "observed_event_types": sorted(observed_events),
                "terminal_event_ok": terminal_event_ok,
                "fulfilled": fulfilled,
            }
        )
    eligible_rows = [
        row for row in rows if bool(row["included_in_fulfillment"])
    ]
    fulfilled_count = sum(bool(row["fulfilled"]) for row in eligible_rows)
    admission_status_counts: dict[str, int] = {}
    for row in rows:
        status_name = _clean(row.get("admission_status")) or "not_recorded"
        admission_status_counts[status_name] = (
            admission_status_counts.get(status_name, 0) + 1
        )
    return {
        "status": "complete" if eligible_rows else "not_available",
        "emitted_command_count": len(rows),
        "command_count": len(eligible_rows),
        "non_admitted_command_count": len(rows) - len(eligible_rows),
        "admission_status_counts": admission_status_counts,
        "fulfilled_count": fulfilled_count,
        "failed_or_incomplete_count": len(eligible_rows) - fulfilled_count,
        "fulfillment_rate": (
            fulfilled_count / len(eligible_rows) if eligible_rows else None
        ),
        "commands": rows,
        "notes": [
            "A command is fulfilled only when it has a successful terminal status and its expected terminal DT event is observed.",
            "Commands rejected or duplicate-suppressed at shadow admission are reported separately and excluded from execution-fulfillment denominators; they remain inputs to BT decision-quality auditing.",
        ],
    }


def _build_scorecard(
    *,
    phase: dict[str, Any],
    layers: dict[str, Any],
    trace_records: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    ground_truth: list[dict[str, Any]],
    lead_window_sec: float,
    identity_map: dict[str, str],
    evaluation_mask: EvaluationMask,
    tool_inventory: dict[str, int],
    specialized_group_actions: dict[str, Any],
) -> dict[str, Any]:
    phase_rows = {
        layer: {
            "correct_count": payload.get("correct_count", 0),
            "evaluated_count": payload.get("evaluated_count", 0),
            "accuracy": payload.get("accuracy"),
        }
        for layer, payload in phase.get("layers", {}).items()
    }
    tool_rows = {}
    for layer in (
        "vlm_model_raw",
        "vlm_raw",
        "reducer_fused",
        "bt_decision",
    ):
        payload = layers.get(layer, {})
        if (
            layer == "vlm_model_raw"
            and payload.get("prediction_record_count", 0) <= 0
        ):
            continue
        outcomes = payload.get("outcomes", {})
        target_count = int(payload.get("target_count", 0) or 0)
        combined_correct = int(outcomes.get("exact_match", 0) or 0)
        proactive_correct = int(
            payload.get(
                "proactive_exact_count",
                payload.get("anticipatory_exact_count", 0),
            )
            or 0
        )
        pre_request_correct = int(
            payload.get("pre_request_proactive_exact_count", 0) or 0
        )
        unrequested_forecast_correct = int(
            payload.get(
                "unrequested_pre_handover_exact_count",
                0,
            )
            or 0
        )
        post_request_visual_correct = int(
            payload.get("post_request_visual_exact_count", 0) or 0
        )
        request_backed_correct = int(
            payload.get("request_backed_exact_count", 0) or 0
        )
        tool_rows[layer] = {
            "correct_count": combined_correct,
            "evaluated_count": target_count,
            "accuracy": payload.get("top1_exact_rate"),
            "false_positive_count": payload.get("false_positive_count", 0),
            "combined_action_selection_correct_count": combined_correct,
            "combined_action_selection_accuracy": payload.get(
                "top1_exact_rate"
            ),
            "proactive_correct_count": proactive_correct,
            "proactive_target_recall": (
                proactive_correct / target_count
                if target_count
                else None
            ),
            # Compatibility aliases for existing scorecard consumers.
            "anticipatory_correct_count": proactive_correct,
            "anticipatory_target_recall": (
                proactive_correct / target_count
                if target_count
                else None
            ),
            "pre_request_proactive_correct_count": pre_request_correct,
            "unrequested_pre_handover_correct_count": (
                unrequested_forecast_correct
            ),
            "post_request_visual_correct_count": (
                post_request_visual_correct
            ),
            "post_request_visual_target_recall": (
                post_request_visual_correct / target_count
                if target_count
                else None
            ),
            "request_backed_correct_count": request_backed_correct,
            "request_backed_target_recall": (
                request_backed_correct / target_count
                if target_count
                else None
            ),
        }
    return {
        "schema": "taskplanner.shadow_scorecard.v1",
        "status": "development_evaluation",
        "phase_estimation": {
            "reference_quality": phase.get("reference_quality"),
            "status": phase.get("status"),
            "layers": phase_rows,
        },
        "next_tool_prediction": {
            "reference_quality": "confirmed handover targets",
            "metric_semantics": {
                "proactive_target_recall": (
                    "Targets correctly forecast before the paired confirmed "
                    "request; targets without a request label must be "
                    "forecast before handover."
                ),
                "post_request_visual_target_recall": (
                    "Targets first recognized visually after the paired "
                    "confirmed request but before handover."
                ),
                "request_backed_target_recall": (
                    "Targets correctly selected after public request evidence."
                ),
                "combined_action_selection_accuracy": (
                    "Legacy combined exact rate; not anticipatory forecast "
                    "accuracy."
                ),
            },
            "layers": tool_rows,
        },
        "intent_recognition": _evaluate_vlm_intent(
            trace_records,
            targets,
            lead_window_sec=lead_window_sec,
            evaluation_mask=evaluation_mask,
            layer="vlm_raw",
        ),
        "model_raw_intent_recognition": _evaluate_vlm_intent(
            trace_records,
            targets,
            lead_window_sec=lead_window_sec,
            evaluation_mask=evaluation_mask,
            layer="vlm_model_raw",
        ),
        "dt_tool_management": _evaluate_dt_tool_endpoints(
            trace_records,
            ground_truth,
            identity_map=identity_map,
            evaluation_mask=evaluation_mask,
            tool_inventory=tool_inventory,
        ),
        "command_fulfillment": _evaluate_command_fulfillment(trace_records),
        "specialized_group_action": {
            "status": specialized_group_actions.get("status"),
            "reference_quality": specialized_group_actions.get(
                "reference_quality"
            ),
            "correct_count": specialized_group_actions.get(
                "exact_match_count",
                0,
            ),
            "evaluated_count": specialized_group_actions.get(
                "target_count",
                0,
            ),
            "accuracy": specialized_group_actions.get("command_recall"),
            "false_positive_count": specialized_group_actions.get(
                "false_positive_command_count",
                0,
            ),
            "unscorable_command_count": specialized_group_actions.get(
                "unscorable_activation_command_count",
                0,
            ),
            "terminal_success_count": specialized_group_actions.get(
                "terminal_success_count",
                0,
            ),
            "execution_fulfillment_rate": specialized_group_actions.get(
                "execution_fulfillment_rate"
            ),
        },
        "bt_decision": tool_rows.get("bt_decision", {}),
        "notes": [
            "Replay cases used during iteration are development/calibration data, not held-out generalization results.",
            "Provisional Phase scores must be revised if later label adjudication changes the boundaries.",
            "Top-1 tool accuracy combines anticipatory forecasts and public request-backed tool identification; report both source counts.",
        ],
    }


def evaluate_shadow(
    *,
    ground_truth: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    lead_window_sec: float,
    mode: str = "strict",
    stable_sec: float = 3.0,
    max_prediction_age_sec: float = 3.0,
    request_reaction_window_sec: float = 2.0,
    episode_gap_sec: float = 2.5,
    recovery_reuse_warning_sec: float = 15.0,
    phase_ground_truth: list[dict[str, Any]] | None = None,
    allow_provisional_phase: bool = False,
    tool_identity_map: dict[str, str] | None = None,
    tool_inventory: dict[str, int] | None = None,
    evaluation_mask: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode not in RUN_MODES:
        raise ValueError(f"invalid mode {mode!r}")
    identity_map = tool_identity_map or {}
    inventory = tool_inventory or {}
    mask = EvaluationMask(evaluation_mask)
    reference_case_ids = {
        _clean(item.get("case_id"))
        for item in ground_truth
        if _clean(item.get("case_id"))
    }
    if (
        mask.present
        and mask.case_id
        and reference_case_ids
        and reference_case_ids != {mask.case_id}
    ):
        raise ValueError(
            "evaluation mask case_id does not match reference events: "
            f"{mask.case_id!r} vs {sorted(reference_case_ids)!r}"
        )
    targets, _excluded, reference = _target_handovers(
        ground_truth,
        tool_identity_map=identity_map,
        evaluation_mask=mask,
    )
    timeline = ReferenceTimeline(
        ground_truth,
        tool_identity_map=identity_map,
        evaluation_mask=mask,
    )
    (
        specialized_group_actions,
        specialized_target_ids,
    ) = _evaluate_specialized_group_actions(
        records=decisions,
        targets=targets,
        timeline=timeline,
        lead_window_sec=lead_window_sec,
        tool_identity_map=identity_map,
    )
    ordinary_targets = [
        target
        for target in targets
        if _clean(target.get("event_id")) not in specialized_target_ids
    ]
    request_pairs, _unmatched_requests = _pair_requests_to_handovers(
        ground_truth=ground_truth,
        targets=ordinary_targets,
        identity_map=identity_map,
        evaluation_mask=mask,
    )
    request_time_by_target_id = {
        _clean(pair["target_event_id"]): float(pair["request_time_sec"])
        for pair in request_pairs
        if pair.get("request_time_sec") is not None
    }
    behavior_quality = _evaluate_behavior_quality(
        trace_records=decisions,
        ground_truth=ground_truth,
        targets=ordinary_targets,
        identity_map=identity_map,
        evaluation_mask=mask,
    )
    reference["confirmed_ordinary_handover_count"] = len(
        ordinary_targets
    )
    reference["confirmed_specialized_group_action_count"] = len(
        specialized_target_ids
    )
    predictions_by_layer = extract_predictions(
        decisions,
        tool_identity_map=identity_map,
    )
    phase_report = _evaluate_phase(
        predictions_by_layer,
        phase_ground_truth,
        allow_provisional=allow_provisional_phase,
    )
    reference["tool_identity_mapping"] = {
        "enabled": bool(identity_map),
        "alias_key_count": len(identity_map),
        "canonical_tool_count": len(set(identity_map.values())),
    }
    trace_errors = (
        validate_trace_records(decisions)
        if any(record.get("schema") == "taskplanner.shadow_trace.v1" for record in decisions)
        else []
    )

    if reference["confirmed_ground_truth_count"] == 0:
        report = {
            "schema": EVALUATION_SCHEMA,
            "mode": mode,
            "status": "awaiting_confirmed_reference",
            **reference,
            "configuration": {
                "lead_window_sec": lead_window_sec,
                "stable_sec": stable_sec,
                "max_prediction_age_sec": max_prediction_age_sec,
                "request_reaction_window_sec": request_reaction_window_sec,
                "episode_gap_sec": episode_gap_sec,
                "recovery_reuse_warning_sec": recovery_reuse_warning_sec,
            },
            "layers": {},
            "metrics": None,
            "events": [],
            "evaluation_mask": mask.report(),
            "state_audit": timeline.state_audit(),
            "phase": phase_report,
            "specialized_group_actions": specialized_group_actions,
            "behavior_quality": behavior_quality,
            "runtime": _runtime_metrics(decisions, trace_errors=trace_errors),
            "notes": [
                "No metric was computed because there are no confirmed reference events.",
                "Proposed, ambiguous, and rejected records are never promoted to truth.",
            ],
        }
        report["scorecard"] = _build_scorecard(
            phase=phase_report,
            layers={},
            trace_records=decisions,
            targets=ordinary_targets,
            ground_truth=ground_truth,
            lead_window_sec=lead_window_sec,
            identity_map=identity_map,
            evaluation_mask=mask,
            tool_inventory=inventory,
            specialized_group_actions=specialized_group_actions,
        )
        report["scorecard"]["behavior_quality"] = behavior_quality
        return report
    if not targets:
        action_targets_masked = (
            reference["confirmed_handover_before_mask_count"] > 0
        )
        report = {
            "schema": EVALUATION_SCHEMA,
            "mode": mode,
            "status": (
                "not_scorable_action_masked"
                if action_targets_masked
                else "awaiting_handover_ground_truth"
            ),
            **reference,
            "configuration": {
                "lead_window_sec": lead_window_sec,
                "stable_sec": stable_sec,
                "max_prediction_age_sec": max_prediction_age_sec,
                "request_reaction_window_sec": request_reaction_window_sec,
                "episode_gap_sec": episode_gap_sec,
                "recovery_reuse_warning_sec": recovery_reuse_warning_sec,
            },
            "layers": {},
            "metrics": None,
            "events": [],
            "evaluation_mask": mask.report(),
            "state_audit": timeline.state_audit(),
            "phase": phase_report,
            "specialized_group_actions": specialized_group_actions,
            "behavior_quality": behavior_quality,
            "runtime": _runtime_metrics(decisions, trace_errors=trace_errors),
            "notes": [
                (
                    "Confirmed handovers exist, but the evaluation mask excludes "
                    "all of them from action scoring."
                    if action_targets_masked
                    else "Confirmed records exist, but none is a handover target."
                ),
            ],
        }
        report["scorecard"] = _build_scorecard(
            phase=phase_report,
            layers={},
            trace_records=decisions,
            targets=ordinary_targets,
            ground_truth=ground_truth,
            lead_window_sec=lead_window_sec,
            identity_map=identity_map,
            evaluation_mask=mask,
            tool_inventory=inventory,
            specialized_group_actions=specialized_group_actions,
        )
        report["scorecard"]["behavior_quality"] = behavior_quality
        return report

    layers: dict[str, Any] = {}
    for layer in PREDICTION_LAYERS:
        episodes = collapse_prediction_episodes(
            predictions_by_layer[layer],
            episode_gap_sec=episode_gap_sec,
        )
        layers[layer] = _evaluate_layer(
            layer=layer,
            episodes=episodes,
            targets=ordinary_targets,
            request_time_by_target_id=request_time_by_target_id,
            timeline=timeline,
            lead_window_sec=lead_window_sec,
            stable_sec=stable_sec,
            max_prediction_age_sec=max_prediction_age_sec,
            request_reaction_window_sec=request_reaction_window_sec,
        )
    skill_episodes = collapse_prediction_episodes(
        predictions_by_layer["skill_command"],
        episode_gap_sec=episode_gap_sec,
    )
    recovery_audit = _recovery_action_audit(
        episodes=skill_episodes,
        timeline=timeline,
        reuse_warning_sec=recovery_reuse_warning_sec,
    )

    primary_layer = next(
        (
            layer
            for layer in ("bt_decision", "reducer_fused", "vlm_raw", "skill_command")
            if layers[layer]["scoring_eligible_prediction_record_count"] > 0
        ),
        next(
            (
                layer
                for layer in (
                    "bt_decision",
                    "reducer_fused",
                    "vlm_raw",
                    "skill_command",
                )
                if layers[layer]["prediction_record_count"] > 0
            ),
            "bt_decision",
        ),
    )
    primary = layers[primary_layer]
    compatibility_metrics = {
        "exact_match_count": primary["outcomes"]["exact_match"],
        "wrong_prediction_count": primary["outcomes"]["wrong_prediction"],
        "missed_opportunity_count": primary["outcomes"]["missed_opportunity"],
        "unsafe_or_impossible_count": primary["outcomes"]["unsafe_or_impossible"],
        "needs_human_adjudication_count": primary["outcomes"][
            "needs_human_adjudication"
        ],
        "false_positive_count": primary["false_positive_count"],
        "physical_feasibility_not_scorable_count": primary[
            "physical_feasibility_counts"
        ].get("not_scorable", 0),
        "top1_exact_rate": primary["top1_exact_rate"],
        "stable_exact_rate": primary["stable_exact_rate"],
    }
    notes = [
        "Strict mode never feeds reference labels to VLM, reducer, or BT."
        if mode == "strict"
        else (
            "Reconciled mode may apply confirmed observations only after their event timestamp."
            if mode == "reconciled"
            else "Oracle mode is a downstream upper-bound baseline and is not end-to-end performance."
        ),
        "A prediction episode can match at most one reference handover.",
        (
            "Only public-request-backed reducer, BT, and skill episodes may "
            "match shortly after a reference handover; their delay is "
            "reported as request reaction lag, never as prediction lead."
        ),
        "Recovery actions are excluded from handover false-positive counts and audited separately.",
        "Clinically acceptable alternatives remain human-adjudication items unless physical impossibility is provable.",
    ]
    if specialized_target_ids:
        notes.append(
            "Targets uniquely served by declared retraction-arm end-effectors are "
            "excluded from ordinary handover denominators and scored under "
            "specialized_group_actions."
        )
    if reference["reference_authority"] == "mixed":
        notes.append(
            "Reference labels mix human review and authorized assistant video adjudication."
        )
    if mask.present:
        notes.append(
            "Action, latency, state, physical, and reuse metrics honor the "
            "machine-readable evaluation mask and cutoffs."
        )
    if timeline.duplicate_type_instances:
        notes.append(
            "Type-level tool identity is insufficient for physical/location/"
            "reuse success scoring where multiple instances or unobserved "
            "transitions are required; those results are marked not_scorable."
        )
    report = {
        "schema": EVALUATION_SCHEMA,
        "mode": mode,
        "oracle_post_event_reconciliation": mode == "reconciled",
        "status": "complete",
        **reference,
        "configuration": {
            "lead_window_sec": lead_window_sec,
            "stable_sec": stable_sec,
            "max_prediction_age_sec": max_prediction_age_sec,
            "request_reaction_window_sec": request_reaction_window_sec,
            "episode_gap_sec": episode_gap_sec,
            "recovery_reuse_warning_sec": recovery_reuse_warning_sec,
        },
        "primary_layer": primary_layer,
        "metrics": compatibility_metrics,
        "events": primary["events"],
        "layers": layers,
        "recovery_audit": recovery_audit,
        "evaluation_mask": mask.report(),
        "state_audit": timeline.state_audit(),
        "phase": phase_report,
        "specialized_group_actions": specialized_group_actions,
        "behavior_quality": behavior_quality,
        "runtime": _runtime_metrics(decisions, trace_errors=trace_errors),
        "notes": notes,
    }
    report["scorecard"] = _build_scorecard(
        phase=phase_report,
        layers=layers,
        trace_records=decisions,
        targets=ordinary_targets,
        ground_truth=ground_truth,
        lead_window_sec=lead_window_sec,
        identity_map=identity_map,
        evaluation_mask=mask,
        tool_inventory=inventory,
        specialized_group_actions=specialized_group_actions,
    )
    report["scorecard"]["behavior_quality"] = behavior_quality
    return report


def write_layer_csv(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "layer",
        "target_count",
        "exact_match",
        "wrong_prediction",
        "missed_opportunity",
        "unsafe_or_impossible",
        "needs_human_adjudication",
        "false_positive_count",
        "handover_prediction_episode_count",
        "non_handover_action_episode_count",
        "top1_exact_rate",
        "stable_exact_rate",
        "request_backed_exact_count",
        "proactive_exact_count",
        "pre_request_proactive_exact_count",
        "unrequested_pre_handover_exact_count",
        "post_request_visual_exact_count",
        "anticipatory_exact_count",
        "first_correct_lead_median_sec",
        "first_correct_lead_p95_sec",
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for layer, payload in report.get("layers", {}).items():
            outcomes = payload["outcomes"]
            writer.writerow(
                {
                    "layer": layer,
                    "target_count": payload["target_count"],
                    "exact_match": outcomes["exact_match"],
                    "wrong_prediction": outcomes["wrong_prediction"],
                    "missed_opportunity": outcomes["missed_opportunity"],
                    "unsafe_or_impossible": outcomes["unsafe_or_impossible"],
                    "needs_human_adjudication": outcomes[
                        "needs_human_adjudication"
                    ],
                    "false_positive_count": payload["false_positive_count"],
                    "handover_prediction_episode_count": payload[
                        "prediction_episode_count"
                    ],
                    "non_handover_action_episode_count": payload[
                        "non_handover_action_episode_count"
                    ],
                    "top1_exact_rate": payload["top1_exact_rate"],
                    "stable_exact_rate": payload["stable_exact_rate"],
                    "request_backed_exact_count": payload[
                        "request_backed_exact_count"
                    ],
                    "proactive_exact_count": payload.get(
                        "proactive_exact_count",
                        payload.get("anticipatory_exact_count", 0),
                    ),
                    "pre_request_proactive_exact_count": payload.get(
                        "pre_request_proactive_exact_count",
                        0,
                    ),
                    "unrequested_pre_handover_exact_count": payload.get(
                        "unrequested_pre_handover_exact_count",
                        0,
                    ),
                    "post_request_visual_exact_count": payload.get(
                        "post_request_visual_exact_count",
                        0,
                    ),
                    "anticipatory_exact_count": payload[
                        "anticipatory_exact_count"
                    ],
                    "first_correct_lead_median_sec": payload[
                        "first_correct_lead_sec"
                    ]["median"],
                    "first_correct_lead_p95_sec": payload[
                        "first_correct_lead_sec"
                    ]["p95"],
                }
            )


def write_scorecard_csv(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scorecard = report.get("scorecard", {})
    rows: list[dict[str, Any]] = []

    for layer, metric in scorecard.get(
        "phase_estimation",
        {},
    ).get("layers", {}).items():
        rows.append(
            {
                "metric": "phase_estimation",
                "layer": layer,
                "correct_count": metric.get("correct_count"),
                "evaluated_count": metric.get("evaluated_count"),
                "accuracy": metric.get("accuracy"),
                "status": scorecard.get(
                    "phase_estimation",
                    {},
                ).get("status"),
                "reference_quality": scorecard.get(
                    "phase_estimation",
                    {},
                ).get("reference_quality"),
            }
        )
    for layer, metric in scorecard.get(
        "next_tool_prediction",
        {},
    ).get("layers", {}).items():
        reference_quality = scorecard.get(
            "next_tool_prediction",
            {},
        ).get("reference_quality")
        rows.extend(
            [
                {
                    "metric": "combined_tool_action_selection",
                    "layer": layer,
                    "correct_count": metric.get("correct_count"),
                    "evaluated_count": metric.get("evaluated_count"),
                    "accuracy": metric.get("accuracy"),
                    "status": "complete",
                    "reference_quality": reference_quality,
                },
                {
                    "metric": "proactive_next_tool",
                    "layer": layer,
                    "correct_count": metric.get(
                        "proactive_correct_count",
                        metric.get("anticipatory_correct_count"),
                    ),
                    "evaluated_count": metric.get("evaluated_count"),
                    "accuracy": metric.get(
                        "proactive_target_recall",
                        metric.get("anticipatory_target_recall"),
                    ),
                    "status": "complete",
                    "reference_quality": reference_quality,
                },
                {
                    "metric": "post_request_visual_tool",
                    "layer": layer,
                    "correct_count": metric.get(
                        "post_request_visual_correct_count"
                    ),
                    "evaluated_count": metric.get("evaluated_count"),
                    "accuracy": metric.get(
                        "post_request_visual_target_recall"
                    ),
                    "status": "complete",
                    "reference_quality": reference_quality,
                },
                {
                    "metric": "request_backed_tool_selection",
                    "layer": layer,
                    "correct_count": metric.get(
                        "request_backed_correct_count"
                    ),
                    "evaluated_count": metric.get("evaluated_count"),
                    "accuracy": metric.get(
                        "request_backed_target_recall"
                    ),
                    "status": "complete",
                    "reference_quality": reference_quality,
                },
            ]
        )
    for metric_name, payload, accuracy_key in (
        (
            "model_raw_intent_recognition",
            scorecard.get("model_raw_intent_recognition", {}),
            "accuracy",
        ),
        (
            "intent_recognition",
            scorecard.get("intent_recognition", {}),
            "accuracy",
        ),
        (
            "dt_tool_management",
            scorecard.get("dt_tool_management", {}),
            "endpoint_accuracy",
        ),
        (
            "command_fulfillment",
            scorecard.get("command_fulfillment", {}),
            "fulfillment_rate",
        ),
    ):
        rows.append(
            {
                "metric": metric_name,
                "layer": {
                    "model_raw_intent_recognition": "vlm_model_raw",
                    "intent_recognition": "vlm_raw",
                    "dt_tool_management": "reducer_fused",
                    "command_fulfillment": "skill_execution",
                }[metric_name],
                "correct_count": payload.get(
                    "correct_count",
                    payload.get("fulfilled_count"),
                ),
                "evaluated_count": payload.get(
                    "evaluated_count",
                    payload.get("command_count"),
                ),
                "accuracy": payload.get(accuracy_key),
                "precision": payload.get("precision"),
                "recall": payload.get("recall"),
                "f1": payload.get("f1"),
                "status": payload.get("status"),
                "reference_quality": payload.get("reference_quality"),
            }
        )

    specialized = scorecard.get("specialized_group_action", {})
    rows.append(
        {
            "metric": "specialized_group_action",
            "layer": "bed_robot_arm_group_command",
            "correct_count": specialized.get("correct_count"),
            "evaluated_count": specialized.get("evaluated_count"),
            "accuracy": specialized.get("accuracy"),
            "status": specialized.get("status"),
            "reference_quality": specialized.get("reference_quality"),
        }
    )
    rows.append(
        {
            "metric": "specialized_group_execution",
            "layer": "bed_robot_arm_group_status",
            "correct_count": specialized.get("terminal_success_count"),
            "evaluated_count": specialized.get("correct_count"),
            "accuracy": specialized.get("execution_fulfillment_rate"),
            "status": specialized.get("status"),
            "reference_quality": specialized.get("reference_quality"),
        }
    )

    behavior = scorecard.get("behavior_quality", {})
    behavior_summary = behavior.get("summary", {})
    preparation = behavior.get("preparation_coverage", {})
    request_latency = behavior_summary.get(
        "request_to_handover_latency_sec",
        {},
    )
    request_wall_clock_latency = behavior_summary.get(
        "request_to_handover_wall_clock_latency_sec",
        {},
    )
    release_latency = behavior_summary.get(
        "wrong_preposition_release_latency_sec",
        {},
    )
    release_wall_clock_latency = behavior_summary.get(
        "wrong_preposition_release_wall_clock_latency_sec",
        {},
    )
    pipeline_latency = behavior_summary.get(
        "request_pipeline_latency",
        {},
    )
    rows.extend(
        [
            {
                "metric": "preparation_coverage",
                "layer": "skill_execution",
                "correct_count": preparation.get(
                    "prepared_before_request_count"
                ),
                "evaluated_count": preparation.get(
                    "eligible_handover_count"
                ),
                "accuracy": behavior_summary.get(
                    "preparation_coverage"
                ),
                "status": behavior.get("status"),
                "reference_quality": behavior.get("reference_quality"),
            },
            {
                "metric": "request_to_handover_latency",
                "layer": "skill_execution",
                "evaluated_count": request_latency.get("count"),
                "value": request_latency.get("mean"),
                "p95": request_latency.get("p95"),
                "max": request_latency.get("max"),
                "unit": "seconds",
                "status": behavior.get("status"),
                "reference_quality": behavior.get("reference_quality"),
            },
            {
                "metric": "request_to_handover_wall_clock_latency",
                "layer": "skill_execution",
                "evaluated_count": request_wall_clock_latency.get("count"),
                "value": request_wall_clock_latency.get("mean"),
                "p95": request_wall_clock_latency.get("p95"),
                "max": request_wall_clock_latency.get("max"),
                "unit": "wall_clock_seconds",
                "status": behavior.get("status"),
                "reference_quality": behavior.get("reference_quality"),
            },
            {
                "metric": "wrong_preposition_release_latency",
                "layer": "skill_execution",
                "evaluated_count": release_latency.get("count"),
                "value": release_latency.get("mean"),
                "p95": release_latency.get("p95"),
                "max": release_latency.get("max"),
                "unit": "seconds",
                "status": behavior.get("status"),
                "reference_quality": behavior.get("reference_quality"),
            },
            {
                "metric": "wrong_preposition_release_wall_clock_latency",
                "layer": "skill_execution",
                "evaluated_count": release_wall_clock_latency.get("count"),
                "value": release_wall_clock_latency.get("mean"),
                "p95": release_wall_clock_latency.get("p95"),
                "max": release_wall_clock_latency.get("max"),
                "unit": "wall_clock_seconds",
                "status": behavior.get("status"),
                "reference_quality": behavior.get("reference_quality"),
            },
            *[
                {
                    "metric": metric_name,
                    "layer": layer,
                    "evaluated_count": distribution.get("count"),
                    "value": distribution.get("mean"),
                    "p95": distribution.get("p95"),
                    "max": distribution.get("max"),
                    "unit": unit,
                    "status": behavior.get("status"),
                    "reference_quality": behavior.get(
                        "reference_quality"
                    ),
                }
                for metric_name, layer, key, unit in (
                    (
                        "ground_truth_to_dt_request_fact_latency",
                        "reducer_fused",
                        "ground_truth_to_dt_request_fact_latency_sec",
                        "source_seconds",
                    ),
                    (
                        "dt_request_fact_to_bt_ingress_latency",
                        "bt_decision",
                        "dt_request_fact_to_bt_ingress_latency_sec",
                        "source_seconds",
                    ),
                    (
                        "dt_request_fact_to_bt_evaluation_latency",
                        "bt_decision",
                        "dt_request_fact_to_bt_evaluation_latency_sec",
                        "source_seconds",
                    ),
                    (
                        "dt_request_fact_to_bt_acceptance_latency",
                        "bt_decision",
                        "dt_request_fact_to_bt_acceptance_latency_sec",
                        "source_seconds",
                    ),
                    (
                        "bt_acceptance_to_handover_latency",
                        "skill_execution",
                        "bt_acceptance_to_handover_latency_sec",
                        "source_seconds",
                    ),
                    (
                        "ground_truth_to_dt_request_fact_wall_clock_latency",
                        "reducer_fused",
                        (
                            "ground_truth_to_dt_request_fact_"
                            "wall_clock_latency_sec"
                        ),
                        "wall_clock_seconds",
                    ),
                    (
                        "dt_request_fact_to_bt_ingress_wall_clock_latency",
                        "bt_decision",
                        (
                            "dt_request_fact_to_bt_ingress_"
                            "wall_clock_latency_sec"
                        ),
                        "wall_clock_seconds",
                    ),
                    (
                        "dt_request_fact_to_bt_evaluation_wall_clock_latency",
                        "bt_decision",
                        (
                            "dt_request_fact_to_bt_evaluation_"
                            "wall_clock_latency_sec"
                        ),
                        "wall_clock_seconds",
                    ),
                    (
                        "dt_request_fact_to_bt_acceptance_wall_clock_latency",
                        "bt_decision",
                        (
                            "dt_request_fact_to_bt_acceptance_"
                            "wall_clock_latency_sec"
                        ),
                        "wall_clock_seconds",
                    ),
                    (
                        "bt_acceptance_to_handover_wall_clock_latency",
                        "skill_execution",
                        (
                            "bt_acceptance_to_handover_"
                            "wall_clock_latency_sec"
                        ),
                        "wall_clock_seconds",
                    ),
                )
                for distribution in [pipeline_latency.get(key, {})]
            ],
            {
                "metric": "unnecessary_preparation",
                "layer": "skill_execution",
                "correct_count": behavior_summary.get(
                    "unnecessary_preparation_count"
                ),
                "evaluated_count": behavior.get(
                    "unnecessary_preparation",
                    {},
                ).get("completed_preparation_count"),
                "value": behavior_summary.get(
                    "unnecessary_preparation_count"
                ),
                "accuracy": behavior_summary.get(
                    "unnecessary_preparation_rate"
                ),
                "unit": "count_and_rate",
                "status": behavior.get("status"),
                "reference_quality": behavior.get("reference_quality"),
            },
            {
                "metric": "invariant_violation",
                "layer": "runtime_safety",
                "value": behavior_summary.get(
                    "invariant_violation_count"
                ),
                "unit": "count",
                "status": behavior.get("status"),
                "reference_quality": (
                    "explicit runtime invariant markers"
                ),
            },
        ]
    )

    fields = (
        "metric",
        "layer",
        "correct_count",
        "evaluated_count",
        "accuracy",
        "precision",
        "recall",
        "f1",
        "status",
        "reference_quality",
        "value",
        "unit",
        "p95",
        "max",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    reference = parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--ground-truth", type=Path)
    reference.add_argument("--case-dir", type=Path)
    trace = parser.add_mutually_exclusive_group(required=True)
    trace.add_argument("--trace", type=Path)
    trace.add_argument("--decisions", type=Path)
    parser.add_argument("--mode", choices=sorted(RUN_MODES), default="strict")
    parser.add_argument("--lead-window-sec", type=float, default=10.0)
    parser.add_argument("--stable-sec", type=float, default=3.0)
    parser.add_argument("--max-prediction-age-sec", type=float, default=3.0)
    parser.add_argument(
        "--request-reaction-window-sec",
        type=float,
        default=2.0,
        help=(
            "Maximum post-reference delay for public-request-backed reducer, "
            "BT, or skill decisions. VLM predictions remain pre-event only."
        ),
    )
    parser.add_argument("--episode-gap-sec", type=float, default=2.5)
    parser.add_argument(
        "--recovery-reuse-warning-sec",
        type=float,
        default=15.0,
    )
    parser.add_argument("--phase-ground-truth", type=Path)
    parser.add_argument(
        "--score-provisional-phase",
        action="store_true",
        help=(
            "Score ambiguous phase_start boundaries as a development-only "
            "reference. The output remains explicitly provisional."
        ),
    )
    parser.add_argument("--tool-catalog", type=Path)
    parser.add_argument("--procedure-prompt", type=Path)
    parser.add_argument("--evaluation-mask", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--scorecard-csv", type=Path)
    args = parser.parse_args()

    manifest: dict[str, Any] | None = None
    phase_context_path: Path | None = None
    if args.case_dir:
        manifest, ground_truth_path = resolve_case_reference(args.case_dir)
        evaluation_mask_path = (
            args.evaluation_mask
            or resolve_case_evaluation_mask(args.case_dir, manifest)
        )
        tool_catalog_path = (
            args.tool_catalog
            or resolve_case_tool_catalog(args.case_dir, manifest)
        )
        if not args.phase_ground_truth:
            phase_context_path = resolve_case_phase_context(
                args.case_dir,
                manifest,
            )
    else:
        ground_truth_path = args.ground_truth
        evaluation_mask_path = args.evaluation_mask
        tool_catalog_path = args.tool_catalog
    trace_path = args.trace or args.decisions
    assert ground_truth_path is not None
    assert trace_path is not None
    phase_ground_truth_path = args.phase_ground_truth
    if (
        phase_ground_truth_path is None
        and args.score_provisional_phase
        and phase_context_path is not None
    ):
        phase_ground_truth_path = phase_context_path
    phase_ground_truth = (
        load_jsonl(phase_ground_truth_path)
        if phase_ground_truth_path
        else None
    )
    evaluation_mask: dict[str, Any] | None = None
    if evaluation_mask_path is not None:
        loaded_mask = json.loads(
            evaluation_mask_path.read_text(encoding="utf-8")
        )
        if not isinstance(loaded_mask, dict):
            raise ValueError(
                f"{evaluation_mask_path}: evaluation mask must be an object"
            )
        evaluation_mask = loaded_mask
    identity_map = load_tool_identity_map(tool_catalog_path)
    report = evaluate_shadow(
        ground_truth=load_jsonl(ground_truth_path),
        decisions=load_trace_jsonl(trace_path),
        lead_window_sec=args.lead_window_sec,
        mode=args.mode,
        stable_sec=args.stable_sec,
        max_prediction_age_sec=args.max_prediction_age_sec,
        request_reaction_window_sec=args.request_reaction_window_sec,
        episode_gap_sec=args.episode_gap_sec,
        recovery_reuse_warning_sec=args.recovery_reuse_warning_sec,
        phase_ground_truth=phase_ground_truth,
        allow_provisional_phase=args.score_provisional_phase,
        tool_identity_map=identity_map,
        tool_inventory=load_tool_inventory(
            args.procedure_prompt,
            identity_map,
        ),
        evaluation_mask=evaluation_mask,
    )
    if evaluation_mask_path is not None:
        report["evaluation_mask"]["source"] = {
            "file": str(evaluation_mask_path.resolve()),
            "sha256": shadow_sha256_file(evaluation_mask_path),
        }
    if tool_catalog_path is not None:
        report["tool_identity_mapping"]["source"] = {
            "file": str(tool_catalog_path.resolve()),
            "sha256": shadow_sha256_file(tool_catalog_path),
        }
    if phase_context_path is not None and not args.score_provisional_phase:
        report["phase"] = _provisional_phase_context_report(
            load_jsonl(phase_context_path)
        )
        report["phase"]["source"] = {
            "file": str(phase_context_path.resolve()),
            "sha256": shadow_sha256_file(phase_context_path),
        }
    elif phase_ground_truth_path is not None:
        report["phase"]["source"] = {
            "file": str(phase_ground_truth_path.resolve()),
            "sha256": shadow_sha256_file(phase_ground_truth_path),
        }
    if args.procedure_prompt is not None:
        report["scorecard"]["dt_tool_management"]["inventory_contract"][
            "source"
        ] = {
            "file": str(args.procedure_prompt.resolve()),
            "sha256": shadow_sha256_file(args.procedure_prompt),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.csv:
        write_layer_csv(report, args.csv)
    if args.scorecard_csv:
        write_scorecard_csv(report, args.scorecard_csv)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["runtime"]["trace_contract_error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
