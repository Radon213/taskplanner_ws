"""ROS message conversion helpers with bounded image payloads."""

from __future__ import annotations

import hashlib
from typing import Any

from rosidl_runtime_py.convert import message_to_ordereddict


def stamp_sec(stamp: Any) -> float:
    if stamp is None:
        return 0.0
    return float(getattr(stamp, "sec", 0)) + (
        float(getattr(stamp, "nanosec", 0)) / 1_000_000_000.0
    )


def message_source_stamp(msg: Any) -> float | None:
    direct = stamp_sec(getattr(msg, "stamp", None))
    if direct > 0.0:
        return direct
    header = getattr(msg, "header", None)
    header_stamp = stamp_sec(getattr(header, "stamp", None))
    return header_stamp if header_stamp > 0.0 else None


def message_payload(msg: Any) -> dict[str, Any]:
    converted = message_to_ordereddict(msg)
    return dict(converted)


def compressed_image_payload(msg: Any, *, source: str) -> dict[str, Any]:
    data = bytes(msg.data)
    return {
        "source": source,
        "format": str(msg.format),
        "frame_id": str(msg.header.frame_id),
        "header_stamp_sec": stamp_sec(msg.header.stamp),
        "byte_count": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
