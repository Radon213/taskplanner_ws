from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_execute_skill_action_carries_physical_instance_identity() -> None:
    action = (
        ROOT
        / "surgical_msgs"
        / "action"
        / "ExecuteSkill.action"
    ).read_text(encoding="utf-8")
    bridge = (
        ROOT
        / "skill_execution"
        / "skill_execution"
        / "bridge.py"
    ).read_text(encoding="utf-8")

    assert "string instrument_instance_id" in action
    assert (
        "goal.instrument_instance_id = command.instrument_instance_id"
        in bridge
    )


def test_mock_events_preserve_instance_identity() -> None:
    mock_server = (
        ROOT
        / "skill_execution"
        / "skill_execution"
        / "mock_server.py"
    ).read_text(encoding="utf-8")

    assert "event.instance_id = kwargs.get(" in mock_server
    assert 'detail.setdefault("instrument_instance_id", event.instance_id)' in (
        mock_server
    )
