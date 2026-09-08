from __future__ import annotations

from types import SimpleNamespace

import pytest

from or_digital_twin.hand_handover_gate import (
    ContinuousHandHandoverGate,
    ExactStampHandJoiner,
    ExactStampHandTripletJoiner,
    FrameDisposition,
    HandFrameEvidence,
    HandPerceptionPins,
    classify_hand_frame,
    validate_hand_health,
)


PINS = HandPerceptionPins(
    source_frame_id="cam_4_color_optical_frame",
    gesture_model_name="VIPLab Top-View Landmark Gesture Classifier",
    gesture_model_version="landmark-geometry-v2-world-closed",
    gesture_model_sha256=(
        "258ed1676863df9317fb084a446a2ae9645c1f4acc468a4c4ad92979cf0ef821"
    ),
    facing_estimator_name="VIPLab CAM4 Depth Palm-Facing Estimator",
    facing_estimator_version="depth-palm-normal-v1",
    facing_spec_sha256=(
        "45c61773790b8510f373bd9f0940ce1189567e7f4e74ff3c0d6c5b5ee3785b37"
    ),
    calibration_version="cam4_live_aprilgrid_depth_workplane_20260822",
    handedness_mapping_version=(
        "cam4_forced_right_camera_constraint_v1_pending_live_check"
    ),
)


def _header(stamp: float, frame_id: str = "cam_4_color_optical_frame") -> SimpleNamespace:
    sec = int(stamp)
    return SimpleNamespace(
        stamp=SimpleNamespace(
            sec=sec,
            nanosec=int(round((stamp - sec) * 1_000_000_000)),
        ),
        frame_id=frame_id,
    )


def _hand(
    *,
    index: int = 0,
    gesture: str = "Open_Palm",
    facing: str = "PALM_UP",
    handedness: str = "Right",
) -> tuple[SimpleNamespace, SimpleNamespace]:
    common = {
        "hand_index": index,
        "has_handedness": True,
        "handedness_label": handedness,
        "handedness_score": 0.99,
    }
    return (
        SimpleNamespace(
            **common,
            has_classification=True,
            category_name=gesture,
            score=0.95,
        ),
        SimpleNamespace(
            **common,
            has_facing=True,
            facing_label=facing,
            palm_up_score=0.82,
        ),
    )


def _messages(
    stamp: float,
    *,
    gesture: str = "Open_Palm",
    facing: str = "PALM_UP",
    handedness: str = "Right",
    include_hand: bool = True,
) -> tuple[SimpleNamespace, SimpleNamespace]:
    gesture_hand, facing_hand = _hand(
        gesture=gesture,
        facing=facing,
        handedness=handedness,
    )
    return (
        SimpleNamespace(
            header=_header(stamp),
            model_name=PINS.gesture_model_name,
            model_version=PINS.gesture_model_version,
            model_asset_sha256=PINS.gesture_model_sha256,
            supported_gestures=["Closed_Fist", "Open_Palm"],
            hands=[gesture_hand] if include_hand else [],
        ),
        SimpleNamespace(
            header=_header(stamp),
            estimator_name=PINS.facing_estimator_name,
            estimator_version=PINS.facing_estimator_version,
            estimator_spec_sha256=PINS.facing_spec_sha256,
            calibration_version=PINS.calibration_version,
            handedness_mapping_version=PINS.handedness_mapping_version,
            supported_facings=["PALM_UP", "PALM_DOWN", "EDGE"],
            hands=[facing_hand] if include_hand else [],
        ),
    )


def _keypoints(stamp: float, positions: dict[int, float]) -> SimpleNamespace:
    return SimpleNamespace(
        header=_header(stamp),
        hands=[
            SimpleNamespace(
                hand_index=index,
                joints_2d=[SimpleNamespace(u=position) for _ in range(21)],
            )
            for index, position in positions.items()
        ],
    )


def _positive(stamp: float, confidence: float = 0.91) -> HandFrameEvidence:
    return HandFrameEvidence(
        stamp,
        FrameDisposition.POSITIVE,
        "right_open_palm_palm_up",
        confidence=confidence,
    )


