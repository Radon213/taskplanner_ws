"""Least-privilege rosbridge policy for the integrated Debug Mode UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEBUG_TOPICS_PUBLISH_ALLOWLIST = ("/integration/debug/heartbeat",)
DEBUG_MULTICAM_SUBSCRIBE_ALLOWLIST = (
    # Driver and synchronized images/metadata required by the operator
    # multicam console.  These patterns are subscribe-only; publishing remains
    # restricted to the debug heartbeat below.
    "/camera/*",
    "/flir_camera/*",
    "/synced/*",
    "/multicam_node/*",
    "/tf_static",
    "/world_anchor_node/status",
)
DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST = (
    "/integration/debug/status",
    "/integration/debug/events",
    "/integration/debug/readiness",
) + DEBUG_MULTICAM_SUBSCRIBE_ALLOWLIST
DEBUG_MULTICAM_SERVICES_ALLOWLIST = (
    # The browser maps the original world_console keys b/x/w/p to these exact
    # Trigger calls.  No robot-control Action or service is admitted here.
    "/world_anchor_node/begin",
    "/world_anchor_node/stop",
    "/world_anchor_node/solve",
    "/world_anchor_node/publish",
)
DEBUG_ROSAPI_SERVICES_ALLOWLIST = ("/rosapi/topics",)
DEBUG_SERVICES_ALLOWLIST = (
    "/integration/debug/command",
) + DEBUG_MULTICAM_SERVICES_ALLOWLIST + DEBUG_ROSAPI_SERVICES_ALLOWLIST
# Operational Live/LLM sidecars share a ROS domain with the planner.  Keep the
# gateway's own interlocked command endpoint and read-only topic discovery, but
# never expose world-anchor mutation services through the Tailnet bridge.  The
# standalone Debug profile retains its explicit operator-only policy above.
OPERATIONAL_DEBUG_SERVICES_ALLOWLIST = (
    "/integration/debug/command",
) + DEBUG_ROSAPI_SERVICES_ALLOWLIST
DEBUG_ACTIONS_ALLOWLIST: tuple[str, ...] = ()
DEBUG_CAPABILITY_CLASS_NAMES = (
    "Advertise",
    "Publish",
    "Subscribe",
    "Defragment",
    "CallService",
)
DEBUG_TOPICS_ALLOWLIST = tuple(
    sorted(
        set(DEBUG_TOPICS_PUBLISH_ALLOWLIST)
        | set(DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST)
    )
)
DEBUG_ROSAPI_TOPICS_GLOB = "[" + ", ".join(DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST) + "]"


# The always-on multicam observer is deliberately separate from Debug Mode.
# It must remain safe beside every operational profile, so a browser can only
# subscribe to camera/TF/status topics and call the observer's own read-only
# rosapi topic-list service.  In particular it cannot advertise or publish,
# call world-anchor/debug services, or use any Action protocol capability.
MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST = (
    "/camera/*",
    "/flir_camera/*",
    "/synced/*",
    "/multicam_node/*",
    "/tf_static",
    "/world_anchor_node/status",
)
MULTICAM_OBSERVER_TOPICS_PUBLISH_ALLOWLIST: tuple[str, ...] = ()
MULTICAM_OBSERVER_SERVICES_ALLOWLIST = (
    "/multicam_observer/rosapi/topics",
)
MULTICAM_OBSERVER_ACTIONS_ALLOWLIST: tuple[str, ...] = ()
MULTICAM_OBSERVER_CAPABILITY_CLASS_NAMES = (
    "Subscribe",
    "Defragment",
    "CallService",
)
MULTICAM_OBSERVER_ROSAPI_TOPICS_GLOB = (
    "[" + ", ".join(MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST) + "]"
)


def restrict_debug_rosbridge_protocol(
    protocol_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Return protocol parameters with a non-overridable Debug UI allowlist."""

    restricted = dict(protocol_parameters)
    # Older rosbridge capabilities consume only the legacy topics_glob key;
    # newer versions additionally honor direction-specific keys.  Set all
    # three to exact Debug endpoints so neither version falls back to None
    # (which means unrestricted access).
    restricted["topics_glob"] = list(DEBUG_TOPICS_ALLOWLIST)
    restricted["topics_pub_glob"] = list(DEBUG_TOPICS_PUBLISH_ALLOWLIST)
    restricted["topics_sub_glob"] = list(DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST)
    restricted["services_glob"] = list(DEBUG_SERVICES_ALLOWLIST)
    restricted["actions_glob"] = list(DEBUG_ACTIONS_ALLOWLIST)
    return restricted


def restrict_operational_debug_rosbridge_protocol(
    protocol_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the Debug policy safe to colocate with Live/LLM operation."""

    restricted = restrict_debug_rosbridge_protocol(protocol_parameters)
    restricted["services_glob"] = list(OPERATIONAL_DEBUG_SERVICES_ALLOWLIST)
    return restricted


def restrict_multicam_observer_rosbridge_protocol(
    protocol_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a non-overridable, read-only multicam observer policy."""

    restricted = dict(protocol_parameters)
    restricted["topics_glob"] = list(MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST)
    restricted["topics_pub_glob"] = list(
        MULTICAM_OBSERVER_TOPICS_PUBLISH_ALLOWLIST
    )
    restricted["topics_sub_glob"] = list(
        MULTICAM_OBSERVER_TOPICS_SUBSCRIBE_ALLOWLIST
    )
    restricted["services_glob"] = list(MULTICAM_OBSERVER_SERVICES_ALLOWLIST)
    restricted["actions_glob"] = list(MULTICAM_OBSERVER_ACTIONS_ALLOWLIST)
    return restricted
