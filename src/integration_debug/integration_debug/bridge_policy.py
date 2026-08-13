"""Least-privilege rosbridge policy for the integrated Debug Mode UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEBUG_TOPICS_PUBLISH_ALLOWLIST = ("/integration/debug/heartbeat",)
DEBUG_TOPICS_SUBSCRIBE_ALLOWLIST = (
    "/integration/debug/status",
    "/integration/debug/events",
    "/integration/debug/readiness",
)
DEBUG_SERVICES_ALLOWLIST = ("/integration/debug/command",)
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