def test_exact_join_is_order_independent_and_header_strict() -> None:
    gesture, facing = _messages(10.0)
    joiner = ExactStampHandJoiner()
    assert joiner.add_gesture(gesture) is None
    assert joiner.add_facing(facing) == (gesture, facing)

    wrong_gesture, wrong_facing = _messages(11.0)
    wrong_facing.header.frame_id = "different_frame"
    assert joiner.add_facing(wrong_facing) is None
    assert joiner.add_gesture(wrong_gesture) is None


def test_exact_triplet_join_waits_for_keypoints_in_any_arrival_order() -> None:
    gesture, facing = _messages(12.0)
    keypoints = _keypoints(12.0, {0: 300.0})
    joiner = ExactStampHandTripletJoiner()

    assert joiner.add_facing(facing) is None
    assert joiner.add_gesture(gesture) is None
    assert joiner.add_keypoints(keypoints) == (gesture, facing, keypoints)


def test_classifier_requires_exact_right_open_palm_palm_up_tuple() -> None:
    gesture, facing = _messages(20.0)
    positive = classify_hand_frame(gesture, facing, pins=PINS)
    assert positive.disposition is FrameDisposition.POSITIVE
    assert positive.reason == "right_open_palm_palm_up"

    gesture, facing = _messages(20.1, facing="PALM_DOWN")
    negative = classify_hand_frame(gesture, facing, pins=PINS)
    assert negative.disposition is FrameDisposition.RELEASE

    gesture, facing = _messages(20.2, handedness="Left")
    unknown = classify_hand_frame(gesture, facing, pins=PINS)
    assert unknown.disposition is FrameDisposition.UNKNOWN
    assert unknown.reason == "right_hand_unavailable"


def test_classifier_rejects_unpinned_source_frame() -> None:
    gesture, facing = _messages(20.3)
    gesture.header.frame_id = "other_camera_color_optical_frame"
    facing.header.frame_id = "other_camera_color_optical_frame"

    evidence = classify_hand_frame(gesture, facing, pins=PINS)

    assert evidence.disposition is FrameDisposition.UNKNOWN
    assert evidence.reason == "source_frame_mismatch"


def test_palm_up_label_with_negative_normal_score_is_not_positive() -> None:
    gesture, facing = _messages(20.4)
    facing.hands[0].palm_up_score = -0.01

    evidence = classify_hand_frame(gesture, facing, pins=PINS)

    assert evidence.disposition is FrameDisposition.UNKNOWN
    assert evidence.reason == "hand_classification_unknown"


def test_classifier_fails_closed_for_ambiguous_multiple_right_hands() -> None:
    gesture, facing = _messages(30.0)
    second_gesture, second_facing = _hand(index=1)
    gesture.hands.append(second_gesture)
    facing.hands.append(second_facing)
    evidence = classify_hand_frame(gesture, facing, pins=PINS)
    assert evidence.disposition is FrameDisposition.UNKNOWN
    assert evidence.reason == "multiple_hands_in_mayo_frame"


def test_classifier_ignores_multi_hand_frame_even_with_one_eligible_requester() -> None:
    gesture, facing = _messages(30.05)
    second_gesture, second_facing = _hand(
        index=1,
        gesture="Closed_Fist",
        facing="PALM_DOWN",
        handedness="Left",
    )
    gesture.hands.append(second_gesture)
    facing.hands.append(second_facing)

    evidence = classify_hand_frame(gesture, facing, pins=PINS)

    assert evidence.disposition is FrameDisposition.UNKNOWN
    assert evidence.reason == "multiple_hands_in_mayo_frame"


def test_classifier_uses_only_leftmost_hand_when_keypoints_are_joined() -> None:
    gesture, facing = _messages(30.07)
    left_gesture, left_facing = _hand(index=4)
    right_gesture, right_facing = _hand(
        index=9,
        gesture="Closed_Fist",
        facing="PALM_DOWN",
    )
    gesture.hands = [right_gesture, left_gesture]
    facing.hands = [right_facing, left_facing]

    evidence = classify_hand_frame(
        gesture,
        facing,
        keypoints_message=_keypoints(30.07, {4: 120.0, 9: 520.0}),
        pins=PINS,
    )

    assert evidence.disposition is FrameDisposition.POSITIVE
    assert evidence.hand_index == 4


