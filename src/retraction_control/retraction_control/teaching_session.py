"""Atomic, checksummed persistence for direct-teaching sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from .adapters import ForceTorqueSample, JointStateSample
from .target_planner import TargetPlannerIdentity


SESSION_SCHEMA_VERSION = 2
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_JOINT_FILE = "joint_samples.csv"
_FORCE_FILE = "force_samples.csv"
_MANIFEST_FILE = "manifest.json"


class TeachingSessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


class SessionValidationError(TeachingSessionError):
    pass


class SessionIntegrityError(TeachingSessionError):
    pass


class SessionProfileMismatchError(SessionValidationError):
    pass


def _validate_session_id(session_id: str) -> str:
    value = str(session_id).strip()
    if not _SESSION_ID_RE.fullmatch(value) or value in {".", ".."}:
        raise SessionValidationError(
            "invalid_session_id",
            "session_id must be a path-safe identifier of at most 128 characters",
        )
    return value


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise SessionValidationError(
            "missing_metadata", f"{field_name} must not be empty"
        )
    return normalized


def _finite_values(values: Sequence[float], field_name: str) -> tuple[float, ...]:
    normalized = tuple(float(value) for value in values)
    if not normalized or not all(math.isfinite(value) for value in normalized):
        raise SessionValidationError(
            "invalid_sample", f"{field_name} must contain finite values"
        )
    return normalized


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionValidationError(
                "invalid_metadata", "metadata must not contain non-finite numbers"
            )
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    raise SessionValidationError(
        "invalid_metadata",
        f"metadata value of type {type(value).__name__} is not JSON serializable",
    )


def _canonical_json(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                _json_safe(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SessionValidationError(
            "invalid_metadata", f"could not serialize session metadata: {exc}"
        ) from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    # Normalize through JSON first, then recursively freeze nested containers.
    normalized = json.loads(_canonical_json(value).decode("utf-8"))
    return _deep_freeze(normalized)


@dataclass(frozen=True, slots=True)
class TeachingSessionMetadata:
    session_id: str
    created_at_ns: int
    profile_name: str
    profile_version: str
    profile_checksum: str
    robot_id: str
    controller_id: str
    source_revision: str
    target_planner: Mapping[str, Any]
    calibration: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _validate_session_id(self.session_id))
        if int(self.created_at_ns) < 0:
            raise SessionValidationError(
                "invalid_timestamp", "created_at_ns must be non-negative"
            )
        for name in (
            "profile_name",
            "profile_version",
            "profile_checksum",
            "robot_id",
            "controller_id",
            "source_revision",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        try:
            planner = TargetPlannerIdentity.from_mapping(self.target_planner)
        except (TypeError, ValueError) as exc:
            raise SessionValidationError(
                "invalid_target_planner", f"invalid target planner identity: {exc}"
            ) from exc
        object.__setattr__(self, "target_planner", _freeze_mapping(planner.as_dict()))
        object.__setattr__(self, "calibration", _freeze_mapping(self.calibration))


@dataclass(frozen=True, slots=True)
class TeachingSession:
    metadata: TeachingSessionMetadata
    completed_at_ns: int
    joint_samples: tuple[JointStateSample, ...]
    force_samples: tuple[ForceTorqueSample, ...]
    target_joint_positions: Mapping[str, tuple[float, ...]]
    target_force_n: Mapping[str, tuple[float, ...]]
    normally_completed: bool = True

    def __post_init__(self) -> None:
        if int(self.completed_at_ns) < int(self.metadata.created_at_ns):
            raise SessionValidationError(
                "invalid_timestamp",
                "completed_at_ns must not precede created_at_ns",
            )
        joints = tuple(self.joint_samples)
        forces = tuple(self.force_samples)
        _validate_monotonic_samples(joints, "joint")
        _validate_monotonic_samples(forces, "force")
        targets = {
            str(arm_id): _finite_values(positions, "target_joint_positions")
            for arm_id, positions in self.target_joint_positions.items()
        }
        target_force = {
            str(sensor_id): _finite_values(values, "target_force_n")
            for sensor_id, values in self.target_force_n.items()
        }
        object.__setattr__(self, "joint_samples", joints)
        object.__setattr__(self, "force_samples", forces)
        object.__setattr__(self, "target_joint_positions", MappingProxyType(targets))
        object.__setattr__(self, "target_force_n", MappingProxyType(target_force))

    @property
    def session_id(self) -> str:
        return self.metadata.session_id

    @property
    def sample_start_ns(self) -> int | None:
        timestamps = [sample.timestamp_ns for sample in self.joint_samples]
        timestamps.extend(sample.timestamp_ns for sample in self.force_samples)
        return min(timestamps) if timestamps else None

    @property
    def sample_end_ns(self) -> int | None:
        timestamps = [sample.timestamp_ns for sample in self.joint_samples]
        timestamps.extend(sample.timestamp_ns for sample in self.force_samples)
        return max(timestamps) if timestamps else None

    def validate_complete(self) -> None:
        if not self.normally_completed:
            raise SessionValidationError(
                "incomplete_session", "teaching session was not normally completed"
            )
        if not self.joint_samples or not self.force_samples:
            raise SessionValidationError(
                "insufficient_samples",
                "a valid teaching session requires joint and force samples",
            )
        if any(not sample.valid for sample in self.force_samples):
            raise SessionValidationError(
                "invalid_sample",
                "a complete teaching session cannot contain invalid force samples",
            )
        if not self.target_joint_positions or not self.target_force_n:
            raise SessionValidationError(
                "missing_targets",
                "a valid teaching session requires computed joint and force targets",
            )

    def validate_for_profile(
        self,
        profile_name: str,
        profile_checksum: str,
        target_planner_checksum: str | None = None,
    ) -> None:
        self.validate_complete()
        if self.metadata.profile_name != str(profile_name):
            raise SessionProfileMismatchError(
                "profile_mismatch",
                f"session profile {self.metadata.profile_name!r} does not match "
                f"{profile_name!r}",
            )
        if self.metadata.profile_checksum != str(profile_checksum):
            raise SessionProfileMismatchError(
                "profile_checksum_mismatch",
                "session profile checksum does not match the active profile",
            )
        if target_planner_checksum is not None and (
            self.metadata.target_planner["checksum"] != str(target_planner_checksum)
        ):
            raise SessionProfileMismatchError(
                "target_planner_checksum_mismatch",
                "session target planner checksum does not match the active planner",
            )


def _validate_monotonic_samples(samples: Sequence[Any], label: str) -> None:
    by_source: dict[str, int] = {}
    source_field = "arm_id" if label == "joint" else "sensor_id"
    for sample in samples:
        source = str(getattr(sample, source_field))
        timestamp_ns = int(sample.timestamp_ns)
        previous = by_source.get(source)
        if previous is not None and timestamp_ns < previous:
            raise SessionValidationError(
                "non_monotonic_samples",
                f"{label} sample timestamps for {source!r} moved backwards",
            )
        by_source[source] = timestamp_ns


class TeachingSessionRecorder:
    """In-memory recorder finalized exactly once into an immutable session."""

    def __init__(self, metadata: TeachingSessionMetadata) -> None:
        self.metadata = metadata
        self._joint_samples: list[JointStateSample] = []
        self._force_samples: list[ForceTorqueSample] = []
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def joint_samples(self) -> tuple[JointStateSample, ...]:
        return tuple(self._joint_samples)

    @property
    def force_samples(self) -> tuple[ForceTorqueSample, ...]:
        return tuple(self._force_samples)

    def _ensure_open(self) -> None:
        if self._finished:
            raise SessionValidationError(
                "recorder_closed", "teaching session recorder is already finalized"
            )

    def record_joint(self, sample: JointStateSample) -> None:
        self._ensure_open()
        _validate_monotonic_samples((*self._joint_samples, sample), "joint")
        self._joint_samples.append(sample)

    def record_force(self, sample: ForceTorqueSample) -> None:
        self._ensure_open()
        if not sample.valid:
            raise SessionValidationError(
                "invalid_sample", "invalid force samples cannot be recorded"
            )
        _validate_monotonic_samples((*self._force_samples, sample), "force")
        self._force_samples.append(sample)

    def record_pair(
        self, joint: JointStateSample, force: ForceTorqueSample
    ) -> None:
        self.record_joint(joint)
        try:
            self.record_force(force)
        except Exception:
            self._joint_samples.pop()
            raise

    def finish(
        self,
        *,
        completed_at_ns: int,
        target_joint_positions: Mapping[str, Sequence[float]],
        target_force_n: Mapping[str, Sequence[float]],
        normally_completed: bool = True,
    ) -> TeachingSession:
        self._ensure_open()
        session = TeachingSession(
            metadata=self.metadata,
            completed_at_ns=int(completed_at_ns),
            joint_samples=tuple(self._joint_samples),
            force_samples=tuple(self._force_samples),
            target_joint_positions={
                str(key): tuple(float(value) for value in values)
                for key, values in target_joint_positions.items()
            },
            target_force_n={
                str(key): tuple(float(value) for value in values)
                for key, values in target_force_n.items()
            },
            normally_completed=bool(normally_completed),
        )
        if normally_completed:
            session.validate_complete()
        self._finished = True
        return session

    def abort(self) -> None:
        self._finished = True


class TeachingSessionRepository:
    """Commit a whole session directory by one atomic rename."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("teaching session data directory must be absolute")

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise TeachingSessionError(
                "data_directory_symlink",
                f"teaching session data directory must not be a symlink: {self.root}",
            )
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir() or self.root.is_symlink():
            raise TeachingSessionError(
                "data_directory_invalid", f"not a directory: {self.root}"
            )

    def session_path(self, session_id: str) -> Path:
        return self.root / _validate_session_id(session_id)

    def save(self, session: TeachingSession) -> Path:
        session.validate_complete()
        self._ensure_root()
        final_path = self.session_path(session.session_id)
        if final_path.exists():
            raise TeachingSessionError(
                "session_exists", f"teaching session already exists: {session.session_id}"
            )

        joint_payload = _joint_csv(session.joint_samples)
        force_payload = _force_csv(session.force_samples)
        manifest = _manifest_dict(session, joint_payload, force_payload)
        manifest_payload = _canonical_json(manifest)

        temporary = Path(
            tempfile.mkdtemp(prefix=f".{session.session_id}.tmp-", dir=self.root)
        )
        committed = False
        try:
            _write_synced(temporary / _JOINT_FILE, joint_payload)
            _write_synced(temporary / _FORCE_FILE, force_payload)
            _write_synced(temporary / _MANIFEST_FILE, manifest_payload)
            _fsync_directory(temporary)
            try:
                os.rename(temporary, final_path)
            except FileExistsError as exc:
                raise TeachingSessionError(
                    "session_exists",
                    f"teaching session already exists: {session.session_id}",
                ) from exc
            committed = True
            _fsync_directory(self.root)
            return final_path
        except TeachingSessionError:
            raise
        except OSError as exc:
            raise TeachingSessionError(
                "session_write_failed",
                f"could not atomically save teaching session: {exc}",
            ) from exc
        finally:
            if not committed and temporary.exists():
                shutil.rmtree(temporary)

    def load(
        self,
        session_id: str,
        *,
        expected_profile_name: str | None = None,
        expected_profile_checksum: str | None = None,
        expected_target_planner_checksum: str | None = None,
    ) -> TeachingSession:
        path = self.session_path(session_id)
        if not path.is_dir() or path.is_symlink():
            raise TeachingSessionError(
                "session_not_found", f"teaching session not found: {session_id}"
            )
        try:
            manifest_payload = (path / _MANIFEST_FILE).read_bytes()
            joint_payload = (path / _JOINT_FILE).read_bytes()
            force_payload = (path / _FORCE_FILE).read_bytes()
        except OSError as exc:
            raise SessionIntegrityError(
                "session_read_failed", f"could not read teaching session: {exc}"
            ) from exc
        try:
            manifest = json.loads(manifest_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionIntegrityError(
                "manifest_invalid", f"invalid teaching session manifest: {exc}"
            ) from exc
        _verify_manifest(manifest, joint_payload, force_payload)
        session = _session_from_payloads(manifest, joint_payload, force_payload)
        if session.session_id != _validate_session_id(session_id):
            raise SessionIntegrityError(
                "session_id_mismatch", "manifest session_id does not match its directory"
            )
        session.validate_complete()
        if (expected_profile_name is None) != (expected_profile_checksum is None):
            raise ValueError(
                "expected_profile_name and expected_profile_checksum must be provided together"
            )
        if expected_profile_name is not None and expected_profile_checksum is not None:
            session.validate_for_profile(
                expected_profile_name,
                expected_profile_checksum,
                expected_target_planner_checksum,
            )
        return session

    def latest_valid(
        self,
        *,
        expected_profile_name: str,
        expected_profile_checksum: str,
        expected_target_planner_checksum: str | None = None,
    ) -> TeachingSession:
        if not self.root.is_dir():
            raise TeachingSessionError(
                "session_not_found", "no teaching session data directory exists"
            )
        valid: list[TeachingSession] = []
        for path in self.root.iterdir():
            if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            try:
                session = self.load(
                    path.name,
                    expected_profile_name=expected_profile_name,
                    expected_profile_checksum=expected_profile_checksum,
                    expected_target_planner_checksum=expected_target_planner_checksum,
                )
            except TeachingSessionError:
                continue
            valid.append(session)
        if not valid:
            raise TeachingSessionError(
                "session_not_found",
                "no complete teaching session matches the active profile",
            )
        return max(
            valid,
            key=lambda session: (session.completed_at_ns, session.session_id),
        )


def _joint_csv(samples: Iterable[JointStateSample]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("timestamp_ns", "arm_id", "positions_rad_json"))
    for sample in samples:
        writer.writerow(
            (
                str(sample.timestamp_ns),
                sample.arm_id,
                json.dumps(list(sample.positions), separators=(",", ":")),
            )
        )
    return output.getvalue().encode("utf-8")


def _force_csv(samples: Iterable[ForceTorqueSample]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "timestamp_ns",
            "sensor_id",
            "fx_n",
            "fy_n",
            "fz_n",
            "tx_nm",
            "ty_nm",
            "tz_nm",
            "calibration_id",
            "valid",
        )
    )
    for sample in samples:
        writer.writerow(
            (
                str(sample.timestamp_ns),
                sample.sensor_id,
                *[repr(value) for value in sample.force_n],
                *[repr(value) for value in sample.torque_nm],
                sample.calibration_id,
                "1" if sample.valid else "0",
            )
        )
    return output.getvalue().encode("utf-8")


