"""Fair, bounded scheduling helpers for multicamera websocket writes."""

from __future__ import annotations

from collections import deque
import re
from threading import Lock
from typing import Any, Generic, TypeVar


_Payload = TypeVar("_Payload")
_TOPIC_PATTERN = re.compile(r'"topic"\s*:\s*"([^"\\]+)"')


def _cbor_head(data: memoryview, offset: int) -> tuple[int, int, int] | None:
    if offset >= len(data):
        return None
    initial = data[offset]
    offset += 1
    major = initial >> 5
    additional = initial & 0x1F
    if additional < 24:
        return major, additional, offset
    byte_count = {24: 1, 25: 2, 26: 4, 27: 8}.get(additional)
    if byte_count is None or offset + byte_count > len(data):
        return None
    value = int.from_bytes(data[offset : offset + byte_count], "big")
    return major, value, offset + byte_count


def _cbor_text(data: memoryview, offset: int) -> tuple[str, int] | None:
    head = _cbor_head(data, offset)
    if head is None:
        return None
    major, length, value_offset = head
    if major != 3 or length > 4096 or value_offset + length > len(data):
        return None
    try:
        value = bytes(data[value_offset : value_offset + length]).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return value, value_offset + length


def _cbor_envelope_topic(message: bytes | bytearray | memoryview) -> str | None:
    """Read only the small CBOR map header preceding the image payload.

    rosbridge encodes ordered ``op``, ``topic``, then ``msg`` map entries for
    both cbor and cbor-raw.  Stop before ``msg`` so a multi-megabyte byte array
    is never decoded or copied merely to schedule the write.
    """

    data = memoryview(message)
    head = _cbor_head(data, 0)
    if head is None:
        return None
    major, item_count, offset = head
    if major != 5 or item_count > 16:
        return None
    for _ in range(item_count):
        key_result = _cbor_text(data, offset)
        if key_result is None:
            return None
        key, offset = key_result
        value_result = _cbor_text(data, offset)
        if value_result is None:
            # The large ``msg`` value follows topic in rosbridge envelopes.
            # Do not attempt a generic recursive skip that would scan it.
            return None
        value, offset = value_result
        if key == "topic":
            return value
    return None


def rosbridge_topic_key(message: Any) -> str:
    """Extract a stable topic key without parsing a multi-megabyte image."""

    if isinstance(message, str):
        prefix = message[:4096]
    elif isinstance(message, (bytes, bytearray, memoryview)):
        cbor_topic = _cbor_envelope_topic(message)
        if cbor_topic is not None:
            return cbor_topic
        prefix = bytes(message[:4096]).decode("utf-8", errors="ignore")
    else:
        return "__unknown_large_message__"
    match = _TOPIC_PATTERN.search(prefix)
    return match.group(1) if match else "__unknown_large_message__"


class LatestPerTopicScheduler(Generic[_Payload]):
    """Bound in-flight writes while fairly retaining each topic's latest frame.

    At most one frame per topic is in flight and at most one newer frame per
    topic is retained. Completion drains the FIFO of waiting topic keys, so a
    high-rate first camera cannot permanently crowd out later cameras.
    """

    def __init__(self, max_inflight: int) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        self._max_inflight = max_inflight
        self._lock = Lock()
        self._inflight: set[str] = set()
        self._pending_latest: dict[str, _Payload] = {}
        self._ready_topics: deque[str] = deque()
        self._ready_topic_set: set[str] = set()

    def offer(self, topic: str, payload: _Payload) -> list[tuple[str, _Payload]]:
        with self._lock:
            if topic in self._inflight or len(self._inflight) >= self._max_inflight:
                self._pending_latest[topic] = payload
                if topic not in self._ready_topic_set:
                    self._ready_topics.append(topic)
                    self._ready_topic_set.add(topic)
                return []
            self._inflight.add(topic)
            return [(topic, payload)]

    def complete(self, topic: str) -> list[tuple[str, _Payload]]:
        ready: list[tuple[str, _Payload]] = []
        with self._lock:
            self._inflight.discard(topic)
            candidates = len(self._ready_topics)
            while (
                candidates > 0
                and self._ready_topics
                and len(self._inflight) < self._max_inflight
            ):
                candidates -= 1
                next_topic = self._ready_topics.popleft()
                if next_topic in self._inflight:
                    self._ready_topics.append(next_topic)
                    continue
                self._ready_topic_set.discard(next_topic)
                payload = self._pending_latest.pop(next_topic, None)
                if payload is None:
                    continue
                self._inflight.add(next_topic)
                ready.append((next_topic, payload))
        return ready

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending_latest)
