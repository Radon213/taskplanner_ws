import math

import pytest

from tool_belief_tracker.core import (
    ALLOWED_SEMANTIC_EVENT_TYPES,
    BeliefTracker,
    InventoryItem,
    MAX_COMMAND_EVIDENCE,
    MAX_IGNORED_CLASS_NAMES,
    TrackerConfig,
    semantic_location,
)


def _item(
    instrument_id: str = "T04",
    instance_id: str = "T04#1",
    location: str = "tray",
    *,
    exchangeable_population: bool = True,
) -> InventoryItem:
    return InventoryItem(
        instrument_id=instrument_id,
        instance_id=instance_id,
        display_name=instrument_id,
        initial_location=location,
        exchangeable_population=exchangeable_population,
    )


def _by_id(tracker: BeliefTracker, now: float = 10.0):
    return {row["instance_id"]: row for row in tracker.snapshot(now)}


def _repeat_camera(
    tracker: BeliefTracker,
    *,
    view: str,
    zone: str,
    detections: list[tuple[str, float]],
    start_sec: float = 1.0,
    count: int = 3,
) -> None:
    """Apply clean continuous evidence long enough to cross one commit gate."""

    for index in range(count):
        tracker.apply_camera_frame(
            view=view,
            zone=zone,
            detections=detections,
            timestamp_sec=start_sec + 0.25 * index,
            health_valid=True,
        )


def _advance_commit_after_terminal_evidence(
    tracker: BeliefTracker,
    timestamp_sec: float = 1.6,
) -> None:
    """Supply one healthy post-result frame without inventing target evidence."""

    tracker.apply_camera_frame(
        view="cam_3",
        zone="tray",
        detections=[],
        timestamp_sec=timestamp_sec,
        health_valid=True,
    )


def test_cleaner_slot_maps_to_cleaner_before_generic_slot_fallback() -> None:
    assert semantic_location("cleaner_slot") == "cleaner"
    assert semantic_location("cleaner_slot", "cleaner_slot") == "cleaner"


def test_tracker_never_creates_or_deletes_inventory_tracks() -> None:
    tracker = BeliefTracker([_item(), _item("T03", "T03#1", "field")])

    assert tracker.instance_ids == ("T03#1", "T04#1")
    assert tracker.instrument_ids == ("T03", "T04")
    tracker.note_ignored_class("Brand new detector class")
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[("T99", 0.99)],
        timestamp_sec=1.0,
        health_valid=True,
    )

    assert tracker.instance_ids == ("T03#1", "T04#1")
    assert tracker.ignored_out_of_inventory_count == 1
    assert tracker.ignored_class_names == ("Brand new detector class",)


def test_positive_camera_evidence_moves_location_gradually_then_commits() -> None:
    tracker = BeliefTracker([_item()])

    first = _by_id(tracker, 0.0)["T04#1"]
    assert first["committed_location_id"] == "tray"
    for index in range(1, 7):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[("T04", 0.9)],
            timestamp_sec=index * 0.25,
            health_valid=True,
            model_version="model-any-version",
            ontology_version="provider-ontology-any-version",
        )

    belief = _by_id(tracker, 2.0)["T04#1"]
    assert belief["most_likely_location_id"] == "mayo"
    assert belief["most_likely_probability"] > 0.85
    assert belief["committed_location_id"] == "mayo"
    assert belief["model_version"] == "model-any-version"


def test_invalid_health_cannot_apply_positive_or_negative_camera_evidence() -> None:
    tracker = BeliefTracker([_item()])
    before = _by_id(tracker, 0.0)["T04#1"]["locations"]

    assert not tracker.apply_camera_frame(
        view="cam_3",
        zone="tray",
        detections=[],
        timestamp_sec=10.0,
        health_valid=False,
    )

    after = _by_id(tracker, 10.0)["T04#1"]["locations"]
    assert after == before


def test_missing_detection_decays_gradually_and_robot_motion_scales_decay() -> None:
    normal = BeliefTracker([_item()])
    moving = BeliefTracker([_item()])
    moving.set_robot_motion(True, 0.0, "cmd-1")

    for tracker in (normal, moving):
        tracker.apply_camera_frame(
            view="cam_3",
            zone="tray",
            detections=[],
            timestamp_sec=0.0,
            health_valid=True,
        )
        tracker.apply_camera_frame(
            view="cam_3",
            zone="tray",
            detections=[],
            timestamp_sec=3.0,
            health_valid=True,
        )

    normal_tray = _by_id(normal, 3.0)["T04#1"]["locations"]["tray"]
    moving_tray = _by_id(moving, 3.0)["T04#1"]["locations"]["tray"]
    assert 0.40 < normal_tray < 0.60
    assert moving_tray > 0.85
    assert moving_tray > normal_tray


def test_camera_absence_only_moves_a_zone_toward_unknown() -> None:
    """A missing tray/Mayo detection cannot invent surgeon or robot custody."""

    tracker = BeliefTracker([_item(location="mayo")])
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[],
        timestamp_sec=0.0,
        health_valid=True,
    )
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[],
        timestamp_sec=6.0,
        health_valid=True,
    )

    locations = _by_id(tracker, 6.0)["T04#1"]["locations"]
    assert locations["mayo"] < 0.30
    assert locations["unknown"] > 0.70
    assert locations["surgeon"] < 0.001
    assert locations["robot"] < 0.001


