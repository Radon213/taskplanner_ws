from __future__ import annotations

from types import SimpleNamespace

from vlm_node.cam4_typed_mayo_adapter import (
    CAM4_TYPED_MAYO_CORRELATION_PREFIX,
    Cam4TypedMayoAdapter,
    RunStartVisibilityFence,
    TemporalTypedMayoFilter,
    summarize_typed_mayo_detections,
    typed_mayo_correlation_id,
)


def _frame(
    *instances,
    view: str = "cam_4",
    sequence: int = 7,
    sec: int = 44,
    nanosec: int = 80_000_000,
):
    return SimpleNamespace(
        view=view,
        sequence=sequence,
        header=SimpleNamespace(
            stamp=SimpleNamespace(sec=sec, nanosec=nanosec)
        ),
        stamp=SimpleNamespace(sec=0, nanosec=0),
        instances=list(instances),
    )


def _instance(
    name: str,
    confidence: float,
    local_id: int,
    bbox=(10.0, 10.0, 50.0, 50.0),
):
    return SimpleNamespace(
        class_name=name,
        class_confidence=confidence,
        frame_local_instance_id=local_id,
        bbox_xyxy_px=bbox,
    )


def _aliases(label: str) -> str | None:
    return {
        "Bovie": "T04",
        "Bovie surgical cautery": "T04",
        "애드손": "T02",
    }.get(label)


def test_typed_cam4_mayo_adapter_uses_scenario_aliases_and_preserves_count():
    detections = summarize_typed_mayo_detections(
        _frame(
            _instance("Bovie", 0.91, 4),
            _instance("Bovie surgical cautery", 0.82, 2),
            _instance("애드손", 0.88, 1),
        ),
        resolve_instrument_alias=_aliases,
    )

    assert [item.instrument_id for item in detections] == ["T02", "T04", "T04"]
    assert [item.observed_count for item in detections] == [1, 2, 2]
    assert [item.frame_local_instance_id for item in detections] == [1, 2, 4]
    assert all(item.source_stamp_sec == 44 for item in detections)
    assert all(item.source_stamp_nanosec == 80_000_000 for item in detections)


def test_typed_cam4_mayo_adapter_ignores_wrong_view_unknown_and_low_confidence():
    assert not summarize_typed_mayo_detections(
        _frame(_instance("Bovie", 0.91, 1), view="cam_3"),
        resolve_instrument_alias=_aliases,
    )
    assert not summarize_typed_mayo_detections(
        _frame(
            _instance("unknown", 0.99, 1),
            _instance("Bovie", 0.59, 2),
        ),
        resolve_instrument_alias=_aliases,
    )


def test_typed_cam4_mayo_correlation_carries_only_counting_metadata():
    correlation_id = typed_mayo_correlation_id(
        source_epoch=9,
        input_sequence=17,
        ordinal=2,
        observed_count=3,
    )

    assert correlation_id == f"{CAM4_TYPED_MAYO_CORRELATION_PREFIX}:9:17:2:3"


def test_temporal_filter_suppresses_momentary_overlapping_class_swap():
    temporal_filter = TemporalTypedMayoFilter()

    bovie = summarize_typed_mayo_detections(
        _frame(_instance("Bovie", 0.91, 1), sequence=1),
        resolve_instrument_alias=_aliases,
    )
    assert [item.instrument_id for item in temporal_filter.update(bovie)] == ["T04"]

    for index, nanosec in enumerate(
        (280_000_000, 480_000_000, 680_000_000),
        start=2,
    ):
        swapped = summarize_typed_mayo_detections(
            _frame(
                _instance("애드손", 0.93, 1),
                sequence=index,
                nanosec=nanosec,
            ),
            resolve_instrument_alias=_aliases,
        )
        assert temporal_filter.update(swapped) == ()

    restored = summarize_typed_mayo_detections(
        _frame(
            _instance("Bovie", 0.89, 1),
            sequence=5,
            nanosec=880_000_000,
        ),
        resolve_instrument_alias=_aliases,
    )
    assert [item.instrument_id for item in temporal_filter.update(restored)] == ["T04"]


