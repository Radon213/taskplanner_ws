"""Small launch-action factories for the shared Taskplanner runtime core.

The public launch entrypoints remain ``taskplanner_mock.launch.py`` and
``taskplanner_live.launch.py``.  Factories in this module return ordinary
actions so callers can keep one flat, ordered launch graph while moving one
runtime capability at a time out of the legacy monolithic launch file.
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
                    "tick_rate": 0.1,
                    "groot2_port": 0,
                    "state_change_logger": True,
                }
            ],
            output="screen",
        ),
    ]
