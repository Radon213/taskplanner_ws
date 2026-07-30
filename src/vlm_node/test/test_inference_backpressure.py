from __future__ import annotations

from collections import deque
import threading
from types import SimpleNamespace

from builtin_interfaces.msg import Time
import requests

from vlm_node.real_vlm import (
    INFERENCE_FAILURE_HISTORY_LENGTH,
    INFERENCE_TRIGGER_PERIODIC_LIVE,
    INFERENCE_TRIGGER_REPLAY_FRAME,
    INFERENCE_TRIGGER_SPEECH,
    INFERENCE_TRIGGER_SOURCE_FRAME,
    InferenceBackpressure,
    ModelImage,
    RealVLMNode,
)


class _Publisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> None:
        self.messages.append(message)


def test_in_flight_frames_coalesce_to_latest_and_run_immediately_afterward() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._inference_backpressure = InferenceBackpressure()
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[bool, str]] = []
    active_count = 0
    max_active_count = 0
    active_lock = threading.Lock()

    def tick_once(*, force: bool, inference_trigger: str) -> None:
        nonlocal active_count, max_active_count
        with active_lock:
            active_count += 1
            max_active_count = max(max_active_count, active_count)
        calls.append((force, inference_trigger))
        if inference_trigger == "frame-0":
            started.set()
            assert release.wait(timeout=2.0)
        with active_lock:
            active_count -= 1

    node._tick_once = tick_once
    worker = threading.Thread(
        target=lambda: node._tick(
            force=True,
            inference_trigger="frame-0",
        ),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=2.0)

    assert not node._tick(force=True, inference_trigger="frame-1")
    assert not node._tick(force=True, inference_trigger="frame-2")
    assert not node._tick(force=True, inference_trigger="frame-3")
    release.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert calls == [
        (True, "frame-0"),
        (True, "frame-3"),
    ]
    assert max_active_count == 1
    assert node._inference_backpressure.snapshot() == {
        "in_flight": False,
        "current_trigger": "",
        "pending_trigger": "",
        "coalesced_count": 2,
    }


def test_failed_inference_does_not_block_latest_pending_frame() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._inference_backpressure = InferenceBackpressure()
    calls: list[str] = []
    failures: list[dict] = []

    def tick_once(*, force: bool, inference_trigger: str) -> None:
        assert force
        calls.append(inference_trigger)
        if inference_trigger == "frame-failing":
            node._inference_backpressure.queue("frame-latest")
            raise RuntimeError("provider stopped responding")

    node._tick_once = tick_once
    node._record_inference_failure = lambda **kwargs: failures.append(kwargs)

    assert node._tick(
        force=True,
        inference_trigger="frame-failing",
    )

    assert calls == ["frame-failing", "frame-latest"]
    assert len(failures) == 1
    assert failures[0]["mode"] == "unhandled_exception"
    assert failures[0]["error"] == "provider stopped responding"
    assert node._inference_backpressure.snapshot()["in_flight"] is False


def test_queued_source_frames_keep_only_one_pending_slot() -> None:
    policy = InferenceBackpressure()

    assert policy.queue(INFERENCE_TRIGGER_SOURCE_FRAME).disposition == "queued"
    assert policy.queue("source-frame-2").disposition == "coalesced"
    assert policy.queue("source-frame-3").disposition == "coalesced"
    assert policy.begin() == "source-frame-3"
    assert policy.complete() is None


def test_latest_frame_worker_runs_without_periodic_timer() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._inference_backpressure = InferenceBackpressure()
    node._inference_wakeup = threading.Event()
    node._inference_shutdown = threading.Event()
    started = threading.Event()
    release = threading.Event()
    second_completed = threading.Event()
    calls: list[str] = []

    def tick_once(*, force: bool, inference_trigger: str) -> None:
        assert force
        calls.append(inference_trigger)
        if inference_trigger == "frame-1":
            started.set()
            assert release.wait(timeout=2.0)
        else:
            second_completed.set()

    node._tick_once = tick_once
    worker = threading.Thread(
        target=node._inference_worker_loop,
        daemon=True,
    )
    worker.start()

    node._queue_inference("frame-1")
    assert started.wait(timeout=2.0)
    node._queue_inference("frame-2")
    node._queue_inference("frame-3")
    release.set()
    assert second_completed.wait(timeout=2.0)

    node._inference_shutdown.set()
    node._inference_wakeup.set()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert calls == ["frame-1", "frame-3"]


def test_periodic_timer_cannot_consume_replay_frame_admission() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "replay"
    node._source_time_triggered_live = False
    node._inference_backpressure = InferenceBackpressure()
    node._inference_backpressure.queue(INFERENCE_TRIGGER_REPLAY_FRAME)

    assert not node._tick()
    assert node._inference_backpressure.begin() == (
        INFERENCE_TRIGGER_REPLAY_FRAME
    )
    assert node._inference_backpressure.complete() is None


