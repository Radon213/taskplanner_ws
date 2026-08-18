#!/usr/bin/env python3
"""Validate the exact Taskplanner public rosbridge WebSocket contract."""

from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path
import os
import re
import socket
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src" / "surgical_interop_gateway"))

from surgical_interop_gateway.public_bridge_policy import (  # noqa: E402
    PUBLIC_BRIDGE_CONTRACT,
    PUBLIC_BRIDGE_CONTRACT_HEADER,
)


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_RESPONSE_HEADER_BYTES = 8 * 1024
HTTP_FIELD_NAME_PATTERN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def read_http_headers(connection, *, limit: int = MAX_RESPONSE_HEADER_BYTES) -> bytes:
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = connection.recv(min(4096, limit + 1 - len(response)))
        if not chunk:
            raise ValueError("incomplete WebSocket upgrade response")
        response.extend(chunk)
        if len(response) > limit:
            raise ValueError("WebSocket upgrade response headers are too large")
    end = response.index(b"\r\n\r\n") + 4
    if end > limit:
        raise ValueError("WebSocket upgrade response headers are too large")
    return bytes(response[:end])


def validate_websocket_upgrade(response: bytes, key: str) -> None:
    try:
        lines = response.decode("iso-8859-1").split("\r\n")
    except UnicodeDecodeError as error:
        raise ValueError("invalid HTTP response encoding") from error
    if not lines or not lines[0].startswith("HTTP/1.1 101 "):
        raise ValueError("public bridge did not return HTTP/1.1 101")

    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line:
            break
        if line[:1].isspace() or ":" not in line:
            raise ValueError("malformed WebSocket upgrade response header")
        name, value = line.split(":", 1)
        if name != name.strip() or HTTP_FIELD_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("malformed WebSocket upgrade response header name")
        headers.setdefault(name.strip().lower(), []).append(value.strip())

    def exactly_one(name: str) -> str:
        values = headers.get(name.lower(), [])
        if len(values) != 1:
            raise ValueError(f"expected exactly one {name} header")
        return values[0]

    if exactly_one("Upgrade").lower() != "websocket":
        raise ValueError("invalid WebSocket Upgrade header")
    connection_tokens = {
        token.strip().lower()
        for token in exactly_one("Connection").split(",")
        if token.strip()
    }
    if "upgrade" not in connection_tokens:
        raise ValueError("Connection header does not contain upgrade")
    expected_accept = base64.b64encode(
        hashlib.sha1(f"{key}{WEBSOCKET_GUID}".encode("ascii")).digest()
    ).decode("ascii")
    if exactly_one("Sec-WebSocket-Accept") != expected_accept:
        raise ValueError("invalid Sec-WebSocket-Accept header")
    if exactly_one(PUBLIC_BRIDGE_CONTRACT_HEADER) != PUBLIC_BRIDGE_CONTRACT:
        raise ValueError("invalid public bridge contract header")


def probe(host: str, port: int, path: str, origin: str, timeout: float) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Origin: {origin}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(request)
        response = read_http_headers(connection)
    validate_websocket_upgrade(response, key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--path", default="/")
    parser.add_argument("--origin", default="http://127.0.0.1")
    parser.add_argument("--timeout", default=1.0, type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not (1 <= args.port <= 65535):
        print("public bridge health port is invalid", file=sys.stderr)
        return 2
    if not args.path.startswith("/") or args.timeout <= 0:
        print("public bridge health arguments are invalid", file=sys.stderr)
        return 2
    try:
        probe(args.host, args.port, args.path, args.origin, args.timeout)
    except (OSError, ValueError) as error:
        print(f"public bridge health failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
