"""Regression tests for the local RGB-only RF-DETR bridge contract."""

from __future__ import annotations

import base64
import json
import threading
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace

from sensor_msgs.msg import CompressedImage

from vlm_node.rfdetr_bridge import BufferedFrame, RFDETRBridgeNode


class _Publisher:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def publish(self, message: object) -> None:
        self.messages.append(message)


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        raise AssertionError("the RGB-only RF-DETR request must not fail")


@dataclass
class _Response:
    payload: dict

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self, response_payload: dict) -> None:
        self.response_payload = response_payload
        self.requests: list[dict] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _Response:
        self.requests.append(
            {
                "url": url,
                "json": json,
                "timeout": timeout,
            }
        )
        return _Response(self.response_payload)


def _frame(*, sec: int, nanosec: int, frame_id: str) -> BufferedFrame:
    return BufferedFrame(
        received_monotonic=1.0,
        stamp_sec=sec,
        stamp_nanosec=nanosec,
        frame_id=frame_id,
        format="jpeg",
        data=f"{frame_id}-rgb".encode(),
    )


def _diagnostics(*, view: str) -> dict:
    training_run = "mayo_bbox_3class"
    return {
        "model_version": f"rfdetr-{view}-bbox-v1",
        "ontology_version": "mayo-bbox-3class-v1",
        "model_provenance": {
            "checkpoint_id": f"{training_run}/checkpoint_best_regular.pth",
            "sha256": "a" * 64,
            "training_run": training_run,
        },
        "instances": [],
    }


def _overlay_payload(marker: bytes) -> dict:
    return {
        "width": 1280,
        "height": 720,
        "mime_type": "image/webp",
        "data_base64": base64.b64encode(marker).decode("ascii"),
    }


def _bridge_harness() -> tuple[
    RFDETRBridgeNode,
    _Session,
    dict[str, _Publisher],
]:
    response_payload = {
        "schema": "taskplanner.rfdetr_perception.v1",
        "cam3_overlay_image": _overlay_payload(b"cam3-overlay"),
        "cam4_overlay_image": _overlay_payload(b"cam4-overlay"),
        "diagnostics": {
            "cam3": _diagnostics(view="cam3"),
            "cam4": _diagnostics(view="cam4"),
        },
    }
    session = _Session(response_payload)
    publishers = {
        name: _Publisher()
        for name in (
            "segmented_flir",
            "flir_overlay",
            "detected_cam4",
            "cam3_overlay",
            "cam4_overlay",
            "cam4_semantics",
            "cam3_observations",
            "cam4_observations",
            "diagnostics",
        )
    }

    node = RFDETRBridgeNode.__new__(RFDETRBridgeNode)
    node._segmented_output_rate_hz = 2.0
    node._last_segmented_requested_monotonic = 0.0
    node._session = session
    node._service_url = "http://127.0.0.1:8010"
    node._request_timeout_sec = 1.0
    node._generation_is_active = lambda generation: generation == 7
    node._segmented_flir_pub = publishers["segmented_flir"]
    node._flir_overlay_pub = publishers["flir_overlay"]
    node._detected_cam4_pub = publishers["detected_cam4"]
    node._cam3_overlay_pub = publishers["cam3_overlay"]
    node._cam4_overlay_pub = publishers["cam4_overlay"]
    node._cam4_semantics_pub = publishers["cam4_semantics"]
    node._cam3_tool_observations_pub = publishers["cam3_observations"]
    node._cam4_tool_observations_pub = publishers["cam4_observations"]
    node._tool_observation_sequences = {"cam_3": 0, "cam_4": 0}
    node._last_tool_observation_source_stamps = {
        "cam_3": None,
        "cam_4": None,
    }
    node._diagnostics_sequence = 0
    node._diagnostics_pub = publishers["diagnostics"]
    node._last_success_monotonic = 0.0
    node._publish_health = lambda **_kwargs: None
    node.get_logger = lambda: _Logger()
    return node, session, publishers


def _stamp_tuple(message: object) -> tuple[int, int]:
    return (
        int(message.header.stamp.sec),
        int(message.header.stamp.nanosec),
    )


