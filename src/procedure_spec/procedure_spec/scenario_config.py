"""Small shared contract for ScenarioStore's read-only revision topic.

The ScenarioStore is the sole writer of this payload.  Consumers use it only
to learn which local, researcher-authored bundle revision is current; it is
not a command, route, or safety authority.  Keeping parsing here prevents
each runtime owner from growing a slightly different JSON/schema gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping


SCENARIO_CONFIG_SCHEMA = "taskplanner.scenario_config.v1"
_MAX_TEXT_CHARS = 1024


@dataclass(frozen=True, slots=True)
class ScenarioConfigSnapshot:
    """One bounded ScenarioStore revision notice."""

    bundle_name: str
    spec_dir: str
    revision: str


def scenario_config_payload(
    *,
    bundle_name: object,
    spec_dir: object,
    revision: object,
) -> dict[str, str]:
    """Build a canonical, bounded public snapshot payload."""

    snapshot = ScenarioConfigSnapshot(
        bundle_name=_text(bundle_name, "bundle_name"),
        spec_dir=_text(spec_dir, "spec_dir"),
        revision=_text(revision, "revision"),
    )
    return {
        "schema": SCENARIO_CONFIG_SCHEMA,
        "bundle_name": snapshot.bundle_name,
        "spec_dir": snapshot.spec_dir,
        "revision": snapshot.revision,
    }


def parse_scenario_config(value: object) -> ScenarioConfigSnapshot:
    """Parse one topic message while leaving filesystem validation to owners."""

    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("scenario config is not valid JSON") from exc
    else:
        payload = value
    if not isinstance(payload, Mapping):
        raise ValueError("scenario config must be an object")
    if payload.get("schema") != SCENARIO_CONFIG_SCHEMA:
        raise ValueError("scenario config schema mismatch")
    return ScenarioConfigSnapshot(
        bundle_name=_text(payload.get("bundle_name"), "bundle_name"),
        spec_dir=_text(payload.get("spec_dir"), "spec_dir"),
        revision=_text(payload.get("revision"), "revision"),
    )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"scenario config {field} must be a string")
    text = value.strip()
    if not text or len(text) > _MAX_TEXT_CHARS or any(ch in text for ch in "\r\n\x00"):
        raise ValueError(f"scenario config {field} is invalid")
    return text


__all__ = [
    "SCENARIO_CONFIG_SCHEMA",
    "ScenarioConfigSnapshot",
    "parse_scenario_config",
    "scenario_config_payload",
]
