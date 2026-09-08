import pytest

from tool_belief_tracker.core import TrackerConfig
from tool_belief_tracker.parameters import validated_runtime_update


def _update(updates):
    return validated_runtime_update(TrackerConfig(), 0.2, 1.0, updates)


def test_dynamic_parameters_apply_atomically_when_valid() -> None:
    config, publish_period, health_freshness = _update(
        {
            "positive_gain": 4.0,
            "max_camera_evidence_dt_sec": 0.7,
            "miss_half_life_sec": 5.0,
            "robot_motion_positive_scale": 0.4,
            "robot_motion_negative_scale": 0.05,
            "mayo_hand_positive_scale": 0.2,
            "mayo_hand_negative_scale": 0.04,
            "mayo_hand_grace_sec": 0.6,
            "commit_dwell_sec": 0.7,
            "uv_memory_sec": 1.2,
            "uv_match_radius_px": 80.0,
            "uv_ambiguity_margin_px": 20.0,
            "uv_relabel_scale": 0.35,
            "uv_ambiguous_evidence_scale": 0.1,
            "publish_period_sec": 0.1,
            "health_freshness_sec": 2.0,
        }
    )

    assert config.positive_gain == 4.0
    assert config.max_camera_evidence_dt_sec == 0.7
    assert config.miss_half_life_sec == 5.0
    assert config.robot_motion_positive_scale == 0.4
    assert config.robot_motion_negative_scale == 0.05
    assert config.mayo_hand_positive_scale == 0.2
    assert config.mayo_hand_negative_scale == 0.04
    assert config.mayo_hand_grace_sec == 0.6
    assert config.commit_dwell_sec == 0.7
    assert config.uv_memory_sec == 1.2
    assert config.uv_match_radius_px == 80.0
    assert config.uv_ambiguity_margin_px == 20.0
    assert config.uv_relabel_scale == 0.35
    assert config.uv_ambiguous_evidence_scale == 0.1
    assert publish_period == 0.1
    assert health_freshness == 2.0


def test_enabled_is_a_runtime_boolean_parameter() -> None:
    config, publish_period, health_freshness = _update({"enabled": False})
    assert config == TrackerConfig()
    assert publish_period == 0.2
    assert health_freshness == 1.0

    for invalid in (0, 1, "false", None):
        with pytest.raises(ValueError, match="enabled must be boolean"):
            _update({"enabled": invalid})


def test_exchangeable_instances_is_a_hot_tunable_boolean() -> None:
    config, publish_period, health_freshness = _update(
        {"exchangeable_instances": True}
    )
    assert config.exchangeable_instances is True
    assert publish_period == 0.2
    assert health_freshness == 1.0

    for invalid in (0, 1, "true", None):
        with pytest.raises(
            ValueError,
            match="exchangeable_instances must be boolean",
        ):
            _update({"exchangeable_instances": invalid})


def test_mayo_human_return_policy_is_a_hot_tunable_boolean() -> None:
    config, publish_period, health_freshness = _update(
        {"mayo_appearance_assumes_surgeon_return": False}
    )
    assert config.mayo_appearance_assumes_surgeon_return is False
    assert publish_period == 0.2
    assert health_freshness == 1.0

    for invalid in (0, 1, "false", None):
        with pytest.raises(
            ValueError,
            match="mayo_appearance_assumes_surgeon_return must be boolean",
        ):
            _update({"mayo_appearance_assumes_surgeon_return": invalid})


@pytest.mark.parametrize(
    "updates",
    [
        {"miss_half_life_sec": 0.0},
        {"confirm_threshold": 0.4, "probable_threshold": 0.7},
        {"robot_motion_negative_scale": 1.5},
        {"mayo_hand_positive_scale": -0.1},
        {"max_camera_evidence_dt_sec": 0.01},
        {"commit_dwell_sec": 0.0},
        {"uv_memory_sec": 0.0},
        {"uv_match_radius_px": 20.0, "uv_ambiguity_margin_px": 21.0},
        {"uv_relabel_scale": 1.1},
        {"uv_ambiguous_evidence_scale": -0.1},
        {"publish_period_sec": 0.0},
        {"health_freshness_sec": 100.0},
    ],
)
def test_invalid_dynamic_batch_is_rejected(updates) -> None:
    current = TrackerConfig()
    with pytest.raises(ValueError):
        validated_runtime_update(current, 0.2, 1.0, updates)
    assert current == TrackerConfig()


def test_subscription_topology_is_restart_required() -> None:
    with pytest.raises(ValueError, match="restart required"):
        _update({"cam3_pose_topic": "/different"})

    with pytest.raises(ValueError, match="restart required"):
        _update({"spec_root": "/different/specs"})

    with pytest.raises(ValueError, match="restart required"):
        _update({"bundle_snapshot_root": "/different/snapshots"})


def test_spec_dir_is_a_validated_reload_parameter() -> None:
    config, publish_period, health_freshness = _update({"spec_dir": "/other"})
    assert config == TrackerConfig()
    assert publish_period == 0.2
    assert health_freshness == 1.0

    with pytest.raises(ValueError, match="non-empty string"):
        _update({"spec_dir": ""})
