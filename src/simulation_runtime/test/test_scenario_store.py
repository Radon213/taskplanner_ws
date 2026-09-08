from __future__ import annotations

import json
from pathlib import Path

import pytest

from procedure_spec import parse_scenario_config
from simulation_runtime.scenario_store import (
    ScenarioSnapshot,
    ScenarioStore,
    load_selected_bundle_state,
    persist_selected_bundle_state,
    scenario_change_is_allowed,
    scenario_config_json,
)


def _snapshot(
    *,
    bundle_name: str = "thyroidectomy",
    revision: str = "sha256:initial",
) -> ScenarioSnapshot:
    return ScenarioSnapshot(
        bundle_name=bundle_name,
        spec_dir=Path(f"/tmp/specs/{bundle_name}"),
        revision=revision,
        spec=object(),
    )


def test_store_keeps_last_good_snapshot_when_a_replacement_is_invalid() -> None:
    initial = _snapshot()
    store = ScenarioStore(initial)

    with pytest.raises(ValueError, match="spec is required"):
        store.replace(
            ScenarioSnapshot(
                bundle_name="thyroidectomy_demo",
                spec_dir=Path("/tmp/specs/thyroidectomy_demo"),
                revision="sha256:bad",
                spec=None,
            )
        )

    assert store.snapshot() is not initial
    assert store.snapshot().bundle_name == "thyroidectomy"
    assert store.snapshot().revision == "sha256:initial"


def test_store_swap_is_atomic_and_snapshot_topic_payload_is_canonical() -> None:
    initial = _snapshot()
    replacement = _snapshot(
        bundle_name="thyroidectomy_demo",
        revision="sha256:replacement",
    )
    store = ScenarioStore(initial)

    assert store.replace(replacement).revision == "sha256:initial"
    published = parse_scenario_config(scenario_config_json(store.snapshot()))

    assert published.bundle_name == "thyroidectomy_demo"
    assert published.spec_dir == "/tmp/specs/thyroidectomy_demo"
    assert published.revision == "sha256:replacement"


def test_selected_bundle_state_round_trips_without_becoming_a_world_checkpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scenario" / "selected_bundle.json"
    snapshot = _snapshot(bundle_name="thyroidectomy_demo", revision="sha256:replacement")

    persist_selected_bundle_state(path, snapshot)

    assert load_selected_bundle_state(path) == "thyroidectomy_demo"


def test_selected_bundle_state_contains_only_the_restartable_selection(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scenario" / "selected_bundle.json"
    snapshot = _snapshot(bundle_name="thyroidectomy_demo", revision="sha256:replacement")
    persist_selected_bundle_state(path, snapshot)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert load_selected_bundle_state(path) == "thyroidectomy_demo"
    assert payload == {
        "schema": "taskplanner.scenario_selection.v1",
        "bundle_name": "thyroidectomy_demo",
        "revision": "sha256:replacement",
    }


@pytest.mark.parametrize(
    ("running", "execution_state", "expected"),
    [
        (False, "idle", True),
        (False, "halted", True),
        (False, "completed", True),
        (False, "stopped", True),
        (False, "terminated", True),
        (True, "paused", True),
        (False, "paused", True),
        (True, "running", False),
        (False, "unknown", False),
    ],
)
def test_full_scenario_selection_requires_a_paused_or_stopped_lifecycle_state(
    running: bool,
    execution_state: str,
    expected: bool,
) -> None:
    accepted, _message = scenario_change_is_allowed(
        running=running,
        execution_state=execution_state,
    )

    assert accepted is expected