def test_time_normalized_camera_evidence_is_nearly_rate_independent() -> None:
    config = TrackerConfig(evidence_window_sec=0.01)
    slow = BeliefTracker([_item()], config=config)
    fast = BeliefTracker([_item()], config=config)

    for timestamp in (0.0, 0.2, 0.4, 0.6, 0.8):
        slow.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[("T04", 0.9)],
            timestamp_sec=timestamp,
            health_valid=True,
        )
    for index in range(13):
        fast.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[("T04", 0.9)],
            timestamp_sec=index / 15.0,
            health_valid=True,
        )

    slow_mayo = _by_id(slow, 1.0)["T04#1"]["locations"]["mayo"]
    fast_mayo = _by_id(fast, 1.0)["T04#1"]["locations"]["mayo"]
    assert abs(slow_mayo - fast_mayo) < 0.06


def test_unique_uv_continuity_repairs_lower_confidence_reflection_class_flip() -> None:
    tracker = BeliefTracker(
        [
            _item("T02", "T02#1", "mayo"),
            _item("T04", "T04#1", "mayo"),
        ]
    )
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[
            ("T02", 0.80, 790.0, 288.0),
            ("T04", 0.80, 785.0, 424.0),
        ],
        timestamp_sec=0.0,
        health_valid=True,
    )

    # The true Bovie keeps its own UV and higher confidence. The reflected
    # Adson has flipped to Bovie, but remains uniquely near the recent Adson UV.
    for index in range(1, 9):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[
                ("T04", 0.84, 785.0, 424.0),
                ("T04", 0.56, 790.0, 288.0),
            ],
            timestamp_sec=index * 0.1,
            health_valid=True,
        )

    beliefs = _by_id(tracker, 0.8)
    assert beliefs["T02#1"]["committed_location_id"] == "mayo"
    assert beliefs["T02#1"]["locations"]["mayo"] > 0.90
    assert "camera:cam_4:uv_class_continuity" in beliefs["T02#1"][
        "evidence_sources"
    ]
    assert tracker.ignored_out_of_inventory_count == 0


def test_overlapping_uv_detections_reduce_evidence_without_reassigning_identity() -> None:
    tracker = BeliefTracker(
        [
            _item("T02", "T02#1", "mayo"),
            _item("T04", "T04#1", "mayo"),
        ]
    )
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[
            ("T02", 0.80, 100.0, 100.0),
            ("T04", 0.80, 100.0, 160.0),
        ],
        timestamp_sec=0.0,
        health_valid=True,
    )

    for index in range(1, 9):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[
                ("T04", 0.80, 100.0, 128.0),
                ("T04", 0.70, 100.0, 132.0),
            ],
            timestamp_sec=index * 0.1,
            health_valid=True,
        )

    beliefs = _by_id(tracker, 0.8)
    adson = beliefs["T02#1"]
    assert adson["committed_location_id"] == "mayo"
    assert adson["locations"]["mayo"] > 0.85
    assert "camera:cam_4:uv_ambiguous_scaled" in adson["evidence_sources"]
    assert "camera:cam_4:uv_class_continuity" not in adson["evidence_sources"]


def test_hand_occlusion_scales_camera_evidence_without_hard_blocking() -> None:
    clear = BeliefTracker([_item()])
    occluded = BeliefTracker([_item()])
    occluded.set_mayo_hand_present(True, 0.0)

    for tracker in (clear, occluded):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[("T04", 0.99)],
            timestamp_sec=1.0,
            health_valid=True,
        )

    clear_belief = _by_id(clear, 1.0)["T04#1"]
    occluded_belief = _by_id(occluded, 1.0)["T04#1"]
    assert clear_belief["committed_location_id"] == "tray"
    assert occluded_belief["committed_location_id"] == "tray"
    assert clear_belief["locations"]["mayo"] > occluded_belief["locations"]["mayo"]
    assert "mayo_hand_occlusion_scaled" in occluded_belief["status_flags"]

    # The observation was down-weighted, not rejected. Once the hand leaves,
    # clean continuous Mayo evidence passes the same global commit threshold.
    occluded.set_mayo_hand_present(False, 1.1)
    _repeat_camera(
        occluded,
        view="cam_4",
        zone="mayo",
        detections=[("T04", 0.99)],
        start_sec=1.7,
        count=4,
    )
    assert _by_id(occluded, 3.0)["T04#1"]["committed_location_id"] == "mayo"


def test_operator_verified_run_prior_fixes_population_but_not_location_updates() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    frozen = tracker.freeze_run_prior()
    assert frozen.instance_ids == ("T03#1",)
    _repeat_camera(
        frozen,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.99), ("T03", 0.99)],
    )

    belief = _by_id(frozen, 2.0)["T03#1"]
    assert frozen.instance_ids == ("T03#1",)
    assert frozen.ignored_out_of_inventory_count == 3
    assert belief["existence_probability"] == 1.0
    assert belief["committed_location_id"] == "mayo"
    assert "operator_verified_run_prior" in belief["status_flags"]


def test_identical_instances_receive_symmetric_evidence_when_identity_is_ambiguous() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "field"),
            _item("T03", "T03#2", "field"),
        ]
    )
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.9)],
        timestamp_sec=1.0,
        health_valid=True,
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["locations"] == beliefs["T03#2"]["locations"]
    assert "identity_ambiguous_symmetric_evidence" in beliefs["T03#1"]["status_flags"]