def _manifest_dict(
    session: TeachingSession, joint_payload: bytes, force_payload: bytes
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session.session_id,
        "created_at_ns": session.metadata.created_at_ns,
        "completed_at_ns": session.completed_at_ns,
        "profile": {
            "name": session.metadata.profile_name,
            "version": session.metadata.profile_version,
            "checksum": session.metadata.profile_checksum,
        },
        "robot": {
            "robot_id": session.metadata.robot_id,
            "controller_id": session.metadata.controller_id,
        },
        "source_revision": session.metadata.source_revision,
        "target_planner": dict(session.metadata.target_planner),
        "calibration": dict(session.metadata.calibration),
        "joint_sample_count": len(session.joint_samples),
        "force_sample_count": len(session.force_samples),
        "sample_start_ns": session.sample_start_ns,
        "sample_end_ns": session.sample_end_ns,
        "target_joint_positions": {
            key: list(value) for key, value in session.target_joint_positions.items()
        },
        "target_force_n": {
            key: list(value) for key, value in session.target_force_n.items()
        },
        "normally_completed": session.normally_completed,
        "files": {
            _JOINT_FILE: {"sha256": _sha256(joint_payload)},
            _FORCE_FILE: {"sha256": _sha256(force_payload)},
        },
    }
    manifest["manifest_sha256"] = _sha256(_canonical_json(manifest))
    return manifest


