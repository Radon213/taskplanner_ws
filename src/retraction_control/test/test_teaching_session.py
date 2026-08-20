import json

import pytest

from retraction_control.adapters import ForceTorqueSample, JointStateSample
from retraction_control.teaching_session import (
    SessionIntegrityError,
    SessionProfileMismatchError,
    TeachingSession,
    TeachingSessionError,
    TeachingSessionMetadata,
    TeachingSessionRecorder,
    TeachingSessionRepository,
)


def _metadata(session_id: str = "teach-command-1") -> TeachingSessionMetadata:
    return TeachingSessionMetadata(
        session_id=session_id,
        created_at_ns=1_000,
        profile_name="synthetic",
        profile_version="1.0.0",
        profile_checksum="sha256:" + "a" * 64,
        robot_id="robot-test",
        controller_id="controller-test",
        source_revision="test-revision",
        calibration={"approved": True, "sensor": "cal-test"},
    )


def _session(session_id: str = "teach-command-1") -> TeachingSession:
    recorder = TeachingSessionRecorder(_metadata(session_id))
    recorder.record_pair(
        JointStateSample(10, "arm_1", (0.1, 0.2, 0.3)),
        ForceTorqueSample(
            11,
            "sensor_1",
            (1.0, 2.0, 3.0),
            (0.1, 0.2, 0.3),
            calibration_id="cal-test",
        ),
    )
    recorder.record_pair(
        JointStateSample(20, "arm_1", (0.4, 0.5, 0.6)),
        ForceTorqueSample(
            21,
            "sensor_1",
            (4.0, 5.0, 6.0),
            (0.4, 0.5, 0.6),
            calibration_id="cal-test",
        ),
    )
    return recorder.finish(
        completed_at_ns=2_000,
        target_joint_positions={"arm_1": (0.4, 0.5, 0.6)},
        target_force_n={"sensor_1": (4.0, 5.0, 6.0)},
    )


def test_session_round_trip_is_directory_atomic_and_checksum_bound(tmp_path):
    repository = TeachingSessionRepository(tmp_path / "sessions")
    original = _session()

    path = repository.save(original)

    assert sorted(item.name for item in path.iterdir()) == [
        "force_samples.csv",
        "joint_samples.csv",
        "manifest.json",
    ]
    assert not list(repository.root.glob(".*.tmp-*"))
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["normally_completed"] is True
    assert manifest["joint_sample_count"] == 2
    assert manifest["force_sample_count"] == 2
    assert len(manifest["manifest_sha256"]) == 64

    loaded = repository.load(
        original.session_id,
        expected_profile_name="synthetic",
        expected_profile_checksum="sha256:" + "a" * 64,
    )

    assert loaded == original
    assert loaded.target_joint_positions["arm_1"] == (0.4, 0.5, 0.6)
    assert loaded.target_force_n["sensor_1"] == (4.0, 5.0, 6.0)


def test_sample_corruption_is_detected_before_csv_is_trusted(tmp_path):
    repository = TeachingSessionRepository(tmp_path / "sessions")
    path = repository.save(_session())
    sample_path = path / "force_samples.csv"
    sample_path.write_bytes(sample_path.read_bytes() + b"corruption\n")

    with pytest.raises(SessionIntegrityError) as raised:
        repository.load("teach-command-1")

    assert raised.value.code == "sample_checksum_mismatch"


def test_manifest_corruption_and_profile_mismatch_fail_closed(tmp_path):
    repository = TeachingSessionRepository(tmp_path / "sessions")
    path = repository.save(_session())

    with pytest.raises(SessionProfileMismatchError) as mismatch:
        repository.load(
            "teach-command-1",
            expected_profile_name="another-profile",
            expected_profile_checksum="sha256:" + "a" * 64,
        )
    assert mismatch.value.code == "profile_mismatch"

    manifest_path = path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["profile"]["name"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SessionIntegrityError) as corrupted:
        repository.load("teach-command-1")
    assert corrupted.value.code == "manifest_checksum_mismatch"


def test_duplicate_session_never_overwrites_committed_data(tmp_path):
    repository = TeachingSessionRepository(tmp_path / "sessions")
    session = _session()
    path = repository.save(session)
    original_manifest = (path / "manifest.json").read_bytes()

    with pytest.raises(TeachingSessionError) as duplicate:
        repository.save(session)

    assert duplicate.value.code == "session_exists"
    assert (path / "manifest.json").read_bytes() == original_manifest


def test_recorder_rejects_non_monotonic_samples_and_post_finish_writes():
    recorder = TeachingSessionRecorder(_metadata())
    recorder.record_joint(JointStateSample(20, "arm_1", (0.0,)))
    with pytest.raises(TeachingSessionError) as non_monotonic:
        recorder.record_joint(JointStateSample(19, "arm_1", (0.0,)))
    assert non_monotonic.value.code == "non_monotonic_samples"

    recorder.record_force(
        ForceTorqueSample(20, "sensor_1", (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    )
    recorder.finish(
        completed_at_ns=2_000,
        target_joint_positions={"arm_1": (0.0,)},
        target_force_n={"sensor_1": (0.0, 0.0, 0.0)},
    )
    with pytest.raises(TeachingSessionError) as closed:
        recorder.record_joint(JointStateSample(21, "arm_1", (0.0,)))
    assert closed.value.code == "recorder_closed"
