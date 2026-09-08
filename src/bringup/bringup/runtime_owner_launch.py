"""Small, independently restartable Taskplanner runtime-owner launches.

Each runtime owner defines only its own node wiring, so a scoped restart never
constructs an unrelated graph. This module is launch wiring, not a second
command, scenario, or execution contract.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from launch import LaunchDescription
from launch_ros.actions import Node

from bringup.runtime_owners.cam4_mayo import (
    _cam4_mayo_actions,
    _cam4_mayo_profile_actions,
    _generate_cam4_mayo_launch_description,
)
from bringup.runtime_owners.command import (
    _command_actions,
    _command_profile_actions,
    _generate_command_launch_description,
)
from bringup.runtime_owners.execution import (
    _execution_actions,
    _execution_profile_actions,
    _generate_execution_launch_description,
)
from bringup.runtime_owners.operator_bridge import (
    _generate_operator_bridge_launch_description,
    _operator_bridge_actions,
    _operator_bridge_profile_actions,
)
from bringup.runtime_owners.perception import (
    _generate_perception_launch_description,
    _perception_actions,
    _perception_profile_actions,
)
from bringup.runtime_owners.projection import (
    _generate_projection_launch_description,
    _projection_profile_actions,
)
from bringup.runtime_owners.scenario import (
    _generate_scenario_owner_launch_description,
    _scenario_actions,
    _scenario_profile_actions,
)
from bringup.runtime_owners.simulation_input import (
    _generate_simulation_input_launch_description,
    _simulation_input_actions,
    _simulation_input_profile_actions,
)
from bringup.runtime_owners.state_core import (
    _generate_state_core_launch_description,
    _state_core_actions,
    _state_core_profile_actions,
)
from bringup.runtime_owners.tool_state import (
    _generate_tool_state_launch_description,
    _tool_state_profile_actions,
)
from bringup.runtime_owners.debug import (
    DEBUG_OWNER_NODE_IDENTITIES,
    generate_debug_control_launch_description,
    generate_debug_observer_launch_description,
    generate_debug_virtual_launch_description,
)
from bringup.runtime_owners.surgery_record import (
    SURGERY_RECORD_OWNER_NODE_IDENTITIES,
    generate_surgery_record_launch_description,
)
from bringup.runtime_owners.rosbag_recorder import (
    ROSBAG_RECORDER_OWNER_NODE_IDENTITIES,
    generate_rosbag_recorder_launch_description,
)


NodeIdentity = tuple[str, str, str]


# These identities are intentionally explicit.  A new legacy node must be
# assigned to exactly one owner before it can appear in the split runtime.
OWNER_NODE_IDENTITIES: Final[dict[str, frozenset[NodeIdentity]]] = {
    "operator-bridge": frozenset({("rosapi", "rosapi_node", "rosapi")}),
    "scenario": frozenset(
        {("simulation_runtime", "scenario_store", "scenario_store")}
    ),
    "state-core": frozenset(
        {
            ("btops_gateway", "btops_gateway", "btops_gateway"),
            ("auto_apms_behavior_tree", "tree_executor", "tree_executor"),
            ("or_digital_twin", "or_digital_twin", "or_digital_twin"),
            ("bt_orchestrator", "decision_bridge", "bt_decision_bridge"),
            (
                "bt_orchestrator",
                "bed_robot_arm_group_orchestrator",
                "bed_robot_arm_group_orchestrator",
            ),
            ("simulation_runtime", "integration_preflight", "integration_preflight"),
            ("simulation_runtime", "simulation_manager", "simulation_manager"),
        }
    ),
    "command": frozenset(
        {
            ("simulation_runtime", "speech_input_adapter", "speech_input_adapter"),
            ("voice_command", "voice_intent_resolver", "voice_command_resolver"),
            ("voice_command", "command_router", "command_router"),
        }
    ),
    "tool-state": frozenset(
        {("tool_belief_tracker", "tool_belief_tracker_node", "tool_belief_tracker")}
    ),
    "perception": frozenset(
        {
            ("simulation_runtime", "source_health_monitor", "source_health_monitor"),
            ("simulation_runtime", "cv_contract_monitor", "cv_contract_monitor"),
            ("vlm_node", "rfdetr_perception_bridge", "rfdetr_perception_bridge"),
            ("vlm_node", "pnu_perception_bridge", "pnu_perception_bridge"),
            ("vlm_node", "mock_vlm", "mock_vlm_node"),
            ("vlm_node", "snapshot_bridge", "field_snapshot_bridge"),
            ("vlm_node", "real_vlm", "real_vlm_node"),
            ("phase_estimator", "phase_estimator", "phase_estimator"),
        }
    ),
    "cam4-mayo": frozenset(
        {
            (
                "vlm_node",
                "cam4_typed_mayo_adapter",
                "cam4_typed_mayo_adapter",
            )
        }
    ),
    "projection": frozenset(
        {
            (
                "surgical_interop_gateway",
                "surgical_interop_gateway",
                "surgical_interop_gateway",
            ),
            (
                "surgical_interop_gateway",
                "camera_alias_relay",
                "surgical_camera_alias_relay",
            ),
        }
    ),
    "execution": frozenset(
        {
            (
                "surgical_interop_execution",
                "execution_command_proxy",
                "execution_command_proxy",
            ),
            (
                "surgical_interop_execution",
                "fault_action_emulator",
                "robot_contract_emulator",
            ),
            (
                "surgical_interop_execution",
                "fault_action_emulator",
                "virtual_robot_contract_emulator",
            ),
            (
                "surgical_interop_execution",
                "surgical_interop_execution_bridge",
                "surgical_interop_execution_bridge",
            ),
        }
    ),
    "simulation-input": frozenset(
        {
            ("vlm_node", "synthetic_scene_camera", "synthetic_scene_camera"),
            ("vlm_node", "no_image_camera", "no_image_camera"),
            ("simulation_runtime", "surgeon_actor", "surgeon_actor"),
            ("simulation_runtime", "llm_surgeon_actor", "surgeon_actor"),
        }
    ),
    **SURGERY_RECORD_OWNER_NODE_IDENTITIES,
    **ROSBAG_RECORDER_OWNER_NODE_IDENTITIES,
    **DEBUG_OWNER_NODE_IDENTITIES,
}


# This is deliberately the only owner dispatch table.  Individual owner
# modules own their node/process declarations; adding or changing launch
# wiring never turns this file back into a composite graph.
OWNER_LAUNCH_BUILDERS: Final[dict[str, Callable[[], LaunchDescription]]] = {
    "scenario": _generate_scenario_owner_launch_description,
    "operator-bridge": _generate_operator_bridge_launch_description,
    "state-core": _generate_state_core_launch_description,
    "command": _generate_command_launch_description,
    "tool-state": _generate_tool_state_launch_description,
    "perception": _generate_perception_launch_description,
    "cam4-mayo": _generate_cam4_mayo_launch_description,
    "projection": _generate_projection_launch_description,
    "execution": _generate_execution_launch_description,
    "simulation-input": _generate_simulation_input_launch_description,
    "surgery-record": generate_surgery_record_launch_description,
    "rosbag-recorder": generate_rosbag_recorder_launch_description,
    "debug-observer": generate_debug_observer_launch_description,
    "debug-control": generate_debug_control_launch_description,
    "debug-virtual": generate_debug_virtual_launch_description,
}


def node_identity(action: Node) -> NodeIdentity:
    """Return static identity without evaluating a launch context."""

    name = getattr(action, "_Node__node_name", None)
    if not isinstance(name, str):
        raise RuntimeError("owner launch requires a static legacy node name")
    return (str(action.node_package), str(action.node_executable), name)


def owner_node_identities(owner: str) -> frozenset[NodeIdentity]:
    if owner not in OWNER_NODE_IDENTITIES:
        _validate_owner_name(owner)
    return OWNER_NODE_IDENTITIES[owner]


def _validate_owner_name(owner: str) -> None:
    if owner not in OWNER_NODE_IDENTITIES:
        known = ", ".join(OWNER_NODE_IDENTITIES)
        raise RuntimeError(f"unknown runtime owner {owner!r}; expected one of {known}")


def generate_owner_launch_description(owner: str) -> LaunchDescription:
    """Build one independently restartable owner launch.

    Every owner uses direct wiring and a small pure runtime-profile slice.
    """

    _validate_owner_name(owner)
    return OWNER_LAUNCH_BUILDERS[owner]()