def _verify_manifest(
    manifest: Any, joint_payload: bytes, force_payload: bytes
) -> None:
    if not isinstance(manifest, dict):
        raise SessionIntegrityError("manifest_invalid", "manifest must be an object")
    if manifest.get("schema_version") != SESSION_SCHEMA_VERSION:
        raise SessionIntegrityError(
            "schema_version_unsupported", "unsupported teaching session schema"
        )
    checksum = manifest.get("manifest_sha256")
    without_checksum = dict(manifest)
    without_checksum.pop("manifest_sha256", None)
    if not isinstance(checksum, str) or checksum != _sha256(
        _canonical_json(without_checksum)
    ):
        raise SessionIntegrityError(
            "manifest_checksum_mismatch", "teaching session manifest was modified"
        )
    try:
        files = manifest["files"]
        expected_joint = files[_JOINT_FILE]["sha256"]
        expected_force = files[_FORCE_FILE]["sha256"]
    except (KeyError, TypeError) as exc:
        raise SessionIntegrityError(
            "manifest_invalid", "manifest does not contain sample checksums"
        ) from exc
    if expected_joint != _sha256(joint_payload):
        raise SessionIntegrityError(
            "sample_checksum_mismatch", f"{_JOINT_FILE} was modified"
        )
    if expected_force != _sha256(force_payload):
        raise SessionIntegrityError(
            "sample_checksum_mismatch", f"{_FORCE_FILE} was modified"
        )


