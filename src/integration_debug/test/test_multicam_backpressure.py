from integration_debug.multicam_backpressure import (
    LatestPerTopicScheduler,
    rosbridge_topic_key,
)


def _cbor_head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 256:
        return bytes([(major << 5) | 24, value])
    if value < 65536:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, "big")
    return bytes([(major << 5) | 26]) + value.to_bytes(4, "big")


def _cbor_text(value: str) -> bytes:
    encoded = value.encode()
    return _cbor_head(3, len(encoded)) + encoded


def _cbor_bytes(value: bytes) -> bytes:
    return _cbor_head(2, len(value)) + value


def _rosbridge_cbor_fixture(topic: str, *, raw: bool = False) -> bytes:
    data = b"x" * 100_000
    if raw:
        message = (
            _cbor_head(5, 3)
            + _cbor_text("secs")
            + bytes([1])
            + _cbor_text("nsecs")
            + bytes([2])
            + _cbor_text("bytes")
            + _cbor_bytes(data)
        )
    else:
        message = _cbor_head(5, 1) + _cbor_text("data") + _cbor_bytes(data)
    return (
        _cbor_head(5, 3)
        + _cbor_text("op")
        + _cbor_text("publish")
        + _cbor_text("topic")
        + _cbor_text(topic)
        + _cbor_text("msg")
        + message
    )


def test_topic_key_is_extracted_without_parsing_large_payload() -> None:
    message = '{"op":"publish","topic":"/camera/three","msg":{"data":"' + (
        "x" * 100_000
    )
    assert rosbridge_topic_key(message) == "/camera/three"


def test_topic_key_is_extracted_from_cbor_and_cbor_raw_envelopes() -> None:
    cbor_message = _rosbridge_cbor_fixture("/camera/cbor")
    cbor_raw_message = _rosbridge_cbor_fixture("/camera/cbor_raw", raw=True)
    assert rosbridge_topic_key(cbor_message) == "/camera/cbor"
    assert rosbridge_topic_key(cbor_raw_message) == "/camera/cbor_raw"


def test_slow_writes_deliver_all_five_camera_topics_without_unbounded_queue() -> None:
    scheduler = LatestPerTopicScheduler[str](max_inflight=2)
    submitted: list[tuple[str, str]] = []

    cbor_frames = [
        _rosbridge_cbor_fixture(f"/camera/{index}")
        for index in range(5)
    ]
    for index, frame in enumerate(cbor_frames):
        submitted.extend(
            scheduler.offer(rosbridge_topic_key(frame), f"initial-{index}")
        )
    # Simulate a high-rate first camera while its initial prewrite is slow.
    for frame in range(100):
        submitted.extend(scheduler.offer("/camera/0", f"newest-{frame}"))

    assert scheduler.inflight_count == 2
    assert scheduler.pending_count == 4

    completed: list[str] = []
    while submitted:
        topic, _payload = submitted.pop(0)
        completed.append(topic)
        submitted.extend(scheduler.complete(topic))
        assert scheduler.inflight_count <= 2
        assert scheduler.pending_count <= 5

    assert set(completed) == {f"/camera/{index}" for index in range(5)}
    assert completed[-1] == "/camera/0"
    assert scheduler.inflight_count == 0
    assert scheduler.pending_count == 0


def test_pending_slot_keeps_only_the_latest_frame_per_topic() -> None:
    scheduler = LatestPerTopicScheduler[str](max_inflight=1)
    assert scheduler.offer("/camera/0", "inflight")
    assert scheduler.offer("/camera/1", "old") == []
    assert scheduler.offer("/camera/1", "latest") == []
    assert scheduler.pending_count == 1
    assert scheduler.complete("/camera/0") == [("/camera/1", "latest")]


def test_completing_another_topic_does_not_drop_an_inflight_topics_latest() -> None:
    scheduler = LatestPerTopicScheduler[str](max_inflight=2)
    assert scheduler.offer("/camera/0", "inflight-0")
    assert scheduler.offer("/camera/1", "inflight-1")
    assert scheduler.offer("/camera/0", "latest-0") == []

    assert scheduler.complete("/camera/1") == []
    assert scheduler.pending_count == 1
    assert scheduler.complete("/camera/0") == [("/camera/0", "latest-0")]
