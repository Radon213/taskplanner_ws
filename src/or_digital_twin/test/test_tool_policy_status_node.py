import inspect
import json
from types import SimpleNamespace

from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.tool_policy_status import TOOL_POLICY_STATUS_TOPIC
from test_tool_policy_status import _inputs


def _node():
    inputs = _inputs()
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = SimpleNamespace(
        state=inputs["state"], instrument_states=inputs["instruments"],
        spec=SimpleNamespace(list_requestable_instrument_ids=lambda: ["T02", "T08"]),
    )
    node._ngram_prepare_probability_threshold = inputs["prepare_probability_threshold"]
    node._ngram_recovery_probability_threshold = inputs["recovery_probability_threshold"]
    node._ngram_policy_stability_sec = inputs["dwell_sec"]
    node._ngram_recovery_enabled_tools = inputs["recovery_enabled_tools"]
    node._ngram_recovery_stability = inputs["recovery_dwell"]
    messages = []
    node._tool_policy_status_pub = SimpleNamespace(publish=messages.append)
    return node, messages


def test_policy_republishes_parameter_updates_and_run_lifecycle_without_state_mutation():
    node, messages = _node()
    node._publish_tool_policy_status()
    node._publish_tool_policy_status()
    assert len(messages) == 1
    result = node._on_parameters_changed([
        SimpleNamespace(name="ngram_prepare_probability_threshold", value=0.61),
        SimpleNamespace(name="ngram_recovery_probability_threshold", value=0.27),
        SimpleNamespace(name="ngram_policy_stability_sec", value=1.4),
    ])
    assert result.successful
    assert len(messages) == 2
    payload = json.loads(messages[-1].data)
    assert payload["prepare"]["probability_threshold"] == 0.61
    assert payload["recovery"]["probability_threshold"] == 0.27
    assert payload["prepare"]["dwell_sec"] == 1.4
    assert payload["recovery"]["dwell_sec"] == 1.4

    node._twin.state.execution_state = "paused"
    node._publish_tool_policy_status()
    assert json.loads(messages[-1].data)["prepare"]["candidate"] is None
    node._twin.state.execution_state = "running"
    node._twin.state.procedure_run_id = "run-2"
    node._publish_tool_policy_status()
    assert json.loads(messages[-1].data)["procedure_run_id"] == "run-2"


def test_recovery_allowlist_update_is_projected_immediately():
    node, messages = _node()
    result = node._on_parameters_changed([
        SimpleNamespace(name="ngram_recovery_enabled_tools", value=["T08"]),
    ])
    assert result.successful
    payload = json.loads(messages[-1].data)
    assert payload["recovery"]["enabled_instrument_ids"] == ["T08"]
    assert payload["recovery"]["candidates"] == []


def test_status_publisher_is_retained_and_world_edges_emit_it():
    source = inspect.getsource(ORDigitalTwinNode.__init__)
    publisher = source.split("self._tool_policy_status_pub =", 1)[1].split(
        "self._last_tool_policy_status_json", 1
    )[0]
    assert TOOL_POLICY_STATUS_TOPIC == "/twin/tool_policy_status"
    assert "String" in publisher and "depth=1" in publisher
    assert "DurabilityPolicy.TRANSIENT_LOCAL" in publisher
    assert "self._publish_tool_policy_status()" in inspect.getsource(ORDigitalTwinNode._emit_world_state)
