"""Configuration, validation, and deterministic voice parsing for Debug Mode."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml


CONFIG_SCHEMA = "taskplanner.integration_debug.v1"
VALID_TOOL_TRANSITIONS = {
    ("tray", "robot"),
    ("tray", "surgeon"),
    ("robot", "surgeon"),
    ("robot", "tray"),
    ("mayo", "tray"),
}
VALID_RETRACTION_DIRECTIONS = {
    "UP",
    "DOWN",
    "LEFT",
    "RIGHT",
    "LEFT_RIGHT",
    "UP_DOWN",
}
MAX_RETRACTION_DISTANCE_MM = 30.0


@dataclass(frozen=True, slots=True)
class VoiceParse:
    matched: bool
    ambiguous: bool
    operation: str = ""
    payload: dict[str, Any] | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "ambiguous": self.ambiguous,
            "operation": self.operation,
            "payload": dict(self.payload or {}),
            "reason": self.reason,
        }


def load_config(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != CONFIG_SCHEMA:
        raise ValueError("integration Debug Mode config schema is invalid")
    if not isinstance(payload.get("inputs"), list) or not payload["inputs"]:
        raise ValueError("integration Debug Mode config requires input endpoints")
    if not isinstance(payload.get("outputs"), list) or not payload["outputs"]:
        raise ValueError("integration Debug Mode config requires output endpoints")
    return payload


def decode_payload(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("payload_json must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload_json must be a JSON object")
    return payload


def validate_tool_handover(payload: dict[str, Any]) -> dict[str, Any]:
    instrument_id = str(payload.get("instrument_id", "")).strip()
    instance_id = str(payload.get("instrument_instance_id", "")).strip()
    source = str(payload.get("source_location", "")).strip().lower()
    target = str(payload.get("target_location", "")).strip().lower()
    if not instrument_id:
        raise ValueError("instrument_id is required")
    if (source, target) not in VALID_TOOL_TRANSITIONS:
        raise ValueError("unsupported tool handover transition")
    return {
        "instrument_id": instrument_id,
        "instrument_instance_id": instance_id or f"{instrument_id}#1",
        "source_location": source,
        "target_location": target,
    }


def validate_retraction(payload: dict[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation", "")).strip().upper()
    if operation not in {"MOVE", "RELEASE", "CHANGE_END_EFFECTOR"}:
        raise ValueError("unsupported retraction operation")
    direction = str(payload.get("direction", "")).strip().upper()
    profile = str(payload.get("end_effector_profile", "")).strip()
    try:
        distance_mm = float(payload.get("distance_mm", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("distance_mm must be numeric") from exc
    if operation == "MOVE":
        if direction not in VALID_RETRACTION_DIRECTIONS:
            raise ValueError("unsupported retraction direction")
        if not 0.0 < distance_mm <= MAX_RETRACTION_DISTANCE_MM:
            raise ValueError("distance_mm must be greater than 0 and at most 30")
        profile = ""
    elif operation == "CHANGE_END_EFFECTOR":
        if not profile:
            raise ValueError("end_effector_profile is required")
        direction = ""
        distance_mm = 0.0
    else:
        direction = ""
        distance_mm = 0.0
        profile = ""
    return {
        "operation": operation,
        "direction": direction,
        "distance_mm": distance_mm,
        "end_effector_profile": profile,
    }


def measured_rate(samples: Iterable[float], now: float, window_sec: float) -> tuple[float, int]:
    recent = [value for value in samples if now - value <= window_sec]
    if len(recent) < 2:
        return 0.0, len(recent)
    span = recent[-1] - recent[0]
    return ((len(recent) - 1) / span if span > 0.0 else 0.0), len(recent)


def _normalized(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣_.]+", " ", text.lower()).strip()


def parse_voice_command(text: str, voice_config: dict[str, Any]) -> VoiceParse:
    normalized = _normalized(text)
    if not normalized:
        return VoiceParse(False, False, reason="empty_sentence")

    suction_on = ("석션" in normalized or "suction" in normalized) and any(
        token in normalized for token in ("켜", "시작", "on")
    )
    suction_off = ("석션" in normalized or "suction" in normalized) and any(
        token in normalized for token in ("꺼", "중지", "off")
    )
    if suction_on and suction_off:
        return VoiceParse(False, True, reason="ambiguous_suction_command")
    if suction_on or suction_off:
        return VoiceParse(
            True,
            False,
            operation="suction",
            payload={"enabled": suction_on},
        )

    is_retractor = "리트랙터" in normalized or "견인기" in normalized or "retractor" in normalized
    if is_retractor and any(token in normalized for token in ("해제", "풀어", "release")):
        return VoiceParse(
            True,
            False,
            operation="retraction",
            payload={"operation": "RELEASE", "direction": "", "distance_mm": 0.0},
        )
    if is_retractor:
        direction_aliases = {
            "왼쪽 오른쪽": "LEFT_RIGHT",
            "좌우": "LEFT_RIGHT",
            "위 아래": "UP_DOWN",
            "상하": "UP_DOWN",
            "오른쪽": "RIGHT",
            "right": "RIGHT",
            "왼쪽": "LEFT",
            "left": "LEFT",
            "아래": "DOWN",
            "down": "DOWN",
            "위": "UP",
            "up": "UP",
        }
        directions = {
            value for alias, value in direction_aliases.items() if alias in normalized
        }
        distance_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|밀리(?:미터)?)", normalized)
        if len(directions) == 1 and distance_match:
            payload = {
                "operation": "MOVE",
                "direction": directions.pop(),
                "distance_mm": float(distance_match.group(1)),
            }
            try:
                return VoiceParse(
                    True,
                    False,
                    operation="retraction",
                    payload=validate_retraction(payload),
                )
            except ValueError as exc:
                return VoiceParse(False, False, reason=str(exc))
        if len(directions) > 1:
            return VoiceParse(False, True, reason="ambiguous_retraction_direction")
        return VoiceParse(False, False, reason="incomplete_retraction_command")

    request_words = ("줘", "주세요", "전달", "건네", "please", "handover", "give")
    if not any(word in normalized for word in request_words):
        return VoiceParse(False, False, reason="no_supported_command")
    aliases = voice_config.get("aliases", {})
    matches = {
        str(public_name).strip()
        for alias, public_name in aliases.items()
        if _normalized(str(alias)) and _normalized(str(alias)) in normalized
    }
    if len(matches) > 1:
        return VoiceParse(False, True, reason="ambiguous_instrument")
    if not matches:
        return VoiceParse(False, False, reason="unknown_instrument")
    instrument_id = matches.pop()
    return VoiceParse(
        True,
        False,
        operation="tool_handover",
        payload={
            "instrument_id": instrument_id,
            "instrument_instance_id": f"{instrument_id}#1",
            "source_location": str(
                voice_config.get("default_source_location", "tray")
            ),
            "target_location": str(
                voice_config.get("default_target_location", "surgeon")
            ),
        },
    )
