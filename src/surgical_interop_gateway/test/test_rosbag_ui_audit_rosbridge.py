from surgical_interop_gateway.rosbag_ui_audit_rosbridge import (
    AUDIT_CONTRACT,
    AUDIT_MESSAGE_TYPE,
    AUDIT_REJECTED_OPERATION,
    ROSBAG_STATUS_TOPIC,
    SURGIMATE_UI_AUDIT_TOPIC,
    audit_origin_is_allowed,
    restrict_audit_advertise_request,
    restrict_audit_incoming_message,
    restrict_audit_publish_request,
    restrict_audit_subscription_request,
)


def test_ui_audit_bridge_has_a_distinct_contract_and_exact_origin_boundary() -> None:
    origins = ("http://127.0.0.1:5174", "http://localhost:5174")

    assert AUDIT_CONTRACT == "rosbag-ui-audit-v1"
    assert audit_origin_is_allowed("http://127.0.0.1:5174", origins)
    assert audit_origin_is_allowed("http://localhost:5174/", origins)
    assert not audit_origin_is_allowed("http://127.0.0.1:4173", origins)
    assert not audit_origin_is_allowed("http://192.168.1.10:5174", origins)


def test_ui_audit_bridge_allows_only_recorder_status_and_its_own_journal_subscriptions() -> None:
    status = restrict_audit_subscription_request({"op": "subscribe", "topic": ROSBAG_STATUS_TOPIC})
    journal = restrict_audit_subscription_request(
        {"op": "subscribe", "topic": SURGIMATE_UI_AUDIT_TOPIC}
    )

    assert status["qos"]["durability"] == "transient_local"
    assert journal["qos"]["durability"] == "volatile"
    assert (
        restrict_audit_incoming_message(
            {"op": "subscribe", "topic": "/simulation/control"}
        )["op"]
        == AUDIT_REJECTED_OPERATION
    )


def test_ui_audit_bridge_restricts_publish_to_one_bounded_string_topic() -> None:
    advertised = restrict_audit_advertise_request(
        {
            "op": "advertise",
            "topic": SURGIMATE_UI_AUDIT_TOPIC,
            "type": AUDIT_MESSAGE_TYPE,
            "latch": True,
            "queue_size": 1000,
        }
    )
    published = restrict_audit_publish_request(
        {
            "op": "publish",
            "topic": SURGIMATE_UI_AUDIT_TOPIC,
            "type": AUDIT_MESSAGE_TYPE,
            "msg": {"data": "bounded presentation event"},
        }
    )

    assert advertised["latch"] is False
    assert advertised["queue_size"] == 10
    assert published["type"] == AUDIT_MESSAGE_TYPE
    assert published["msg"] == {"data": "bounded presentation event"}
    assert (
        restrict_audit_incoming_message(
            {"op": "publish", "topic": "/surgery/command", "msg": {"data": "no"}}
        )["op"]
        == AUDIT_REJECTED_OPERATION
    )
    assert (
        restrict_audit_incoming_message(
            {"op": "call_service", "service": "/recording/rosbag/set_enabled"}
        )["op"]
        == AUDIT_REJECTED_OPERATION
    )


def test_ui_audit_bridge_rejects_wrong_type_extra_fields_and_oversized_data() -> None:
    wrong_type = {
        "op": "advertise",
        "topic": SURGIMATE_UI_AUDIT_TOPIC,
        "type": "std_msgs/msg/Bool",
    }
    extra_payload = {
        "op": "publish",
        "topic": SURGIMATE_UI_AUDIT_TOPIC,
        "msg": {"data": "event", "unexpected": True},
    }
    too_large = {
        "op": "publish",
        "topic": SURGIMATE_UI_AUDIT_TOPIC,
        "msg": {"data": "x" * 2049},
    }

    for request in (wrong_type, extra_payload, too_large):
        assert restrict_audit_incoming_message(request)["op"] == AUDIT_REJECTED_OPERATION