def test_repeated_single_detection_cannot_confirm_both_identical_instances() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "field"),
            _item("T03", "T03#2", "field"),
        ]
    )
    for index in range(1, 13):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[("T03", 0.95)],
            timestamp_sec=index * 0.25,
            health_valid=True,
        )

    beliefs = _by_id(tracker, 3.0)
    for instance_id in ("T03#1", "T03#2"):
        belief = beliefs[instance_id]
        assert belief["locations"]["mayo"] <= 0.500001
        assert belief["status"] != "confirmed"
        assert belief["committed_location_id"] != "mayo"


def test_two_detections_can_confirm_both_identical_instances() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "field"),
            _item("T03", "T03#2", "field"),
        ]
    )
    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95), ("T03", 0.90)],
    )

    beliefs = _by_id(tracker, 1.0)
    for instance_id in ("T03#1", "T03#2"):
        belief = beliefs[instance_id]
        assert belief["locations"]["mayo"] > 0.85
        assert belief["status"] == "confirmed"
        assert belief["committed_location_id"] == "mayo"


def test_count_negative_is_attenuated_during_robot_motion() -> None:
    inventory = [
        _item("T03", "T03#1", "mayo"),
        _item("T03", "T03#2", "mayo"),
    ]
    normal = BeliefTracker(inventory)
    moving = BeliefTracker(inventory)
    moving.set_robot_motion(True, 0.0, "cmd")

    for tracker in (normal, moving):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[("T03", 0.95)],
            timestamp_sec=1.0,
            health_valid=True,
        )

    normal_probability = _by_id(normal, 1.0)["T03#1"]["locations"]["mayo"]
    moving_probability = _by_id(moving, 1.0)["T03#1"]["locations"]["mayo"]
    assert normal_probability < 0.80
    assert moving_probability > 0.85
    assert moving_probability > normal_probability


def test_action_stages_preserve_contract_semantics() -> None:
    tracker = BeliefTracker([_item()])
    assert tracker.register_command("cmd", "T04", "T04#1", "tray", "surgeon", 0.0)

    tracker.apply_skill_status("cmd", "moving_to_source", False, 0.1, 0.1)
    moving_to_source = _by_id(tracker, 0.1)["T04#1"]["locations"]
    assert moving_to_source["tray"] > moving_to_source["robot"]

    tracker.apply_skill_status("cmd", "grasping", False, 0.3, 0.3)
    grasping = _by_id(tracker, 0.3)["T04#1"]["locations"]
    assert grasping["tray"] > grasping["robot"] > grasping["surgeon"]

    tracker.apply_skill_status("cmd", "moving_to_target", False, 0.5, 0.5)
    moving_to_target = _by_id(tracker, 0.5)["T04#1"]
    assert (
        moving_to_target["locations"]["robot"]
        > moving_to_target["locations"]["tray"]
    )
    assert moving_to_target["committed_location_id"] == "tray"

    tracker.apply_skill_status("cmd", "waiting_for_takeover", False, 0.8, 0.8)
    waiting = _by_id(tracker, 0.8)["T04#1"]["locations"]
    assert waiting["robot"] > waiting["surgeon"]
    assert waiting["robot"] > waiting["tray"]


@pytest.mark.parametrize(
    ("source", "target"),
    [("tray", "robot"), ("mayo", "robot"), ("tray", "surgeon")],
)
def test_secured_delivery_commits_robot_after_common_dwell(source, target) -> None:
    tracker = BeliefTracker([_item(location=source)])
    assert tracker.register_command(
        "cmd",
        "T04",
        "T04#1",
        source,
        target,
        0.0,
    )

    assert tracker.apply_skill_status("cmd", "grasping", False, 0.3, 0.3)
    assert tracker.apply_skill_status("cmd", "moving_to_target", False, 0.5, 0.5)

    assert _by_id(tracker, 0.99)["T04#1"]["committed_location_id"] == source
    secured = _by_id(tracker, 1.01)["T04#1"]
    assert secured["committed_location_id"] == "robot"
    assert secured["locations"]["robot"] >= tracker.config.confirm_threshold


def test_grasping_alone_never_claims_delivery_hand_custody() -> None:
    tracker = BeliefTracker([_item()])
    assert tracker.register_command(
        "cmd", "T04", "T04#1", "tray", "surgeon", 0.0
    )

    assert tracker.apply_skill_status("cmd", "grasping", False, 0.3, 0.3)

    belief = _by_id(tracker, 2.0)["T04#1"]
    assert belief["committed_location_id"] == "tray"
    assert belief["locations"]["tray"] > belief["locations"]["robot"]


def test_controller_failure_releases_secured_delivery_hand_commit() -> None:
    tracker = BeliefTracker([_item()])
    assert tracker.register_command(
        "cmd", "T04", "T04#1", "tray", "surgeon", 0.0
    )
    assert tracker.apply_skill_status("cmd", "moving_to_target", False, 0.5, 0.5)
    assert _by_id(tracker, 1.01)["T04#1"]["committed_location_id"] == "robot"

    assert tracker.apply_skill_status(
        "cmd", "failed", False, 0.7, 1.1, "controller_lost"
    )

    failed = _by_id(tracker, 1.1)["T04#1"]
    assert failed["committed_location_id"] != "robot"
    assert failed["locations"]["unknown"] > 0.10


