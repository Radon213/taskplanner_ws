"""Small launch-action factories for the shared Taskplanner runtime core.

Managed deployments enter through the split owner launch files selected by
``scripts/taskplanner``.  The retained composite mock/live launch sources are
used only by explicit legacy probes and topology-reference tests.  Factories
in this module stay small so the state-core owner can share the BT pair without
constructing that retained graph.
"""

from launch_ros.actions import Node


def build_bt_engine_actions() -> list[Node]:
    """Create the two unconditional Behavior Tree engine actions in order."""

    return [
        Node(
            package="btops_gateway",
            executable="btops_gateway",
            name="btops_gateway",
            output="screen",
        ),
        Node(
            package="auto_apms_behavior_tree",
            executable="tree_executor",
            name="tree_executor",
            parameters=[
                {
                    # A direct CAM4 request is already admitted by the Twin.
                    # 40 Hz keeps the remaining BT decision latency below one
                    # 25 ms tick without making perception/video owners work
                    # harder.
                    "tick_rate": 0.025,
                    "groot2_port": 0,
                    "state_change_logger": True,
                }
            ],
            output="screen",
        ),
    ]