def test_classifier_releases_when_leftmost_hand_is_not_the_request_pose() -> None:
    gesture, facing = _messages(30.08)
    left_gesture, left_facing = _hand(
        index=4,
        gesture="Closed_Fist",
        facing="PALM_DOWN",
    )
    right_gesture, right_facing = _hand(index=9)
    gesture.hands = [left_gesture, right_gesture]
    facing.hands = [left_facing, right_facing]

    evidence = classify_hand_frame(
        gesture,
        facing,
        keypoints_message=_keypoints(30.08, {4: 120.0, 9: 520.0}),
        pins=PINS,
    )

    assert evidence.disposition is FrameDisposition.RELEASE
    assert evidence.hand_index == 4


def test_classifier_fails_closed_when_multi_hand_keypoints_are_incomplete() -> None:
    gesture, facing = _messages(30.09)
    second_gesture, second_facing = _hand(index=1)
    gesture.hands.append(second_gesture)
    facing.hands.append(second_facing)

    evidence = classify_hand_frame(
        gesture,
        facing,
        keypoints_message=_keypoints(30.09, {0: 120.0}),
        pins=PINS,
    )

    assert evidence.disposition is FrameDisposition.UNKNOWN
    assert evidence.reason == "hand_keypoint_index_set_mismatch"


def test_classifier_fails_closed_for_asymmetric_hand_index_sets() -> None:
    gesture, facing = _messages(30.1)
    second_gesture, _second_facing = _hand(index=1)
    gesture.hands.append(second_gesture)

    evidence = classify_hand_frame(gesture, facing, pins=PINS)

    assert evidence.disposition is FrameDisposition.UNKNOWN
    assert evidence.reason == "hand_index_set_mismatch"


def test_gate_requires_source_time_300ms_and_four_samples() -> None:
    gate = ContinuousHandHandoverGate(dwell_sec=0.300, minimum_positive_samples=4)
    for stamp in (100.0, 100.1, 100.2):
        update = gate.observe(
            _positive(stamp),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )
        assert update.active is False
    update = gate.observe(
        _positive(100.299),
        source_now_sec=100.299,
        receipt_monotonic=100.299,
    )
    assert update.active is False
    update = gate.observe(
        _positive(100.3),
        source_now_sec=100.3,
        receipt_monotonic=100.3,
    )
    assert update.active is True
    assert update.rising_edge is True
    assert update.generation == 1
    assert update.stability_sec == pytest.approx(0.3)


def test_gate_requires_300ms_of_receipt_time_not_a_buffered_source_burst() -> None:
    gate = ContinuousHandHandoverGate(dwell_sec=0.300, minimum_positive_samples=4)
    for index, stamp in enumerate((100.0, 100.1, 100.2, 100.3)):
        update = gate.observe(
            _positive(stamp),
            source_now_sec=stamp,
            receipt_monotonic=10.0 + index * 0.01,
        )

    assert update.active is False
    assert update.stability_sec == pytest.approx(0.03)


def test_short_observed_no_hand_gap_preserves_candidate_dwell() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.300,
        release_confirm_sec=0.180,
        minimum_positive_samples=4,
    )
    for stamp in (10.0, 10.1, 10.2):
        update = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )
    assert update.active is False

    dropout = gate.observe(
        HandFrameEvidence(10.25, FrameDisposition.RELEASE, "observed_no_hand"),
        source_now_sec=10.25,
        receipt_monotonic=10.25,
    )
    assert dropout.active is False
    assert dropout.reason == "transient_input_gap:observed_no_hand"

    recovered = gate.observe(
        _positive(10.30), source_now_sec=10.30, receipt_monotonic=10.30
    )
    assert recovered.active is True
    assert recovered.rising_edge is True
    assert recovered.generation == 1


