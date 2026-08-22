import threading
from types import MethodType

import pytest

from integration_debug.asr_endpoints import (
    ASR_ENDPOINT_CLOUD,
    ASR_ENDPOINT_LAN,
    DEFAULT_LAN_SERVER_URL,
)
from integration_debug.node import IntegrationDebugNode


class _FakePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


class _FakeAsrRuntime:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.state = "STOPPED"
        self.connected = False
        self.events = []
        self.fail_start = fail_start
        self.stop_calls = 0
        self.start_calls = []

    def snapshot(self):
        return {"state": self.state, "connected": self.connected}

    def start(self, **kwargs) -> None:
        if self.fail_start:
            raise RuntimeError("test microphone start failure")
        self.start_calls.append(kwargs)
        self.state = "LISTENING"

    def stop_async(self) -> None:
        self.stop_calls += 1
        self.state = "STOPPING"

    def drain_events(self):
        events = list(self.events)
        self.events.clear()
        return events


class _FakeSurgeryRecordRuntime:
    @staticmethod
    def drain_events():
        return []


def _harness(*, fail_start: bool = False, network_locked: bool = False):
    class Harness:
        pass

    harness = Harness()
    harness._auxiliary_lock = threading.RLock()
    harness._asr_topic = "/sensors/surgeon/sentence"
    harness._asr_sentence_pub = None
    harness._asr_capture_requested = False
    harness._manual_sentence_pub = None
    harness._asr = _FakeAsrRuntime(fail_start=fail_start)
    harness._asr_endpoint = ASR_ENDPOINT_CLOUD
    harness._asr_server_url = "wss://arpa.worker-02.puzzle-ai.com"
    harness._asr_cloud_url = harness._asr_server_url
    harness._asr_lan_url = DEFAULT_LAN_SERVER_URL
    harness._network_locked_to_runtime = network_locked
    harness._surgery_record = _FakeSurgeryRecordRuntime()
    harness._lock = threading.RLock()
    harness._output_states = {}
    harness._output_publishers = {}
    harness.created_publishers = []
    harness.destroyed_publishers = []
    harness.recorded_events = []
    harness._manual_write_block_reason = lambda: ""
    harness._record = lambda event_type, event: harness.recorded_events.append(
        (event_type, event)
    )

    def create_publisher(_message_type, _topic, _qos):
        publisher = _FakePublisher()
        harness.created_publishers.append(publisher)
        return publisher

    def destroy_publisher(publisher):
        harness.destroyed_publishers.append(publisher)

    harness.create_publisher = create_publisher
    harness.destroy_publisher = destroy_publisher
    for method_name in (
        "_ensure_asr_publisher",
        "_destroy_asr_publisher",
        "_sync_asr_publisher",
        "_destroy_manual_sentence_publisher",
        "_release_manual_publishers",
        "_drain_auxiliary_events",
        "_asr_status_snapshot",
    ):
        setattr(
            harness,
            method_name,
            MethodType(getattr(IntegrationDebugNode, method_name), harness),
        )
    return harness


def test_debug_asr_publisher_exists_only_while_requested_and_connected() -> None:
    harness = _harness()

    accepted, _command_id, _message, _snapshot = (
        IntegrationDebugNode._handle_asr_command(harness, "asr_start", {})
    )
    assert accepted is True
    assert harness._asr_capture_requested is True
    assert harness._asr_sentence_pub is None

    harness._asr.connected = True
    harness._asr.events.append({"type": "asr_connection", "connected": True})
    harness._drain_auxiliary_events()
    first_publisher = harness._asr_sentence_pub
    assert first_publisher is not None

    harness._asr.events.append({"type": "asr_final", "text": "Bovie please"})
    harness._drain_auxiliary_events()
    assert [message.data for message in first_publisher.messages] == ["Bovie please"]

    harness._asr.connected = False
    harness._asr.events.append({"type": "asr_connection", "connected": False})
    harness._drain_auxiliary_events()
    assert harness._asr_sentence_pub is None
    assert first_publisher in harness.destroyed_publishers

    harness._asr.connected = True
    harness._asr.events.append({"type": "asr_connection", "connected": True})
    harness._drain_auxiliary_events()
    assert harness._asr_sentence_pub is not None
    assert harness._asr_sentence_pub is not first_publisher

    accepted, _command_id, _message, _snapshot = (
        IntegrationDebugNode._handle_asr_command(harness, "asr_stop", {})
    )
    assert accepted is True
    assert harness._asr_capture_requested is False
    assert harness._asr_sentence_pub is None
    assert harness._asr.stop_calls == 1

    # Events queued before stop must not resurrect readiness or publish text.
    harness._asr.connected = True
    harness._asr.events.extend(
        [
            {"type": "asr_connection", "connected": True},
            {"type": "asr_final", "text": "Kelly please"},
        ]
    )
    harness._drain_auxiliary_events()
    assert harness._asr_sentence_pub is None