def _compressed_frame(
    *,
    sec: int,
    nanosec: int,
    frame_id: str,
) -> CompressedImage:
    message = CompressedImage()
    message.header.stamp.sec = sec
    message.header.stamp.nanosec = nanosec
    message.header.frame_id = frame_id
    message.format = "jpeg"
    message.data = f"{frame_id}:{sec}:{nanosec}".encode()
    return message


def test_bbox_request_is_rgb_only_and_preserves_delayed_camera_stamps() -> None:
    """CAM3/CAM4 bbox inference must not depend on an RGB-depth exact gate.

    The two RGB sources may arrive with a small source-time difference.  The
    bridge sends both RGB frames without fabricating a common timestamp, and
    all structured/debug outputs retain the exact header stamp of their own
    source frame.
    """
    node, session, publishers = _bridge_harness()
    cam4 = _frame(sec=1_787_000_000, nanosec=125_000_000, frame_id="cam4_rgb")
    # Deliberately 23 ms behind CAM4: approximate pairing is acceptable, but
    # the original CAM3 timestamp must not be overwritten with CAM4's stamp.
    cam3 = _frame(sec=1_787_000_000, nanosec=102_000_000, frame_id="cam3_rgb")

    node._process_pair(None, cam4, cam3, generation=7)

    assert len(session.requests) == 1
    worker_payload = session.requests[0]["json"]
    assert worker_payload["cam3_image_base64"]
    assert worker_payload["cam4_image_base64"]
    assert worker_payload["cam3_stamp_sec"] == cam3.source_stamp_sec
    assert worker_payload["cam4_stamp_sec"] == cam4.source_stamp_sec
    assert not any("depth" in key.casefold() for key in worker_payload)
    assert not any("camera_info" in key.casefold() for key in worker_payload)

    cam3_observation = publishers["cam3_observations"].messages[0]
    cam4_observation = publishers["cam4_observations"].messages[0]
    cam3_overlay = publishers["cam3_overlay"].messages[0]
    cam4_overlay = publishers["cam4_overlay"].messages[0]

    assert _stamp_tuple(cam3_observation) == (
        cam3.stamp_sec,
        cam3.stamp_nanosec,
    )
    assert _stamp_tuple(cam4_observation) == (
        cam4.stamp_sec,
        cam4.stamp_nanosec,
    )
    assert _stamp_tuple(cam3_overlay) == (cam3.stamp_sec, cam3.stamp_nanosec)
    assert _stamp_tuple(cam4_overlay) == (cam4.stamp_sec, cam4.stamp_nanosec)
    assert cam3_observation.observation_id == (
        f"cam_3:{cam3.stamp_sec}:{cam3.stamp_nanosec}"
    )
    assert cam4_observation.observation_id == (
        f"cam_4:{cam4.stamp_sec}:{cam4.stamp_nanosec}"
    )


def test_delayed_cam3_runs_alone_without_false_cam4_alignment_error() -> None:
    """A delayed CAM3-only request is valid and not a CAM4 alignment fault."""

    node, session, publishers = _bridge_harness()
    cam3 = _frame(
        sec=1_787_000_002,
        nanosec=400_000_000,
        frame_id="cam3_synced_rgb",
    )

    node._process_pair(None, None, cam3, generation=7)

    request = session.requests[0]["json"]
    assert request["cam3_stamp_sec"] == cam3.source_stamp_sec
    assert "cam4_stamp_sec" not in request
    assert "cam4_image_base64" not in request
    assert publishers["diagnostics"].messages == []
    observation = publishers["cam3_observations"].messages[0]
    overlay = publishers["cam3_overlay"].messages[0]
    assert _stamp_tuple(observation) == (cam3.stamp_sec, cam3.stamp_nanosec)
    assert observation.header.frame_id == cam3.frame_id
    assert _stamp_tuple(overlay) == (cam3.stamp_sec, cam3.stamp_nanosec)
    assert overlay.header.frame_id.startswith(f"{cam3.frame_id}|")


