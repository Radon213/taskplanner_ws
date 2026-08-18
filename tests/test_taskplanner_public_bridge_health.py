from __future__ import annotations

import base64
import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "taskplanner_public_bridge_health.py"
SPEC = importlib.util.spec_from_file_location("taskplanner_public_bridge_health", MODULE_PATH)
assert SPEC and SPEC.loader
health = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = health
SPEC.loader.exec_module(health)


KEY = "dGhlIHNhbXBsZSBub25jZQ=="
ACCEPT = base64.b64encode(
    hashlib.sha1(f"{KEY}{health.WEBSOCKET_GUID}".encode("ascii")).digest()
).decode("ascii")


def response(*, status: str = "101 Switching Protocols", headers=()) -> bytes:
    defaults = [
        ("Upgrade", "websocket"),
        ("Connection", "keep-alive, Upgrade"),
        ("Sec-WebSocket-Accept", ACCEPT),
        (health.PUBLIC_BRIDGE_CONTRACT_HEADER, health.PUBLIC_BRIDGE_CONTRACT),
    ]
    selected = list(headers) if headers else defaults
    rendered = "".join(f"{name}: {value}\r\n" for name, value in selected)
    return f"HTTP/1.1 {status}\r\n{rendered}\r\n".encode("ascii")


def test_actual_format_websocket_upgrade_is_accepted() -> None:
    health.validate_websocket_upgrade(response(), KEY)


def test_header_names_and_token_values_are_case_insensitive() -> None:
    health.validate_websocket_upgrade(
        response(
            headers=[
                ("upgrade", "WebSocket"),
                ("connection", "keep-alive, UpGrAdE"),
                ("sec-websocket-accept", ACCEPT),
                (
                    health.PUBLIC_BRIDGE_CONTRACT_HEADER.lower(),
                    health.PUBLIC_BRIDGE_CONTRACT,
                ),
            ]
        ),
        KEY,
    )


@pytest.mark.parametrize(
    "field_name",
    [" Upgrade", "Upgrade ", "Bad Header", "Bad\x01Header", ""],
)
def test_invalid_http_field_names_are_rejected(field_name: str) -> None:
    malformed = response().replace(b"Upgrade:", f"{field_name}:".encode("ascii"), 1)
    with pytest.raises(ValueError, match="malformed"):
        health.validate_websocket_upgrade(malformed, KEY)


@pytest.mark.parametrize(
    "headers",
    [
        [("Upgrade", "websocket"), ("Connection", "Upgrade")],
        [
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Accept", "wrong"),
            (health.PUBLIC_BRIDGE_CONTRACT_HEADER, health.PUBLIC_BRIDGE_CONTRACT),
        ],
        [
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Accept", ACCEPT),
            ("Sec-WebSocket-Accept", ACCEPT),
            (health.PUBLIC_BRIDGE_CONTRACT_HEADER, health.PUBLIC_BRIDGE_CONTRACT),
        ],
        [
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Accept", ACCEPT),
            (health.PUBLIC_BRIDGE_CONTRACT_HEADER, "wrong"),
        ],
        [
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Accept", ACCEPT),
            (health.PUBLIC_BRIDGE_CONTRACT_HEADER, health.PUBLIC_BRIDGE_CONTRACT),
            (health.PUBLIC_BRIDGE_CONTRACT_HEADER, health.PUBLIC_BRIDGE_CONTRACT),
        ],
    ],
)
def test_missing_wrong_or_duplicate_identity_headers_are_rejected(headers) -> None:
    with pytest.raises(ValueError):
        health.validate_websocket_upgrade(response(headers=headers), KEY)


def test_non_101_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="101"):
        health.validate_websocket_upgrade(response(status="503 Unavailable"), KEY)


class _Chunks:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    def recv(self, _size: int) -> bytes:
        return next(self._chunks, b"")


def test_incomplete_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        health.read_http_headers(_Chunks([b"HTTP/1.1 101 Switching Protocols\r\n"]))


def test_oversized_response_is_rejected() -> None:
    oversized = b"HTTP/1.1 101 Switching Protocols\r\nX-Fill: " + b"x" * 8200
    with pytest.raises(ValueError, match="too large"):
        health.read_http_headers(_Chunks([oversized]))