def test_temporal_filter_accepts_only_sustained_overlapping_class_change():
    temporal_filter = TemporalTypedMayoFilter()
    initial = summarize_typed_mayo_detections(
        _frame(_instance("Bovie", 0.91, 1), sequence=1),
        resolve_instrument_alias=_aliases,
    )
    temporal_filter.update(initial)

    accepted = ()
    for index, (sec, nanosec) in enumerate(
        (
            (44, 280_000_000),
            (44, 480_000_000),
            (44, 680_000_000),
            (45, 80_000_000),
        ),
        start=2,
    ):
        changed = summarize_typed_mayo_detections(
            _frame(
                _instance("애드손", 0.93, 1),
                sequence=index,
                sec=sec,
                nanosec=nanosec,
            ),
            resolve_instrument_alias=_aliases,
        )
        accepted = temporal_filter.update(changed)

    assert [item.instrument_id for item in accepted] == ["T02"]
    assert accepted[0].observed_count == 1


def test_run_start_fence_suppresses_existing_bbox_until_real_disappearance():
    fence = RunStartVisibilityFence(
        capture_duration_sec=1.0,
        clear_duration_sec=0.75,
    )
    existing = summarize_typed_mayo_detections(
        _frame(_instance("Bovie", 0.91, 1), sequence=1),
        resolve_instrument_alias=_aliases,
    )
    fence.start(10.0)
    assert fence.update(existing, now_sec=10.1) == ()
    assert fence.update(existing, now_sec=11.1) == ()
    assert fence.update((), now_sec=11.9) == ()

    returned = summarize_typed_mayo_detections(
        _frame(_instance("Bovie", 0.93, 2), sequence=2),
        resolve_instrument_alias=_aliases,
    )
    assert [item.instrument_id for item in fence.update(returned, now_sec=12.0)] == [
        "T04"
    ]


def test_run_start_fence_allows_new_non_overlapping_tool_during_run():
    fence = RunStartVisibilityFence(capture_duration_sec=1.0)
    existing = summarize_typed_mayo_detections(
        _frame(_instance("Bovie", 0.91, 1), sequence=1),
        resolve_instrument_alias=_aliases,
    )
    fence.start(20.0)
    assert fence.update(existing, now_sec=20.1) == ()
    added = summarize_typed_mayo_detections(
        _frame(
            _instance("Bovie", 0.90, 1),
            _instance("애드손", 0.92, 2, bbox=(100.0, 10.0, 140.0, 50.0)),
            sequence=2,
        ),
        resolve_instrument_alias=_aliases,
    )
    emitted = fence.update(added, now_sec=21.1)
    assert [item.instrument_id for item in emitted] == ["T02"]
    assert emitted[0].observed_count == 1


def test_runtime_state_edges_reset_source_epoch_and_start_visibility_fence():
    calls: list[str] = []
    fake = SimpleNamespace(
        _active_run_id="",
        _runtime_active=False,
        _advance_epoch=lambda: calls.append("epoch"),
        _run_start_fence=SimpleNamespace(
            start=lambda _now: calls.append("start"),
            reset=lambda: calls.append("reset"),
        ),
    )

    active = SimpleNamespace(
        procedure_run_id="run-1",
        execution_state="running",
        running=True,
    )
    Cam4TypedMayoAdapter._on_simulation_state(fake, active)
    Cam4TypedMayoAdapter._on_simulation_state(fake, active)
    assert calls == ["epoch", "start"]
    assert fake._runtime_active is True

    stopped = SimpleNamespace(
        procedure_run_id="",
        execution_state="idle",
        running=False,
    )
    Cam4TypedMayoAdapter._on_simulation_state(fake, stopped)
    assert calls == ["epoch", "start", "epoch", "reset"]
    assert fake._runtime_active is False
