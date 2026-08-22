"""Run rosbridge with an immutable, least-privilege Debug Mode policy.

The upstream rosbridge executable automatically adds ``/rosapi/*`` whenever a
service allowlist is configured.  On the shared live ROS domain that would let
a Debug UI client reach mutable rosapi services owned by the production
runtime.  This wrapper deliberately omits that append.  The launch file starts
a separate, topic-filtered rosapi node and the policy permits only its
read-only ``/rosapi/topics`` service for the multicam console.
"""

from __future__ import annotations

import asyncio
from importlib import util as importlib_util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from ament_index_python.packages import get_package_prefix

from integration_debug.bridge_policy import (
    restrict_debug_rosbridge_protocol,
    restrict_operational_debug_rosbridge_protocol,
)
from integration_debug.multicam_backpressure import (
    LatestPerTopicScheduler,
    rosbridge_topic_key,
)


# Tornado schedules one coroutine for every outgoing rosbridge message.  A
# browser that is only slightly slower than five full-rate camera publishers
# can therefore retain an unbounded series of encoded images. Keep a very
# small number of large writes in flight and retain only the latest waiting
# image per topic. Small TF/status and service messages are never scheduled
# through this gate.
_LARGE_MESSAGE_BYTES = 64 * 1024
_MAX_PENDING_LARGE_WRITES = 2


def _load_upstream_websocket() -> ModuleType:
    prefix = Path(get_package_prefix("rosbridge_server"))
    executable_dir = prefix / "lib" / "rosbridge_server"
    candidates = (
        executable_dir / "rosbridge_websocket",
        executable_dir / "rosbridge_websocket.py",
    )
    source = next((path for path in candidates if path.is_file()), candidates[0])
    loader = SourceFileLoader(
        "integration_debug._upstream_rosbridge_websocket", str(source)
    )
    spec = importlib_util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load rosbridge websocket executable: {source}")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PolicyRestrictor = Callable[[dict[str, Any]], dict[str, Any]]


def _run(policy_restrictor: PolicyRestrictor) -> None:
    upstream = _load_upstream_websocket()
    from rosbridge_library.capabilities.advertise import Advertise
    from rosbridge_library.capabilities.call_service import CallService
    from rosbridge_library.capabilities.defragmentation import Defragment
    from rosbridge_library.capabilities.publish import Publish
    from rosbridge_library.capabilities.subscribe import Subscribe
    from rosbridge_library.rosbridge_protocol import RosbridgeProtocol

    # The Debug UI only advertises/publishes its heartbeat, subscribes to
    # Debug status, and calls the gated command service.  Service advertising,
    # service responses, and every Action capability are intentionally absent.
    RosbridgeProtocol.rosbridge_capabilities = (
        Advertise,
        Publish,
        Subscribe,
        Defragment,
        CallService,
    )

    class BackpressureRosbridgeWebSocket(upstream.RosbridgeWebSocket):
        def open(self, *args: str, **kwargs: str) -> None:
            self._large_write_scheduler = LatestPerTopicScheduler[tuple[Any, bool]](
                _MAX_PENDING_LARGE_WRITES
            )
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
            binary = compression in {"cbor", "cbor-raw"} or not isinstance(message, str)
            if is_large:
                topic = rosbridge_topic_key(message)
                for scheduled_topic, payload in self._large_write_scheduler.offer(
                    topic, (message, binary)
                ):
                    self._submit_large_write(scheduled_topic, payload)
                return

            asyncio.run_coroutine_threadsafe(
                self.prewrite_message(message, binary),
                event_loop,
            )

    upstream.RosbridgeWebSocket = BackpressureRosbridgeWebSocket

    class SecureDebugRosbridgeWebsocketNode(upstream.RosbridgeWebsocketNode):
        def __init__(self) -> None:
            # This intentionally mirrors the small upstream constructor except
            # for its unconditional services_glob.append('/rosapi/*').
            upstream.Node.__init__(self, "secure_debug_rosbridge_websocket")
            upstream.RosbridgeWebSocket.node_handle = self
            upstream.RosbridgeWebSocket.client_manager = upstream.ClientManager(self)
            upstream.RosbridgeWebSocket.event_loop = asyncio.get_event_loop()
            self._handle_parameters()
            self.protocol_parameters = policy_restrictor(
                self.protocol_parameters
            )
            upstream.RosbridgeWebSocket.protocol_parameters = self.protocol_parameters
            upstream.RosbridgeWebSocket.use_compression = self.use_compression
            self._start_server()

    upstream.RosbridgeWebsocketNode = SecureDebugRosbridgeWebsocketNode
    upstream.main()


def main() -> None:
    """Run the standalone Debug bridge with operator mutation endpoints."""

    _run(restrict_debug_rosbridge_protocol)


def operational_main() -> None:
    """Run the Live/LLM monitoring bridge without world-anchor services."""

    _run(restrict_operational_debug_rosbridge_protocol)


if __name__ == "__main__":
    main()