def _session_from_payloads(
    manifest: Mapping[str, Any], joint_payload: bytes, force_payload: bytes
) -> TeachingSession:
    try:
        profile = manifest["profile"]
        robot = manifest["robot"]
        metadata = TeachingSessionMetadata(
            session_id=str(manifest["session_id"]),
            created_at_ns=int(manifest["created_at_ns"]),
            profile_name=str(profile["name"]),
            profile_version=str(profile["version"]),
            profile_checksum=str(profile["checksum"]),
            robot_id=str(robot["robot_id"]),
            controller_id=str(robot["controller_id"]),
            source_revision=str(manifest["source_revision"]),
            target_planner=manifest["target_planner"],
            calibration=manifest.get("calibration", {}),
        )
        session = TeachingSession(
            metadata=metadata,
            completed_at_ns=int(manifest["completed_at_ns"]),
            joint_samples=_parse_joint_csv(joint_payload),
            force_samples=_parse_force_csv(force_payload),
            target_joint_positions=manifest["target_joint_positions"],
            target_force_n=manifest["target_force_n"],
            normally_completed=bool(manifest["normally_completed"]),
        )
        expected_joint_count = int(manifest["joint_sample_count"])
        expected_force_count = int(manifest["force_sample_count"])
    except (KeyError, TypeError, ValueError, SessionValidationError) as exc:
        raise SessionIntegrityError(
            "manifest_invalid", f"manifest fields are invalid: {exc}"
        ) from exc
    if len(session.joint_samples) != expected_joint_count:
        raise SessionIntegrityError(
            "sample_count_mismatch", "joint sample count does not match manifest"
        )
    if len(session.force_samples) != expected_force_count:
        raise SessionIntegrityError(
            "sample_count_mismatch", "force sample count does not match manifest"
        )
    if session.sample_start_ns != manifest.get("sample_start_ns"):
        raise SessionIntegrityError(
            "sample_range_mismatch", "sample start timestamp does not match manifest"
        )
    if session.sample_end_ns != manifest.get("sample_end_ns"):
        raise SessionIntegrityError(
            "sample_range_mismatch", "sample end timestamp does not match manifest"
        )
    return session


