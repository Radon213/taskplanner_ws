from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from surgical_interop_execution.route_selection import (
    ROUTE_SELECTION_STATE_SCHEMA,
    load_persisted_route_selection,
    persist_route_selection,
)


def test_stopped_route_selection_round_trips_only_for_its_runtime_mode(tmp_path) -> None:
    path = tmp_path / "route_selection.json"

    persist_route_selection(
        path,
        selected_source="virtual",
        retraction_source="external",
        runtime_mode="live",
    )

    assert load_persisted_route_selection(path, runtime_mode="live") == (
        "virtual",
        "external",
    )
    assert load_persisted_route_selection(path, runtime_mode="llm-surgeon") is None


def test_invalid_persisted_route_selection_falls_back_to_launch_defaults(tmp_path) -> None:
    path = tmp_path / "route_selection.json"
    path.write_text(
        json.dumps(
            {
                "schema": ROUTE_SELECTION_STATE_SCHEMA,
                "selected_source": "not-a-route",
                "retraction_source": "virtual",
            }
        ),
        encoding="utf-8",
    )

    assert load_persisted_route_selection(path, runtime_mode="live") is None
