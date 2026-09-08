"""Start Taskplanner's stateful Twin, lifecycle, and BT core owner."""

from bringup.runtime_owner_launch import generate_owner_launch_description


def generate_launch_description():
    return generate_owner_launch_description("state-core")

