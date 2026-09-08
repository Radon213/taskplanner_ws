import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import rclpy

import integration_debug.rosbag_recorder as recorder_module
from integration_debug.rosbag_recorder import (
    ManualRosbagRecorder,
    SURGIMATE_INPUT_TOPICS,
    SURGIMATE_UI_AUDIT_TOPIC,
    UI_AUDIT_TOPIC,
    build_record_command,
    create_recording_session,
    session_manifest,
    ui_replay_contract,
    write_private_json,
)


class _FakeRecorderProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 999_999

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return self.returncode


@pytest.fixture
def ros_context():
    rclpy.init()
    try:
        yield
    finally:
        rclpy.shutdown()


def test_all_graph_command_preserves_ui_replay_inputs(tmp_path) -> None:
    command = build_record_command(
        bag_dir=tmp_path / "recording",
        recorder_node_name="taskplanner_rosbag_recorder_1234abcd",
        max_bag_bytes=4096,
        max_cache_bytes=1024,
    )

    assert command[:3] == ["ros2", "bag", "record"]
    assert command[command.index("--storage") + 1] == "mcap"
    assert "--all" in command
    assert "--all-services" in command
    assert "--include-hidden-topics" in command
    assert "--include-unpublished-topics" in command
    assert command[command.index("--storage-preset-profile") + 1] == "zstd_fast"
    assert "--disable-keyboard-controls" in command
    assert "scenario" not in " ".join(command).lower()
    assert command[command.index("--output") + 1] == str(tmp_path / "recording")


@pytest.mark.parametrize(
    ("max_bag_bytes", "max_cache_bytes"),
    [(0, 1), (1, 0), (-1, 1), (1, -1)],
)
def test_command_rejects_non_positive_storage_limits(tmp_path, max_bag_bytes, max_cache_bytes) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        build_record_command(
            bag_dir=tmp_path / "recording",
            recorder_node_name="taskplanner_rosbag_recorder_1234abcd",
            max_bag_bytes=max_bag_bytes,
            max_cache_bytes=max_cache_bytes,
        )


def test_manual_session_is_private_and_manifest_names_both_ui_surfaces(tmp_path) -> None:
    output_root = tmp_path / "rosbag2"
    session = create_recording_session(
        output_root,
        host_output_root="/home/operator/.local/state/taskplanner/rosbag2",
        now=datetime(2026, 9, 3, 1, 2, 3, tzinfo=timezone.utc),
    )
    command = build_record_command(
        bag_dir=session.bag_dir,
        recorder_node_name="taskplanner_rosbag_recorder_1234abcd",
        max_bag_bytes=4096,
        max_cache_bytes=1024,
    )
    manifest = session_manifest(
        session,
        command,
        state="recording",
        recording_active=True,
        message="manual recorder started",
    )
    write_private_json(session.manifest_path, manifest)

    assert stat.S_IMODE(output_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(session.session_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(session.manifest_path.stat().st_mode) == 0o600
    assert session.displayed_output_dir.startswith(
        "/home/operator/.local/state/taskplanner/rosbag2/"
    )
    assert json.loads(session.manifest_path.read_text(encoding="utf-8")) == manifest
    assert manifest["ui_replay"]["surfaces"]["taskplanner_4173"]["operator_audit"] == UI_AUDIT_TOPIC
    assert manifest["ui_replay"]["surfaces"]["surgimate_5174"]["operator_audit"] == (
        SURGIMATE_UI_AUDIT_TOPIC
    )
    assert manifest["ui_replay"]["surfaces"]["surgimate_5174"]["input_topics"] == list(
        SURGIMATE_INPUT_TOPICS
    )


def test_ui_replay_contract_is_honest_about_service_introspection() -> None:
    contract = ui_replay_contract()

    assert contract["mode"] == "state_faithful_ros_replay"
    assert contract["screen_video"] is False
    assert contract["captured"]["camera_and_compressed_video"] is True
    assert contract["captured"]["hidden_topics"] is True
    assert contract["captured"]["action_feedback_and_status"] is True
    assert contract["captured"]["ui_audit_topics"] == {
        "taskplanner_4173": UI_AUDIT_TOPIC,
        "surgimate_5174": SURGIMATE_UI_AUDIT_TOPIC,
    }
    assert "introspection" in contract["captured"]["service_event_topics"]
    assert any("rosbagReplay=1" in item for item in contract["requirements"])
    assert any("cannot recover raw service" in item for item in contract["limits"])


def test_private_json_replaces_an_existing_manifest_atomically(tmp_path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"old": true}\n', encoding="utf-8")
    manifest.chmod(0o644)

    write_private_json(manifest, {"new": True})

    assert json.loads(manifest.read_text(encoding="utf-8")) == {"new": True}
    assert stat.S_IMODE(os.stat(manifest).st_mode) == 0o600
    assert not manifest.with_suffix(".json.tmp").exists()


def test_manual_recorder_finalizes_a_private_mcap_session_without_scenario_coupling(
    tmp_path,
    monkeypatch,
    ros_context,
) -> None:
    launched: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        launched.append(list(command))
        bag_dir = Path(command[command.index("--output") + 1])
        bag_dir.mkdir(parents=True)
        (bag_dir / "metadata.yaml").write_text("rosbag2_bagfile_information: {}\n", encoding="utf-8")
        return _FakeRecorderProcess()

    monkeypatch.setattr(recorder_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(recorder_module.os, "killpg", lambda _pid, _signal: None)
    node = ManualRosbagRecorder(
        popen=fake_popen,
        disk_usage=lambda _path: SimpleNamespace(free=1024 * 1024),
    )
    try:
        node._output_root = tmp_path / "recordings"
        node._host_output_root = str(tmp_path / "recordings")
        node._min_free_bytes = 1

        started, _ = node._start()
        saved, _ = node._stop(reason="test stop")

        assert started is True
        assert saved is True
        assert len(launched) == 1
        assert "--all" in launched[0]
        assert "--all-services" in launched[0]
        assert "scenario" not in " ".join(launched[0]).lower()
        assert node._last_status["state"] == "saved"
        assert node._session is not None
        manifest = json.loads(node._session.manifest_path.read_text(encoding="utf-8"))
        assert manifest["state"] == "saved"
        assert manifest["metadata_present"] is True
    finally:
        node.destroy_node()
