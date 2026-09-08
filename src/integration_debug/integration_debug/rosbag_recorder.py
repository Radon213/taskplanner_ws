"""Manual, observer-only rosbag2 MCAP recording owner.

This node deliberately has no simulation, scenario, controller, or browser
authority.  It owns one explicit ``SetBool`` switch: enabling the switch
starts an all-graph ``ros2 bag record`` child process; disabling it flushes
and closes that child.  The recording window is therefore independent of any
procedure lifecycle.

The resulting MCAP is intended for state-faithful replay of the 4173
Taskplanner and 5174 SurgiMate surfaces.  It records every discoverable ROS
topic (including camera/video and hidden Action feedback/status) plus any ROS
service-event topics whose providers have introspection enabled.  ROS 2 does
not allow a passive recorder to reconstruct service/action RPC bodies when a
provider has not emitted introspection events, so that boundary is made
explicit in the session manifest instead of being silently overstated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from typing import Any, Callable, Final
from uuid import uuid4

import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from std_srvs.srv import SetBool


STATUS_SCHEMA: Final = "taskplanner.rosbag_recording.status.v1"
STATUS_TOPIC: Final = "/recording/rosbag/status"
CONTROL_SERVICE: Final = "/recording/rosbag/set_enabled"
UI_AUDIT_TOPIC: Final = "/recording/rosbag/ui_audit"
SURGIMATE_UI_AUDIT_TOPIC: Final = "/recording/rosbag/surgimate_ui_audit"

DEFAULT_OUTPUT_DIR: Final = "/taskplanner-rosbags"
DEFAULT_MIN_FREE_BYTES: Final = 20 * 1024 * 1024 * 1024
DEFAULT_MAX_BAG_BYTES: Final = 16 * 1024 * 1024 * 1024
DEFAULT_MAX_CACHE_BYTES: Final = 512 * 1024 * 1024
STARTUP_GRACE_SEC: Final = 0.2
STOP_GRACE_SEC: Final = 20.0
TERM_GRACE_SEC: Final = 5.0
MANIFEST_FILENAME: Final = "manifest.json"
RECORDER_LOG_FILENAME: Final = "recorder.log"

STATUS_QOS: Final = QoSProfile(
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
)

# These are the explicit UI inputs known today.  ``--all`` is intentionally
# broader than this inventory so future UI topics and original camera sources
# are captured without a recorder code change.
SURGIMATE_INPUT_TOPICS: Final = (
    "/surgery/gateway_info",
    "/surgery/catalog",
    "/surgery/context",
    "/surgery/instruments",
    "/surgery/robots",
    "/surgery/robot_end_effectors",
    "/surgery/tool_predictions",
    "/surgery/speech",
    "/surgery/health",
    "/surgery/record/receipt",
    "/surgery/images/suction/overlay/compressed",
    "/surgery/images/cam4/overlay/compressed",
    "/surgery/images/cam3/overlay/compressed",
    "/surgery/images/right_ee/overlay/compressed",
)


@dataclass(frozen=True)
class RecordingSession:
    """All paths and immutable start metadata for one manual capture."""

    session_id: str
    started_at: str
    session_dir: Path
    bag_dir: Path
    manifest_path: Path
    log_path: Path
    displayed_output_dir: str


def utc_now_iso() -> str:
    """Return a compact, unambiguous UTC ISO timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _session_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}--{uuid4().hex[:12]}"


