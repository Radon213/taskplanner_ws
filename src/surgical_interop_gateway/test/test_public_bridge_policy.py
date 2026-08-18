import json

import pytest

from surgical_interop_gateway.public_bridge_policy import (
    PUBLIC_ALLOWED_INCOMING_OPERATIONS,
    PUBLIC_BRIDGE_CONTRACT,
    PUBLIC_BRIDGE_CONTRACT_HEADER,
    PUBLIC_ALLOWED_COMPRESSIONS,
    PUBLIC_CAMERA_COMPRESSION,
    PUBLIC_CAMERA_QOS,
    PUBLIC_CAMERA_TOPICS,
    PUBLIC_CAMERA_QUEUE_LENGTH,
    PUBLIC_CAMERA_MIN_THROTTLE_MS,
    PUBLIC_CAPABILITY_CLASS_NAMES,
    PUBLIC_MAX_CLIENTS,
    PUBLIC_MAX_INCOMING_BYTES,
    PUBLIC_MAX_INCOMING_QUEUE,
    PUBLIC_MAX_OUTGOING_MESSAGE_BYTES,
    PUBLIC_MAX_OUTGOING_QUEUE,
    PUBLIC_MAX_SUBSCRIPTION_IDS_PER_TOPIC,
    PUBLIC_EVENT_QOS,
    PUBLIC_REJECTED_OPERATION,
    PUBLIC_LOOPBACK_ADDRESS,
    PUBLIC_STATE_TOPICS,
    PUBLIC_SNAPSHOT_QOS,
    PUBLIC_SUBSCRIBE_ALLOWLIST,
    origin_is_allowed,
    parse_allowed_origins,
    peer_is_loopback,
    restrict_public_subscription_request,
    restrict_public_incoming_message,
    restrict_public_rosbridge_protocol,
)
from surgical_interop_gateway.public_rosbridge import (
    _bound_public_tornado_settings,
    _build_public_rosbridge_protocol,
    _set_public_contract_header,
)


class _FakeSubscribe:
    pass


