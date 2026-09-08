import pytest

from integration_debug import ros_transport


class _FakeMessage:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    @staticmethod
    def get_fields_and_field_types() -> dict[str, str]:
        return {"command_id": "string", "data": "string", "source_id": "string"}


class _FakeService:
    Request = _FakeMessage


def test_generic_service_payload_maps_fixed_and_browser_fields(monkeypatch) -> None:
    monkeypatch.setattr(ros_transport, "get_service", lambda _type: _FakeService)
    monkeypatch.setattr(
        ros_transport,
        "set_message_fields",
        lambda message, fields: message.values.update(fields),
    )

    message = ros_transport.build_wire_payload(
        kind="service",
        ros_type="example_msgs/srv/Move",
        payload={"data": "move"},
        fixed_payload={"source_id": "taskplanner"},
        command_id="debug-123",
        command_id_field="command_id",
    )

    assert message.values == {
        "source_id": "taskplanner",
        "data": "move",
        "command_id": "debug-123",
    }


def test_physical_generic_payload_rejects_missing_or_overridden_command_id(monkeypatch) -> None:
    monkeypatch.setattr(ros_transport, "get_service", lambda _type: _FakeService)
    monkeypatch.setattr(ros_transport, "set_message_fields", lambda *_args: None)

    with pytest.raises(ValueError, match="requires a command ID"):
        ros_transport.build_wire_payload(
            kind="service",
            ros_type="example_msgs/srv/Move",
            payload={},
            command_id_field="command_id",
        )
    with pytest.raises(ValueError, match="must not replace"):
        ros_transport.build_wire_payload(
            kind="service",
            ros_type="example_msgs/srv/Move",
            payload={"command_id": "browser-value"},
            command_id="debug-123",
            command_id_field="command_id",
        )


def test_feedback_and_result_use_conventional_fields_without_endpoint_codec() -> None:
    class Feedback:
        state = "moving"
        progress = 0.4

    class Result:
        success = True
        final_state = "done"
        reason_code = "ok"

    assert ros_transport.action_feedback_fields(Feedback()) == ("moving", 0.4)
    assert ros_transport.action_result_fields(Result()) == (True, "done", "ok")

