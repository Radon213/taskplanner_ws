"""Small rosbag2_py compatibility helpers across supported ROS 2 releases."""

from __future__ import annotations

from typing import Any


def read_next_record(reader: Any) -> tuple[str, bytes, int]:
    """Read one serialized record on Jazzy and newer rosbag2_py APIs."""

    read_next = getattr(reader, "read_next_ext", None)
    if read_next is None:
        read_next = reader.read_next
    result = read_next()
    if len(result) < 3:
        raise RuntimeError(f"unexpected rosbag2 record shape: {len(result)}")
    return str(result[0]), result[1], int(result[2])


def close_reader(reader: Any) -> None:
    """Close a reader when its ROS 2 binding exposes an explicit close method."""

    close = getattr(reader, "close", None)
    if close is not None:
        close()