def test_recovery_leg_does_not_claim_delivery_hand_custody() -> None:
    tracker = BeliefTracker([_item(location="mayo")])
    assert tracker.register_command(
        "cmd", "T04", "T04#1", "mayo", "tray", 0.0
    )

    assert tracker.apply_skill_status("cmd", "moving_to_target", False, 0.5, 0.5)

    assert _by_id(tracker, 2.0)["T04#1"]["committed_location_id"] != "robot"


def test_cancel_recovery_stage_targets_tray_not_original_surgeon_target() -> None:
    tracker = BeliefTracker([_item()])
    tracker.register_command("cmd", "T04", "T04#1", "tray", "surgeon", 0.0)
    tracker.apply_skill_status("cmd", "moving_to_target", False, 0.5, 0.5)
    tracker.apply_skill_status("cmd", "recovering_to_tray", False, 0.8, 0.8)
    probabilities = _by_id(tracker, 0.8)["T04#1"]["locations"]

    assert probabilities["tray"] > probabilities["surgeon"]


def test_cancel_terminal_reason_is_the_strong_location_receipt() -> None:
    source_unchanged = BeliefTracker([_item()])
    source_unchanged.register_command("cmd", "T04", "T04#1", "tray", "surgeon", 0.0)
    source_unchanged.apply_skill_status("cmd", "moving_to_target", False, 0.7, 0.7)
    source_unchanged.apply_skill_status(
        "cmd",
        "canceled",
        False,
        1.0,
        1.0,
        "canceled_source_unchanged",
    )
    source = _by_id(source_unchanged, 1.0)["T04#1"]
    assert source["most_likely_location_id"] == "tray"
    assert source["motion_mode"] == "grace"

    recovered = BeliefTracker([_item(location="field")])
    recovered.register_command("cmd", "T04", "T04#1", "field", "surgeon", 0.0)
    recovered.apply_skill_status(
        "cmd",
        "canceled",
        False,
        1.0,
        1.0,
        "canceled_recovered_to_tray",
    )
    recovery = _by_id(recovered, 1.0)["T04#1"]
    assert recovery["most_likely_location_id"] == "tray"


def test_terminal_event_is_strong_but_observation_only_location_evidence() -> None:
    tracker = BeliefTracker([_item()])
    assert tracker.apply_semantic_event(
        instrument_id="T04",
        instance_id="T04#1",
        location="surgeon",
        confidence=1.0,
        event_type="ToolHandoverCompleted",
        timestamp_sec=1.0,
    )

    belief = _by_id(tracker, 1.0)["T04#1"]
    assert belief["most_likely_location_id"] == "surgeon"
    assert math.isclose(belief["existence_probability"], 1.0)
    assert "fixed_inventory" in belief["status_flags"]
    assert "scenario_bounded_capacity" not in belief["status_flags"]
    assert "observation_only" in belief["status_flags"]


def test_completed_pickup_survives_one_motion_occlusion_echo() -> None:
    tracker = BeliefTracker([_item(location="mayo")])
    assert tracker.register_command(
        "cmd",
        "T04",
        "T04#1",
        "mayo",
        "robot",
        0.0,
    )
    assert tracker.apply_skill_status("cmd", "grasping", False, 0.3, 0.8)
    assert tracker.apply_skill_status("cmd", "completed", True, 1.0, 1.0)

    # A single source-ROI echo while the arm is leaving remains evidence, but
    # cannot cancel an otherwise stable completed pickup.
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[("T04", 0.95)],
        timestamp_sec=1.1,
        health_valid=True,
    )
    for timestamp_sec in (1.3, 1.6, 1.9, 2.2):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[],
            timestamp_sec=timestamp_sec,
            health_valid=True,
        )

    belief = _by_id(tracker, 2.2)["T04#1"]
    assert belief["committed_location_id"] == "robot"
    assert belief["locations"]["robot"] > 0.85


def test_completed_pickup_is_reversed_by_persistent_source_detection() -> None:
    tracker = BeliefTracker([_item(location="mayo")])
    assert tracker.register_command(
        "cmd",
        "T04",
        "T04#1",
        "mayo",
        "robot",
        0.0,
    )
    assert tracker.apply_skill_status("cmd", "grasping", False, 0.3, 0.8)
    assert tracker.apply_skill_status("cmd", "completed", True, 1.0, 1.0)

    # No special "pickup failed" state is needed. If the same fixed-inventory
    # slot keeps being seen at its source, ordinary evidence overtakes the
    # controller receipt and the common dwell rule recommits it to Mayo.
    for timestamp_sec in (1.1, 1.3, 1.6, 1.9, 2.2, 2.5):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[("T04", 0.95)],
            timestamp_sec=timestamp_sec,
            health_valid=True,
        )

    belief = _by_id(tracker, 2.5)["T04#1"]
    assert belief["committed_location_id"] == "mayo"
    assert belief["locations"]["mayo"] > 0.95


@pytest.mark.parametrize("event_key", sorted(ALLOWED_SEMANTIC_EVENT_TYPES))
def test_only_reviewed_semantic_event_types_apply(event_key: str) -> None:
    tracker = BeliefTracker([_item()])

    assert tracker.apply_semantic_event(
        instrument_id="T04",
        instance_id="T04#1",
        location="mayo",
        confidence=0.8,
        event_type=event_key,
        timestamp_sec=1.0,
    )


