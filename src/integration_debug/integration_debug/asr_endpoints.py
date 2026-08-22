"""Reviewed Puzzle ASR endpoint selection shared by Live and Debug nodes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


DEFAULT_CLOUD_SERVER_URL = "wss://arpa.worker-02.puzzle-ai.com"
DEFAULT_LAN_SERVER_URL = "ws://192.168.1.5:1196/"
ASR_ENDPOINT_CLOUD = "cloud"
ASR_ENDPOINT_LAN = "lan"
DEFAULT_ASR_ENDPOINT = ASR_ENDPOINT_CLOUD
# ``endpoint`` is the concrete transport used by a microphone session.
# ``route_policy`` additionally permits an operator-approved, preflight-only
# LAN preference.  Keep these distinct so ``auto`` can never become an
# arbitrary URL or a mid-session transport switch.
ASR_ROUTE_POLICY_AUTO = "auto"
DEFAULT_ASR_ROUTE_POLICY = DEFAULT_ASR_ENDPOINT
ASR_ROUTE_POLICIES = frozenset(
    {ASR_ENDPOINT_CLOUD, ASR_ENDPOINT_LAN, ASR_ROUTE_POLICY_AUTO}
)


def validate_websocket_url(value: Any) -> str:
    """Validate a configured WebSocket URL without accepting inline secrets."""

    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("ASR server URL must use ws:// or wss://")
    if parsed.username or parsed.password:
        raise ValueError("ASR credentials must not be embedded in the server URL")
    if parsed.query or parsed.fragment or "?" in url or "#" in url:
        raise ValueError(
            "ASR server URL must not include query parameters or fragments"
        )
    return url


def validate_asr_route_policy(value: Any) -> str:
    """Normalize the reviewed operational ASR route policy.

    ``auto`` means "prefer LAN only when its independently monitored
    WebSocket handshake is currently healthy".  It does not add a third
    endpoint and must be resolved to one of the two reviewed URLs before a
    microphone session can start.
    """

    policy = str(value or "").strip().casefold()
    if policy in ASR_ROUTE_POLICIES:
        return policy
    raise ValueError("ASR route policy must be 'cloud', 'lan', or 'auto'")


def resolve_puzzle_asr_endpoint(
    endpoint: Any,
    *,
    cloud_url: Any = DEFAULT_CLOUD_SERVER_URL,
    lan_url: Any = DEFAULT_LAN_SERVER_URL,
) -> tuple[str, str]:
    """Resolve the only two reviewed Puzzle ASR routes.

    Browser and ROS control requests select a route identifier rather than
    supplying a URL.  Deployment configuration may customize each named route
    before a session begins, but never turn a start command into arbitrary
    network egress.
    """

    selected = str(endpoint or "").strip().casefold()
    if selected == ASR_ENDPOINT_CLOUD:
        return selected, validate_websocket_url(cloud_url)
    if selected == ASR_ENDPOINT_LAN:
        return selected, validate_websocket_url(lan_url)
    raise ValueError(
        "ASR endpoint must be 'cloud' (arpa.worker-02) or 'lan' (192.168.1.5)"
    )
