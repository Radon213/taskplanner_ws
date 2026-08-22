"""Native ROS 2 admission server and controller status publisher."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any

from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue
from ament_index_python.packages import get_package_share_directory
import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from surgical_interop_msgs.msg import BedRobotArmState, BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand

from .adapters import ForceTorqueSample, SingleOwnerGuard
from .adapters.clock import SystemClock
from .adapters.fake import CallTrace, FakeAft200Adapter, FakeIndyDcp3Adapter
from .adapters.shadow import ShadowIndyDcp3Adapter
from .command_executor import CommandExecutor
from .controller_backend import ControllerBackend
from .diagnostics import public_arm_state
from .profile_loader import ExecutionProfile, load_profile
from .runtime import CommandLedger, CommandWorker
from .runtime_config import (
    RuntimeSettings,
    load_runtime_config,
    validate_data_directory,
)
from .service_admission import AdmissionController
from .teaching_session import TeachingSessionRepository
from .trace_artifact import ShadowTraceRepository


SERVICE_NAME = "/surgery/retraction/command"
STATUS_TOPIC = "/external/bed_robot_arms/status"
DIAGNOSTICS_TOPIC = "/diagnostics"
_VALID_ADAPTER_MODES = frozenset({"fake", "shadow", "hardware"})
_EXPECTED_PUBLIC_ROLES = {
    "thyroidectomy": frozenset({"army_navy"}),
    "nephrectomy": frozenset({"left_malleable", "right_malleable"}),
}


def _absolute_path(value: object, *, parameter: str) -> Path:
    path = Path(str(value or "").strip())
    if not path.is_absolute():
        raise ValueError(f"{parameter} must be an absolute path")
    return path


def _wall_time_message(nanoseconds: int) -> Any:
    from builtin_interfaces.msg import Time

    message = Time()
    message.sec = int(nanoseconds // 1_000_000_000)
    message.nanosec = int(nanoseconds % 1_000_000_000)
    return message


def _public_arm_layout(profile: ExecutionProfile) -> tuple[tuple[str, str], ...]:
    expected_roles = _EXPECTED_PUBLIC_ROLES.get(profile.public_procedure_type)
    if expected_roles is None:
        raise ValueError(
            "approved execution profile procedure_type must be thyroidectomy or "
            "nephrectomy"
        )
    by_role: dict[str, str] = {}
    for mapping in profile.side_mappings.values():
        role = str(mapping.role_instance_id).strip()
        arm_id = str(mapping.arm_id).strip()
        if role in expected_roles:
            if role in by_role and by_role[role] != arm_id:
                raise ValueError(f"public role {role!r} maps to multiple arms")
            by_role[role] = arm_id
    if frozenset(by_role) != expected_roles:
        raise ValueError(
            "profile side mappings do not match the existing public bed-arm "
            "layout for "
            f"{profile.public_procedure_type}: expected {sorted(expected_roles)}"
        )
    if len(set(by_role.values())) != len(by_role):
        raise ValueError("public bed-arm roles must map to distinct arm IDs")
    return tuple((by_role[role], role) for role in sorted(by_role))


class RetractionCommandServer(Node):
    """Admission-only Service front end for one hardware-owning worker."""

    def __init__(self) -> None:
        super().__init__("retraction_command_server")
        self._callback_group = ReentrantCallbackGroup()
        default_runtime_config = (
            Path(get_package_share_directory("retraction_control"))
            / "config"
            / "logging.yaml"
        )
        self.declare_parameter(
            "runtime_config_path", str(default_runtime_config)
        )
        runtime_config_path = _absolute_path(
            self.get_parameter("runtime_config_path").value,
            parameter="runtime_config_path",
        )
        self._runtime_settings: RuntimeSettings = load_runtime_config(
            runtime_config_path
        )
        self.declare_parameter("profile_path", "")
        self.declare_parameter("adapter_mode", "fake")
        self.declare_parameter(
            "data_directory", str(self._runtime_settings.data_directory)
        )
        self.declare_parameter("allow_motion", False)
        self.declare_parameter("sdk_license_path", "")
        self.declare_parameter("expected_ros_domain_id", 0)
        self.declare_parameter("allowed_source_ids", ["taskplanner"])
        self.declare_parameter("max_pending_commands", 8)
        self.declare_parameter(
            "status_period_sec", self._runtime_settings.status_period_sec
        )
        self.declare_parameter(
            "diagnostics_period_sec", self._runtime_settings.diagnostics_period_sec
        )
        self.declare_parameter("shutdown_timeout_sec", 10.0)
        self.declare_parameter(
            "source_revision",
            os.environ.get("RETRACTION_CONTROL_SOURCE_REVISION", "development-uncommitted"),
        )

        expected_domain = int(self.get_parameter("expected_ros_domain_id").value)
        actual_domain = int(os.environ.get("ROS_DOMAIN_ID", "0"))
        if actual_domain != expected_domain:
            raise RuntimeError(
                f"ROS_DOMAIN_ID mismatch: expected {expected_domain}, got {actual_domain}"
            )

        profile_path = _absolute_path(
            self.get_parameter("profile_path").value,
            parameter="profile_path",
        )
        loaded = load_profile(profile_path, require_approved=True)
        if not isinstance(loaded, ExecutionProfile):
            raise RuntimeError("profile did not resolve to an executable profile")
        self._profile = loaded
        self._arm_layout = _public_arm_layout(loaded)

        self._adapter_mode = str(
            self.get_parameter("adapter_mode").value
        ).strip().lower()
        if self._adapter_mode not in _VALID_ADAPTER_MODES:
            raise ValueError(f"unsupported adapter_mode: {self._adapter_mode}")
        allow_motion = bool(self.get_parameter("allow_motion").value)
        if self._adapter_mode == "shadow" and allow_motion:
            raise RuntimeError("shadow mode requires allow_motion=false")
        if self._adapter_mode == "hardware":
            if not allow_motion:
                raise RuntimeError("hardware mode requires explicit allow_motion=true")
            if loaded.name == "synthetic_fake":
                raise RuntimeError("synthetic_fake profile can never authorize hardware mode")
            raise RuntimeError(
                "hardware backend injection is not implemented; use fake/shadow until "
                "the version-pinned IndyDCP3 and AFT200 backends are approved"
            )

        data_directory = validate_data_directory(
            str(self.get_parameter("data_directory").value)
        )
        if self._adapter_mode == "hardware" and data_directory != loaded.data_directory:
            raise RuntimeError(
                "hardware data_directory must match the checksum-bound profile"
            )
        data_directory.mkdir(parents=True, exist_ok=True)
        self._data_directory = data_directory
        self._clock_adapter = SystemClock()
        self._trace = CallTrace(self._clock_adapter)
        self._fake_force_sensor: FakeAft200Adapter | None = None

        joint_positions = {
            str(mapping.arm_id): tuple(0.0 for _ in range(mapping.joint_slice.size))
            for mapping in loaded.side_mappings.values()
        }
        if self._adapter_mode == "shadow":
            robot = ShadowIndyDcp3Adapter(
                trace=self._trace,
                clock=self._clock_adapter,
                observed_joint_positions=joint_positions,
            )
        else:
            robot = FakeIndyDcp3Adapter(
                trace=self._trace,
                clock=self._clock_adapter,
                joint_positions=joint_positions,
            )
        force_sensor = FakeAft200Adapter(
            trace=self._trace,
            clock=self._clock_adapter,
        )
        self._fake_force_sensor = force_sensor
        self._refresh_fake_samples()

        sessions = TeachingSessionRepository(
            data_directory / self._runtime_settings.session_directory_name
        )
        owner_guard = SingleOwnerGuard(data_directory / "controller.lock")
        executor = CommandExecutor(
            robot=robot,
            force_sensor=force_sensor,
            profile=loaded,
            sessions=sessions,
            robot_id="synthetic-fake-robot",
            controller_id="synthetic-fake-controller",
            source_revision=str(self.get_parameter("source_revision").value),
            clock=self._clock_adapter,
            owner_guard=owner_guard,
            calibration_metadata={"adapter_mode": self._adapter_mode},
            execution_mode=self._adapter_mode,
        )
        self._backend = ControllerBackend(executor)
        ledger: CommandLedger | None = None
        backend_started = False
        try:
            # Establish single-process adapter authority before touching the
            # shared recovery ledger.  A losing process must not relabel the
            # active owner's nonterminal command as interrupted.
            self._backend.start()
            backend_started = True
            ledger = CommandLedger(
                data_directory / self._runtime_settings.ledger_filename
            )
            interrupted = ledger.mark_interrupted()
            if interrupted:
                self._backend.state_machine.fail(
                    "execution_state_unknown_after_restart:" + ",".join(interrupted),
                    fatal=True,
                )
        except BaseException:
            if ledger is not None:
                ledger.close()
            if backend_started:
                try:
                    self._backend.shutdown()
                except BaseException:
                    pass
            raise
        self._ledger = ledger

        self._worker = CommandWorker(
            self._backend.execute_runtime,
            self._ledger,
            max_pending=int(self.get_parameter("max_pending_commands").value),
            on_stage=self._on_worker_stage,
        )
        self._admission = AdmissionController(
            self._ledger,
            self._worker,
            allowed_source_ids=tuple(
                str(item)
                for item in self.get_parameter("allowed_source_ids").value
            ),
            check_state=self._backend.check_admission,
        )
        self._worker.start()

        self._status_publisher = self.create_publisher(
            BedRobotArmStateArray, STATUS_TOPIC, 10
        )
        self._diagnostics_publisher = self.create_publisher(
            DiagnosticArray, DIAGNOSTICS_TOPIC, 10
        )
        self._service = self.create_service(
            ExecuteRetractionCommand,
            SERVICE_NAME,
            self._on_command,
            callback_group=self._callback_group,
        )
        self._status_revision = 0
        self._last_wall_time_ns = 0
        self._stage_lock = threading.RLock()
        self._last_worker_stage = "idle"
        self._last_worker_message = ""
        self._last_health: Any | None = None
        self._trace_artifact_error = ""
        self._last_terminal_command_trace_count = 0
        self._shadow_traces = (
            ShadowTraceRepository(
                data_directory / self._runtime_settings.shadow_trace_directory_name
            )
            if self._adapter_mode == "shadow"
            else None
        )
        self._status_timer = self.create_timer(
            float(self.get_parameter("status_period_sec").value),
            self._publish_status,
            callback_group=self._callback_group,
        )
        self._diagnostics_timer = self.create_timer(
            float(self.get_parameter("diagnostics_period_sec").value),
            self._publish_diagnostics,
            callback_group=self._callback_group,
        )
        self._publish_status()
        self._publish_diagnostics()

    def _next_wall_time_ns(self) -> int:
        now_ns = time.time_ns()
        with self._stage_lock:
            now_ns = max(now_ns, self._last_wall_time_ns + 1)
            self._last_wall_time_ns = now_ns
        return now_ns

    def _refresh_fake_samples(self) -> None:
        sensor = self._fake_force_sensor
        if sensor is None:
            return
        now_ns = self._clock_adapter.monotonic_ns()
        seen: set[str] = set()
        for mapping in self._profile.side_mappings.values():
            sensor_id = str(mapping.sensor_id)
            if sensor_id in seen:
                continue
            seen.add(sensor_id)
            sensor.set_sample(
                ForceTorqueSample(
                    timestamp_ns=now_ns,
                    sensor_id=sensor_id,
                    force_n=(0.0, 0.0, 0.0),
                    torque_nm=(0.0, 0.0, 0.0),
                    calibration_id="synthetic-test-only",
                )
            )

    def _on_command(self, request: Any, response: Any) -> Any:
        self._refresh_fake_samples()
        reply = self._admission.admit(request)
        response.request_accepted = bool(reply.request_accepted)
        response.result_code = int(reply.result_code)
        response.command_id = reply.command_id
        response.message = reply.message
        return response

    def _on_worker_stage(self, command_id: str, stage: str, message: str) -> None:
        if stage in {"running", "stopping"}:
            self._trace.set_command_context(command_id)
        if stage in {"completed", "failed", "canceled"}:
            try:
                with self._stage_lock:
                    self._last_terminal_command_trace_count = len(
                        self._trace.records_for(command_id)
                    )
                self._save_shadow_trace(command_id, stage, message)
            finally:
                self._trace.clear_command_context()
        with self._stage_lock:
            self._last_worker_stage = str(stage)
            self._last_worker_message = str(message)

    def _save_shadow_trace(self, command_id: str, stage: str, message: str) -> None:
        repository = self._shadow_traces
        if repository is None:
            return
        outcome = self._backend.last_outcome
        if outcome is not None and outcome.command_id != command_id:
            outcome = None
        try:
            repository.save(
                command_id=command_id,
                command=outcome.command if outcome is not None else None,
                profile_name=self._profile.name,
                profile_version=self._profile.version,
                profile_checksum=self._profile.checksum,
                source_revision=str(self.get_parameter("source_revision").value),
                target_planner=self._backend.executor.target_planner.identity.as_dict(),
                terminal_stage=stage,
                terminal_code=outcome.code if outcome is not None else stage,
                terminal_message=message,
                calls=self._trace.records_for(command_id),
            )
            self._trace_artifact_error = ""
        except Exception as exc:
            self._trace_artifact_error = f"{type(exc).__name__}: {exc}"
            try:
                self._backend.state_machine.fail(
                    "shadow_trace_write_failed:" + self._trace_artifact_error,
                    fatal=True,
                )
            except Exception:
                pass

    def _status_reason(self, public_state: str, health: Any | None) -> str:
        if self._adapter_mode == "shadow":
            return "shadow_record_only"
        outcome = self._backend.last_outcome
        if outcome is not None and not outcome.success:
            return str(outcome.code or "execution_failed")
        if health is not None and not health.robot_connected:
            return "controller_unavailable"
        if health is not None and not health.sensor_available:
            return "sensor_unavailable"
        if public_state == "direct_teach":
            return "teach_button_active"
        return "ok"

    def _publish_status(self) -> None:
        self._refresh_fake_samples()
        snapshot = self._backend.snapshot()
        health = self._last_health
        state = public_arm_state(snapshot.state.state)
        if self._adapter_mode == "shadow":
            state = "unknown"
        if health is not None and not health.robot_connected:
            state = "unknown"
        if health is not None and health.controller_fault_code:
            state = "fault"
        reason = self._status_reason(state, health)
        message = BedRobotArmStateArray()
        message.stamp = _wall_time_message(self._next_wall_time_ns())
        self._status_revision += 1
        message.revision = self._status_revision
        message.procedure_type = self._profile.public_procedure_type
        for arm_id, role_instance_id in self._arm_layout:
            arm = BedRobotArmState()
            arm.arm_id = arm_id
            arm.role = "retraction"
            arm.role_instance_id = role_instance_id
            arm.state = state
            arm.direct_teach_active = state == "direct_teach"
            arm.reason_code = reason
            message.arms.append(arm)
        self._status_publisher.publish(message)

    @staticmethod
    def _key_value(key: str, value: object) -> KeyValue:
        item = KeyValue()
        item.key = str(key)
        item.value = str(value)[:1024]
        return item

    def _publish_diagnostics(self) -> None:
        self._refresh_fake_samples()
        try:
            health = self._backend.health()
            self._last_health = health
            health_error = ""
        except Exception as exc:
            health = None
            health_error = f"{type(exc).__name__}: {exc}"
        snapshot = self._backend.snapshot()
        last_error_code, last_error_message = self._backend.executor.last_error
        with self._stage_lock:
            worker_stage = self._last_worker_stage
            worker_message = self._last_worker_message
        outcome = snapshot.last_outcome

        status = DiagnosticStatus()
        status.name = "retraction_control/controller"
        status.hardware_id = (
            "record-only-shadow"
            if self._adapter_mode == "shadow"
            else "synthetic-fake"
            if self._adapter_mode == "fake"
            else "unconfigured"
        )
        healthy = bool(health is not None and health.healthy and not health_error)
        if self._adapter_mode == "shadow":
            healthy = False
        status.level = (
            DiagnosticStatus.OK
            if healthy
            else DiagnosticStatus.ERROR
            if snapshot.state.state.value == "fault" or health_error
            else DiagnosticStatus.WARN
        )
        status.message = (
            "shadow_record_only"
            if self._adapter_mode == "shadow" and not self._trace_artifact_error
            else "ready"
            if healthy
            else health_error
            or self._trace_artifact_error
            or last_error_message
            or "controller_not_ready"
        )
        values = {
            "adapter_mode": self._adapter_mode,
            "profile_name": self._profile.name,
            "profile_checksum": self._profile.checksum,
            "runtime_config_checksum": self._runtime_settings.checksum,
            "procedure_type": self._profile.procedure_type,
            "public_procedure_type": self._profile.public_procedure_type,
            "internal_state": snapshot.state.state.value,
            "state_revision": snapshot.state.revision,
            "active_command_id": snapshot.state.active_command_id or "",
            "active_operation": snapshot.active_operation,
            "worker_stage": worker_stage,
            "worker_message": worker_message,
            "pending_count": self._worker.pending_count,
            "worker_fatal_error": self._worker.fatal_error,
            "worker_notification_errors": len(self._worker.notification_errors),
            "last_error_code": last_error_code,
            "last_error_message": last_error_message,
            "last_terminal_outcome": outcome.status.value if outcome else "",
            "last_terminal_code": outcome.code if outcome else "",
            "last_terminal_command_id": outcome.command_id if outcome else "",
            "last_terminal_command_trace_count": (
                self._last_terminal_command_trace_count
            ),
            "last_affected_arm_id": outcome.affected_arm_id if outcome else "",
            "robot_connected": health.robot_connected if health else False,
            "sensor_available": health.sensor_available if health else False,
            "stale_sensor_ids": ",".join(health.stale_sensor_ids) if health else "",
            "trace_call_count": len(self._trace.records),
            "execution_evidence": (
                "record_only" if self._adapter_mode == "shadow" else "synthetic"
            ),
            "physical_completion_confirmed": False,
            "shadow_trace_error": self._trace_artifact_error,
        }
        status.values = [self._key_value(key, value) for key, value in values.items()]
        message = DiagnosticArray()
        message.header.stamp = _wall_time_message(self._next_wall_time_ns())
        message.status = [status]
        self._diagnostics_publisher.publish(message)

    def destroy_node(self) -> bool:
        timeout = float(self.get_parameter("shutdown_timeout_sec").value)
        worker_stopped = self._worker.shutdown(timeout)
        if worker_stopped:
            try:
                self._backend.shutdown()
            except Exception as exc:
                self.get_logger().error(f"executor shutdown failed: {exc}")
        else:
            self.get_logger().error(
                "worker did not stop; adapters remain owned until process exit"
            )
        self._ledger.close()
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node: RetractionCommandServer | None = None
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        node = RetractionCommandServer()
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            executor.shutdown()
        except KeyboardInterrupt:
            # ros2run can deliver a second SIGINT while the executor is
            # destroying its signal guard condition.  Continue controller
            # cleanup instead of leaking the interrupt as a process failure.
            pass
        if node is not None:
            try:
                node.destroy_node()
            except KeyboardInterrupt:
                pass
        if rclpy.ok():
            try:
                rclpy.shutdown()
            except KeyboardInterrupt:
                pass


__all__ = ["RetractionCommandServer", "main"]
