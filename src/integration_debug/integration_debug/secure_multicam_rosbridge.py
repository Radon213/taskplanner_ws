"""Run the always-on multicam rosbridge with an immutable read-only policy."""

from __future__ import annotations

import asyncio
from typing import Any

from integration_debug.bridge_policy import (
    restrict_multicam_observer_rosbridge_protocol,
)
from integration_debug.multicam_backpressure import (
    LatestPerTopicScheduler,
    rosbridge_topic_key,
)
from integration_debug.secure_rosbridge import (
    _LARGE_MESSAGE_BYTES,
    _MAX_PENDING_LARGE_WRITES,
    _load_upstream_websocket,
)


def main() -> None:
    upstream = _load_upstream_websocket()
    from rosbridge_library.capabilities.call_service import CallService
    from rosbridge_library.capabilities.defragmentation import Defragment
    from rosbridge_library.capabilities.subscribe import Subscribe
    from rosbridge_library.rosbridge_protocol import RosbridgeProtocol

    # Advertise, Publish, every Action capability, and service advertising are
    # intentionally absent.  CallService is restricted to the observer-owned,
    # namespaced /multicam_observer/rosapi/topics endpoint by the protocol
    # policy below.
    RosbridgeProtocol.rosbridge_capabilities = (
        Subscribe,
        Defragment,
        CallService,
    )

    class BackpressureMulticamRosbridgeWebSocket(upstream.RosbridgeWebSocket):
        def open(self, *args: str, **kwargs: str) -> None:
            self._large_write_scheduler = LatestPerTopicScheduler[
                tuple[Any, bool]
            ](_MAX_PENDING_LARGE_WRITES)
            super().open(*args, **kwargs)

        def _submit_large_write(
            self,
            topic: str,
            payload: tuple[Any, bool],
        ) -> None:
            message, binary = payload
            cls = self.__class__
            event_loop = cls.event_loop
            if event_loop is None:
                raise RuntimeError("rosbridge event loop was not initialized")
            future = asyncio.run_coroutine_threadsafe(
                self.prewrite_message(message, binary),
                event_loop,
            )

            def drain_next(_future: Any) -> None:
                for next_topic, next_payload in self._large_write_scheduler.complete(
                    topic
                ):
                    self._submit_large_write(next_topic, next_payload)

            future.add_done_callback(drain_next)

        def send_message(self, message: Any, compression: str = "none") -> None:
            cls = self.__class__
            event_loop = cls.event_loop
            if event_loop is None:
                raise RuntimeError("rosbridge event loop was not initialized")

            try:
                message_size = len(message)
            except TypeError:
                message_size = 0
            is_large = message_size >= _LARGE_MESSAGE_BYTES
            binary = compression in {"cbor", "cbor-raw"} or not isinstance(
                message, str
            )
            if is_large:
                topic = rosbridge_topic_key(message)
                for scheduled_topic, payload in self._large_write_scheduler.offer(
                    topic, (message, binary)
                ):
                    self._submit_large_write(scheduled_topic, payload)
                return

            future = asyncio.run_coroutine_threadsafe(
                self.prewrite_message(message, binary),
                event_loop,
            )

    upstream.RosbridgeWebSocket = BackpressureMulticamRosbridgeWebSocket

    class SecureMulticamRosbridgeWebsocketNode(upstream.RosbridgeWebsocketNode):
        def __init__(self) -> None:
            # Mirror the upstream constructor while deliberately omitting its
            # unconditional services_glob.append('/rosapi/*').
            upstream.Node.__init__(self, "secure_multicam_rosbridge_websocket")
            upstream.RosbridgeWebSocket.node_handle = self
            upstream.RosbridgeWebSocket.client_manager = upstream.ClientManager(self)
            upstream.RosbridgeWebSocket.event_loop = asyncio.get_event_loop()
            self._handle_parameters()
            self.protocol_parameters = (
                restrict_multicam_observer_rosbridge_protocol(
                    self.protocol_parameters
                )
            )
            upstream.RosbridgeWebSocket.protocol_parameters = self.protocol_parameters
            upstream.RosbridgeWebSocket.use_compression = self.use_compression
            self._start_server()

    upstream.RosbridgeWebsocketNode = SecureMulticamRosbridgeWebsocketNode
    upstream.main()


if __name__ == "__main__":
    main()
