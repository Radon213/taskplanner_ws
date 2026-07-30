from simulation_runtime.integration_preflight import evaluate_readiness


def _snapshot(**overrides):
    values = {
        "sentence_publisher_count": 1,
        "require_sentence_publisher": True,
        "skill_server_ready": True,
        "suction_server_ready": True,
        "retraction_server_ready": True,
        "require_perception": False,
        "rfdetr_health": None,
        "rfdetr_age_sec": -1.0,
        "perception_max_age_sec": 3.0,
    }
    values.update(overrides)
    return evaluate_readiness(**values)


def test_sentence_only_runtime_can_start_without_perception() -> None:
    result = _snapshot()
    assert result["ready"] is True
    assert result["missing"] == []


def test_missing_sentence_publisher_fails_closed() -> None:
    result = _snapshot(sentence_publisher_count=0)
    assert result["ready"] is False
    assert result["missing"] == ["sentence_topic"]


def test_missing_hardware_action_server_fails_closed() -> None:
    result = _snapshot(skill_server_ready=False)
    assert result["ready"] is False
    assert "skill_action_server" in result["missing"]


def test_real_vlm_runtime_requires_fresh_aligned_perception() -> None:
    result = _snapshot(
        require_perception=True,
        rfdetr_health={
            "connected": True,
            "status": "ready",
            "cam4_aligned": True,
        },
        rfdetr_age_sec=0.2,
    )
    assert result["ready"] is True

    stale = _snapshot(
        require_perception=True,
        rfdetr_health={
            "connected": True,
            "status": "ready",
            "cam4_aligned": True,
        },
        rfdetr_age_sec=5.0,
    )
    assert stale["ready"] is False
    assert stale["missing"] == ["perception"]