def test_zero_confidence_or_unknown_semantic_event_is_ignored() -> None:
    tracker = BeliefTracker([_item()])
    before = _by_id(tracker, 0.0)["T04#1"]["locations"]

    assert not tracker.apply_semantic_event(
        instrument_id="T04",
        instance_id="T04#1",
        location="mayo",
        confidence=0.0,
        event_type="ToolHandoverCompleted",
        timestamp_sec=1.0,
    )
    assert not tracker.apply_semantic_event(
        instrument_id="T04",
        instance_id="T04#1",
        location="mayo",
        confidence=1.0,
        event_type="UnreviewedLocationGuess",
        timestamp_sec=1.0,
    )
    assert _by_id(tracker, 1.0)["T04#1"]["locations"] == before


def test_ambiguous_action_without_instance_never_moves_all_copies() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "field"),
            _item("T03", "T03#2", "field"),
        ]
    )

    assert not tracker.register_command(
        "cmd",
        "T03",
        "",
        "field",
        "surgeon",
        0.0,
    )
    assert not tracker.apply_skill_status("cmd", "completed", True, 1.0, 1.0)
    assert tracker.ignored_ambiguous_command_count == 1
    for belief in tracker.snapshot(1.0):
        assert belief["most_likely_location_id"] == "field"
        assert belief["locations"]["surgeon"] < 0.01


def test_ambiguous_semantic_event_without_instance_is_rejected() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "field"),
            _item("T03", "T03#2", "field"),
        ]
    )

    assert not tracker.apply_semantic_event(
        instrument_id="T03",
        instance_id="",
        location="surgeon",
        confidence=1.0,
        event_type="ToolHandoverCompleted",
        timestamp_sec=1.0,
    )
    assert tracker.ignored_ambiguous_command_count == 1
    assert all(
        belief["most_likely_location_id"] == "field"
        for belief in tracker.snapshot(1.0)
    )


def test_exchangeable_camera_assigns_one_detection_to_one_concrete_slot() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "field"),
            _item("T03", "T03#2", "field"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
        start_sec=1.6,
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["most_likely_location_id"] == "mayo"
    assert beliefs["T03#1"]["committed_location_id"] == "mayo"
    assert beliefs["T03#2"]["most_likely_location_id"] == "field"
    assert sum(
        belief["committed_location_id"] == "mayo"
        for belief in beliefs.values()
    ) == 1
    assert tracker.instance_ids == ("T03#1", "T03#2")
    for belief in beliefs.values():
        assert "scenario_bounded_capacity" in belief["status_flags"]
        assert "fixed_inventory" not in belief["status_flags"]
        assert "logical_instance_exchangeable" in belief["status_flags"]
        assert "physical_identity_not_asserted" in belief["status_flags"]
        assert "identity_ambiguous_symmetric_evidence" not in belief["status_flags"]


def test_exchangeable_camera_preserves_counts_across_two_zones() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "tray"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
        start_sec=1.6,
    )
    _repeat_camera(
        tracker,
        view="cam_3",
        zone="tray",
        detections=[("T03", 0.95)],
    )

    locations = [
        belief["committed_location_id"]
        for belief in tracker.snapshot(1.0)
    ]
    assert sorted(locations) == ["mayo", "tray"]


def test_cross_zone_single_detection_relocates_active_slot_before_dormant_birth() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["committed_location_id"] == "mayo"
    assert beliefs["T03#1"]["existence_probability"] > 0.95
    assert beliefs["T03#2"]["existence_probability"] == 0.0
    assert "capacity_slot_inactive" in beliefs["T03#2"]["status_flags"]


def test_fresh_cross_zone_receipts_activate_dormant_slot_as_simultaneous_duplicate() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )
    _repeat_camera(
        tracker,
        view="cam_3",
        zone="tray",
        detections=[("T03", 0.95)],
    )
    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
        # CAM3's last clean tray receipt is at 1.50 s.  A subsequent Mayo
        # receipt makes this a simultaneous two-zone count, not relocation.
        start_sec=1.6,
    )

    beliefs = _by_id(tracker, 1.1)
    assert beliefs["T03#1"]["committed_location_id"] == "tray"
    assert beliefs["T03#2"]["committed_location_id"] == "mayo"
    assert all(row["existence_probability"] >= 0.85 for row in beliefs.values())


def test_legacy_fixed_duplicate_remains_strict_under_global_exchangeable_opt_in() -> None:
    tracker = BeliefTracker(
        [
            _item(
                "T03",
                "T03#1",
                "field",
                exchangeable_population=False,
            ),
            _item(
                "T03",
                "T03#2",
                "field",
                exchangeable_population=False,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
        timestamp_sec=1.0,
        health_valid=True,
    )

    beliefs = tracker.snapshot(1.0)
    assert all("fixed_inventory" in row["status_flags"] for row in beliefs)
    assert all("logical_instance_exchangeable" not in row["status_flags"] for row in beliefs)
    assert all(row["locations"]["mayo"] > 0.20 for row in beliefs)
    assert not tracker.register_command("cmd", "T03", "", "field", "surgeon", 2.0)


def test_camera_detections_over_capacity_are_bounded_and_audited_by_type() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "tray"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95), ("T03", 0.90), ("T03", 0.85)],
        count=4,
    )

    beliefs = tracker.snapshot(1.0)
    assert len(beliefs) == 2
    assert sum(row["committed_location_id"] == "mayo" for row in beliefs) == 2
    assert tracker.ignored_out_of_inventory_count == 4
    assert tracker.ignored_class_names == ("T03:capacity_overflow",)