class _FakeProtocol:
    """Small upstream-shaped protocol for wrapper security regression tests."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.buffer = ""
        self.old_buffer = ""
        self.fragment_size = 1_000_000
        self.outgoing_frames: list[tuple[str | bytes, str]] = []
        self.logs: list[tuple[str, str]] = []

    def incoming(self, message_string: str = "") -> None:
        self.buffer = f"{self.buffer}{message_string}"
        self.deserialize(self.buffer)
        self.buffer = ""

    def deserialize(self, message: str, _cid: str | None = None) -> dict[str, object]:
        return json.loads(message)

    def serialize(
        self,
        message: dict[str, object],
        _cid: str | None = None,
    ) -> str:
        return json.dumps(message, separators=(",", ":"))

    def outgoing(self, message: str | bytes, compression: str = "none") -> None:
        self.outgoing_frames.append((message, compression))

    def log(self, level: str, message: str, _lid: str | None = None) -> None:
        self.logs.append((level, message))


def _protocol_type() -> type:
    return _build_public_rosbridge_protocol(_FakeProtocol, _FakeSubscribe)


def test_public_bridge_has_exact_reviewed_topic_allowlist() -> None:
    assert PUBLIC_STATE_TOPICS == (
        "/surgery/gateway_info",
        "/surgery/catalog",
        "/surgery/context",
        "/surgery/instruments",
        "/surgery/robots",
        "/surgery/robot_end_effectors",
        "/surgery/tool_predictions",
        "/surgery/speech",
        "/surgery/clinical_observations",
        "/surgery/health",
        "/surgery/events",
    )
    assert PUBLIC_CAMERA_TOPICS == (
        "/surgery/images/flir/compressed",
        "/surgery/images/cam4/compressed",
    )
    assert PUBLIC_SUBSCRIBE_ALLOWLIST == PUBLIC_STATE_TOPICS + PUBLIC_CAMERA_TOPICS
    assert len(PUBLIC_SUBSCRIBE_ALLOWLIST) == len(set(PUBLIC_SUBSCRIBE_ALLOWLIST)) == 13
    assert not any("*" in topic or "?" in topic or "[" in topic for topic in PUBLIC_SUBSCRIBE_ALLOWLIST)


def test_public_bridge_policy_cannot_be_widened_by_parameters() -> None:
    restricted = restrict_public_rosbridge_protocol(
        {
            "topics_glob": ["*", "/simulation/*"],
            "services_glob": ["*", "/rosapi/*"],
            "actions_glob": ["*"],
            "max_message_size": 1_000_000,
        }
    )

    assert restricted["topics_glob"] == list(PUBLIC_SUBSCRIBE_ALLOWLIST)
    assert restricted["services_glob"] == []
    assert restricted["actions_glob"] == []
    assert "max_message_size" not in restricted


def test_public_bridge_excludes_internal_control_and_raw_sensor_topics() -> None:
    denied = {
        "/simulation/state",
        "/simulation/control_state",
        "/sensors/surgeon/sentence",
        "/surgery/audio/request_text",
        "/external/bed_robot_arms/status",
        "/synced/flir/color/image_raw/compressed",
        "/synced/cam_4/color/image_raw/compressed",
        "/surgery/images/flir/segmented/compressed",
        "/surgery/tool_change/request",
        "/surgery/retraction/adjust",
        "/rosapi/topics",
    }
    assert denied.isdisjoint(PUBLIC_SUBSCRIBE_ALLOWLIST)


def test_public_bridge_registers_only_read_only_capabilities() -> None:
    assert PUBLIC_CAPABILITY_CLASS_NAMES == ("Subscribe",)
    assert PUBLIC_ALLOWED_INCOMING_OPERATIONS == ("subscribe", "unsubscribe")
    forbidden = {
        "Advertise",
        "Publish",
        "AdvertiseService",
        "CallService",
        "ServiceResponse",
        "UnadvertiseService",
        "AdvertiseAction",
        "ActionFeedback",
        "ActionResult",
        "SendActionGoal",
        "UnadvertiseAction",
        "Defragment",
    }
    assert forbidden.isdisjoint(PUBLIC_CAPABILITY_CLASS_NAMES)


def test_public_bridge_attaches_exact_contract_header() -> None:
    class Handler:
        headers: list[tuple[str, str]] = []

        def set_header(self, name: str, value: str) -> None:
            self.headers.append((name, value))

    handler = Handler()
    _set_public_contract_header(handler)
    assert handler.headers == [
        (PUBLIC_BRIDGE_CONTRACT_HEADER, PUBLIC_BRIDGE_CONTRACT)
    ]


def test_public_rosbridge_runtime_registers_only_subscribe_capability() -> None:
    protocol_type = _protocol_type()
    assert protocol_type.rosbridge_capabilities == (_FakeSubscribe,)
    protocol = protocol_type()
    assert protocol.fragment_size is None


def test_camera_subscription_is_latest_only_even_if_client_requests_unbounded() -> None:
    for topic in PUBLIC_CAMERA_TOPICS:
        restricted = restrict_public_subscription_request(
            {
                "op": "subscribe",
                "topic": topic,
                "queue_length": 0,
                "throttle_rate": 0,
            }
        )
        assert restricted["queue_length"] == PUBLIC_CAMERA_QUEUE_LENGTH == 1
        assert restricted["compression"] == PUBLIC_CAMERA_COMPRESSION == "cbor"
        assert (
            restricted["throttle_rate"]
            == PUBLIC_CAMERA_MIN_THROTTLE_MS
            == 100
        )
        assert restricted["qos"] == PUBLIC_CAMERA_QOS

        slower = restrict_public_subscription_request(
            {
                "op": "subscribe",
                "topic": topic,
                "throttle_rate": 250,
                "compression": "png",
            }
        )
        assert slower["throttle_rate"] == 250
        assert slower["compression"] == "cbor"
        assert slower["qos"] == PUBLIC_CAMERA_QOS

    state_request = {
        "op": "subscribe",
        "topic": "/surgery/context",
        "queue_length": 5,
    }
    assert restrict_public_subscription_request(state_request) == {
        **state_request,
        "qos": PUBLIC_SNAPSHOT_QOS,
    }


def test_public_subscription_qos_is_fixed_by_topic_class() -> None:
    hostile = {
        "history": "keep_all",
        "depth": 1_000_000,
        "reliability": "best_available",
        "durability": "best_available",
    }
    camera = restrict_public_subscription_request(
        {
            "op": "subscribe",
            "topic": "/surgery/images/flir/compressed",
            "qos": hostile,
        }
    )
    snapshot = restrict_public_subscription_request(
        {"op": "subscribe", "topic": "/surgery/context", "qos": hostile}
    )
    event = restrict_public_subscription_request(
        {"op": "subscribe", "topic": "/surgery/events", "qos": hostile}
    )

    assert camera["qos"] == PUBLIC_CAMERA_QOS
    assert snapshot["qos"] == PUBLIC_SNAPSHOT_QOS
    assert event["qos"] == PUBLIC_EVENT_QOS


def test_all_client_camera_requests_are_forced_to_cbor_not_png() -> None:
    assert PUBLIC_ALLOWED_COMPRESSIONS == ("none", "cbor", "cbor-raw")
    for client_index in range(PUBLIC_MAX_CLIENTS):
        for topic in PUBLIC_CAMERA_TOPICS:
            restricted = restrict_public_subscription_request(
                {
                    "op": "subscribe",
                    "id": f"client-{client_index}:{topic}",
                    "topic": topic,
                    "compression": "png",
                }
            )
            assert restricted["compression"] == "cbor"
            assert restricted["queue_length"] == 1
            assert restricted["throttle_rate"] == 100


@pytest.mark.parametrize("compression", ("png", "zip", "", None, 7))
def test_state_subscription_rejects_png_and_unknown_compression(
    compression: object,
) -> None:
    with pytest.raises(ValueError, match="compression not allowed"):
        restrict_public_subscription_request(
            {
                "op": "subscribe",
                "topic": "/surgery/context",
                "compression": compression,
            }
        )


def test_public_bridge_resource_limits_are_small_and_finite() -> None:
    assert PUBLIC_LOOPBACK_ADDRESS == "127.0.0.1"
    assert PUBLIC_MAX_CLIENTS == 8
    assert PUBLIC_MAX_SUBSCRIPTION_IDS_PER_TOPIC == 4
    assert PUBLIC_MAX_INCOMING_BYTES == 64 * 1024
    assert PUBLIC_MAX_INCOMING_QUEUE == 32
    assert PUBLIC_MAX_OUTGOING_MESSAGE_BYTES == 4 * 1024 * 1024
    assert PUBLIC_MAX_OUTGOING_QUEUE == 4

    tornado_settings = {"websocket_max_message_size": 500_000_000}
    _bound_public_tornado_settings(tornado_settings)
    assert tornado_settings["websocket_max_message_size"] == 64 * 1024


def test_malformed_64k_json_repeated_100_times_cannot_accumulate() -> None:
    protocol = _protocol_type()()
    closes: list[tuple[int, str]] = []
    protocol.set_public_fail_close(lambda code, reason: closes.append((code, reason)))
    malformed = "[" + ("x" * (PUBLIC_MAX_INCOMING_BYTES - 1))
    assert len(malformed.encode("utf-8")) == PUBLIC_MAX_INCOMING_BYTES

    for _ in range(100):
        protocol.incoming(malformed)
        assert protocol.buffer == ""
        assert protocol.old_buffer == ""

    assert closes == [(1007, "invalid public rosbridge JSON")]


def test_cumulative_parse_buffer_over_limit_is_cleared_and_closed_1009() -> None:
    protocol = _protocol_type()()
    closes: list[tuple[int, str]] = []
    protocol.set_public_fail_close(lambda code, reason: closes.append((code, reason)))
    protocol.buffer = "x" * PUBLIC_MAX_INCOMING_BYTES
    protocol.incoming("x")

    assert protocol.buffer == ""
    assert protocol.old_buffer == ""
    assert closes == [(1009, "public bridge parse buffer too large")]


def test_oversized_logical_output_is_dropped_before_fragmentation() -> None:
    protocol = _protocol_type()()
    assert protocol.fragment_size is None
    protocol.send(
        {
            "op": "publish",
            "topic": "/surgery/images/flir/compressed",
            "msg": {"data": "x" * PUBLIC_MAX_OUTGOING_MESSAGE_BYTES},
        }
    )
    assert protocol.outgoing_frames == []
    assert protocol.logs == [
        ("warn", "Dropped oversized public rosbridge logical message")
    ]

    protocol.send({"op": "publish", "topic": "/surgery/context", "msg": {}})
    assert len(protocol.outgoing_frames) == 1
    serialized, compression = protocol.outgoing_frames[0]
    assert compression == "none"
    assert isinstance(serialized, str)
    assert '"op":"fragment"' not in serialized


def test_public_bridge_origin_policy_rejects_public_and_malformed_origins() -> None:
    configured = parse_allowed_origins(
        "https://ui.partner.example, http://192.168.1.20:4173, https://UI.PARTNER.EXAMPLE"
    )
    assert configured == (
        "https://ui.partner.example",
        "http://192.168.1.20:4173",
    )
    assert origin_is_allowed("https://ui.partner.example", configured)
    assert origin_is_allowed("http://192.168.1.20:4173", ())
    assert origin_is_allowed("http://10.50.0.7", ())
    assert origin_is_allowed("http://127.0.0.1:4173", ())
    assert not origin_is_allowed("https://evil.example", ())
    assert not origin_is_allowed("https://8.8.8.8", ())
    assert not origin_is_allowed("null", ())
    assert not origin_is_allowed("file:///tmp/ui.html", ())


def test_public_bridge_configured_origin_requires_bare_http_origin() -> None:
    for invalid in (
        "ui.partner.example",
        "ws://ui.partner.example",
        "https://ui.partner.example/path",
        "https://ui.partner.example?token=secret",
    ):
        try:
            parse_allowed_origins(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid origin accepted: {invalid}")


def test_public_bridge_accepts_only_loopback_direct_peers() -> None:
    assert peer_is_loopback("127.0.0.1")
    assert peer_is_loopback("127.12.34.56")
    assert peer_is_loopback("::1")
    assert not peer_is_loopback("192.168.1.20")
    assert not peer_is_loopback("100.66.120.74")
    assert not peer_is_loopback("8.8.8.8")
    assert not peer_is_loopback("localhost")


def test_client_cannot_enable_fragmentation_or_unbounded_send_pacing() -> None:
    restricted = restrict_public_incoming_message(
        {
            "op": "subscribe",
            "topic": "/surgery/images/flir/compressed",
            "fragment_size": 1024,
            "message_intervall": 9999,
            "png": "png",
        }
    )
    assert restricted == {
        "op": "subscribe",
        "topic": "/surgery/images/flir/compressed",
    }


@pytest.mark.parametrize(
    "operation",
    (
        "fragment",
        "advertise",
        "publish",
        "call_service",
        "send_action_goal",
        "set_level",
        "unknown",
        None,
    ),
)
def test_public_bridge_rejects_fragment_and_every_unknown_operation(
    operation: str | None,
) -> None:
    assert restrict_public_incoming_message(
        {
            "op": operation,
            "id": "request-1",
            "num": 0,
            "total": 1,
            "data": '{"op":"subscribe","topic":"/surgery/context"}',
        }
    ) == {
        "op": PUBLIC_REJECTED_OPERATION,
        "id": "request-1",
    }


def test_public_bridge_allows_unsubscribe_without_fragment_fields() -> None:
    assert restrict_public_incoming_message(
        {
            "op": "unsubscribe",
            "id": "context-1",
            "topic": "/surgery/context",
            "fragment_size": 1024,
        }
    ) == {
        "op": "unsubscribe",
        "id": "context-1",
        "topic": "/surgery/context",
    }
