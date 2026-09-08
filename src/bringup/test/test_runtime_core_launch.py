from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from launch import LaunchContext
from launch.utilities import perform_substitutions
from launch_ros.actions import Node
from launch_ros.utilities import evaluate_parameters


BRINGUP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRINGUP_ROOT))

from bringup.runtime_core_launch import build_bt_engine_actions  # noqa: E402


def _load_mock_launch():
    path = BRINGUP_ROOT / "launch" / "taskplanner_mock.launch.py"
    spec = importlib.util.spec_from_file_location(
        "taskplanner_mock_runtime_core_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _identity(node: Node) -> tuple[str, str, str]:
    return (
        str(node.node_package),
        str(node.node_executable),
        str(node._Node__node_name),  # noqa: SLF001 - launch contract audit
    )


def test_bt_engine_factory_returns_fresh_flat_actions_in_contract_order() -> None:
    actions = build_bt_engine_actions()
    second_actions = build_bt_engine_actions()

    assert [_identity(action) for action in actions] == [
        ("btops_gateway", "btops_gateway", "btops_gateway"),
        ("auto_apms_behavior_tree", "tree_executor", "tree_executor"),
    ]
    assert all(isinstance(action, Node) for action in actions)
    assert all(action.condition is None for action in actions)
    assert all(
        perform_substitutions(
            LaunchContext(),
            action._ExecuteLocal__output,  # noqa: SLF001 - launch contract audit
        )
        == "screen"
        for action in actions
    )
    assert actions[0]._Node__parameters == []  # noqa: SLF001
    assert actions[0] is not second_actions[0]
    assert actions[1] is not second_actions[1]

    parameters = evaluate_parameters(
        LaunchContext(),
        actions[1]._Node__parameters,  # noqa: SLF001 - launch contract audit
    )[0]
    assert parameters == {
        "tick_rate": 0.025,
        "groot2_port": 0,
        "state_change_logger": True,
    }


def test_mock_launch_splats_bt_engine_actions_once_without_reordering() -> None:
    description = _load_mock_launch().generate_launch_description()
    nodes = [
        entity for entity in description.entities if isinstance(entity, Node)
    ]
    identities = [_identity(node) for node in nodes]
    gateway_index = identities.index(
        ("btops_gateway", "btops_gateway", "btops_gateway")
    )

    assert identities.count(
        ("btops_gateway", "btops_gateway", "btops_gateway")
    ) == 1
    assert identities.count(
        ("auto_apms_behavior_tree", "tree_executor", "tree_executor")
    ) == 1
    assert identities[gateway_index : gateway_index + 2] == [
        ("btops_gateway", "btops_gateway", "btops_gateway"),
        ("auto_apms_behavior_tree", "tree_executor", "tree_executor"),
    ]