def test_exchangeable_missing_slot_decays_to_inactive_capacity() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "tray"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )
    for timestamp in (0.0, 3.0, 6.0, 9.0):
        tracker.apply_camera_frame(
            view="cam_3",
            zone="tray",
            detections=[("T03", 0.95)],
            timestamp_sec=timestamp,
            health_valid=True,
        )

    beliefs = _by_id(tracker, 9.0)
    assert beliefs["T03#1"]["existence_probability"] > 0.95
    assert "capacity_slot_active" in beliefs["T03#1"]["status_flags"]
    assert beliefs["T03#2"]["existence_probability"] < 0.55
    assert "capacity_slot_inactive" in beliefs["T03#2"]["status_flags"]


def test_stale_unknown_detector_born_slot_returns_to_dormant_capacity() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(
            exchangeable_instances=True,
            unknown_slot_retire_sec=10.0,
        ),
    )
    _repeat_camera(
        tracker,
        view="cam_3",
        zone="tray",
        detections=[("T03", 0.95)],
    )
    tracker.apply_camera_frame(
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
        timestamp_sec=1.6,
        health_valid=True,
    )
    assert tracker.register_command(
        "cmd",
        "T03",
        "T03#2",
        "mayo",
        "surgeon",
        1.7,
    )
    assert tracker.apply_skill_status(
        "cmd",
        "failed",
        False,
        0.2,
        1.8,
        "controller_lost",
    )
    for timestamp in (2.6, 3.4):
        tracker.apply_camera_frame(
            view="cam_4",
            zone="mayo",
            detections=[],
            timestamp_sec=timestamp,
            health_valid=True,
        )

    first_unknown = _by_id(tracker, 3.4)["T03#2"]
    assert first_unknown["most_likely_location_id"] == "unknown"
    assert first_unknown["existence_probability"] >= 0.55

    before_timeout = _by_id(tracker, 13.39)["T03#2"]
    assert before_timeout["existence_probability"] >= 0.55

    retired = _by_id(tracker, 13.4)["T03#2"]
    assert retired["existence_probability"] == 0.0
    assert "capacity_slot_inactive" in retired["status_flags"]
    assert "stale_unknown:capacity_retired" in retired["evidence_sources"]


def test_stale_unknown_retirement_does_not_remove_authored_active_inventory() -> None:
    tracker = BeliefTracker(
        [
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#1",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=1.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(
            exchangeable_instances=True,
            unknown_slot_retire_sec=10.0,
        ),
    )

    belief = _by_id(tracker, 1_000.0)["T03#1"]
    assert belief["existence_probability"] == 1.0
    assert "capacity_slot_active" in belief["status_flags"]


def test_exchangeable_detection_activates_a_dormant_capacity_slot() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )
    before = _by_id(tracker, 0.0)
    assert before["T03#2"]["existence_probability"] == 0.0
    assert "capacity_slot_inactive" in before["T03#2"]["status_flags"]

    _repeat_camera(
        tracker,
        view="cam_3",
        zone="tray",
        detections=[("T03", 0.95)],
        start_sec=0.8,
    )
    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
        start_sec=1.4,
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["committed_location_id"] == "tray"
    assert beliefs["T03#2"]["committed_location_id"] == "mayo"
    assert beliefs["T03#2"]["existence_probability"] >= 0.85
    assert "capacity_slot_active" in beliefs["T03#2"]["status_flags"]


def test_mayo_appearance_rebinds_surgeon_owned_slot_before_dormant_capacity() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "surgeon"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["committed_location_id"] == "mayo"
    assert beliefs["T03#2"]["committed_location_id"] != "mayo"
    assert beliefs["T03#2"]["existence_probability"] == 0.0
    assert "camera:cam_4:assumed_surgeon_return" in beliefs["T03#1"]["evidence_sources"]


