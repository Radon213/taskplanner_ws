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
    RUN_MODES,
    load_jsonl as load_trace_jsonl,
    resolve_case_evaluation_mask,
    resolve_case_phase_context,
    resolve_case_reference,
    resolve_case_tool_catalog,
    sha256_file as shadow_sha256_file,
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


def _eligible_episode_view(
    episode: dict[str, Any],
    *,
    target_time: float,
    lead_window_sec: float,
    max_prediction_age_sec: float,
    evaluation_mask: EvaluationMask,
) -> dict[str, Any] | None:
    prior_times = [
        float(value)
        for value in episode.get("record_times_sec", [])
        if float(value) < target_time
        and evaluation_mask.metric_enabled_at("action", float(value))
    ]
    if not prior_times:
        return None
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
    timeline: ReferenceTimeline,
    lead_window_sec: float,
    stable_sec: float,
    max_prediction_age_sec: float,
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
            )
            if view is not None:
                eligible.append(view)
        exact_candidates = [
            episode for episode in eligible if episode["tool_id"] == target_tool
        ]
        selected: dict[str, Any] | None
        if exact_candidates:
            selected = max(
                exact_candidates,
                key=lambda item: (
                    float(item["last_time_sec"]),
                    float(item["first_time_sec"]),
                ),
            )
            outcome = "exact_match"
        elif eligible:
            selected = max(
                eligible,
                key=lambda item: (
                    float(item["last_time_sec"]),
                    float(item["first_time_sec"]),
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
        feasibility = "not_applicable"
        feasibility_reason = ""
        if selected is not None:
            consumed.add(selected["episode_id"])
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

        results.append(
            {
                "event_id": target["event_id"],
                "time_sec": target_time,
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
        and (
            result.get("prediction_source") == "explicit_request"
            or int(result.get("request_generation", 0)) > 0
        )
        for result in results
    )
    anticipatory_exact = sum(
        result["outcome"] == "exact_match"
        and result.get("prediction_source") == "predicted_tool"
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
        "anticipatory_exact_count": anticipatory_exact,
        "stable_exact_rate": len(stable_leads) / target_count if target_count else None,
        "precision_including_false_positives": (
            exact / precision_denominator if precision_denominator else None
        ),
        "recall": exact / target_count if target_count else None,
        "false_positive_count": len(unmatched),
        "physical_feasibility_counts": dict(feasibility_counts),
        "first_correct_lead_sec": _distribution(first_leads),
        "stable_correct_lead_sec": _distribution(stable_leads),
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
    if action in {"predict_tool", "prepare_tool"}:
        return {"ToolPrepared"}
    if action in HANDOVER_ACTIONS:
        return {
            "ToolHandoverCompleted",
            "ShadowAdditionalToolHandoverCompleted",
        }
    if action in RECOVERY_ACTIONS:
        return {"ToolReturnedToTray"}
    if action == "return_unused_preposition":
        return {"PredictedToolReturnedToRack"}
    return set()


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
        tool_rows[layer] = {
            "correct_count": outcomes.get("exact_match", 0),
            "evaluated_count": payload.get("target_count", 0),
            "accuracy": payload.get("top1_exact_rate"),
            "false_positive_count": payload.get("false_positive_count", 0),
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
        "bt_decision": tool_rows.get("bt_decision", {}),
        "notes": [
            "0704_6 is a development/calibration case, not a held-out generalization result.",
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
                "episode_gap_sec": episode_gap_sec,
                "recovery_reuse_warning_sec": recovery_reuse_warning_sec,
            },
            "layers": {},
            "metrics": None,
            "events": [],
            "evaluation_mask": mask.report(),
            "state_audit": timeline.state_audit(),
            "phase": phase_report,
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
            targets=targets,
            ground_truth=ground_truth,
            lead_window_sec=lead_window_sec,
            identity_map=identity_map,
            evaluation_mask=mask,
            tool_inventory=inventory,
        )
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
                "episode_gap_sec": episode_gap_sec,
                "recovery_reuse_warning_sec": recovery_reuse_warning_sec,
            },
            "layers": {},
            "metrics": None,
            "events": [],
            "evaluation_mask": mask.report(),
            "state_audit": timeline.state_audit(),
            "phase": phase_report,
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
            targets=targets,
            ground_truth=ground_truth,
            lead_window_sec=lead_window_sec,
            identity_map=identity_map,
            evaluation_mask=mask,
            tool_inventory=inventory,
        )
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
            targets=targets,
            timeline=timeline,
            lead_window_sec=lead_window_sec,
            stable_sec=stable_sec,
            max_prediction_age_sec=max_prediction_age_sec,
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
        "Recovery actions are excluded from handover false-positive counts and audited separately.",
        "Clinically acceptable alternatives remain human-adjudication items unless physical impossibility is provable.",
    ]
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
        "runtime": _runtime_metrics(decisions, trace_errors=trace_errors),
        "notes": notes,
    }
    report["scorecard"] = _build_scorecard(
        phase=phase_report,
        layers=layers,
        trace_records=decisions,
        targets=targets,
        ground_truth=ground_truth,
        lead_window_sec=lead_window_sec,
        identity_map=identity_map,
        evaluation_mask=mask,
        tool_inventory=inventory,
    )
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
        rows.append(
            {
                "metric": "next_tool_prediction",
                "layer": layer,
                "correct_count": metric.get("correct_count"),
                "evaluated_count": metric.get("evaluated_count"),
                "accuracy": metric.get("accuracy"),
                "status": "complete",
                "reference_quality": scorecard.get(
                    "next_tool_prediction",
                    {},
                ).get("reference_quality"),
            }
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