def test_short_cam4_dropout_holds_an_active_hand_signal() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.300,
        release_confirm_sec=0.180,
        minimum_positive_samples=4,
    )
    for stamp in (20.0, 20.1, 20.2, 20.3):
        active = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )
    assert active.active is True
    assert active.rising_edge is True

    dropped = gate.observe(
        HandFrameEvidence(20.36, FrameDisposition.RELEASE, "observed_no_hand"),
        source_now_sec=20.36,
        receipt_monotonic=20.36,
    )
    assert dropped.active is True
    assert dropped.rising_edge is False

    resumed = gate.observe(
        _positive(20.42), source_now_sec=20.42, receipt_monotonic=20.42
    )
    assert resumed.active is True
    assert resumed.rising_edge is False
    assert resumed.generation == 1


def test_sustained_no_hand_confirms_release_after_bounded_grace() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.300,
        release_confirm_sec=0.180,
        minimum_positive_samples=4,
    )
    for stamp in (30.0, 30.1, 30.2, 30.3):
        gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )

    for stamp in (30.36, 30.45):
        held = gate.observe(
            HandFrameEvidence(stamp, FrameDisposition.RELEASE, "observed_no_hand"),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )
        assert held.active is True

    released = gate.observe(
        HandFrameEvidence(30.54, FrameDisposition.RELEASE, "observed_no_hand"),
        source_now_sec=30.54,
        receipt_monotonic=30.54,
    )
    assert released.active is False
    assert released.reason == "observed_no_hand"


def test_soft_classifier_dropout_is_bridged_but_multi_hand_is_not() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.300,
        soft_unknown_grace_sec=0.180,
        minimum_positive_samples=4,
    )
    for stamp in (40.0, 40.1, 40.2, 40.3):
        gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )

    classifier_gap = gate.observe(
        HandFrameEvidence(
            40.36, FrameDisposition.UNKNOWN, "hand_classification_unknown"
        ),
        source_now_sec=40.36,
        receipt_monotonic=40.36,
    )
    assert classifier_gap.active is True

    hard_unknown = gate.observe(
        HandFrameEvidence(
            40.42, FrameDisposition.UNKNOWN, "multiple_hands_in_mayo_frame"
        ),
        source_now_sec=40.42,
        receipt_monotonic=40.42,
    )
    assert hard_unknown.active is False
    assert hard_unknown.reason == "multiple_hands_in_mayo_frame"


def test_active_signal_expires_after_the_400ms_observation_silence_lease() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.300,
        max_receipt_silence_sec=0.400,
        minimum_positive_samples=4,
    )
    for stamp in (10.0, 10.1, 10.2, 10.3):
        active = gate.observe(
            _positive(stamp),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )

    assert active.active is True
    assert gate.expire(receipt_monotonic=10.699) is None

    expired = gate.expire(receipt_monotonic=10.701)
    assert expired is not None
    assert expired.active is False
    assert expired.accepted_sample is False
    assert expired.disposition is FrameDisposition.UNKNOWN
    assert expired.reason == "hand_stream_silent"


@pytest.mark.parametrize(
    ("gesture", "facing", "expected"),
    [
        ("Closed_Fist", "PALM_UP", FrameDisposition.RELEASE),
        ("Open_Palm", "PALM_DOWN", FrameDisposition.RELEASE),
        ("Open_Palm", "EDGE", FrameDisposition.RELEASE),
        ("None", "PALM_UP", FrameDisposition.RELEASE),
    ],
)
def test_non_request_pose_is_never_positive(
    gesture: str,
    facing: str,
    expected: FrameDisposition,
) -> None:
    gesture_msg, facing_msg = _messages(
        25.0,
        gesture=gesture,
        facing=facing,
    )
    evidence = classify_hand_frame(gesture_msg, facing_msg, pins=PINS)
    assert evidence.disposition is expected


def test_gap_unknown_and_timestamp_regression_reset_dwell() -> None:
    gate = ContinuousHandHandoverGate(max_positive_gap_sec=0.2)
    gate.observe(_positive(10.0), source_now_sec=10.0, receipt_monotonic=1.0)
    gap = gate.observe(_positive(10.25), source_now_sec=10.25, receipt_monotonic=1.1)
    assert gap.stability_sec == 0.0

    unknown = gate.observe(
        HandFrameEvidence(10.30, FrameDisposition.UNKNOWN, "ambiguous"),
        source_now_sec=10.30,
        receipt_monotonic=1.2,
    )
    assert unknown.active is False

    regression = gate.observe(
        _positive(10.20),
        source_now_sec=10.20,
        receipt_monotonic=1.3,
    )
    assert regression.accepted_sample is False
    assert regression.reason == "source_time_regression"