def _parse_joint_csv(payload: bytes) -> tuple[JointStateSample, ...]:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8"), newline=""))
        if reader.fieldnames != ["timestamp_ns", "arm_id", "positions_rad_json"]:
            raise ValueError("unexpected joint CSV header")
        return tuple(
            JointStateSample(
                timestamp_ns=int(row["timestamp_ns"]),
                arm_id=row["arm_id"],
                positions=tuple(json.loads(row["positions_rad_json"])),
            )
            for row in reader
        )
    except (UnicodeDecodeError, csv.Error, KeyError, TypeError, ValueError) as exc:
        raise SessionIntegrityError(
            "joint_samples_invalid", f"invalid joint sample CSV: {exc}"
        ) from exc


def _parse_force_csv(payload: bytes) -> tuple[ForceTorqueSample, ...]:
    expected_header = [
        "timestamp_ns",
        "sensor_id",
        "fx_n",
        "fy_n",
        "fz_n",
        "tx_nm",
        "ty_nm",
        "tz_nm",
        "calibration_id",
        "valid",
    ]
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8"), newline=""))
        if reader.fieldnames != expected_header:
            raise ValueError("unexpected force CSV header")
        return tuple(
            ForceTorqueSample(
                timestamp_ns=int(row["timestamp_ns"]),
                sensor_id=row["sensor_id"],
                force_n=(float(row["fx_n"]), float(row["fy_n"]), float(row["fz_n"])),
                torque_nm=(
                    float(row["tx_nm"]),
                    float(row["ty_nm"]),
                    float(row["tz_nm"]),
                ),
                calibration_id=row["calibration_id"],
                valid=row["valid"] == "1",
            )
            for row in reader
        )
    except (UnicodeDecodeError, csv.Error, KeyError, TypeError, ValueError) as exc:
        raise SessionIntegrityError(
            "force_samples_invalid", f"invalid force sample CSV: {exc}"
        ) from exc


def _write_synced(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "SESSION_SCHEMA_VERSION",
    "SessionIntegrityError",
    "SessionProfileMismatchError",
    "SessionValidationError",
    "TeachingSession",
    "TeachingSessionError",
    "TeachingSessionMetadata",
    "TeachingSessionRecorder",
    "TeachingSessionRepository",
]