def test_debug_asr_start_failure_clears_readiness_publisher() -> None:
    harness = _harness(fail_start=True)

    with pytest.raises(RuntimeError, match="test microphone start failure"):
        IntegrationDebugNode._handle_asr_command(harness, "asr_start", {})

    assert harness._asr_capture_requested is False
    assert harness._asr_sentence_pub is None
    assert harness.created_publishers == []


def test_debug_asr_selects_only_the_reviewed_lan_route_before_capture() -> None:
    harness = _harness()

    accepted, _command_id, _message, snapshot = (
        IntegrationDebugNode._handle_asr_command(
            harness, "asr_start", {"endpoint_id": ASR_ENDPOINT_LAN}
        )
    )

    assert accepted is True
    assert harness._asr_endpoint == ASR_ENDPOINT_LAN
    assert harness._asr.start_calls == [
        {"device_id": None, "server_url": DEFAULT_LAN_SERVER_URL}
    ]
    assert snapshot["endpoint_id"] == ASR_ENDPOINT_LAN


def test_debug_asr_rejects_browser_websocket_url_override() -> None:
    harness = _harness()

    accepted, _command_id, message, snapshot = (
        IntegrationDebugNode._handle_asr_command(
            harness,
            "asr_start",
            {"server_url": "wss://unapproved.example.test/collect"},
        )
    )

    assert accepted is False
    assert "override is not allowed" in message
    assert snapshot["endpoint_id"] == ASR_ENDPOINT_CLOUD
    assert harness._asr.start_calls == []
    assert harness._asr_capture_requested is False


def test_debug_asr_rejects_unreviewed_endpoint_identifier() -> None:
    harness = _harness()

    accepted, _command_id, message, snapshot = (
        IntegrationDebugNode._handle_asr_command(
            harness, "asr_start", {"endpoint_id": "third-party"}
        )
    )

    assert accepted is False
    assert "cloud" in message and "lan" in message
    assert snapshot["endpoint_id"] == ASR_ENDPOINT_CLOUD
    assert harness._asr.start_calls == []


def test_manual_authority_release_stops_hidden_microphone_stream() -> None:
    harness = _harness()
    harness._asr.state = "LISTENING"
    harness._asr.connected = True
    harness._asr_capture_requested = True
    harness._sync_asr_publisher(True)
    assert harness._asr_sentence_pub is not None

    harness._release_manual_publishers()

    assert harness._asr.stop_calls == 1
    assert harness._asr.state == "STOPPING"
    assert harness._asr_capture_requested is False
    assert harness._asr_sentence_pub is None


def test_integrated_debug_cannot_satisfy_live_preflight_with_debug_asr() -> None:
    harness = _harness(network_locked=True)

    accepted, _command_id, message, result = (
        IntegrationDebugNode._handle_asr_command(harness, "asr_start", {})
    )

    assert accepted is False
    assert "live operating-screen ASR controls" in message
    assert result == {}
    assert harness._asr.state == "STOPPED"
    assert harness._asr_capture_requested is False
    assert harness._asr_sentence_pub is None
