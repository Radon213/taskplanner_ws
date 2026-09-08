"""Loopback-only, least-authority rosbridge for SurgiMate UI replay events.

The public SurgiMate bridge on 9092 deliberately remains read-only.  This
separate local bridge exists solely so the independently hosted browser can
record and replay its *presentation* selections alongside a manual MCAP.  It
cannot call services, send Actions, inspect rosapi, or publish any clinical
topic.  Its only mutation target is the bounded String audit topic below.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import util as importlib_util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from surgical_interop_gateway.public_bridge_policy import (
    PUBLIC_LOOPBACK_ADDRESS,
    parse_allowed_origins,
    peer_is_loopback,
)


ROSBAG_STATUS_TOPIC = "/recording/rosbag/status"
SURGIMATE_UI_AUDIT_TOPIC = "/recording/rosbag/surgimate_ui_audit"
AUDIT_SUBSCRIBE_TOPICS = (ROSBAG_STATUS_TOPIC, SURGIMATE_UI_AUDIT_TOPIC)
AUDIT_PUBLISH_TOPIC = SURGIMATE_UI_AUDIT_TOPIC
AUDIT_ALLOWED_OPERATIONS = (
    "advertise",
    "unadvertise",
    "publish",
    "subscribe",
    "unsubscribe",
)
AUDIT_REJECTED_OPERATION = "__rosbag_ui_audit_rejected__"
AUDIT_MESSAGE_TYPE = "std_msgs/msg/String"
AUDIT_MAX_INCOMING_BYTES = 8 * 1024
AUDIT_MAX_TEXT_BYTES = 2 * 1024
AUDIT_MAX_CLIENTS = 4
AUDIT_CONTRACT_HEADER = "X-Taskplanner-Bridge-Contract"
AUDIT_CONTRACT = "rosbag-ui-audit-v1"


def _wire_size(message: str | bytes) -> int:
    return len(message) if isinstance(message, bytes) else len(message.encode("utf-8"))


def audit_origin_is_allowed(origin: str, allowed_origins: tuple[str, ...]) -> bool:
    """Require an exact configured browser Origin; do not accept all LAN sites."""

    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return False
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" in allowed_origins
    except (TypeError, ValueError):
        return False


def restrict_audit_subscription_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Allow latest, bounded subscriptions to only recorder status/audit data."""

    topic = request.get("topic")
    if topic not in AUDIT_SUBSCRIBE_TOPICS:
        raise ValueError(f"rosbag UI audit subscription not allowed: {topic!r}")
    restricted = dict(request)
    restricted["queue_length"] = 1
    restricted["throttle_rate"] = 0
    restricted["compression"] = "none"
    restricted["qos"] = {
        "history": "keep_last",
        "depth": 1,
        "reliability": "reliable",
        "durability": "transient_local" if topic == ROSBAG_STATUS_TOPIC else "volatile",
    }
    return restricted


def restrict_audit_advertise_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Allow precisely one non-latched String publisher for SurgiMate events."""

    if request.get("topic") != AUDIT_PUBLISH_TOPIC:
        raise ValueError("rosbag UI audit advertisement topic is not allowed")
    if request.get("type") != AUDIT_MESSAGE_TYPE:
        raise ValueError("rosbag UI audit publisher must use std_msgs/msg/String")
    restricted = dict(request)
    restricted["latch"] = False
    restricted["queue_size"] = 10
    return restricted


def restrict_audit_publish_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only a bounded String payload on the private presentation lane."""

    if request.get("topic") != AUDIT_PUBLISH_TOPIC:
        raise ValueError("rosbag UI audit publish topic is not allowed")
    requested_type = request.get("type")
    if requested_type is not None and requested_type != AUDIT_MESSAGE_TYPE:
        raise ValueError("rosbag UI audit publisher must use std_msgs/msg/String")
    message = request.get("msg")
    if not isinstance(message, Mapping) or set(message) != {"data"}:
        raise ValueError("rosbag UI audit payload must be exactly a String data field")
    data = message.get("data")
    if not isinstance(data, str) or _wire_size(data) > AUDIT_MAX_TEXT_BYTES:
        raise ValueError("rosbag UI audit String payload exceeds its bound")
    restricted = dict(request)
    restricted["type"] = AUDIT_MESSAGE_TYPE
    restricted["msg"] = {"data": data}
    restricted.pop("latch", None)
    restricted.pop("queue_size", None)
    return restricted