def test_live_model_failure_never_returns_last_good_as_fresh_payload() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._response_mode = "live"
    node._retry_count = 1
    node._developer_instruction = "json only"
    node._system_prompt = "system"
    node._model_id = "test-model"
    node._temperature = 0.0
    node._top_p = 1.0
    node._generation_seed = 0
    node._max_output_tokens = 32
    node._api_mode = "openai_compat"
    node._response_format = "none"
    node._json_schema = {}
    node._reasoning_effort = ""
    node._last_good_raw = '{"v":"4","phase":[["P03",0.9]]}'
    node._last_good_payload = {
        "v": "4",
        "phase": [["P03", 0.9]],
    }
    node._client = SimpleNamespace(
        request_json=lambda **_kwargs: (_ for _ in ()).throw(
            requests.Timeout("provider timeout")
        )
    )

    raw, payload, latency, mode, retries, error = node._run_model("{}", [])

    assert raw == ""
    assert payload is None
    assert latency >= 0.0
    assert mode == "inference_transport_failed"
    assert retries == 2
    assert "provider timeout" in error


def test_failed_live_tick_records_health_but_publishes_no_stale_result() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._active = True
    node._response_mode = "live"
    node._context_mode = "actor_log"
    node._require_field_image = True
    node._current_image_input_error = ""
    node._last_periodic_live_image_stamp_sec = None
    node._visual_frame_generation = 1
    node._last_submitted_visual_generation = -1
    node._model_input_max_source_lag_sec = 0.01
    node._system_prompt = "system"
    node._developer_instruction = "developer"
    node._last_good_raw = '{"v":"4","phase":[["P03",0.9]]}'
    node._last_good_payload = {
        "v": "4",
        "phase": [["P03", 0.9]],
    }
    node._inference_failures = deque(maxlen=INFERENCE_FAILURE_HISTORY_LENGTH)
    node._inference_failure_count = 0
    node._model_id = "test-model"
    node._select_images = lambda: (
        [("flir", b"image", "image/jpeg")],
        "flir_rfdetr_segmented",
        ModelImage(
            label="FLIR",
            data=b"image",
            mime_type="image/jpeg",
            stamp_sec=71,
            stamp_nanosec=500_000_000,
            frame_id="flir",
        ),
    )
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: SimpleNamespace(to_msg=lambda: Time(sec=71))
    )
    node._causal_now_sec = lambda: 10_000.0
    node._assemble_actor_log_context_dict = lambda: {}
    node._actor_log_request_context_msg = lambda *_args: SimpleNamespace(
        stamp=Time(sec=71)
    )
    node._request_context_pub = _Publisher()
    node._publish_model_ready_image = lambda _image: None
    node._run_model = lambda *_args: (
        node._last_good_raw,
        node._last_good_payload,
        0.25,
        "last_good",
        2,
        "provider timeout",
    )
    node.get_logger = lambda: SimpleNamespace(error=lambda _message: None)
    health_calls = []
    output_calls = []
    node._publish_health = lambda **kwargs: health_calls.append(kwargs)
    node._publish_vlm_outputs = lambda *_args, **kwargs: output_calls.append(
        kwargs
    )

    node._tick_once(
        force=False,
        inference_trigger=INFERENCE_TRIGGER_PERIODIC_LIVE,
    )

    assert output_calls == []
    assert len(node._inference_failures) == 1
    failure = node._inference_failures[0]
    assert failure.trigger == INFERENCE_TRIGGER_PERIODIC_LIVE
    assert failure.mode == "last_good"
    assert health_calls == [
        {
            "image_source": "flir_rfdetr_segmented",
            "latency_sec": 0.25,
            "prompt_chars": len("system") + len("developer") + len("{}"),
            "output_chars": 0,
            "parse_retry_count": 2,
            "last_error": "provider timeout",
            "mode": (
                "inference_failed:periodic_live:last_good"
            ),
            "healthy": False,
            "connected": True,
        }
    ]
    assert node._last_periodic_live_image_stamp_sec is None


def test_failure_history_is_bounded_and_keeps_latest_sequence() -> None:
    node = RealVLMNode.__new__(RealVLMNode)
    node._inference_failures = deque(maxlen=INFERENCE_FAILURE_HISTORY_LENGTH)
    node._inference_failure_count = 0
    node._model_id = "test-model"
    node.get_logger = lambda: SimpleNamespace(error=lambda _message: None)
    node._publish_health = lambda **_kwargs: None

    for index in range(INFERENCE_FAILURE_HISTORY_LENGTH + 5):
        node._record_inference_failure(
            trigger=INFERENCE_TRIGGER_SPEECH,
            mode="inference_response_failed",
            error=f"failure-{index}",
            image_source="flir_rfdetr_segmented",
            latency_sec=0.1,
            prompt_chars=10,
            retry_count=1,
            connected=True,
        )

    assert len(node._inference_failures) == INFERENCE_FAILURE_HISTORY_LENGTH
    assert node._inference_failures[0].sequence == 6
    assert node._inference_failures[-1].sequence == (
        INFERENCE_FAILURE_HISTORY_LENGTH + 5
    )
