from types import SimpleNamespace

from tool_belief_tracker.tune import (
    load_ros_parameter_mapping,
    request_atomic_update,
)


def test_tuning_yaml_is_extracted_for_target_node(tmp_path) -> None:
    path = tmp_path / "tuning.yaml"
    path.write_text(
        "tool_belief_tracker:\n"
        "  ros__parameters:\n"
        "    positive_gain: 4.0\n"
        "    miss_half_life_sec: 5.0\n",
        encoding="utf-8",
    )

    assert load_ros_parameter_mapping(path, "/tool_belief_tracker") == {
        "positive_gain": 4.0,
        "miss_half_life_sec": 5.0,
    }


def test_tuning_client_uses_one_atomic_service_request() -> None:
    calls = []

    class Client:
        def set_parameters_atomically(self, parameters):
            calls.append(parameters)
            return object()

    response = SimpleNamespace(
        result=SimpleNamespace(successful=True, reason=""),
    )
    result = request_atomic_update(
        Client(),
        {"positive_gain": 4.0, "miss_half_life_sec": 5.0},
        lambda _future: response,
    )

    assert result.successful is True
    assert len(calls) == 1
    assert {parameter.name for parameter in calls[0]} == {
        "positive_gain",
        "miss_half_life_sec",
    }


def test_tuning_yaml_rejects_wrong_or_empty_node_section(tmp_path) -> None:
    wrong = tmp_path / "wrong.yaml"
    wrong.write_text(
        "another_node:\n"
        "  ros__parameters:\n"
        "    positive_gain: 4.0\n",
        encoding="utf-8",
    )
    empty = tmp_path / "empty.yaml"
    empty.write_text(
        "tool_belief_tracker:\n"
        "  ros__parameters: {}\n",
        encoding="utf-8",
    )

    for path in (wrong, empty):
        try:
            load_ros_parameter_mapping(path, "/tool_belief_tracker")
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid tuning file: {path}")
