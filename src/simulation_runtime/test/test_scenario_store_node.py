from __future__ import annotations

from pathlib import Path
import threading
from types import SimpleNamespace

from simulation_runtime import scenario_store_node as node_module
from simulation_runtime.scenario_store import (
    ScenarioSnapshot,
    ScenarioStore,
    persist_selected_bundle_state,
)
from simulation_runtime.scenario_store_node import ScenarioStoreNode


def _snapshot(
    bundle_name: str,
    revision: str,
) -> ScenarioSnapshot:
    return ScenarioSnapshot(
        bundle_name=bundle_name,
        spec_dir=Path(f"/tmp/specs/{bundle_name}"),
        revision=revision,
        spec=object(),
    )


def _node(*, running: bool, execution_state: str) -> ScenarioStoreNode:
    node = ScenarioStoreNode.__new__(ScenarioStoreNode)
    node._store = ScenarioStore(_snapshot("thyroidectomy", "sha256:old"))
    node._spec_root = Path("/tmp/specs")
    node._state_lock = threading.RLock()
    node._latest_state = SimpleNamespace(
        running=running,
        execution_state=execution_state,
    )
    node._latest_state_received_monotonic = node_module.time.monotonic()
    node._simulation_state_max_age_sec = 3.0
    node._publish_snapshot_calls = 0
    node._publish_snapshot = lambda: setattr(
        node,
        "_publish_snapshot_calls",
        node._publish_snapshot_calls + 1,
    )
    node._selection_state_path = None
    return node


def _request(**overrides):
    values = {
        "bundle_name": "thyroidectomy_demo",
        "restart_if_running": False,
        "preview_only": False,
        "reload_if_changed": False,
        "expected_candidate_revision": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_store_node_owns_existing_select_service_and_publishes_one_snapshot(
    monkeypatch,
) -> None:
    node = _node(running=False, execution_state="idle")
    candidate = _snapshot("thyroidectomy_demo", "sha256:new")
    monkeypatch.setattr(node_module, "load_scenario_snapshot", lambda *_args: candidate)

    response = node._handle_select_bundle(_request(), SimpleNamespace())

    assert response.success is True
    assert response.active_bundle == "thyroidectomy_demo"
    assert response.applied is True
    assert response.disposition == "applied"
    assert node._store.snapshot().revision == "sha256:new"
    assert node._publish_snapshot_calls == 1


def test_store_node_applies_full_selection_while_paused(monkeypatch) -> None:
    node = _node(running=True, execution_state="paused")
    candidate = _snapshot("thyroidectomy_demo", "sha256:new")
    monkeypatch.setattr(node_module, "load_scenario_snapshot", lambda *_args: candidate)

    response = node._handle_select_bundle(
        _request(restart_if_running=True),
        SimpleNamespace(),
    )

    assert response.success is True
    assert response.applied is True
    assert response.disposition == "applied"
    assert node._store.snapshot().bundle_name == "thyroidectomy_demo"
    assert node._publish_snapshot_calls == 1


def test_store_node_rejects_selection_before_the_first_lifecycle_frame(monkeypatch) -> None:
    node = _node(running=False, execution_state="idle")
    node._latest_state = None
    candidate = _snapshot("thyroidectomy_demo", "sha256:new")
    monkeypatch.setattr(node_module, "load_scenario_snapshot", lambda *_args: candidate)

    response = node._handle_select_bundle(_request(), SimpleNamespace())

    assert response.success is False
    assert response.applied is False
    assert response.disposition == "deferred_paused_or_stopped_required"
    assert "waiting for the current simulation state" in response.message
    assert node._store.snapshot().bundle_name == "thyroidectomy"


def test_store_node_rejects_stale_lifecycle_state(monkeypatch) -> None:
    node = _node(running=False, execution_state="idle")
    node._latest_state_received_monotonic = node_module.time.monotonic() - 3.1
    candidate = _snapshot("thyroidectomy_demo", "sha256:new")
    monkeypatch.setattr(node_module, "load_scenario_snapshot", lambda *_args: candidate)

    response = node._handle_select_bundle(_request(), SimpleNamespace())

    assert response.success is False
    assert response.applied is False
    assert "waiting for a fresh simulation state" in response.message


def test_store_node_persists_only_after_the_atomic_swap(monkeypatch, tmp_path: Path) -> None:
    node = _node(running=False, execution_state="idle")
    node._selection_state_path = tmp_path / "selected_bundle.json"
    candidate = _snapshot("thyroidectomy_demo", "sha256:new")
    monkeypatch.setattr(node_module, "load_scenario_snapshot", lambda *_args: candidate)

    response = node._handle_select_bundle(_request(), SimpleNamespace())

    assert response.success is True
    assert node_module.load_selected_bundle_state(node._selection_state_path) == "thyroidectomy_demo"
    assert node._selection_state_path.read_text(encoding="utf-8") == (
        '{"schema":"taskplanner.scenario_selection.v1","bundle_name":"thyroidectomy_demo","revision":"sha256:new"}\n'
    )


def test_store_node_applies_selection_without_a_topology_restart_scope(
    monkeypatch,
    tmp_path: Path,
) -> None:
    node = _node(running=False, execution_state="stopped")
    node._selection_state_path = tmp_path / "selected_bundle.json"
    candidate = _snapshot("thyroidectomy_demo", "sha256:new")
    monkeypatch.setattr(node_module, "load_scenario_snapshot", lambda *_args: candidate)

    response = node._handle_select_bundle(_request(), SimpleNamespace())

    assert response.success is True
    assert response.applied is True
    assert response.disposition == "applied"
    assert "restart" not in response.message


def test_stale_persisted_selection_falls_back_to_the_launch_default(
    monkeypatch, tmp_path: Path
) -> None:
    node = ScenarioStoreNode.__new__(ScenarioStoreNode)
    node._spec_root = tmp_path / "specs"
    node._selection_state_path = tmp_path / "selected_bundle.json"
    node.get_logger = lambda: SimpleNamespace(warning=lambda _message: None)
    persist_selected_bundle_state(
        node._selection_state_path,
        _snapshot("removed_bundle", "sha256:" + "0" * 64),
    )
    monkeypatch.setattr(
        node_module,
        "load_scenario_snapshot",
        lambda *_args: (_ for _ in ()).throw(FileNotFoundError("bundle missing")),
    )

    assert node._initial_bundle("thyroidectomy") == "thyroidectomy"
