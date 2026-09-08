"""Dedicated Live surgery-record observer and HTTPS submitter owner."""

from __future__ import annotations

from typing import Final

from launch import LaunchDescription
from launch_ros.actions import Node


NodeIdentity = tuple[str, str, str]

SURGERY_RECORD_OWNER_NODE_IDENTITIES: Final[dict[str, frozenset[NodeIdentity]]] = {
    "surgery-record": frozenset(
        {
            (
                "integration_debug",
                "operational_surgery_record",
                "operational_surgery_record",
            )
        }
    )
}


def generate_surgery_record_launch_description() -> LaunchDescription:
    """Launch only the run-bound record observer; it owns no control clients."""

    return LaunchDescription(
        [
            Node(
                package="integration_debug",
                executable="operational_surgery_record",
                name="operational_surgery_record",
                output="screen",
            )
        ]
    )