def restrict_audit_incoming_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Return a narrow, capability-safe rosbridge operation or a reject token."""

    operation = message.get("op")
    if operation not in AUDIT_ALLOWED_OPERATIONS:
        return {"op": AUDIT_REJECTED_OPERATION}
    try:
        if operation == "subscribe":
            return restrict_audit_subscription_request(message)
        if operation == "advertise":
            return restrict_audit_advertise_request(message)
        if operation == "unadvertise":
            if message.get("topic") != AUDIT_PUBLISH_TOPIC:
                raise ValueError("rosbag UI audit unadvertise topic is not allowed")
        if operation == "publish":
            return restrict_audit_publish_request(message)
        if operation == "unsubscribe" and message.get("topic") not in AUDIT_SUBSCRIBE_TOPICS:
            raise ValueError("rosbag UI audit unsubscribe topic is not allowed")
    except ValueError:
        return {"op": AUDIT_REJECTED_OPERATION}
    return dict(message)


def _load_upstream_websocket() -> ModuleType:
    from ament_index_python.packages import get_package_prefix

    prefix = Path(get_package_prefix("rosbridge_server"))
    candidates = (
        prefix / "lib" / "rosbridge_server" / "rosbridge_websocket",
        prefix / "lib" / "rosbridge_server" / "rosbridge_websocket.py",
    )
    source = next((path for path in candidates if path.is_file()), candidates[0])
    loader = SourceFileLoader(
        "surgical_interop_gateway._upstream_rosbag_ui_audit_rosbridge", str(source)
    )
    spec = importlib_util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load rosbridge websocket executable: {source}")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    """Run the local UI-audit bridge with a three-capability protocol only."""

    import asyncio
    import os

    upstream = _load_upstream_websocket()
    allowed_origins = parse_allowed_origins(
        os.environ.get(
            "ROSBAG_UI_AUDIT_ALLOWED_ORIGINS",
            "http://127.0.0.1:5174,http://localhost:5174",
        )
    )
    from rosbridge_library.capabilities.advertise import Advertise
    from rosbridge_library.capabilities.publish import Publish
    from rosbridge_library.capabilities.subscribe import Subscribe
    from rosbridge_library.rosbridge_protocol import RosbridgeProtocol
    import rosbridge_server.websocket_handler as websocket_handler_module

    class AuditAdvertise(Advertise):
        def advertise(self, message: dict[str, Any]) -> None:
            try:
                super().advertise(restrict_audit_advertise_request(message))
            except ValueError:
                self.protocol.log("warn", "Rejected rosbag UI audit advertisement")

        def unadvertise(self, message: dict[str, Any]) -> None:
            try:
                restricted = restrict_audit_incoming_message({"op": "unadvertise", **message})
                if restricted.get("op") == AUDIT_REJECTED_OPERATION:
                    raise ValueError
                super().unadvertise(restricted)
            except ValueError:
                self.protocol.log("warn", "Rejected rosbag UI audit unadvertisement")

    class AuditPublish(Publish):
        def publish(self, message: dict[str, Any]) -> None:
            try:
                super().publish(restrict_audit_publish_request(message))
            except ValueError:
                self.protocol.log("warn", "Rejected rosbag UI audit publish")

    class AuditSubscribe(Subscribe):
        def subscribe(self, message: dict[str, Any]) -> None:
            try:
                super().subscribe(restrict_audit_subscription_request(message))
            except ValueError:
                self.protocol.log("warn", "Rejected rosbag UI audit subscription")

    class AuditRosbridgeProtocol(RosbridgeProtocol):
        rosbridge_capabilities = (AuditAdvertise, AuditPublish, AuditSubscribe)

        def deserialize(self, message: str, cid: str | None = None) -> dict[str, object]:
            parsed = super().deserialize(message, cid)
            return restrict_audit_incoming_message(parsed)

    base_websocket = upstream.RosbridgeWebSocket

    class AuditRosbridgeWebSocket(base_websocket):
        def prepare(self) -> None:
            if not peer_is_loopback(self.request.remote_ip):
                self.set_status(403)
                self.finish("Forbidden")
                return
            self.set_header(AUDIT_CONTRACT_HEADER, AUDIT_CONTRACT)
            super().prepare()

        def check_origin(self, origin: str) -> bool:
            return audit_origin_is_allowed(origin, allowed_origins)

        def open(self, *args: str, **kwargs: str) -> None:
            if self.__class__.clients_connected >= AUDIT_MAX_CLIENTS:
                self.close(code=1013, reason="rosbag UI audit client limit reached")
                return
            super().open(*args, **kwargs)

        def on_message(self, message: str | bytes) -> None:
            if _wire_size(message) > AUDIT_MAX_INCOMING_BYTES:
                self.close(code=1009, reason="rosbag UI audit request too large")
                return
            if isinstance(message, bytes):
                try:
                    message = message.decode("utf-8")
                except UnicodeDecodeError:
                    self.close(code=1007, reason="invalid rosbag UI audit UTF-8")
                    return
            super().on_message(message)

        def send_message(self, message: bytes | str, compression: str = "none") -> None:
            if _wire_size(message) > AUDIT_MAX_INCOMING_BYTES:
                self.__class__.node_handle.get_logger().warning(
                    "Dropped oversized rosbag UI audit output",
                    throttle_duration_sec=5.0,
                )
                return
            super().send_message(message, compression)

    upstream.RosbridgeWebSocket = AuditRosbridgeWebSocket
    websocket_handler_module.RosbridgeProtocol = AuditRosbridgeProtocol

    class AuditRosbridgeWebsocketNode(upstream.RosbridgeWebsocketNode):
        def __init__(self) -> None:
            upstream.Node.__init__(self, "rosbag_ui_audit_rosbridge")
            upstream.RosbridgeWebSocket.node_handle = self
            upstream.RosbridgeWebSocket.client_manager = upstream.ClientManager(self)
            upstream.RosbridgeWebSocket.event_loop = asyncio.get_event_loop()
            self._handle_parameters()
            self.address = PUBLIC_LOOPBACK_ADDRESS
            self.protocol_parameters["topics_glob"] = list(AUDIT_SUBSCRIBE_TOPICS)
            self.protocol_parameters["services_glob"] = []
            self.protocol_parameters["actions_glob"] = []
            self.tornado_settings["websocket_max_message_size"] = AUDIT_MAX_INCOMING_BYTES
            upstream.RosbridgeWebSocket.protocol_parameters = self.protocol_parameters
            upstream.RosbridgeWebSocket.use_compression = self.use_compression
            self._start_server()

    upstream.RosbridgeWebsocketNode = AuditRosbridgeWebsocketNode
    upstream.main()


if __name__ == "__main__":
    main()
