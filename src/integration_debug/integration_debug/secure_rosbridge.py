"""Run rosbridge with an immutable, least-privilege Debug Mode policy.

The upstream rosbridge executable automatically adds ``/rosapi/*`` whenever a
service allowlist is configured.  On the shared live ROS domain that would let
a Debug UI client reach mutable rosapi services owned by the production
runtime.  This wrapper deliberately omits that append and does not launch a
rosapi node; the browser already provides every message and service type it
uses.
"""

from __future__ import annotations

import asyncio
from importlib import util as importlib_util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

from ament_index_python.packages import get_package_prefix

from integration_debug.bridge_policy import restrict_debug_rosbridge_protocol


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


def main() -> None:
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

    class SecureDebugRosbridgeWebsocketNode(upstream.RosbridgeWebsocketNode):
        def __init__(self) -> None:
            # This intentionally mirrors the small upstream constructor except
            # for its unconditional services_glob.append('/rosapi/*').
            upstream.Node.__init__(self, "secure_debug_rosbridge_websocket")
            upstream.RosbridgeWebSocket.node_handle = self
            upstream.RosbridgeWebSocket.client_manager = upstream.ClientManager(self)
            upstream.RosbridgeWebSocket.event_loop = asyncio.get_event_loop()
            self._handle_parameters()
            self.protocol_parameters = restrict_debug_rosbridge_protocol(
                self.protocol_parameters
            )
            upstream.RosbridgeWebSocket.protocol_parameters = self.protocol_parameters
            upstream.RosbridgeWebSocket.use_compression = self.use_compression
            self._start_server()

    upstream.RosbridgeWebsocketNode = SecureDebugRosbridgeWebsocketNode
    upstream.main()


if __name__ == "__main__":
    main()