def test_default_gap_tolerates_measured_facing_cadence_for_release_and_dwell() -> None:
    gate = ContinuousHandHandoverGate()
    gate.inhibit_until_release()

    for stamp in (10.0, 10.403, 10.806):
        released = gate.observe(
            HandFrameEvidence(stamp, FrameDisposition.RELEASE, "closed_fist"),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )
    assert released.active is False

    for stamp in (11.0, 11.11, 11.25, 11.653):
        accepted = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )

    assert accepted.rising_edge is True
    assert accepted.active is True
    assert accepted.generation == 1
    assert gate.expire(receipt_monotonic=12.056) is None
    assert gate.expire(receipt_monotonic=12.154) is not None


def test_episode_is_one_shot_and_only_fresh_release_rearms() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.3,
        release_sec=0.5,
        minimum_positive_samples=4,
    )
    for stamp in (1.0, 1.1, 1.2, 1.3):
        first = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )
    assert first.rising_edge is True
    held = gate.observe(
        _positive(1.4), source_now_sec=1.4, receipt_monotonic=1.4
    )
    assert held.active is True
    assert held.rising_edge is False
    assert held.generation == 1

    gate.expire(receipt_monotonic=2.0)
    for stamp in (2.1, 2.2, 2.3, 2.4):
        same_episode = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )
    assert same_episode.active is True
    assert same_episode.rising_edge is False
    assert same_episode.generation == 1

    for stamp in (2.5, 2.65, 2.8, 2.95, 3.0):
        gate.observe(
            HandFrameEvidence(stamp, FrameDisposition.RELEASE, "closed_fist"),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )
    for stamp in (3.1, 3.2, 3.3, 3.4):
        new_episode = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )
    assert new_episode.rising_edge is True
    assert new_episode.generation == 2


def test_unknown_breaks_release_continuity_and_cannot_rearm_episode() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.3,
        release_sec=0.5,
        minimum_positive_samples=4,
    )
    for stamp in (1.0, 1.1, 1.2, 1.3):
        gate.observe(_positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp)

    for stamp in (1.4, 1.55, 1.7):
        gate.observe(
            HandFrameEvidence(stamp, FrameDisposition.RELEASE, "closed_fist"),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )
    gate.observe(
        HandFrameEvidence(1.8, FrameDisposition.UNKNOWN, "occluded"),
        source_now_sec=1.8,
        receipt_monotonic=1.8,
    )
    for stamp in (1.9, 2.05, 2.2):
        gate.observe(
            HandFrameEvidence(stamp, FrameDisposition.RELEASE, "closed_fist"),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )
    for stamp in (2.3, 2.4, 2.5, 2.6):
        update = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )

    assert update.active is True
    assert update.rising_edge is False
    assert update.generation == 1


def test_pause_inhibition_requires_fresh_release_before_new_episode() -> None:
    gate = ContinuousHandHandoverGate(
        dwell_sec=0.3,
        release_sec=0.5,
        minimum_positive_samples=4,
    )
    for stamp in (1.0, 1.1, 1.2, 1.3):
        gate.observe(_positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp)
    gate.inhibit_until_release()

    for stamp in (2.0, 2.1, 2.2, 2.3):
        held = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )
    assert held.active is False
    assert held.reason == "fresh_release_required"
    assert held.generation == 1

    for stamp in (2.4, 2.55, 2.7, 2.85, 2.9):
        gate.observe(
            HandFrameEvidence(stamp, FrameDisposition.RELEASE, "closed_fist"),
            source_now_sec=stamp,
            receipt_monotonic=stamp,
        )
    for stamp in (3.0, 3.1, 3.2, 3.3):
        rearmed = gate.observe(
            _positive(stamp), source_now_sec=stamp, receipt_monotonic=stamp
        )
    assert rearmed.rising_edge is True
    assert rearmed.generation == 2


