import json
import math
import threading
import time

import pytest
import rclpy
from rclpy._rclpy_pybind11 import RCLError
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy
from surgical_msgs.srv import AsrControl

from integration_debug.operational_asr_node import (
    CONTROL_SERVICE,
    NODE_NAME,
    SENTENCE_TOPIC,
    STATUS_SCHEMA,
    STATUS_TOPIC,
    OperationalAsrNode,
    _absolute_topic_name,
    _json_dumps,
)


class FakeRuntime:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.state = "STOPPED"
        self.connected = False
        self.events = []
        self.start_calls = []
        self.stop_calls = 0
        self.close_calls = 0
        self.devices = []

    def snapshot(self):
        return {
            "available": True,
            "state": self.state,
            "connected": self.connected,
            "devices": list(self.devices),
            "device_status": "READY" if self.devices else "NO_INPUT",
            "device_message": "test microphone" if self.devices else "no input",
            "server_url": self.kwargs["default_url"],
            "topic": self.kwargs["topic"],
            "last_error": "",
            "recording_path": "",
            "transcript_path": "",
            "artifacts_enabled": self.kwargs["save_artifacts"],
        }

    def refresh_devices(self):
        return list(self.devices)

    def start(self, **kwargs) -> None:
        if self.state not in {"STOPPED", "ERROR"}:
            raise ValueError("ASR microphone session is already active")
        self.start_calls.append(kwargs)
        self.state = "LISTENING"

    def stop_async(self) -> None:
        self.stop_calls += 1
        self.state = "STOPPING"

    def close(self) -> bool:
        self.close_calls += 1
        self.connected = False
        self.state = "STOPPED"
        self.events.append({"type": "asr_stopped"})
        return True

    def drain_events(self):
        events = list(self.events)
        self.events.clear()
        return events


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setenv("PUZZLE_ASR_URL", "wss://asr.example.test/v1")
    monkeypatch.setenv("TASKPLANNER_ASR_CAPTURE_LOCK", "/tmp/test-asr.lock")
    monkeypatch.delenv("SENTENCE_INPUT_TOPIC", raising=False)
    rclpy.init(args=[])
    created = OperationalAsrNode(runtime_factory=FakeRuntime)
    try:
        yield created
    finally:
        created.close()
        created.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def request(operation, *, device_id=-1, server_url=""):
    value = AsrControl.Request()
    value.operation = operation
    value.device_id = device_id
    value.server_url = server_url
    return value


def invoke(node, operation, *, device_id=-1, server_url=""):
    return node._handle_control(
        request(operation, device_id=device_id, server_url=server_url),
        AsrControl.Response(),
    )


def test_fixed_contract_and_json_safe_status(node) -> None:
    assert node.get_name() == NODE_NAME == "taskplanner_asr"
    assert STATUS_TOPIC == "/input/asr/runtime_status"
    assert CONTROL_SERVICE == "/input/asr/control"
    assert SENTENCE_TOPIC == "/sensors/surgeon/sentence"
    assert node._sentence_pub is None
    assert node.count_publishers(SENTENCE_TOPIC) == 0
    assert node._status_pub.qos_profile.reliability == ReliabilityPolicy.RELIABLE
    assert (
        node._status_pub.qos_profile.durability
        == DurabilityPolicy.TRANSIENT_LOCAL
    )

    encoded = _json_dumps(node._status_envelope())
    status = json.loads(encoded)
    assert status["schema"] == STATUS_SCHEMA == "taskplanner.asr.status.v1"
    assert isinstance(status["stamp_sec"], float)
    assert status["asr"]["artifacts_enabled"] is False
    with pytest.raises(ValueError):
        _json_dumps({"invalid": math.nan})
    assert _absolute_topic_name("/sensors/surgeon/sentence") == SENTENCE_TOPIC
    with pytest.raises(ValueError, match="absolute"):
        _absolute_topic_name("sensors/surgeon/sentence")
    with pytest.raises(Exception):
        _absolute_topic_name("/sensors//sentence")


def test_start_uses_default_device_and_rejects_endpoint_override(node) -> None:
    rejected = invoke(
        node,
        "start",
        server_url="wss://unapproved.example.test/collect",
    )
    assert rejected.accepted is False
    assert "override is not allowed" in rejected.message
    assert node._runtime.start_calls == []
    assert node._sentence_pub is None

    accepted = invoke(node, "start", device_id=-1)
    assert accepted.accepted is True
    assert node._runtime.start_calls == [
        {
            "device_id": None,
            "server_url": "wss://asr.example.test/v1",
        }
    ]
    assert node._sentence_pub is None
    result = json.loads(accepted.result_json)
    assert result["schema"] == STATUS_SCHEMA
    assert result["asr"]["state"] == "LISTENING"
    assert accepted.stamp.sec >= 0

    duplicate = invoke(node, "start", device_id=-1)
    assert duplicate.accepted is False
    assert "already active" in duplicate.message


