"""Start Taskplanner's endpoint routing and virtual-execution owner."""

from bringup.runtime_owner_launch import generate_owner_launch_description


def generate_launch_description():
    return generate_owner_launch_description("execution")

