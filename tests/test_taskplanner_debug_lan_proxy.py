from __future__ import annotations

import argparse
import asyncio
import importlib.util
import ipaddress
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "taskplanner_debug_lan_proxy",
    ROOT / "scripts" / "taskplanner_debug_lan_proxy.py",
)
assert SPEC is not None and SPEC.loader is not None
proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proxy
SPEC.loader.exec_module(proxy)


def _routes():
    return (
        proxy.Route(9091, "127.0.0.1", 9093),
        proxy.Route(9091, "127.0.0.1", 9099, "/shadow"),
        proxy.Route(9091, "127.0.0.1", 9094, "/multicam"),
    )


class _Writer:
    def __init__(self, peer=("127.0.0.1", 43210)) -> None:
        self.peer = peer
        self.closed = False
        self.writes: list[bytes] = []

    def get_extra_info(self, name: str):
        return self.peer if name == "peername" else None

    def write(self, value: bytes) -> None:
        self.writes.append(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FailingDrainWriter(_Writer):
    async def drain(self) -> None:
        raise ConnectionResetError("upstream reset during handshake")


class _ImmediateEofReader:
    async def read(self, _size: int) -> bytes:
        return b""


class _BlockingReader:
    async def read(self, _size: int) -> bytes:
        await asyncio.Event().wait()
        return b""


class RoutePolicyTests(unittest.TestCase):
    def test_parse_and_group_require_one_default_and_unique_paths(self) -> None:
        default = proxy.parse_route("9091=127.0.0.1:9093")
        shadow = proxy.parse_route("9091/shadow=127.0.0.1:9099")
        grouped = proxy.group_routes([default, shadow])
        self.assertEqual(grouped[9091], (default, shadow))
        with self.assertRaises(ValueError):
            proxy.group_routes([shadow])
        with self.assertRaises(ValueError):
            proxy.group_routes([default, shadow, shadow])
        with self.assertRaises(argparse.ArgumentTypeError):
            proxy.parse_route("9091/?bad=1=127.0.0.1:9099")

    def test_exact_path_rewrites_root_and_preserves_query(self) -> None:
        request = (
            b"GET /shadow?token=abc HTTP/1.1\r\n"
            b"Host: example\r\n\r\n"
        )
        selected, rewritten = proxy.select_route(_routes(), request)
        self.assertEqual(selected.websocket_path, "/shadow")
        self.assertTrue(rewritten.startswith(b"GET /?token=abc HTTP/1.1\r\n"))

    def test_trailing_unknown_and_non_get_paths_use_default_unchanged(self) -> None:
        for target in (b"/shadow/", b"/unknown"):
            request = b"GET " + target + b" HTTP/1.1\r\n\r\n"
            selected, forwarded = proxy.select_route(_routes(), request)
            self.assertIsNone(selected.websocket_path)
            self.assertEqual(forwarded, request)
        request = b"POST /shadow HTTP/1.1\r\n\r\n"
        selected, forwarded = proxy.select_route(_routes(), request)
        self.assertIsNone(selected.websocket_path)
        self.assertEqual(forwarded, request)


class AsyncRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_fragmented_header_is_completed_before_routing(self) -> None:
        reader = asyncio.StreamReader()
        pending = asyncio.create_task(
            proxy.select_connection_route(reader, _routes())
        )
        reader.feed_data(b"GET /multi")
        await asyncio.sleep(0)
        reader.feed_data(b"cam HTTP/1.1\r\nHost: example\r\n\r\n")
        selected = await pending
        self.assertIsNotNone(selected)
        assert selected is not None
        route, rewritten = selected
        self.assertEqual(route.websocket_path, "/multicam")
        self.assertTrue(rewritten.startswith(b"GET / HTTP/1.1\r\n"))

    async def test_incomplete_header_is_rejected_without_upstream_connect(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET /shadow HTTP/1.1\r\nHost: example")
        reader.feed_eof()
        writer = _Writer()
        open_connection = mock.AsyncMock()
        with mock.patch.object(proxy.asyncio, "open_connection", open_connection):
            await proxy.forward_connection(
                reader,
                writer,
                routes=_routes(),
                allowed_network=ipaddress.ip_network("127.0.0.0/8"),
                allow_loopback_clients=True,
            )
        self.assertTrue(writer.closed)
        open_connection.assert_not_awaited()

    async def test_first_eof_cancels_peer_copy_and_closes_both_sides(self) -> None:
        client_writer = _Writer()
        target_writer = _Writer()
        open_connection = mock.AsyncMock(
            return_value=(_BlockingReader(), target_writer)
        )
        with mock.patch.object(proxy.asyncio, "open_connection", open_connection):
            await asyncio.wait_for(
                proxy.forward_connection(
                    _ImmediateEofReader(),
                    client_writer,
                    routes=(proxy.Route(9091, "127.0.0.1", 9093),),
                    allowed_network=ipaddress.ip_network("127.0.0.0/8"),
                    allow_loopback_clients=True,
                ),
                timeout=1.0,
            )
        self.assertTrue(client_writer.closed)
        self.assertTrue(target_writer.closed)

    async def test_upstream_handshake_reset_still_closes_both_sides(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"GET /shadow HTTP/1.1\r\nHost: example\r\n\r\n")
        client_writer = _Writer()
        target_writer = _FailingDrainWriter()
        open_connection = mock.AsyncMock(
            return_value=(_BlockingReader(), target_writer)
        )
        with mock.patch.object(proxy.asyncio, "open_connection", open_connection):
            with self.assertRaises(ConnectionResetError):
                await proxy.forward_connection(
                    reader,
                    client_writer,
                    routes=_routes(),
                    allowed_network=ipaddress.ip_network("127.0.0.0/8"),
                    allow_loopback_clients=True,
                )
        self.assertTrue(client_writer.closed)
        self.assertTrue(target_writer.closed)


if __name__ == "__main__":
    unittest.main()