def test_sentence_publisher_tracks_connection_and_stop(node) -> None:
    assert invoke(node, "start").accepted
    assert node.count_publishers(SENTENCE_TOPIC) == 0

    node._runtime.connected = True
    node._runtime.events.append({"type": "asr_connection", "connected": True})
    node._drain_runtime_events()
    assert node._sentence_pub is not None
    assert node.count_publishers(SENTENCE_TOPIC) == 1
    assert node._sentence_pub.qos_profile.reliability == ReliabilityPolicy.RELIABLE
    assert node._sentence_pub.qos_profile.durability == DurabilityPolicy.VOLATILE

    node._runtime.events.append({"type": "asr_final", "text": "Bovie please"})
    node._drain_runtime_events()
    assert node._sentence_pub is not None

    stopped = invoke(node, "stop")
    assert stopped.accepted is True
    assert node._runtime.stop_calls == 1
    assert node._sentence_pub is None
    assert node.count_publishers(SENTENCE_TOPIC) == 0

    # A queued pre-stop connection event must not resurrect the publisher.
    node._runtime.events.append({"type": "asr_connection", "connected": True})
    node._drain_runtime_events()
    assert node._sentence_pub is None


def test_disconnect_removes_publisher_and_reconnect_restores_it(node) -> None:
    assert invoke(node, "start").accepted
    node._runtime.connected = True
    node._runtime.events.append({"type": "asr_connection", "connected": True})
    node._drain_runtime_events()
    assert node._sentence_pub is not None

    node._runtime.connected = False
    node._runtime.events.append({"type": "asr_connection", "connected": False})
    node._drain_runtime_events()
    assert node._sentence_pub is None

    node._runtime.connected = True
    node._runtime.events.append({"type": "asr_connection", "connected": True})
    node._drain_runtime_events()
    assert node._sentence_pub is not None


def test_refresh_no_input_is_successful_and_shutdown_is_idempotent(node) -> None:
    refreshed = invoke(node, "refresh_devices")
    assert refreshed.accepted is True
    result = json.loads(refreshed.result_json)
    assert result["schema"] == STATUS_SCHEMA
    assert result["asr"]["devices"] == []
    assert result["asr"]["device_status"] == "NO_INPUT"

    assert node.close() is True
    assert node.close() is True
    assert node._runtime.close_calls == 1
    assert node._sentence_pub is None


def test_close_publishes_final_status_while_context_is_valid(
    node, monkeypatch
) -> None:
    published = []
    monkeypatch.setattr(node, "_publish_status", lambda: published.append(True))

    assert node.close() is True

    assert published == [True]
    assert node._runtime.close_calls == 1


def test_close_skips_final_status_after_context_shutdown(node, monkeypatch) -> None:
    published = []
    with monkeypatch.context() as scoped:
        scoped.setattr(rclpy, "ok", lambda *, context=None: False)
        scoped.setattr(node, "_publish_status", lambda: published.append(True))

        assert node.close() is True

    assert published == []
    assert node._runtime.close_calls == 1
    assert node._runtime.events == []
    assert node._sentence_pub is None


def test_close_tolerates_only_context_invalidation_publish_race(
    node, monkeypatch
) -> None:
    context_states = iter((True, False))

    def context_ok(*, context=None) -> bool:
        assert context is node.context
        return next(context_states)

    def invalid_context_publish() -> None:
        raise RCLError("publisher context became invalid")

    with monkeypatch.context() as scoped:
        scoped.setattr(rclpy, "ok", context_ok)
        scoped.setattr(node, "_publish_status", invalid_context_publish)

        assert node.close() is True

    assert node._runtime.close_calls == 1


def test_close_preserves_publish_error_while_context_remains_valid(
    node, monkeypatch
) -> None:
    def invalid_publish() -> None:
        raise RCLError("publisher failed while context remained valid")

    with monkeypatch.context() as scoped:
        scoped.setattr(rclpy, "ok", lambda *, context=None: True)
        scoped.setattr(node, "_publish_status", invalid_publish)

        with pytest.raises(RCLError, match="context remained valid"):
            node.close()

    assert node._runtime.close_calls == 1


def test_start_and_stop_handlers_are_serialized(node) -> None:
    entered_start = threading.Event()
    release_start = threading.Event()
    original_start = node._runtime.start

    def blocked_start(**kwargs):
        entered_start.set()
        assert release_start.wait(timeout=2.0)
        original_start(**kwargs)

    node._runtime.start = blocked_start
    results = {}

    start_thread = threading.Thread(
        target=lambda: results.setdefault("start", invoke(node, "start")),
    )
    stop_thread = threading.Thread(
        target=lambda: results.setdefault("stop", invoke(node, "stop")),
    )
    start_thread.start()
    assert entered_start.wait(timeout=2.0)
    stop_thread.start()
    time.sleep(0.05)
    assert stop_thread.is_alive()

    release_start.set()
    start_thread.join(timeout=2.0)
    stop_thread.join(timeout=2.0)
    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert results["start"].accepted is True
    assert results["stop"].accepted is True
    assert node._runtime.state == "STOPPING"
    assert node._capture_requested is False
    assert node._sentence_pub is None
