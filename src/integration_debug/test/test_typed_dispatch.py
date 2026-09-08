from copy import deepcopy
from pathlib import Path

import pytest

from integration_debug.contracts import load_config, validate_dispatch_payload
from integration_debug.typed_dispatch import DispatchPolicy


def _dispatcher() -> DispatchPolicy:
    config = load_config(
        Path(__file__).parents[1] / "config" / "integration_debug.yaml"
    )
    return DispatchPolicy.from_dispatch_config(config["dispatch"])


def test_service_dispatch_resolves_only_the_declared_endpoint_type_and_shape() -> None:
    request = _dispatcher().resolve(
        {
            "kind": "service",
            "endpoint": "retraction_service",
            "type": "surgical_interop_msgs/srv/ExecuteRetractionCommand",
            "payload": {
                "command": "adjust_retraction",
                "target_side": "left",
                "distance_m": 0.01,
            },
            "timeout_sec": 12.0,
        },
        endpoint_source="external",
    )

    assert request.kind == "service"
    assert request.ros_endpoint == "/surgery/retraction/command"
    assert request.physical is True
    assert request.single_flight is True
    assert request.timeout_sec == 12.0
    assert validate_dispatch_payload(
        request.endpoint.payload_codec, request.payload
    ) == {
        "command": "adjust_retraction",
        "target_side": "left",
        "distance_m": 0.01,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "topic"},
        {"endpoint": "/not/in/the/catalog"},
        {"type": "std_msgs/msg/String"},
        {"payload": []},
        {"timeout_sec": -1},
        {"timeout_sec": 120.1},
    ],
)
def test_dispatch_policy_rejects_kind_endpoint_type_and_payload_substitution(
    overrides: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "kind": "service",
        "endpoint": "retraction_service",
        "type": "surgical_interop_msgs/srv/ExecuteRetractionCommand",
        "payload": {
            "command": "start_retraction",
            "target_side": "none",
            "distance_m": 0.0,
        },
        "timeout_sec": 120.0,
    }
    payload.update(overrides)

    with pytest.raises(ValueError):
        _dispatcher().resolve(payload, endpoint_source="external")


def test_dispatch_can_log_the_exact_current_ros_endpoint_without_widening_policy() -> None:
    dispatcher = _dispatcher()
    request = dispatcher.resolve(
        {
            "kind": "action",
            "endpoint": "/integration/debug/virtual/tool_handover",
            "type": "surgical_interop_msgs/action/ExecuteToolHandover",
            "payload": {
                "instrument_id": "Kelly forceps",
                "source_location": "tray",
                "target_location": "surgeon",
            },
            "timeout_sec": 300.0,
        },
        endpoint_source="virtual",
    )

    assert request.endpoint.name == "tool_handover"
    assert request.ros_endpoint == "/integration/debug/virtual/tool_handover"

    with pytest.raises(ValueError, match="configured policy"):
        dispatcher.resolve(
            {
                "kind": "action",
                "endpoint": "/surgery/tool_handover",
                "type": "surgical_interop_msgs/action/ExecuteToolHandover",
                "payload": {
                    "instrument_id": "Kelly forceps",
                    "source_location": "tray",
                    "target_location": "surgeon",
                },
                "timeout_sec": 300.0,
            },
            endpoint_source="virtual",
        )


def test_runtime_endpoint_override_cannot_add_a_new_source_route() -> None:
    with pytest.raises(ValueError, match="undeclared dispatch endpoint source"):
        _dispatcher().with_endpoint_overrides(
            {("tool_handover", "adhoc"): "/test/adhoc/tool_handover"}
        )


def test_physical_dispatch_policy_requires_a_single_flight_action_or_service() -> None:
    config = load_config(
        Path(__file__).parents[1] / "config" / "integration_debug.yaml"
    )
    physical_topic = deepcopy(config)
    physical_topic["dispatch"]["endpoints"].append(
        {
            "name": "invalid_physical_topic",
            "kind": "topic",
            "type": "std_msgs/msg/String",
            "endpoints": {"default": "/integration/debug/invalid"},
            "payload_codec": "string_data",
            "physical": True,
            "single_flight": True,
            "timeout_sec": 0.0,
            "response_semantics": "none",
        }
    )
    with pytest.raises(ValueError, match="must use action or service"):
        DispatchPolicy.from_config(physical_topic)

    missing_single_flight = deepcopy(config)
    missing_single_flight["dispatch"]["endpoints"][0]["single_flight"] = False
    with pytest.raises(ValueError, match="must enable single_flight"):
        DispatchPolicy.from_config(missing_single_flight)


def test_legacy_operation_is_only_a_conversion_to_the_generic_request() -> None:
    request = _dispatcher().resolve_legacy_operation(
        "tool_handover",
        {
            "instrument_id": "Kelly forceps",
            "source_location": "tray",
            "target_location": "surgeon",
        },
        endpoint_source="external",
    )

    assert request.endpoint.name == "tool_handover"
    assert request.kind == "action"
    assert request.ros_type == "surgical_interop_msgs/action/ExecuteToolHandover"


def test_string_topic_codec_keeps_its_wire_type_strict() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        validate_dispatch_payload("string_data", {"data": 7})


def test_generic_catalog_endpoint_uses_declared_contract_without_new_codec() -> None:
    config = load_config(
        Path(__file__).parents[1] / "config" / "integration_debug.yaml"
    )
    config["dispatch"]["endpoints"].append(
        {
            "name": "review_note",
            "kind": "topic",
            "type": "std_msgs/msg/String",
            "endpoints": {"default": "/integration/review_note"},
            "payload_contract": {
                "data": {"required": True, "type": "string", "max_length": 80}
            },
            "fixed_payload": {},
            "physical": False,
            "single_flight": False,
            "timeout_sec": 0.0,
            "response_semantics": "none",
        }
    )
    dispatcher = DispatchPolicy.from_config(config)

    request = dispatcher.resolve(
        {
            "kind": "topic",
            "endpoint": "review_note",
            "type": "std_msgs/msg/String",
            "payload": {"data": "operator note"},
            "timeout_sec": 0.0,
        },
        endpoint_source="external",
    )

    assert request.endpoint.payload_codec == ""
    assert validate_dispatch_payload(
        request.endpoint.payload_codec,
        request.payload,
        request.endpoint.payload_contract,
    ) == {"data": "operator note"}
    policy = next(
        row for row in dispatcher.public_policy("external") if row["name"] == "review_note"
    )
    assert policy["payload_contract"] == {
        "data": {"required": True, "type": "string", "max_length": 80}
    }


def test_generic_physical_endpoint_requires_a_real_command_id_field() -> None:
    config = load_config(
        Path(__file__).parents[1] / "config" / "integration_debug.yaml"
    )
    config["dispatch"]["endpoints"].append(
        {
            "name": "generic_physical",
            "kind": "service",
            "type": "example_msgs/srv/Move",
            "endpoints": {"default": "/example/move"},
            "payload_contract": {},
            "physical": True,
            "single_flight": True,
            "timeout_sec": 1.0,
            "response_semantics": "admission",
        }
    )

    endpoint = DispatchPolicy.from_config(config).endpoint("generic_physical")

    assert endpoint.command_id_field == "command_id"
