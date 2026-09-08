"""Durable, owner-local execution-route selection for warm restart.

This module intentionally stores only a stopped-state source pair.  It does
not cache endpoint availability, controller contracts, requests, or motion
admission; those remain live observations owned by the execution bridge and
the endpoint controller.
"""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from .virtual_endpoints import normalize_robot_endpoint_source


ROUTE_SELECTION_STATE_SCHEMA = "taskplanner.execution_route_selection.v1"


def load_persisted_route_selection(
    path: str | Path,
    *,
    runtime_mode: str = "",
) -> tuple[str, str] | None:
    """Read a previous stopped-state route choice, if it matches this mode."""

    configured_path = str(path).strip()
    if not configured_path:
        return None
    selection_path = Path(configured_path).expanduser()
    try:
        payload = json.loads(selection_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != ROUTE_SELECTION_STATE_SCHEMA:
        return None
    stored_mode = str(payload.get("runtime_mode", "")).strip()
    requested_mode = str(runtime_mode).strip()
    if requested_mode and stored_mode and stored_mode != requested_mode:
        return None
    try:
        return (
            normalize_robot_endpoint_source(str(payload.get("selected_source", ""))),
            normalize_robot_endpoint_source(str(payload.get("retraction_source", ""))),
        )
    except ValueError:
        return None


def persist_route_selection(
    path: str | Path,
    *,
    selected_source: str,
    retraction_source: str,
    runtime_mode: str = "",
) -> None:
    """Atomically retain the source pair selected after the stopped gate."""

    configured_path = str(path).strip()
    if not configured_path:
        return
    selection_path = Path(configured_path).expanduser()
    payload = {
        "schema": ROUTE_SELECTION_STATE_SCHEMA,
        "runtime_mode": str(runtime_mode).strip(),
        "selected_source": normalize_robot_endpoint_source(selected_source),
        "retraction_source": normalize_robot_endpoint_source(retraction_source),
    }
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = selection_path.with_name(
        f".{selection_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(selection_path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