def _private_directory(path: Path) -> None:
    """Create a recorder-owned directory without relying on a permissive umask."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise RuntimeError("recording output directory is not writable")


def create_recording_session(
    output_root: Path,
    *,
    host_output_root: str = "",
    now: datetime | None = None,
) -> RecordingSession:
    """Reserve a private output directory before invoking rosbag2."""

    _private_directory(output_root)
    session_id = _session_id(now)
    session_dir = output_root / session_id
    _private_directory(session_dir)
    displayed_root = Path(host_output_root).expanduser() if host_output_root else output_root
    return RecordingSession(
        session_id=session_id,
        started_at=utc_now_iso(),
        session_dir=session_dir,
        bag_dir=session_dir / "recording",
        manifest_path=session_dir / MANIFEST_FILENAME,
        log_path=session_dir / RECORDER_LOG_FILENAME,
        displayed_output_dir=str(displayed_root / session_id),
    )


def build_record_command(
    *,
    bag_dir: Path,
    recorder_node_name: str,
    max_bag_bytes: int,
    max_cache_bytes: int,
) -> list[str]:
    """Build the exact all-graph, replay-oriented recorder command.

    ``--all-services`` is included explicitly even though ``--all`` also
    discovers services; it documents that service-event topics are intended
    capture targets whenever a provider enables ROS introspection.
    """

    if max_bag_bytes <= 0 or max_cache_bytes <= 0:
        raise ValueError("rosbag size and cache limits must be positive")
    if not recorder_node_name.startswith("taskplanner_rosbag_recorder_"):
        raise ValueError("recorder node name must use the reserved prefix")
    return [
        "ros2",
        "bag",
        "record",
        "--storage",
        "mcap",
        "--all",
        "--all-services",
        "--include-hidden-topics",
        "--include-unpublished-topics",
        "--storage-preset-profile",
        "zstd_fast",
        "--disable-keyboard-controls",
        "--node-name",
        recorder_node_name,
        "--max-bag-size",
        str(max_bag_bytes),
        "--max-cache-size",
        str(max_cache_bytes),
        "--output",
        str(bag_dir),
    ]


def ui_replay_contract() -> dict[str, Any]:
    """Return a versioned, human-readable fidelity contract for a session."""

    return {
        "mode": "state_faithful_ros_replay",
        "screen_video": False,
        "requirements": [
            "Use the same 4173 and 5174 application builds when replaying.",
            "Replay the MCAP at recorded timing (for example ros2 bag play <recording> --clock).",
            "Publish the MCAP into the ROS graph served by the selected replay UI profile (the isolated Replay profile uses its shadow DDS domain).",
            "Open each browser surface with ?rosbagReplay=1 to apply its recorded client-only presentation events without reissuing controls.",
        ],
        "captured": {
            "all_discovered_topics": True,
            "camera_and_compressed_video": True,
            "hidden_topics": True,
            "action_feedback_and_status": True,
            "ui_audit_topics": {
                "taskplanner_4173": UI_AUDIT_TOPIC,
                "surgimate_5174": SURGIMATE_UI_AUDIT_TOPIC,
            },
            "service_event_topics": "captured when providers emit ROS service introspection events",
        },
        "limits": [
            "A passive rosbag recorder cannot recover raw service request/response or Action goal/result RPC bodies from providers that do not publish introspection events.",
            "This is state-faithful UI replay, not a pixel/screen-video recording.",
        ],
        "surfaces": {
            "taskplanner_4173": {
                "url": "http://127.0.0.1:4173/",
                "operator_audit": UI_AUDIT_TOPIC,
                "replay_query": "rosbagReplay=1",
                "input_classes": [
                    "simulation state and event streams",
                    "tool and phase predictions",
                    "ASR/TTS and surgery-record receipts",
                    "all original and derived camera streams",
                ],
            },
            "surgimate_5174": {
                "url": "http://127.0.0.1:5174/",
                "mode": "read_only_clinical_ros_projection",
                "operator_audit": SURGIMATE_UI_AUDIT_TOPIC,
                "replay_query": "rosbagReplay=1",
                "ui_journal": "observer-only presentation telemetry; no service, Action, or clinical-topic control",
                "input_topics": list(SURGIMATE_INPUT_TOPICS),
            },
        },
    }


def session_manifest(
    session: RecordingSession,
    command: list[str],
    *,
    state: str,
    recording_active: bool,
    stopped_at: str = "",
    exit_code: int | None = None,
    metadata_present: bool | None = None,
    message: str = "",
) -> dict[str, Any]:
    """Produce the bounded manifest written beside the MCAP, never in it."""

    return {
        "schema": "taskplanner.rosbag_recording.manifest.v1",
        "session_id": session.session_id,
        "state": state,
        "recording_active": recording_active,
        "started_at": session.started_at,
        "stopped_at": stopped_at,
        "output_dir": session.displayed_output_dir,
        "container_recording_dir": str(session.bag_dir),
        "storage": "mcap",
        "command": list(command),
        "exit_code": exit_code,
        "metadata_present": metadata_present,
        "message": message,
        "ui_replay": ui_replay_contract(),
    }


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace a small recorder manifest with owner-only permissions."""

    temporary = path.with_suffix(f"{path.suffix}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    path.chmod(0o600)


def _safe_positive_int(value: object, default: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        return default
    return candidate if candidate > 0 else default


class ManualRosbagRecorder(Node):
    """Dedicated Live owner whose child process is controlled manually only."""

    def __init__(
        self,
        *,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        disk_usage: Callable[[str | os.PathLike[str]], shutil._ntuple_diskusage] = shutil.disk_usage,
    ) -> None:
        # Every new session/log/child artifact must be private even if an
        # external environment accidentally supplied a permissive umask.
        os.umask(0o077)
        super().__init__("operational_rosbag_recorder")
        self.declare_parameter("output_dir", DEFAULT_OUTPUT_DIR)
        self.declare_parameter("min_free_bytes", DEFAULT_MIN_FREE_BYTES)
        self.declare_parameter("max_bag_bytes", DEFAULT_MAX_BAG_BYTES)
        self.declare_parameter("max_cache_bytes", DEFAULT_MAX_CACHE_BYTES)

        self._output_root = Path(str(self.get_parameter("output_dir").value)).expanduser()
        self._host_output_root = os.environ.get("TASKPLANNER_ROSBAG_HOST_OUTPUT_DIR", "").strip()
        self._min_free_bytes = _safe_positive_int(
            self.get_parameter("min_free_bytes").value, DEFAULT_MIN_FREE_BYTES
        )
        self._max_bag_bytes = _safe_positive_int(
            self.get_parameter("max_bag_bytes").value, DEFAULT_MAX_BAG_BYTES
        )
        self._max_cache_bytes = _safe_positive_int(
            self.get_parameter("max_cache_bytes").value, DEFAULT_MAX_CACHE_BYTES
        )
        self._popen = popen
        self._disk_usage = disk_usage
        self._process: subprocess.Popen[bytes] | None = None
        self._session: RecordingSession | None = None
        self._command: list[str] = []
        self._log_handle: Any | None = None
        self._last_status = self._status("idle", False, "Ready for manual recording.")

        self._status_pub = self.create_publisher(String, STATUS_TOPIC, STATUS_QOS)
        self._control_service = self.create_service(
            SetBool, CONTROL_SERVICE, self._on_control
        )
        self._watchdog = self.create_timer(1.0, self._watch_recorder)
        self._publish_status(self._last_status)

    @property
    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _status(
        self,
        state: str,
        recording_active: bool,
        message: str,
        *,
        stopped_at: str = "",
    ) -> dict[str, Any]:
        session = self._session
        return {
            "schema": STATUS_SCHEMA,
            "state": state,
            "recording_active": recording_active,
            "session_id": session.session_id if session else "",
            "message": message[:1024],
            "output_dir": session.displayed_output_dir if session else "",
            "started_at": session.started_at if session else "",
            "stopped_at": stopped_at,
        }

    def _publish_status(self, status: dict[str, Any]) -> None:
        self._last_status = status
        self._status_pub.publish(String(data=json.dumps(status, ensure_ascii=False)))

    def _write_manifest(
        self,
        *,
        state: str,
        recording_active: bool,
        message: str,
        stopped_at: str = "",
        exit_code: int | None = None,
        metadata_present: bool | None = None,
    ) -> None:
        if self._session is None:
            return
        write_private_json(
            self._session.manifest_path,
            session_manifest(
                self._session,
                self._command,
                state=state,
                recording_active=recording_active,
                stopped_at=stopped_at,
                exit_code=exit_code,
                metadata_present=metadata_present,
                message=message,
            ),
        )

    def _has_space(self) -> bool:
        try:
            return self._disk_usage(self._output_root).free >= self._min_free_bytes
        except OSError:
            return False

    def _start(self) -> tuple[bool, str]:
        if self.is_recording:
            return True, "ROSbag2 recording is already active."
        if not self._has_space():
            message = "Recording was not started: private output storage is below the free-space reserve."
            self._publish_status(self._status("failed", False, message))
            return False, message
        try:
            session = create_recording_session(
                self._output_root, host_output_root=self._host_output_root
            )
            recorder_node_name = (
                f"taskplanner_rosbag_recorder_{session.session_id.rsplit('--', 1)[-1]}"
            )
            command = build_record_command(
                bag_dir=session.bag_dir,
                recorder_node_name=recorder_node_name,
                max_bag_bytes=self._max_bag_bytes,
                max_cache_bytes=self._max_cache_bytes,
            )
            self._session = session
            self._command = command
            self._write_manifest(
                state="starting",
                recording_active=False,
                message="Launching all-graph MCAP recorder.",
            )
            self._publish_status(
                self._status("starting", False, "Launching all-graph MCAP recorder.")
            )
            self._log_handle = session.log_path.open("ab", buffering=0)
            os.chmod(session.log_path, 0o600)
            self._process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                cwd=session.session_dir,
                start_new_session=True,
                close_fds=True,
            )
            time.sleep(STARTUP_GRACE_SEC)
            if self._process.poll() is not None:
                return_code = self._process.returncode
                self._close_log()
                message = f"rosbag2 exited during startup (exit {return_code})."
                self._write_manifest(
                    state="failed",
                    recording_active=False,
                    message=message,
                    stopped_at=utc_now_iso(),
                    exit_code=return_code,
                    metadata_present=False,
                )
                self._publish_status(self._status("failed", False, message, stopped_at=utc_now_iso()))
                self._process = None
                return False, message
            message = (
                "Recording all discovered ROS topics, video, service events, and hidden Action feedback/status."
            )
            self._write_manifest(
                state="recording", recording_active=True, message=message
            )
            self._publish_status(self._status("recording", True, message))
            return True, message
        except (OSError, RuntimeError, ValueError) as error:
            self._close_log()
            self._process = None
            message = f"Recording was not started: {error}"
            try:
                self._write_manifest(
                    state="failed",
                    recording_active=False,
                    message=message,
                    stopped_at=utc_now_iso(),
                    metadata_present=False,
                )
            except OSError:
                pass
            self._publish_status(self._status("failed", False, message, stopped_at=utc_now_iso()))
            return False, message

    def _close_log(self) -> None:
        if self._log_handle is None:
            return
        try:
            self._log_handle.flush()
            self._log_handle.close()
        finally:
            self._log_handle = None

    def _signal_child(self, sig: signal.Signals) -> None:
        if self._process is None or self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, sig)
        except ProcessLookupError:
            return

    def _stop(self, *, reason: str) -> tuple[bool, str]:
        process = self._process
        if process is None or process.poll() is not None:
            return True, "ROSbag2 recording is already stopped."
        self._publish_status(self._status("stopping", True, reason))
        self._write_manifest(state="stopping", recording_active=True, message=reason)
        self._signal_child(signal.SIGINT)
        forced = False
        try:
            process.wait(timeout=STOP_GRACE_SEC)
        except subprocess.TimeoutExpired:
            forced = True
            self._signal_child(signal.SIGTERM)
            try:
                process.wait(timeout=TERM_GRACE_SEC)
            except subprocess.TimeoutExpired:
                self._signal_child(signal.SIGKILL)
                process.wait(timeout=TERM_GRACE_SEC)
        stopped_at = utc_now_iso()
        return_code = process.returncode
        self._process = None
        self._close_log()
        metadata_present = bool(
            self._session and (self._session.bag_dir / "metadata.yaml").is_file()
        )
        saved = metadata_present and not forced
        message = (
            "MCAP recording saved and finalized."
            if saved
            else "Recorder stopped, but a finalized MCAP metadata file was not confirmed."
        )
        state = "saved" if saved else "failed"
        self._write_manifest(
            state=state,
            recording_active=False,
            message=message,
            stopped_at=stopped_at,
            exit_code=return_code,
            metadata_present=metadata_present,
        )
        self._publish_status(self._status(state, False, message, stopped_at=stopped_at))
        return saved, message

    def _watch_recorder(self) -> None:
        process = self._process
        if process is None:
            return
        return_code = process.poll()
        if return_code is not None:
            # The child ended without an explicit OFF action. Preserve its log
            # and manifest, but never leave a UI in a false 'recording' state.
            stopped_at = utc_now_iso()
            self._process = None
            self._close_log()
            metadata_present = bool(
                self._session and (self._session.bag_dir / "metadata.yaml").is_file()
            )
            message = f"rosbag2 stopped unexpectedly (exit {return_code})."
            self._write_manifest(
                state="failed",
                recording_active=False,
                message=message,
                stopped_at=stopped_at,
                exit_code=return_code,
                metadata_present=metadata_present,
            )
            self._publish_status(self._status("failed", False, message, stopped_at=stopped_at))
            return
        if not self._has_space():
            self._stop(reason="Free-space reserve reached; finalizing MCAP recording.")
            return

    def _on_control(
        self, request: SetBool.Request, response: SetBool.Response
    ) -> SetBool.Response:
        success, message = self._start() if request.data else self._stop(
            reason="Manual recording stop requested."
        )
        response.success = success
        response.message = message
        return response

    def destroy_node(self) -> bool:
        if self.is_recording:
            self._stop(reason="Recorder owner is shutting down; finalizing MCAP recording.")
        self._close_log()
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = ManualRosbagRecorder()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
