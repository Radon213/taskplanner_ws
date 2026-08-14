from dataclasses import dataclass, field

import pytest

from surgical_interop_gateway.camera_alias_relay import (
    CameraAliasBinding,
    CameraAliasRelay,
    _camera_qos,
    active_camera_aliases,
    procedure_is_active,
    publish_when_requested,
)
from rclpy.qos import DurabilityPolicy, ReliabilityPolicy


def test_default_physical_sources_use_multicam_synced_topics() -> None:
    source = __import__(
        "inspect"
    ).getsource(CameraAliasRelay.__init__)
    assert "/synced/flir/color/image_raw/compressed" in source
    assert "/synced/cam_4/color/image_raw/compressed" in source


def _binding(name: str, source: str, public: str) -> CameraAliasBinding:
    return CameraAliasBinding(name=name, source_topic=source, public_topic=public)


def test_matching_source_and_public_topic_is_rejected_as_gate_bypass() -> None:
    with pytest.raises(ValueError, match="gated public topic"):
        active_camera_aliases(
            [_binding("flir", "/surgery/flir", "/surgery/flir")]
        )


def test_independent_camera_aliases_are_retained() -> None:
    requested = (
        _binding("flir", "/external/flir", "/surgery/flir"),
        _binding("cam4", "/external/cam4", "/surgery/cam4"),
    )
    assert active_camera_aliases(requested) == requested


@pytest.mark.parametrize(
    "requested",
    [
        (
            _binding("flir", "/external/flir", "/surgery/image"),
            _binding("cam4", "/external/cam4", "/surgery/image"),
        ),
        (
            _binding("flir", "/external/flir", "/surgery/flir"),
            _binding("cam4", "/surgery/flir", "/external/flir"),
        ),
    ],
)
def test_unsafe_alias_graph_is_rejected(requested) -> None:
    with pytest.raises(ValueError):
        active_camera_aliases(requested)


@dataclass
class _FakePublisher:
    subscription_count: int
    messages: list[object] = field(default_factory=list)

    def get_subscription_count(self) -> int:
        return self.subscription_count

    def publish(self, message: object) -> None:
        self.messages.append(message)


def test_frames_are_dropped_when_alias_has_no_consumer() -> None:
    publisher = _FakePublisher(subscription_count=0)
    message = object()

    assert publish_when_requested(publisher, message) is False
    assert publisher.messages == []


def test_frame_payload_is_forwarded_unchanged_on_demand() -> None:
    publisher = _FakePublisher(subscription_count=1)
    message = object()

    assert publish_when_requested(publisher, message) is True
    assert publisher.messages == [message]


def test_camera_alias_qos_matches_cv_workbook() -> None:
    qos = _camera_qos()
    assert qos.depth == 5
    assert qos.reliability == ReliabilityPolicy.BEST_EFFORT
    assert qos.durability == DurabilityPolicy.VOLATILE


def test_camera_gate_requires_fresh_matching_active_procedure() -> None:
    common = {
        "expected_procedure_id": "thyroidectomy",
        "now_monotonic_sec": 10.0,
        "stale_after_sec": 3.0,
    }

    assert procedure_is_active(
        running=True,
        procedure_id="thyroidectomy",
        received_monotonic_sec=8.0,
        **common,
    )
    assert not procedure_is_active(
        running=False,
        procedure_id="thyroidectomy",
        received_monotonic_sec=8.0,
        **common,
    )
    assert not procedure_is_active(
        running=True,
        procedure_id="nephrectomy",
        received_monotonic_sec=8.0,
        **common,
    )
    assert not procedure_is_active(
        running=True,
        procedure_id="thyroidectomy",
        received_monotonic_sec=6.0,
        **common,
    )
    assert not procedure_is_active(
        running=True,
        procedure_id="thyroidectomy",
        received_monotonic_sec=None,
        **common,
    )


def test_frame_callback_drops_late_frame_after_world_gate_expires() -> None:
    node = CameraAliasRelay.__new__(CameraAliasRelay)
    node._world_running = True
    node._world_procedure_id = "thyroidectomy"
    node._expected_procedure_id = "thyroidectomy"
    node._world_received_monotonic_sec = 1.0
    node._world_stale_after_sec = 3.0
    node._monotonic = lambda: 5.0
    publisher = _FakePublisher(subscription_count=1)

    assert node._publish_if_active(publisher, object()) is False
    assert publisher.messages == []
