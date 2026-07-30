from __future__ import annotations

from or_digital_twin.node import ORDigitalTwinNode
from surgical_msgs.msg import SurgeonActorEvent
from surgical_msgs.msg import SurgeonRequest


def test_validation_actor_event_is_not_applied_by_default() -> None:
    calls: list[str] = []
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._accept_validation_actor_events = False
    node._twin = type(
        "TwinProbe",
        (),
        {"apply_surgeon_actor_event": lambda _self, _msg: calls.append("applied")},
    )()
    node._publish_outward_signal = lambda _msg: calls.append("outward")
    node._publish_world_state = lambda: calls.append("world")

    node._on_surgeon_actor_event(SurgeonActorEvent())

    assert calls == []


def test_non_override_structured_request_is_rejected_by_default() -> None:
    calls: list[str] = []
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._accept_non_override_structured_requests = False
    node._twin = type(
        "TwinProbe",
        (),
        {"update_surgeon_request": lambda _self, _msg: calls.append("applied")},
    )()
    logger = type("LoggerProbe", (), {"warning": lambda _self, *_args, **_kwargs: None})()
    node.get_logger = lambda: logger

    request = SurgeonRequest()
    request.event_type = "request_tool"
    request.requested_tool = "T01"
    request.override = False
    node._on_surgeon_request(request)

    assert calls == []
