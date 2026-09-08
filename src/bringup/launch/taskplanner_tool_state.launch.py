"""Start the independent read-only tool-belief owner."""

from bringup.runtime_owner_launch import generate_owner_launch_description


def generate_launch_description():
    return generate_owner_launch_description("tool-state")