def _health(*, mapping_verified: bool) -> dict:
    return {
        "schema": "pnu.hand_keypoint_health.v1",
        "ready": True,
        "rgb_ready": True,
        "depth_ready": True,
        "depth_alignment_validated": True,
        "depth_registration_ready": True,
        "depth_registration_degraded": False,
        "depth_registration_backend_active": "cuda_cabi_v1",
        "model_ready": True,
        "hand_inference_ready": True,
        "gesture_model_ready": True,
        "gesture_inference_ready": True,
        "palm_facing_observation_ready": True,
        "palm_facing_mapping_verified": mapping_verified,
        "handedness_policy": "forced_camera_constraint",
        "forced_handedness_label": "Right",
        "gesture_model_version": PINS.gesture_model_version,
        "gesture_model_asset_sha256": PINS.gesture_model_sha256,
        "palm_facing_estimator": {
            "name": PINS.facing_estimator_name,
            "version": PINS.facing_estimator_version,
            "spec_sha256": PINS.facing_spec_sha256,
            "calibration_version": PINS.calibration_version,
            "handedness_mapping_version": PINS.handedness_mapping_version,
        },
    }


def test_health_requires_mapping_verification_or_explicit_operator_approval() -> None:
    ready, reason = validate_hand_health(
        _health(mapping_verified=False),
        pins=PINS,
        operator_mapping_approved=False,
    )
    assert ready is False
    assert reason == "health_palm_mapping_unverified"

    ready, reason = validate_hand_health(
        _health(mapping_verified=False),
        pins=PINS,
        operator_mapping_approved=True,
    )
    assert ready is True
    assert reason == "health_ready"


@pytest.mark.parametrize(
    ("path", "expected_reason"),
    [
        (("depth_registration_backend_active",), "health_depth_backend_mismatch"),
        (("gesture_model_version",), "health_gesture_version_mismatch"),
        (("gesture_model_asset_sha256",), "health_gesture_sha_mismatch"),
        (("palm_facing_estimator", "name"), "health_facing_name_mismatch"),
        (("palm_facing_estimator", "version"), "health_facing_version_mismatch"),
        (
            ("palm_facing_estimator", "spec_sha256"),
            "health_facing_spec_sha256_mismatch",
        ),
        (
            ("palm_facing_estimator", "calibration_version"),
            "health_facing_calibration_version_mismatch",
        ),
        (
            ("palm_facing_estimator", "handedness_mapping_version"),
            "health_facing_handedness_mapping_version_mismatch",
        ),
    ],
)
def test_operator_mapping_approval_never_bypasses_a_mutated_health_pin(
    path: tuple[str, ...],
    expected_reason: str,
) -> None:
    payload = _health(mapping_verified=False)
    target = payload
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = "mutated-unapproved-provenance"

    ready, reason = validate_hand_health(
        payload,
        pins=PINS,
        operator_mapping_approved=True,
    )

    assert ready is False
    assert reason == expected_reason


@pytest.mark.parametrize(
    ("message_kind", "field", "expected_reason"),
    [
        ("gesture", "model_name", "gesture_provenance_mismatch"),
        ("gesture", "model_version", "gesture_provenance_mismatch"),
        ("gesture", "model_asset_sha256", "gesture_provenance_mismatch"),
        ("facing", "estimator_name", "facing_provenance_mismatch"),
        ("facing", "estimator_version", "facing_provenance_mismatch"),
        ("facing", "estimator_spec_sha256", "facing_provenance_mismatch"),
        ("facing", "calibration_version", "facing_provenance_mismatch"),
        ("facing", "handedness_mapping_version", "facing_provenance_mismatch"),
    ],
)
def test_frame_provenance_pin_mutation_is_unknown_not_positive(
    message_kind: str,
    field: str,
    expected_reason: str,
) -> None:
    gesture, facing = _messages(40.0)
    target = gesture if message_kind == "gesture" else facing
    setattr(target, field, "mutated-unapproved-provenance")

    evidence = classify_hand_frame(gesture, facing, pins=PINS)

    assert evidence.disposition is FrameDisposition.UNKNOWN
    assert evidence.reason == expected_reason