def test_existing_mayo_slot_wins_before_surgeon_return_when_count_is_unchanged() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "surgeon"),
            _item("T03", "T03#2", "mayo"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["committed_location_id"] == "surgeon"
    assert beliefs["T03#2"]["committed_location_id"] == "mayo"


def test_human_return_inference_can_be_disabled_without_restarting_tracker() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "surgeon"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(
            exchangeable_instances=True,
            mayo_appearance_assumes_surgeon_return=False,
        ),
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["committed_location_id"] == "surgeon"
    assert beliefs["T03#2"]["committed_location_id"] == "mayo"


def test_exchangeable_rehydrate_requires_active_slots_but_allows_dormant_capacity_omission() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            InventoryItem(
                instrument_id="T03",
                instance_id="T03#2",
                display_name="T03",
                initial_location="unknown",
                initial_confidence=0.0,
                initial_activity_probability=0.0,
                exchangeable_population=True,
            ),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    assert not tracker.reconcile_fixed_states([])
    assert not tracker.reconcile_fixed_states(
        [
            ("T03#1", "T03", "tray", 1.0),
            ("T03#2", "T03", "mayo", 1.0),
        ]
    )
    assert tracker.reconcile_fixed_states(
        [("T03#1", "T03", "tray", 1.0)]
    )
    beliefs = _by_id(tracker, 0.0)
    assert beliefs["T03#1"]["existence_probability"] >= 0.90
    assert beliefs["T03#2"]["existence_probability"] == 0.0
    assert "capacity_slot_inactive" in beliefs["T03#2"]["status_flags"]


def test_zero_initial_exchangeable_population_rehydrates_from_empty_dt_inventory() -> None:
    dormant = InventoryItem(
        instrument_id="T03",
        instance_id="T03#1",
        display_name="T03",
        initial_location="unknown",
        initial_confidence=0.0,
        initial_activity_probability=0.0,
        exchangeable_population=True,
    )
    tracker = BeliefTracker(
        [dormant],
        config=TrackerConfig(exchangeable_instances=True),
    )
    strict_tracker = BeliefTracker([dormant], config=TrackerConfig())

    assert tracker.reconcile_fixed_states([])
    assert not strict_tracker.reconcile_fixed_states([])
    belief = tracker.snapshot(0.0)[0]
    assert belief["existence_probability"] == 0.0
    assert "logical_instance_exchangeable" in belief["status_flags"]
    assert "capacity_slot_inactive" in belief["status_flags"]


def test_exchangeable_type_only_commands_bind_distinct_source_slots() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "tray"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    assert tracker.register_command("cmd-1", "T03", "", "tray", "surgeon", 0.0)
    assert _by_id(tracker, 0.0)["T03#1"]["active_command_id"] == "cmd-1"
    assert tracker.apply_skill_status("cmd-1", "completed", True, 1.0, 1.0)
    _advance_commit_after_terminal_evidence(tracker)
    assert tracker.register_command("cmd-2", "T03", "", "tray", "surgeon", 2.0)

    beliefs = _by_id(tracker, 2.0)
    assert beliefs["T03#1"]["committed_location_id"] == "surgeon"
    assert beliefs["T03#2"]["active_command_id"] == "cmd-2"
    assert tracker.ignored_ambiguous_command_count == 0


def test_exchangeable_explicit_action_id_rebinds_to_best_free_source_slot() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "mayo"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    assert tracker.register_command(
        "cmd",
        "T03",
        "T03#2",
        "tray",
        "surgeon",
        0.0,
    )

    rebound = _by_id(tracker, 0.0)
    assert rebound["T03#1"]["committed_location_id"] == "mayo"
    assert rebound["T03#2"]["committed_location_id"] == "tray"
    assert rebound["T03#2"]["active_command_id"] == "cmd"
    assert "exchangeable_slot_rebound" in rebound["T03#2"]["status_flags"]
    assert tracker.apply_skill_status("cmd", "completed", True, 1.0, 1.0)
    _advance_commit_after_terminal_evidence(tracker)
    assert _by_id(tracker, 1.0)["T03#2"]["committed_location_id"] == "surgeon"


def test_exchangeable_camera_never_relabels_an_action_locked_slot() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "tray"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )
    assert tracker.register_command("cmd", "T03", "", "tray", "surgeon", 0.0)
    assert not tracker.register_command(
        "competing-cmd",
        "T03",
        "T03#1",
        "tray",
        "mayo",
        0.1,
    )

    _repeat_camera(
        tracker,
        view="cam_4",
        zone="mayo",
        detections=[("T03", 0.95)],
    )

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["active_command_id"] == "cmd"
    assert beliefs["T03#1"]["committed_location_id"] == "tray"
    assert beliefs["T03#2"]["committed_location_id"] == "mayo"


def test_exchangeable_type_only_event_selects_best_source_slot() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "robot"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    assert tracker.apply_semantic_event(
        instrument_id="T03",
        instance_id="",
        location="surgeon",
        confidence=1.0,
        event_type="ToolHandoverCompleted",
        timestamp_sec=1.0,
    )
    _advance_commit_after_terminal_evidence(tracker)

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["committed_location_id"] == "tray"
    assert beliefs["T03#2"]["committed_location_id"] == "surgeon"
    assert tracker.ignored_ambiguous_command_count == 0


@pytest.mark.parametrize(
    ("event_type", "destination", "source_location"),
    [
        ("ToolReceivedFromSurgeon", "robot", "surgeon"),
        ("ToolSentToCleaner", "cleaner", "robot"),
        ("ToolCleaningProgress", "cleaner", "cleaner"),
        ("ToolCleaningCompleted", "cleaner", "cleaner"),
    ],
)
def test_reviewed_recovery_receipts_bind_source_best_slot_and_destination(
    event_type: str,
    destination: str,
    source_location: str,
) -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", source_location),
            _item("T03", "T03#2", "tray"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )

    assert tracker.apply_semantic_event(
        instrument_id="T03",
        instance_id="",
        location=destination,
        confidence=1.0,
        event_type=event_type,
        timestamp_sec=1.0,
    )
    _advance_commit_after_terminal_evidence(tracker)

    beliefs = _by_id(tracker, 1.0)
    assert beliefs["T03#1"]["committed_location_id"] == destination
    assert beliefs["T03#2"]["committed_location_id"] == "tray"


