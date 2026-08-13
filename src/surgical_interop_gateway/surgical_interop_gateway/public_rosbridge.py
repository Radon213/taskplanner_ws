"""Run a subscribe-only rosbridge for the reviewed public ROS contract.

Unlike the upstream executable, this wrapper does not append ``/rosapi/*`` or
launch a rosapi node.  The executable is intended to listen on loopback; the
deployment's wired-interface proxy is the only supported remote ingress.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from functools import partial
from importlib import util as importlib_util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import os
import threading
from types import ModuleType
from typing import Any

from surgical_interop_gateway.public_bridge_policy import (
    PUBLIC_MAX_CLIENTS,
    PUBLIC_MAX_INCOMING_BYTES,
    PUBLIC_MAX_INCOMING_QUEUE,
    PUBLIC_MAX_OUTGOING_MESSAGE_BYTES,
    PUBLIC_MAX_OUTGOING_QUEUE,
    PUBLIC_MAX_SUBSCRIPTION_IDS_PER_TOPIC,
    PUBLIC_LOOPBACK_ADDRESS,
    origin_is_allowed,
    parse_allowed_origins,
    peer_is_loopback,
    restrict_public_incoming_message,
    restrict_public_subscription_request,
    restrict_public_rosbridge_protocol,
)


def _wire_size(message: str | bytes) -> int:
    return len(message) if isinstance(message, bytes) else len(message.encode("utf-8"))


def _bound_public_tornado_settings(settings: dict[str, Any]) -> None:
    """Force Tornado to reject oversized frames before handler allocation."""

    settings["websocket_max_message_size"] = PUBLIC_MAX_INCOMING_BYTES


def _build_public_rosbridge_protocol(
    protocol_base: type,
    subscribe_capability: type,
) -> type:
    """Build the exact subscribe-only protocol used by the WebSocket handler.

    Keeping this factory free of ROS imports makes the security boundary
    regression-testable without starting a ROS graph or listening socket.
    """

    class PublicRosbridgeProtocol(protocol_base):
        rosbridge_capabilities = (subscribe_capability,)

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # Protocol.buffer is an upstream class attribute. Shadow it per
            # connection so malformed input can never cross client boundaries.
            self.buffer = ""
            self.old_buffer = ""
            self.fragment_size = None
            self._public_input_failed = False
            self._public_fail_close: Callable[[int, str], None] | None = None

        def set_public_fail_close(
            self,
            callback: Callable[[int, str], None],
        ) -> None:
            self._public_fail_close = callback

        def _fail_public_input(self, code: int, reason: str) -> None:
            self.buffer = ""
            self.old_buffer = ""
            if self._public_input_failed:
                return
            self._public_input_failed = True
            if self._public_fail_close is not None:
                self._public_fail_close(code, reason)

        def incoming(self, message_string: str = "") -> None:
            if self._public_input_failed:
                return

            cumulative = f"{self.buffer}{message_string}"
            if _wire_size(cumulative) > PUBLIC_MAX_INCOMING_BYTES:
                self._fail_public_input(1009, "public bridge parse buffer too large")
                return

            # A WebSocket frame is the public protocol message boundary. Parse
            # the complete candidate first so upstream's permissive substring
            # recovery cannot retain incomplete data or execute an inner object
            # extracted from malformed JSON.
            try:
                self.deserialize(cumulative)
            except Exception:
                self._fail_public_input(1007, "invalid public rosbridge JSON")
                return

            super().incoming(message_string)
            if self.buffer:
                # Defensive guard for upstream version drift: a successfully
                # admitted frame must never leave cumulative parser state.
                self._fail_public_input(1007, "incomplete public rosbridge JSON")

        def deserialize(
            self,
            msg: str,
            cid: str | None = None,
        ) -> dict[str, object]:
            parsed = super().deserialize(msg, cid)
            return restrict_public_incoming_message(parsed)

        def send(
            self,
            message: dict[str, Any] | bytes,
            cid: str | None = None,
            compression: str = "none",
        ) -> None:
            # Upstream serializes the whole logical message and then fragments
            # it, allowing each small fragment to pass a WebSocket-frame cap.
            # Serialize once, enforce the whole-message bound, and emit at most
            # one frame. No outgoing `fragment` operation exists on this bridge.
            serialized = (
                message if isinstance(message, bytes) else self.serialize(message, cid)
            )
            if serialized is None:
                return
            if _wire_size(serialized) > PUBLIC_MAX_OUTGOING_MESSAGE_BYTES:
                self.log("warn", "Dropped oversized public rosbridge logical message")
                return
            self.outgoing(serialized, compression)

    PublicRosbridgeProtocol.__name__ = "PublicRosbridgeProtocol"
    return PublicRosbridgeProtocol


def _load_upstream_websocket() -> ModuleType:
    from ament_index_python.packages import get_package_prefix

    prefix = Path(get_package_prefix("rosbridge_server"))
    executable_dir = prefix / "lib" / "rosbridge_server"
    candidates = (
        executable_dir / "rosbridge_websocket",
        executable_dir / "rosbridge_websocket.py",
    )
    source = next((path for path in candidates if path.is_file()), candidates[0])
    loader = SourceFileLoader(
        "surgical_interop_gateway._upstream_public_rosbridge", str(source)
    )
    spec = importlib_util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load rosbridge websocket executable: {source}")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    upstream = _load_upstream_websocket()
    allowed_origins = parse_allowed_origins(
        os.environ.get("PUBLIC_ROSBRIDGE_ALLOWED_ORIGINS", "")
    )
    from rosbridge_library.capabilities.subscribe import Subscribe
    from rosbridge_library.rosbridge_protocol import RosbridgeProtocol
    import rosbridge_server.websocket_handler as websocket_handler_module

    class BoundedPublicSubscribe(Subscribe):
        """Subscribe capability with image and request-count backpressure."""

        def subscribe(self, msg: dict[str, object]) -> None:
            restricted = restrict_public_subscription_request(msg)
            topic = restricted.get("topic")
            sid = restricted.get("id")
            existing = self._subscriptions.get(topic) if isinstance(topic, str) else None
            if (
                existing is not None
                and sid not in existing.clients
                and len(existing.clients) >= PUBLIC_MAX_SUBSCRIPTION_IDS_PER_TOPIC
            ):
                self.protocol.log(
                    "warn",
                    f"Subscription ID limit reached for {topic}; request ignored",
                )
                return
            super().subscribe(restricted)

    base_websocket = upstream.RosbridgeWebSocket

    class BoundedPublicRosbridgeWebSocket(base_websocket):
        """Bound both ingress and egress independently for every connection."""

        def prepare(self) -> None:
            # The wired proxy is the only supported remote ingress. Reject a
            # Tailscale/Wi-Fi/local-DNAT peer before the WebSocket upgrade even
            # if it forges an otherwise acceptable browser Origin.
            if not peer_is_loopback(self.request.remote_ip):
                self.set_status(403)
                self.finish("Forbidden")
                return
            super().prepare()

        def check_origin(self, origin: str) -> bool:
            return origin_is_allowed(origin, allowed_origins)

        def open(self, *args: str, **kwargs: str) -> None:
            cls = self.__class__
            self._public_admitted = False
            self._public_protocol_close_scheduled = False
            if cls.clients_connected >= PUBLIC_MAX_CLIENTS:
                self.close(code=1013, reason="public bridge client limit reached")
                return
            self._public_outgoing_lock = threading.Lock()
            self._public_outgoing = deque(maxlen=PUBLIC_MAX_OUTGOING_QUEUE)
            self._public_drain_scheduled = False
            self._public_admitted = True
            super().open(*args, **kwargs)
            self.protocol.set_public_fail_close(self._schedule_public_protocol_close)

        def _schedule_public_protocol_close(self, code: int, reason: str) -> None:
            """Fail-close a connection from the protocol's incoming thread."""

            if not self._public_admitted or self._public_protocol_close_scheduled:
                return
            self._public_protocol_close_scheduled = True
            self.incoming_queue.finish()
            self.__class__.event_loop.call_soon_threadsafe(
                partial(self.close, code=code, reason=reason)
            )

        def on_message(self, message: str | bytes) -> None:
            if not self._public_admitted or self._public_protocol_close_scheduled:
                return
            if _wire_size(message) > PUBLIC_MAX_INCOMING_BYTES:
                self._public_protocol_close_scheduled = True
                self.incoming_queue.finish()
                self.close(code=1009, reason="public bridge request too large")
                return
            if isinstance(message, bytes):
                try:
                    message = message.decode("utf-8")
                except UnicodeDecodeError:
                    self._public_protocol_close_scheduled = True
                    self.incoming_queue.finish()
                    self.close(code=1007, reason="invalid public bridge UTF-8")
                    return
            with self.incoming_queue.cond:
                queued = len(self.incoming_queue.queue)
            if queued >= PUBLIC_MAX_INCOMING_QUEUE:
                self._public_protocol_close_scheduled = True
                self.incoming_queue.finish()
                self.close(code=1013, reason="public bridge request queue full")
                return
            super().on_message(message)

        def send_message(self, message: bytes | str, compression: str = "none") -> None:
            if not self._public_admitted:
                return
            if _wire_size(message) > PUBLIC_MAX_OUTGOING_MESSAGE_BYTES:
                self.__class__.node_handle.get_logger().warning(
                    "Dropped oversized public rosbridge message",
                    throttle_duration_sec=5.0,
                )
                return
            binary = compression in {"cbor", "cbor-raw"}
            schedule = False
            with self._public_outgoing_lock:
                self._public_outgoing.append((message, binary))
                if not self._public_drain_scheduled:
                    self._public_drain_scheduled = True
                    schedule = True
            if schedule:
                asyncio.run_coroutine_threadsafe(
                    self._drain_public_outgoing(),
                    self.__class__.event_loop,
                )

        async def _drain_public_outgoing(self) -> None:
            while True:
                with self._public_outgoing_lock:
                    if not self._public_outgoing:
                        self._public_drain_scheduled = False
                        return
                    message, binary = self._public_outgoing.popleft()
                await self.prewrite_message(message, binary)

        def on_close(self) -> None:
            if not self._public_admitted:
                return
            self._public_admitted = False
            with self._public_outgoing_lock:
                self._public_outgoing.clear()
            super().on_close()

    upstream.RosbridgeWebSocket = BoundedPublicRosbridgeWebSocket

    # Do not import or register any mutation, service, action, or rosapi
    # capability. Unknown opcodes are rejected by the protocol dispatcher.
    PublicRosbridgeProtocol = _build_public_rosbridge_protocol(
        RosbridgeProtocol,
        BoundedPublicSubscribe,
    )
    # The upstream handler resolves this module global when a socket opens.
    websocket_handler_module.RosbridgeProtocol = PublicRosbridgeProtocol

    class PublicReadOnlyRosbridgeWebsocketNode(upstream.RosbridgeWebsocketNode):
        def __init__(self) -> None:
            # Mirror the small upstream constructor while intentionally
            # omitting its services_glob.append('/rosapi/*').
            upstream.Node.__init__(self, "public_read_only_rosbridge")
            upstream.RosbridgeWebSocket.node_handle = self
            upstream.RosbridgeWebSocket.client_manager = upstream.ClientManager(self)
            upstream.RosbridgeWebSocket.event_loop = asyncio.get_event_loop()
            self._handle_parameters()
            # Enforce the ingress limit inside Tornado before a complete large
            # WebSocket frame is materialized and reaches on_message().
            _bound_public_tornado_settings(self.tornado_settings)
            # Network exposure is a deployment invariant, not a user-tunable
            # ROS parameter. Remote clients must traverse the wired-LAN proxy.
            self.address = PUBLIC_LOOPBACK_ADDRESS
            self.protocol_parameters = restrict_public_rosbridge_protocol(
                self.protocol_parameters
            )
            upstream.RosbridgeWebSocket.protocol_parameters = self.protocol_parameters
            upstream.RosbridgeWebSocket.use_compression = self.use_compression
            self._start_server()

    upstream.RosbridgeWebsocketNode = PublicReadOnlyRosbridgeWebsocketNode
    upstream.main()


if __name__ == "__main__":
    main()