def test_worker_processes_late_cam3_without_waiting_for_another_cam4() -> None:
    """A far-skew CAM3 frame independently wakes the bounded worker."""

    node = RFDETRBridgeNode.__new__(RFDETRBridgeNode)
    node._condition = threading.Condition()
    node._pending_flir = None
    node._pending_cam3 = None
    node._pending_cam4 = None
    node._cam3_frames = deque(maxlen=4)
    node._cam4_frames = deque(maxlen=4)
    node._cam3_input_topic = (
        "/synced/cam_3/color/image_raw/compressed"
    )
    node._max_source_skew_sec = 0.001
    node._max_rate_hz = 10_000.0
    node._last_request_started = 0.0
    node._enabled = True
    node._running = True
    node._generation = 9
    captured: list[
        tuple[
            BufferedFrame | None,
            BufferedFrame | None,
            BufferedFrame | None,
            int,
        ]
    ] = []
    first_processed = threading.Event()
    second_processed = threading.Event()

    def _capture(
        flir: BufferedFrame | None,
        cam4: BufferedFrame | None,
        cam3: BufferedFrame | None,
        generation: int,
    ) -> None:
        captured.append((flir, cam4, cam3, generation))
        if len(captured) == 1:
            first_processed.set()
        if len(captured) == 2:
            with node._condition:
                node._running = False
                node._condition.notify_all()
            second_processed.set()

    node._process_pair = _capture
    worker = threading.Thread(target=node._worker_loop, daemon=True)
    worker.start()

    cam4 = _frame(sec=100, nanosec=0, frame_id="cam4_synced_rgb")
    with node._condition:
        node._pending_cam4 = cam4
        node._cam4_frames.append(cam4)
        node._condition.notify_all()
    assert first_processed.wait(timeout=1.0)

    # This CAM3 source stamp is far outside the old 100 ms nearest-frame
    # gate.  Its later delivery must wake the worker and run independently.
    cam3 = _frame(sec=95, nanosec=0, frame_id="cam3_synced_rgb")
    with node._condition:
        node._pending_cam3 = cam3
        node._cam3_frames.append(cam3)
        node._condition.notify_all()
    assert second_processed.wait(timeout=1.0)
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert captured == [
        (None, cam4, None, 9),
        (None, None, cam3, 9),
    ]


def test_camera_latest_only_replacements_are_visible_in_health() -> None:
    """Latest-only replacements remain bounded and observable."""

    node = RFDETRBridgeNode.__new__(RFDETRBridgeNode)
    node._condition = threading.Condition()
    node._enabled = True
    node._cam3_input_topic = (
        "/synced/cam_3/color/image_raw/compressed"
    )
    node._pending_cam3 = None
    node._pending_cam4 = None
    node._cam3_frames = deque(maxlen=4)
    node._cam4_frames = deque(maxlen=4)
    node._camera_coalesced_frames = {"cam_3": 0, "cam_4": 0}

    node._on_cam3(
        _compressed_frame(sec=10, nanosec=1, frame_id="cam3_first")
    )
    node._on_cam3(
        _compressed_frame(sec=10, nanosec=2, frame_id="cam3_latest")
    )
    node._on_cam4(
        _compressed_frame(sec=20, nanosec=1, frame_id="cam4_first")
    )
    node._on_cam4(
        _compressed_frame(sec=20, nanosec=2, frame_id="cam4_middle")
    )
    node._on_cam4(
        _compressed_frame(sec=20, nanosec=3, frame_id="cam4_latest")
    )

    assert node._pending_cam3.frame_id == "cam3_latest"
    assert node._pending_cam4.frame_id == "cam4_latest"
    assert node._camera_coalesced_frames == {"cam_3": 1, "cam_4": 2}

    node._health_pub = _Publisher()
    node.get_name = lambda: "rfdetr_perception_bridge"
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(
            to_msg=lambda: SimpleNamespace(sec=30, nanosec=40)
        )
    )
    node._publish_health(
        connected=True,
        status="ready",
        latency_ms=12.0,
        pair_skew_sec=0.0,
        cam3_pair_skew_sec=0.0,
        error="",
    )
    health = json.loads(node._health_pub.messages[0].data)
    assert health["camera_queue_policy"] == "latest_only"
    assert health["cam3_coalesced_frames"] == 1
    assert health["cam4_coalesced_frames"] == 2
    assert health["last_error_code"] == ""