def test_terminal_command_is_retired_and_never_beats_new_active_event_binding() -> None:
    tracker = BeliefTracker(
        [
            _item("T03", "T03#1", "tray"),
            _item("T03", "T03#2", "tray"),
        ],
        config=TrackerConfig(exchangeable_instances=True),
    )
    assert tracker.register_command(
        "old-command",
        "T03",
        "T03#1",
        "tray",
        "surgeon",
        0.0,
    )
    assert tracker.apply_skill_status(
        "old-command",
        "completed",
        True,
        1.0,
        1.0,
    )
    assert "old-command" not in tracker._commands
    assert tracker.register_command(
        "new-command",
        "T03",
        "T03#2",
        "tray",
        "surgeon",
        2.0,
    )

    assert tracker.apply_semantic_event(
        instrument_id="T03",
        instance_id="",
        location="surgeon",
        confidence=1.0,
        event_type="ToolHandoverCompleted",
        timestamp_sec=2.1,
    )

    beliefs = _by_id(tracker, 2.1)
    assert "event:ToolHandoverCompleted" in beliefs["T03#2"]["evidence_sources"]
    assert "event:ToolHandoverCompleted" not in beliefs["T03#1"]["evidence_sources"]
    assert tracker.register_command(
        "old-command",
        "T03",
        "T03#1",
        "tray",
        "surgeon",
        3.0,
    )
    assert "old-command" not in tracker._commands
    assert beliefs["T03#1"]["active_command_id"] == ""


def test_robot_motion_does_not_invent_occlusion_without_geometry() -> None:
    tracker = BeliefTracker([_item("T03", "T03#1", "field")])
    tracker.set_robot_motion(True, 0.0, "cmd")

    belief = _by_id(tracker, 5.0)["T03#1"]

    assert belief["status"] == "confirmed"
    assert belief["motion_mode"] == "active"
    assert "robot_motion_negative_scaled" in belief["status_flags"]


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("duplicate_suppressed", ""),
        ("rejected", "goal_rejected"),
        ("dispatch_failed", "action_server_unavailable"),
        ("fault", "dispatch_failed:transport"),
    ],
)
def test_predispatch_outcomes_keep_source_and_motion_idle(state, reason) -> None:
    tracker = BeliefTracker([_item()])
    tracker.register_command("cmd", "T04", "T04#1", "tray", "surgeon", 0.0)

    tracker.apply_skill_status("cmd", state, False, 0.0, 0.1, reason)

    belief = _by_id(tracker, 0.1)["T04#1"]
    assert belief["locations"]["tray"] > 0.95
    assert belief["motion_mode"] == "idle"
    assert belief["active_command_id"] == ""


def test_failure_after_acceptance_keeps_provisional_source_robot_unknown_split() -> None:
    tracker = BeliefTracker([_item()])
    tracker.register_command("cmd", "T04", "T04#1", "tray", "surgeon", 0.0)
    tracker.apply_skill_status("cmd", "accepted", False, 0.0, 0.1)
    tracker.apply_skill_status("cmd", "unknown", False, 0.2, 0.2, "controller_lost")

    belief = _by_id(tracker, 0.2)["T04#1"]
    assert belief["locations"]["robot"] > 0.10
    assert belief["locations"]["unknown"] > 0.10
    assert belief["motion_mode"] == "grace"


def test_repeated_active_task_registration_preserves_execution_started() -> None:
    tracker = BeliefTracker([_item()])
    tracker.register_command("cmd", "T04", "T04#1", "tray", "surgeon", 0.0)
    tracker.apply_skill_status("cmd", "accepted", False, 0.0, 0.1)

    assert tracker.register_command(
        "cmd",
        "T04",
        "T04#1",
        "tray",
        "surgeon",
        0.2,
    )
    tracker.apply_skill_status("cmd", "busy", False, 0.2, 0.3)

    belief = _by_id(tracker, 0.3)["T04#1"]
    assert belief["motion_mode"] == "active"
    assert belief["active_command_id"] == "cmd"


def test_goal_response_unavailable_is_not_treated_as_known_no_dispatch() -> None:
    tracker = BeliefTracker([_item()])
    tracker.register_command("cmd", "T04", "T04#1", "tray", "surgeon", 0.0)

    tracker.apply_skill_status(
        "cmd",
        "unknown",
        False,
        0.0,
        0.1,
        "goal_response_unavailable",
    )

    belief = _by_id(tracker, 0.1)["T04#1"]
    assert belief["locations"]["tray"] < 0.90
    assert belief["locations"]["robot"] > 0.10
    assert belief["locations"]["unknown"] > 0.10


def test_audit_class_names_are_bounded_while_total_count_remains_exact() -> None:
    tracker = BeliefTracker([_item()])

    for index in range(MAX_IGNORED_CLASS_NAMES + 10):
        tracker.note_ignored_class(f"external-class-{index}")

    assert tracker.ignored_out_of_inventory_count == MAX_IGNORED_CLASS_NAMES + 10
    assert len(tracker.ignored_class_names) == MAX_IGNORED_CLASS_NAMES


def test_unfinished_command_evidence_is_bounded() -> None:
    tracker = BeliefTracker([_item()])

    for index in range(MAX_COMMAND_EVIDENCE + 10):
        assert tracker.register_command(
            f"cmd-{index}",
            "T04",
            "T04#1",
            "tray",
            "surgeon",
            float(index),
        )

    assert len(tracker._commands) == MAX_COMMAND_EVIDENCE
    assert "cmd-0" not in tracker._commands
    assert f"cmd-{MAX_COMMAND_EVIDENCE + 9}" in tracker._commands
