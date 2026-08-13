"""Immutable read-only rosbridge policy for institutional UI consumers.

The public bridge is deliberately smaller than the operator/debug bridges.  A
client may only subscribe to the reviewed public projections and the two
scenario-gated compressed camera aliases.  It cannot publish, advertise, call
services, send Action goals, inspect rosapi, or widen this list with ROS
parameters.
"""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
from typing import Any
from urllib.parse import urlsplit


PUBLIC_STATE_TOPICS = (
    "/surgery/gateway_info",
    "/surgery/catalog",
    "/surgery/context",
    "/surgery/instruments",
    "/surgery/robots",
    "/surgery/robot_end_effectors",
    "/surgery/tool_predictions",
    "/surgery/speech",
    "/surgery/clinical_observations",
    "/surgery/health",
    "/surgery/events",
)
PUBLIC_CAMERA_TOPICS = (
    "/surgery/images/flir/compressed",
    "/surgery/images/cam4/compressed",
)
PUBLIC_SUBSCRIBE_ALLOWLIST = PUBLIC_STATE_TOPICS + PUBLIC_CAMERA_TOPICS
PUBLIC_CAPABILITY_CLASS_NAMES = ("Subscribe",)
PUBLIC_ALLOWED_INCOMING_OPERATIONS = ("subscribe", "unsubscribe")
PUBLIC_REJECTED_OPERATION = "__public_rejected__"
PUBLIC_LOOPBACK_ADDRESS = "127.0.0.1"
PUBLIC_CAMERA_QUEUE_LENGTH = 1
PUBLIC_CAMERA_MIN_THROTTLE_MS = 100
PUBLIC_CAMERA_COMPRESSION = "cbor"
PUBLIC_ALLOWED_COMPRESSIONS = ("none", "cbor", "cbor-raw")
PUBLIC_MAX_SUBSCRIPTION_IDS_PER_TOPIC = 4
PUBLIC_MAX_CLIENTS = 8
PUBLIC_MAX_INCOMING_BYTES = 64 * 1024
PUBLIC_MAX_INCOMING_QUEUE = 32
PUBLIC_MAX_OUTGOING_MESSAGE_BYTES = 4 * 1024 * 1024
PUBLIC_MAX_OUTGOING_QUEUE = 4


def parse_allowed_origins(raw: str) -> tuple[str, ...]:
    """Parse an optional comma-separated exact WebSocket Origin allowlist."""

    origins: list[str] = []
    for value in raw.split(","):
        origin = value.strip()
        if not origin:
            continue
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"invalid public rosbridge origin: {origin}")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError(f"origin must not include path/query/fragment: {origin}")
        canonical = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if canonical not in origins:
            origins.append(canonical)
    return tuple(origins)


def origin_is_allowed(origin: str, allowed_origins: tuple[str, ...]) -> bool:
    """Accept an exact configured Origin or a browser on a private LAN host.

    When no explicit list is configured, the bridge still fails closed for
    public hostnames and Internet IPs. The TCP proxy independently restricts
    peers to the designated wired interface's directly connected subnet.
    """

    try:
        parsed = urlsplit(origin)
        canonical = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if canonical in allowed_origins:
            return True
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        address = ipaddress.ip_address(host)
        return address.version == 4 and address.is_private
    except (ValueError, TypeError):
        return False


def peer_is_loopback(remote_ip: str) -> bool:
    """Accept only the local proxy as the bridge's direct TCP peer.

    This is the network security boundary. Origin is browser-controlled and
    therefore cannot prevent a raw Tailscale/Wi-Fi client from claiming a LAN
    Origin. The designated wired-LAN proxy terminates the external connection
    and opens a new loopback connection to this sidecar.
    """

    try:
        return ipaddress.ip_address(remote_ip).is_loopback
    except (ValueError, TypeError):
        return False


def restrict_public_rosbridge_protocol(
    protocol_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Return parameters with a non-overridable exact subscription allowlist.

    Current rosbridge releases use ``topics_glob`` for Subscribe.  Empty
    service/action lists are still set defensively for forward/backward
    compatibility, although this bridge does not load those capabilities.
    """

    restricted = dict(protocol_parameters)
    restricted["topics_glob"] = list(PUBLIC_SUBSCRIBE_ALLOWLIST)
    restricted["services_glob"] = []
    restricted["actions_glob"] = []
    # Upstream uses this value as an outgoing fragmentation threshold, not as a
    # whole-message admission limit. The public protocol disables that behavior
    # and owns a separate pre-send cap, so do not expose this misleading knob.
    restricted.pop("max_message_size", None)
    return restricted


def restrict_public_subscription_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Force safe compression and latest-only buffering for subscriptions.

    A browser-provided ``queue_length=0`` means no ROS-side backpressure in
    rosbridge.  Ignore that value for the two high-rate image aliases so a slow
    browser can retain only the newest pending frame. Camera output is always
    CBOR; accepting PNG would invoke rosbridge's CPU-heavy PNG encoder for every
    frame. State topics accept only the bounded non-PNG encodings below.
    """

    restricted = dict(request)
    requested_compression = restricted.get("compression", "none")
    if restricted.get("topic") in PUBLIC_CAMERA_TOPICS:
        restricted["queue_length"] = PUBLIC_CAMERA_QUEUE_LENGTH
        restricted["compression"] = PUBLIC_CAMERA_COMPRESSION
        requested_throttle = restricted.get("throttle_rate", 0)
        if not isinstance(requested_throttle, int) or isinstance(
            requested_throttle, bool
        ):
            requested_throttle = 0
        restricted["throttle_rate"] = max(
            requested_throttle,
            PUBLIC_CAMERA_MIN_THROTTLE_MS,
        )
    else:
        if requested_compression not in PUBLIC_ALLOWED_COMPRESSIONS:
            raise ValueError(
                f"public rosbridge compression not allowed: {requested_compression!r}"
            )
        if "compression" in restricted:
            restricted["compression"] = requested_compression
    return restricted


def restrict_public_incoming_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Reject every non-subscription opcode and remove unsafe client knobs.

    Upstream rosbridge lets any incoming operation lower ``fragment_size``.
    Fragmenting one camera frame into many WebSocket messages would conflict
    with our drop-oldest queue and could deliver corrupt partial frames.
    Public clients therefore cannot submit ``fragment`` frames, change outgoing
    fragmentation, alter send pacing, or select PNG protocol mode. Subscribe's
    matching ``unsubscribe`` operation remains available for clean teardown.
    """

    operation = message.get("op")
    if operation not in PUBLIC_ALLOWED_INCOMING_OPERATIONS:
        # Return a minimal unregistered operation instead of raising from
        # deserialize(). Protocol.incoming() then clears its JSON buffer and
        # follows its normal unknown-operation rejection path. Retaining a
        # rejected frame in that buffer would otherwise permit unbounded growth
        # across later WebSocket messages.
        rejected: dict[str, Any] = {"op": PUBLIC_REJECTED_OPERATION}
        request_id = message.get("id")
        if isinstance(request_id, str):
            rejected["id"] = request_id
        return rejected
    restricted = dict(message)
    for field in ("fragment_size", "message_intervall", "png"):
        restricted.pop(field, None)
    return restricted
