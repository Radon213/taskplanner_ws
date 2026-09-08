"""Start Taskplanner's always-on Live surgery-record owner."""

from bringup.runtime_owner_launch import generate_owner_launch_description


def generate_launch_description():
    return generate_owner_launch_description("surgery-record")
