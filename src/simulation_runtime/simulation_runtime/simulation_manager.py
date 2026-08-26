"""Simulation manager service surface for bundle and runtime control."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import socket
import threading
import time

from ament_index_python.packages import get_package_share_directory
from btops_interfaces.srv import CommandExecutor, GetRuntimeState, StartBehavior
from procedure_spec import load_bundle
import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.parameter_client import AsyncParameterClient
from std_msgs.msg import String
from std_srvs.srv import Trigger
from surgical_msgs.msg import SimulationState, SurgeonRequest, VoiceCommandIntent
from surgical_msgs.srv import (
    ControlSimulation,
    InjectSurgeonOverride,
    SelectSimulationBundle,
)


RESOURCE_ID = "tree/taskplanner_bt_trees::surgical_assist_v1::TaskplannerAssistDemo"
ENTRY_POINT = "TaskplannerAssistDemo"
NODE_MANIFESTS = ["taskplanner_bt_nodes::taskplanner_bt_nodes"]
RETRYABLE_START_ERROR_MARKERS = (
    "/tree_executor/set_parameters",
    "address already in use",
    "previous one is still busy",
    "currently executing",
    "parameter is not allowed to change while tree executor is running",
    "node_manifest_identities is empty",
)
ALLOWED_OVERRIDE_EVENTS = {"request_tool", "voice_request", "return_tool", "cancel_request"}
TOOL_REQUIRED_OVERRIDE_EVENTS = {"request_tool", "voice_request", "return_tool"}
TRANSITION_READY_MANAGER_STATES = {"idle", "halted", "completed", "terminated"}
TRANSITION_READY_EXECUTOR_STATES = {"idle", "halted", "terminated"}
NO_RUNTIME_SNAPSHOT_PREFIX = "No runtime snapshot is available for"
TRANSITION_STATE_MAX_AGE_SEC = 3.0
TRANSITION_PROTOCOL_MARKER = (
    "transition-reservation-v2; dt_receipt_max_age=3.0;"
)
_FAULTED_ROBOT_STATES = frozenset(
    {
        "fault",
        "error",
        "failed",
        "protective_stop",
        "emergency_stop",
        "estop",
    }
)
_LIVE_SWITCH_SAFE_ROBOT_STATES = frozenset({"idle"})
_VOICE_PROCEDURE_START = "procedure_start"
_VOICE_PROCEDURE_STOP = "procedure_stop"
_VOICE_PROCEDURE_INTENTS = frozenset({_VOICE_PROCEDURE_START, _VOICE_PROCEDURE_STOP})
_BUNDLE_CONFIG_REVISION_SCHEMA = b"taskplanner.procedure_bundle.revision.v1\0"
_BUNDLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def validate_bundle_name(bundle_name: str) -> str:
    """Return one path-safe bundle identifier or raise ``ValueError``."""

    value = str(bundle_name or "").strip()
    if not _BUNDLE_NAME_PATTERN.fullmatch(value) or ".." in value:
        raise ValueError(
            "bundle name must be a simple identifier using only "
            "letters, digits, '_', '-', or '.'; path traversal is not allowed"
        )
    return value


def compute_bundle_config_revision(bundle_dir: str | Path) -> str:
    """Hash every YAML input that can affect one loaded procedure bundle.

    The revision intentionally ignores mtimes and absolute paths.  It covers
    the bundle-local YAML files plus the shared display catalog consumed by
    ``procedure_spec.load_bundle`` so an edited, active bundle can be detected
    without rebuilding or restarting the ROS runtime.
    """

    bundle_path = Path(bundle_dir)
    if not bundle_path.is_dir():
        raise FileNotFoundError(
            f"procedure bundle directory does not exist: {bundle_path}"
        )

    inputs: dict[str, Path] = {}
    shared_catalog = bundle_path.parent / "display_catalog.yaml"
    if shared_catalog.is_file():
        inputs["../display_catalog.yaml"] = shared_catalog
    for path in bundle_path.rglob("*"):
        if path.is_file() and path.suffix.casefold() in {".yaml", ".yml"}:
            inputs[path.relative_to(bundle_path).as_posix()] = path

    digest = hashlib.sha256(_BUNDLE_CONFIG_REVISION_SCHEMA)
    for logical_path, path in sorted(inputs.items()):
        logical_bytes = logical_path.encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(logical_bytes).to_bytes(8, "big"))
        digest.update(logical_bytes)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class VoiceProcedureIntentAdmission:
    """Fail-closed admission result for an ASR-originated lifecycle proposal."""

    accepted: bool
    reason: str
    command: str = ""


class RecentVoiceProcedureIntentIds:
    """Suppress replay of one immutable ASR utterance across the lifecycle gate."""

    def __init__(self, retention_sec: float) -> None:
        self._retention_sec = max(1.0, float(retention_sec))
        self._seen: dict[str, float] = {}

    def accept(self, utterance_id: str, now_monotonic: float) -> bool:
        cutoff = float(now_monotonic) - self._retention_sec
        self._seen = {
            key: seen_at
            for key, seen_at in self._seen.items()
            if seen_at >= cutoff
        }
        key = str(utterance_id or "").strip()
        if not key or key in self._seen:
            return False
        self._seen[key] = float(now_monotonic)
        return True


def _stamp_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def evaluate_voice_procedure_intent(
    msg: VoiceCommandIntent,
    *,
    active_procedure_id: str,
    now_sec: float,
    min_confidence: float,
    accept_missing_confidence: bool = False,
    max_age_sec: float,
    max_future_skew_sec: float,
    running: bool,
    execution_state: str,
) -> VoiceProcedureIntentAdmission:
    """Re-admit a lifecycle proposal before it can control the simulation.

    The resolver publishes proposals only.  The second boundary here requires
    the final, fresh surgeon ASR envelope and an exact active procedure binding
    before forwarding to the existing integration-preflight-gated control path.
    """

    intent = str(getattr(msg, "intent", "") or "").strip()
    if intent not in _VOICE_PROCEDURE_INTENTS:
        return VoiceProcedureIntentAdmission(False, "unsupported_voice_intent")
    if str(getattr(msg, "disposition", "") or "").strip() != "propose":
        return VoiceProcedureIntentAdmission(False, "voice_intent_not_proposed")
    if bool(getattr(msg, "requires_confirmation", False)):
        return VoiceProcedureIntentAdmission(False, "voice_intent_requires_confirmation")
    if str(getattr(msg, "procedure_id", "") or "").strip() != str(
        active_procedure_id
    ).strip():
        return VoiceProcedureIntentAdmission(False, "voice_intent_procedure_mismatch")
    if not str(getattr(msg, "utterance_id", "") or "").strip():
        return VoiceProcedureIntentAdmission(False, "missing_utterance_id")
    if not str(getattr(msg, "source", "") or "").strip():
        return VoiceProcedureIntentAdmission(False, "missing_source")
    if not bool(getattr(msg, "source_is_final", False)):
        return VoiceProcedureIntentAdmission(False, "interim_transcript")
    if str(getattr(msg, "source_speaker_role", "") or "").strip().casefold() != "surgeon":
        return VoiceProcedureIntentAdmission(False, "unexpected_speaker_role")
    source_has_confidence = bool(
        getattr(msg, "source_has_confidence", False)
    )
    if not source_has_confidence and not accept_missing_confidence:
        return VoiceProcedureIntentAdmission(False, "missing_confidence")
    if source_has_confidence and float(
        getattr(msg, "source_confidence", 0.0)
    ) < float(min_confidence):
        return VoiceProcedureIntentAdmission(False, "low_confidence")
    stamp = getattr(getattr(msg, "header", None), "stamp", None)
    if stamp is None or _stamp_sec(stamp) <= 0.0:
        return VoiceProcedureIntentAdmission(False, "missing_timestamp")
    age_sec = float(now_sec) - _stamp_sec(stamp)
    if age_sec > max(0.0, float(max_age_sec)):
        return VoiceProcedureIntentAdmission(False, f"stale:{age_sec:.3f}s")
    if age_sec < -max(0.0, float(max_future_skew_sec)):
        return VoiceProcedureIntentAdmission(
            False,
            f"future_timestamp:{-age_sec:.3f}s",
        )
    state = str(execution_state or "").strip().casefold()
    if intent == _VOICE_PROCEDURE_START:
        if bool(running) or state != "idle":
            return VoiceProcedureIntentAdmission(False, "procedure_not_idle")
        command = "start"
    else:
        if not bool(running) or state != "running":
            return VoiceProcedureIntentAdmission(False, "procedure_not_running")
        command = "pause"
    return VoiceProcedureIntentAdmission(
        True,
        "accepted",
        command=command,
    )


@dataclass(frozen=True, slots=True)
class ExternalRobotContract:
    """Launch-lifetime external controller contract for one procedure bundle."""

    procedure_type: str
    require_retraction_service: bool
    require_bed_robot_arm_status: bool
    require_tool_handover_action_server: bool = True

    @property
    def endpoint_shape(self) -> tuple[bool, bool, bool]:
        """The launch-time endpoint topology, excluding procedure identity.

        A stopped Live runtime can replace a procedure specification only when
        the already-running processes expose the same endpoint shape.  The
        procedure identity itself is intentionally *not* part of this shape:
        it is re-admitted against the external controller manifest before a
        later start, rather than pinning the whole Live process forever.
        """

        return (
            bool(self.require_tool_handover_action_server),
            bool(self.require_retraction_service),
            bool(self.require_bed_robot_arm_status),
        )

    def supports(self, required: "ExternalRobotContract") -> bool:
        """Return whether this launch-time endpoint capacity can host a spec.

        Live may safely select a procedure that requires a subset of endpoints
        already launched. It must still reject a target that would require an
        endpoint the running topology does not own.
        """

        return all(
            runtime_has or not target_requires
            for runtime_has, target_requires in zip(
                self.endpoint_shape,
                required.endpoint_shape,
                strict=True,
            )
        )


_NO_BED_ROBOT_CONTRACT = ExternalRobotContract("", False, False)
_THYROID_BED_ROBOT_CONTRACT = ExternalRobotContract(
    "thyroidectomy", True, False
)
_NEPHRECTOMY_BED_ROBOT_CONTRACT = ExternalRobotContract(
    "nephrectomy", True, False
)
_INGUINAL_HERNIA_BED_ROBOT_CONTRACT = ExternalRobotContract(
    "inguinal_hernia_repair",
    True,
    False,
    False,
)


def external_robot_contract_for_spec(spec) -> ExternalRobotContract:
    """Resolve only the focused controller contracts supported by bringup."""

    procedure_id = str(spec.procedure_id).strip().casefold()
    get_bed_spec = getattr(spec, "get_bed_robot_arm_group_spec", None)
    bed_spec = (
        get_bed_spec()
        if callable(get_bed_spec)
        else getattr(spec, "bed_robot_arm_groups", None)
    )
    enabled_groups = [
        group
        for group in (bed_spec.groups if bed_spec is not None else [])
        if bool(group.enabled)
    ]
    operations = {
        str(operation).strip()
        for group in enabled_groups
        for operation in group.allowed_operations
        if str(operation).strip()
    }

    if procedure_id == "thyroidectomy":
        if len(enabled_groups) != 1 or operations != {"change_end_effector"}:
            raise RuntimeError(
                f"procedure '{procedure_id}' does not match the thyroidectomy "
                "external robot contract"
            )
        return _THYROID_BED_ROBOT_CONTRACT
    if procedure_id == "thyroidectomy_demo":
        if len(enabled_groups) != 1 or operations != {"retraction"}:
            raise RuntimeError(
                "procedure 'thyroidectomy_demo' does not match the "
                "pre-mounted retraction-service external robot contract"
            )
        return _THYROID_BED_ROBOT_CONTRACT
    if procedure_id == "nephrectomy":
        if len(enabled_groups) != 1 or operations != {"retraction"}:
            raise RuntimeError(
                "procedure 'nephrectomy' does not match the nephrectomy "
                "external robot contract"
            )
        return _NEPHRECTOMY_BED_ROBOT_CONTRACT
    if procedure_id == "inguinal_hernia_repair_demo":
        if len(enabled_groups) != 1 or operations != {"retraction"}:
            raise RuntimeError(
                "procedure 'inguinal_hernia_repair_demo' does not match the "
                "voice-retraction-only external robot contract"
            )
        return _INGUINAL_HERNIA_BED_ROBOT_CONTRACT
    if enabled_groups:
        raise RuntimeError(
            f"procedure '{procedure_id}' enables an unsupported external robot contract"
        )
    return _NO_BED_ROBOT_CONTRACT


class SimulationManagerNode(Node):
    def __init__(self) -> None:
        super().__init__("simulation_manager")
        default_root = Path(get_package_share_directory("procedure_spec")) / "specs"
        self.declare_parameter("spec_root", str(default_root))
        self.declare_parameter("default_bundle", "thyroidectomy")
        self.declare_parameter("executor_name", "tree_executor")
        self.declare_parameter("tick_rate_hz", 0.1)
        self.declare_parameter("groot2_port", 0)
        self.declare_parameter("surgeon_actor_mode", "llm")
        self.declare_parameter("manual_override_actor_mute_sec", 8.0)
        self.declare_parameter("execution_backend", "mock")
        self.declare_parameter("require_integration_preflight", False)
        self.declare_parameter(
            "integration_preflight_service",
            "/integration/check_readiness",
        )
        self.declare_parameter(
            "integration_preflight_node",
            "/integration_preflight",
        )
        self.declare_parameter("integration_preflight_timeout_sec", 5.0)
        self.declare_parameter("transition_reservation_ttl_sec", 75.0)
        # The execution bridge owns the single public route coordinator.  The
        # manager remains its stopped-state/reset authority through
        # `/simulation/check_transition_ready` and `/simulation/control`.
        self.declare_parameter("enable_runtime_route_control", False)
        # Disabled outside Live by default.  This cannot replace the existing
        # `/simulation/control` gate; it only provides a validated spoken
        # request to that same gate.
        self.declare_parameter("enable_voice_procedure_control", False)
        self.declare_parameter("voice_intent_topic", "/surgery/voice/intent")
        self.declare_parameter(
            "voice_procedure_result_topic",
            "/simulation/voice_procedure_control/result",
        )
        self.declare_parameter("voice_procedure_min_confidence", 0.55)
        # Operational ASR may truthfully report that no calibrated confidence
        # is available. Keep the generic default fail-closed; Live must opt in
        # explicitly while retaining every other typed-ASR authority check.
        self.declare_parameter(
            "voice_procedure_accept_missing_confidence",
            False,
        )
        self.declare_parameter("voice_procedure_max_age_sec", 3.0)
        self.declare_parameter("voice_procedure_max_future_skew_sec", 1.0)
        self.declare_parameter("voice_procedure_dedupe_retention_sec", 120.0)

        self._spec_root = Path(str(self.get_parameter("spec_root").value))
        self._active_bundle = str(self.get_parameter("default_bundle").value)
        (
            self._active_spec_dir,
            self._active_spec,
            self._active_config_revision,
        ) = self._load_spec_candidate(self._active_bundle)
        self._runtime_external_robot_contract = external_robot_contract_for_spec(
            self._active_spec
        )
        self._executor_name = str(self.get_parameter("executor_name").value)
        self._tick_rate_hz = float(self.get_parameter("tick_rate_hz").value)
        self._groot2_port = int(self.get_parameter("groot2_port").value)
        self._surgeon_actor_mode = str(
            self.get_parameter("surgeon_actor_mode").value
        ).strip().lower()
        self._manual_override_actor_mute_sec = float(
            self.get_parameter("manual_override_actor_mute_sec").value
        )
        self._execution_backend = str(
            self.get_parameter("execution_backend").value
        ).strip().lower()
        self._require_integration_preflight = bool(
            self.get_parameter("require_integration_preflight").value
        )
        self._integration_preflight_timeout_sec = max(
            0.5,
            float(self.get_parameter("integration_preflight_timeout_sec").value),
        )
        # The launcher may need to wait through the ASR container's 45 second
        # graceful shutdown budget.  Keep the reservation valid throughout
        # that stop, while still allowing automatic recovery if the launcher
        # itself disappears.
        self._transition_reservation_ttl_sec = max(
            60.0,
            float(self.get_parameter("transition_reservation_ttl_sec").value),
        )
        self._enable_runtime_route_control = bool(
            self.get_parameter("enable_runtime_route_control").value
        )
        self._enable_voice_procedure_control = bool(
            self.get_parameter("enable_voice_procedure_control").value
        )
        self._voice_procedure_min_confidence = max(
            0.0,
            min(
                1.0,
                float(self.get_parameter("voice_procedure_min_confidence").value),
            ),
        )
        self._voice_procedure_accept_missing_confidence = bool(
            self.get_parameter(
                "voice_procedure_accept_missing_confidence"
            ).value
        )
        self._voice_procedure_max_age_sec = max(
            0.0,
            float(self.get_parameter("voice_procedure_max_age_sec").value),
        )
        self._voice_procedure_max_future_skew_sec = max(
            0.0,
            float(
                self.get_parameter("voice_procedure_max_future_skew_sec").value
            ),
        )
        self._voice_procedure_intent_ids = RecentVoiceProcedureIntentIds(
            float(
                self.get_parameter("voice_procedure_dedupe_retention_sec").value
            )
        )
        self._running = False
        self._execution_state = "idle"
        self._bundle_dirty = False
        self._bundle_reload_recovery_required = False
        self._operation_name = ""
        self._operation_cancel = threading.Event()
        self._operation_lock = threading.Lock()
        self._operation_rejection_message = ""
        self._transition_reservation_until_monotonic = 0.0
        self._bundle_transition_in_progress = False
        self._override_in_progress = False
        self._completion_terminate_started = False
        # A fresh manager has never dispatched this executor.  Once a start is
        # attempted, only an explicit terminal BTops snapshot can restore this
        # bit.  This distinguishes a legitimate initial no-session state from
        # a lost/unknown runtime snapshot after execution began.
        self._executor_settled_confirmed = True
        self._latest_state: SimulationState | None = None
        self._latest_state_generation = 0
        self._latest_state_received_monotonic = 0.0
        self._latest_state_lock = threading.Lock()
        self._callback_group = ReentrantCallbackGroup()

        self._control_pub = self.create_publisher(String, "/simulation/control_state", 10)
        self._override_pub = self.create_publisher(SurgeonRequest, "/simulation/surgeon_override", 10)
        self._direct_request_pub = self.create_publisher(SurgeonRequest, "/surgeon/request", 10)
        self.create_subscription(
            SimulationState,
            "/simulation/state",
            self._on_simulation_state,
            20,
            callback_group=self._callback_group,
        )
        self._voice_procedure_result_pub = None
        if self._enable_voice_procedure_control:
            voice_intent_topic = str(self.get_parameter("voice_intent_topic").value)
            voice_result_topic = str(
                self.get_parameter("voice_procedure_result_topic").value
            )
            self._voice_procedure_result_pub = self.create_publisher(
                String,
                voice_result_topic,
                20,
            )
            self.create_subscription(
                VoiceCommandIntent,
                voice_intent_topic,
                self._on_voice_procedure_intent,
                20,
                callback_group=self._callback_group,
            )
            self.get_logger().info(
                "voice procedure control enabled: "
                f"{voice_intent_topic} -> /simulation/control; "
                f"result={voice_result_topic}"
            )

        self._start_client = self.create_client(
            StartBehavior,
            "/btops/start_behavior",
            callback_group=self._callback_group,
        )
        self._command_client = self.create_client(
            CommandExecutor,
            "/btops/command_executor",
            callback_group=self._callback_group,
        )
        self._runtime_client = self.create_client(
            GetRuntimeState,
            "/btops/get_runtime_state",
            callback_group=self._callback_group,
        )
        self._integration_preflight_client = self.create_client(
            Trigger,
            str(self.get_parameter("integration_preflight_service").value),
            callback_group=self._callback_group,
        )
        self._integration_preflight_parameter_client = AsyncParameterClient(
            self,
            str(self.get_parameter("integration_preflight_node").value),
            callback_group=self._callback_group,
        )
        self._parameter_clients = {
            # The typed voice resolver and direct execution bridge are part of
            # the Live procedure contract.  They must reload their catalog/
            # instrument allowlist with every stopped-state Live switch, or a
            # stale alias could be interpreted against the next bundle.
            "/voice_command_resolver": AsyncParameterClient(
                self,
                "/voice_command_resolver",
                callback_group=self._callback_group,
            ),
            "/surgical_interop_execution_bridge": AsyncParameterClient(
                self,
                "/surgical_interop_execution_bridge",
                callback_group=self._callback_group,
            ),
            "/surgical_interop_gateway": AsyncParameterClient(
                self,
                "/surgical_interop_gateway",
                callback_group=self._callback_group,
            ),
            "/mock_vlm_node": AsyncParameterClient(
                self, "/mock_vlm_node", callback_group=self._callback_group
            ),
            "/real_vlm_node": AsyncParameterClient(
                self, "/real_vlm_node", callback_group=self._callback_group
            ),
            "/no_image_camera": AsyncParameterClient(
                self, "/no_image_camera", callback_group=self._callback_group
            ),
            "/phase_estimator": AsyncParameterClient(
                self, "/phase_estimator", callback_group=self._callback_group
            ),
            "/or_digital_twin": AsyncParameterClient(
                self, "/or_digital_twin", callback_group=self._callback_group
            ),
            "/surgeon_actor": AsyncParameterClient(
                self, "/surgeon_actor", callback_group=self._callback_group
            ),
            "/bed_robot_arm_group_orchestrator": AsyncParameterClient(
                self,
                "/bed_robot_arm_group_orchestrator",
                callback_group=self._callback_group,
            ),
        }
        self.create_service(
            SelectSimulationBundle,
            "/simulation/select_bundle",
            self._handle_select_bundle,
            callback_group=self._callback_group,
        )
        self.create_service(
            ControlSimulation,
            "/simulation/control",
            self._handle_control,
            callback_group=self._callback_group,
        )
        self.create_service(
            Trigger,
            "/simulation/check_transition_ready",
            self._handle_check_transition_ready,
            callback_group=self._callback_group,
        )
        self.create_service(
            Trigger,
            "/simulation/reserve_transition",
            self._handle_reserve_transition,
            callback_group=self._callback_group,
        )
        self.create_service(
            InjectSurgeonOverride,
            "/simulation/inject_surgeon_override",
            self._handle_override,
            callback_group=self._callback_group,
        )

    def _wait_future(self, future, timeout_sec: float = 10.0):
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if future.done():
                return future.result()
            time.sleep(0.05)
        raise TimeoutError("Timed out waiting for async operation to complete.")

    def _load_spec_for_bundle(self, bundle_name: str):
        bundle_id = validate_bundle_name(bundle_name)
        spec_root = Path(self._spec_root).resolve()
        bundle_dir = (spec_root / bundle_id).resolve()
        try:
            bundle_dir.relative_to(spec_root)
        except ValueError as exc:
            raise ValueError(
                f"bundle '{bundle_id}' resolves outside the configured spec root"
            ) from exc
        if not bundle_dir.is_dir():
            raise FileNotFoundError(
                f"bundle '{bundle_id}' not found under {spec_root}"
            )
        return bundle_dir, load_bundle(bundle_dir)

    def _bundle_config_revision(self, spec_dir: str | Path) -> str:
        return compute_bundle_config_revision(spec_dir)

    def _load_spec_candidate(self, bundle_name: str):
        """Load one revision-stable candidate, retrying an in-progress save."""

        spec_root = getattr(self, "_spec_root", None)
        if spec_root is None:
            spec_dir, spec = self._load_spec_for_bundle(bundle_name)
            return spec_dir, spec, self._bundle_config_revision(spec_dir)

        bundle_id = validate_bundle_name(bundle_name)
        spec_root_path = Path(spec_root).resolve()
        expected_spec_dir = (spec_root_path / bundle_id).resolve()
        try:
            expected_spec_dir.relative_to(spec_root_path)
        except ValueError as exc:
            raise ValueError(
                f"bundle '{bundle_id}' resolves outside the configured spec root"
            ) from exc
        for _attempt in range(3):
            before_revision = self._bundle_config_revision(expected_spec_dir)
            spec_dir, spec = self._load_spec_for_bundle(bundle_name)
            after_revision = self._bundle_config_revision(spec_dir)
            if before_revision == after_revision:
                return spec_dir, spec, after_revision
        raise RuntimeError(
            f"bundle '{bundle_name}' changed while it was being loaded; retry preview or reload"
        )

    def _set_bundle_response_metadata(
        self,
        response,
        *,
        candidate_revision: str | None = None,
        changed: bool = False,
        applied: bool = False,
        disposition: str = "rejected",
    ) -> None:
        """Populate revision fields while tolerating a rolling interface build.

        The ``AttributeError`` fallback matters only while a source workspace
        has the new manager code but still has the previous generated service
        class installed.  Once ``surgical_msgs`` is rebuilt, all fields are
        present on the typed response.
        """

        active_revision = str(
            getattr(self, "_active_config_revision", "") or ""
        )
        values = {
            "active_config_revision": active_revision,
            "candidate_config_revision": (
                active_revision
                if candidate_revision is None
                else str(candidate_revision or "")
            ),
            "changed": bool(changed),
            "applied": bool(applied),
            "disposition": str(disposition or "rejected"),
        }
        for name, value in values.items():
            try:
                setattr(response, name, value)
            except AttributeError:
                # Generated ROS response classes reject fields unknown to an
                # older install.  Preserve the legacy response during the one
                # build needed to regenerate this interface.
                pass

    def _spec_dir_for_bundle(self, bundle_name: str) -> Path:
        spec_dir, _ = self._load_spec_for_bundle(bundle_name)
        return spec_dir

    def _publish_control(self, command: str, repeat_count: int = 2) -> None:
        deadline = time.time() + 2.0
        while self._control_pub.get_subscription_count() < 1 and time.time() < deadline:
            time.sleep(0.05)
        msg = String()
        msg.data = command
        for _ in range(max(1, repeat_count)):
            self._control_pub.publish(msg)
            time.sleep(0.05)

    @staticmethod
    def _control_with_phase(command: str, phase_id: str = "") -> str:
        phase_id = str(phase_id or "").strip()
        return f"{command}:{phase_id}" if phase_id else command

    def _normalize_start_phase(self, phase_id: str = "") -> str:
        requested = str(phase_id or "").strip()
        if not requested:
            return ""
        if requested not in self._active_spec.phase_ids:
            allowed = ", ".join(self._active_spec.phase_ids)
            raise ValueError(f"unknown start phase '{requested}' for {self._active_bundle}; allowed: {allowed}")
        return requested

    def _on_simulation_state(self, msg: SimulationState) -> None:
        with self._latest_state_lock:
            self._latest_state = msg
            self._latest_state_generation += 1
            # Receipt time, rather than source time, is the mode-transition
            # liveness contract.  Identical semantic heartbeats must refresh it.
            self._latest_state_received_monotonic = time.monotonic()
        if msg.execution_state == "completed":
            should_terminate = self._running or self._execution_state != "completed"
            self._running = False
            self._execution_state = "completed"
            if should_terminate and not self._completion_terminate_started:
                self._completion_terminate_started = True
                thread = threading.Thread(
                    target=self._terminate_executor_after_completion,
                    name="simulation-completion-terminate",
                    daemon=True,
                )
                thread.start()

    def _on_voice_procedure_intent(self, msg: VoiceCommandIntent) -> None:
        """Forward one validated spoken lifecycle request through normal control.

        No robot Action or service is called from this subscription.  Start
        remains blocked by `_check_integration_preflight`; end speech reuses
        the same pause path as the operator UI.
        """

        now_sec = self.get_clock().now().nanoseconds / 1_000_000_000.0
        admission = evaluate_voice_procedure_intent(
            msg,
            active_procedure_id=self._active_bundle,
            now_sec=now_sec,
            min_confidence=self._voice_procedure_min_confidence,
            accept_missing_confidence=(
                self._voice_procedure_accept_missing_confidence
            ),
            max_age_sec=self._voice_procedure_max_age_sec,
            max_future_skew_sec=self._voice_procedure_max_future_skew_sec,
            running=self._running,
            execution_state=self._execution_state,
        )
        if not admission.accepted:
            self.get_logger().warning(
                f"voice procedure command rejected: {admission.reason}",
                throttle_duration_sec=2.0,
            )
            return
        if not self._voice_procedure_intent_ids.accept(
            str(msg.utterance_id),
            time.monotonic(),
        ):
            self.get_logger().warning(
                "voice procedure command rejected: duplicate_utterance_id",
                throttle_duration_sec=2.0,
            )
            return
        request = ControlSimulation.Request()
        request.command = admission.command
        request.start_phase_id = ""
        result = self._handle_control(request, ControlSimulation.Response())
        self._publish_voice_procedure_result(
            msg=msg,
            command=admission.command,
            result=result,
        )
        if result.success:
            self.get_logger().info(
                "voice procedure command admitted: "
                f"{admission.command} ({msg.utterance_id})"
            )
        else:
            self.get_logger().warning(
                "voice procedure command blocked by simulation control: "
                f"{result.message}",
                throttle_duration_sec=2.0,
            )

    def _publish_voice_procedure_result(
        self,
        *,
        msg: VoiceCommandIntent,
        command: str,
        result,
    ) -> None:
        """Publish a bounded, transcript-free receipt for operational sidecars."""

        publisher = self._voice_procedure_result_pub
        if publisher is None:
            return
        payload = {
            "schema": "taskplanner.voice_procedure_control.result.v1",
            "stamp_sec": round(
                self.get_clock().now().nanoseconds / 1_000_000_000.0,
                6,
            ),
            "utterance_id": str(getattr(msg, "utterance_id", "") or "")[:128],
            "procedure_id": str(getattr(msg, "procedure_id", "") or "")[:64],
            "requested_command": str(command or "")[:16],
            "success": bool(getattr(result, "success", False)),
            "message": str(getattr(result, "message", "") or "")[:512],
            "running": bool(getattr(result, "running", self._running)),
            "execution_state": str(
                getattr(result, "execution_state", self._execution_state) or ""
            )[:32],
        }
        publisher.publish(
            String(data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        )

    def _terminate_executor_after_completion(self) -> None:
        try:
            self._command_executor("terminate")
            self._wait_for_executor_idle(timeout_sec=8.0)
        except Exception as exc:
            self.get_logger().warn(f"failed to terminate executor after completion: {exc}")
        finally:
            self._running = False
            self._execution_state = "completed"
            self._completion_terminate_started = False

    def _wait_for_simulation_state(
        self,
        predicate,
        timeout_sec: float,
        description: str,
        *,
        warn: bool = True,
        after_generation: int = -1,
    ) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._latest_state_lock:
                state = self._latest_state
                generation = self._latest_state_generation
            if state is not None and generation > after_generation:
                try:
                    if predicate(state):
                        return True
                except Exception:
                    pass
            time.sleep(0.1)
        if warn:
            self.get_logger().warn(f"Timed out waiting for {description}.")
        return False

    def _raise_if_operation_cancelled(self) -> None:
        if self._operation_cancel.is_set():
            raise RuntimeError("operation interrupted by newer control command")

    def _publish_control_until(self, command: str, predicate, timeout_sec: float, description: str) -> bool:
        deadline = time.time() + timeout_sec
        attempts = 3
        for attempt in range(attempts):
            if self._operation_cancel.is_set():
                return False
            with self._latest_state_lock:
                previous_generation = self._latest_state_generation
            self._publish_control(command, repeat_count=2)
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait_slice = max(0.5, remaining / max(1, attempts - attempt))
            if self._wait_for_simulation_state(
                predicate,
                timeout_sec=min(wait_slice, remaining),
                description=description,
                warn=False,
                after_generation=previous_generation,
            ):
                return True
        self.get_logger().warn(f"Timed out waiting for {description}.")
        return False

    def _all_instruments_at_initial_layout(self, state: SimulationState) -> bool:
        if not state.instrument_states:
            return False
        configured_states = {
            str(initial_state.instance_id): initial_state
            for initial_state in self._active_spec.get_initial_instrument_states()
        }
        observed_configured_instances: set[str] = set()
        for instrument in state.instrument_states:
            instance_id = str(instrument.instance_id)
            configured_state = configured_states.get(instance_id)
            if configured_state is not None:
                observed_configured_instances.add(instance_id)
                if str(instrument.location_id) != str(configured_state.location_id):
                    return False
                if (
                    configured_state.lifecycle_stage
                    and str(instrument.lifecycle_stage)
                    != str(configured_state.lifecycle_stage)
                ):
                    return False
                continue
            home_location_id = str(instrument.home_location_id)
            home_location_type = str(instrument.home_location_type)
            if home_location_id and str(instrument.location_id) != home_location_id:
                return False
            if home_location_type and str(instrument.location_type) != home_location_type:
                return False
        return observed_configured_instances == set(configured_states)

    def _set_spec_dir_on_runtime(
        self,
        spec_dir: Path,
        *,
        rollback_spec_dir: Path | None = None,
    ) -> set[str]:
        """Reload every present procedure-aware participant.

        For Live, the resolver, direct bridge, and authoritative digital twin
        are mandatory.  A VLM/actor which is not launched is harmless; one
        that is present but rejects the update is not.  If any update fails,
        best-effort rollback leaves the integration preflight closed, so an
        incomplete procedure transition can never dispatch a command.
        """

        # Optional participants may be launch-disabled, so absence is allowed.
        # Once a parameter service is present, however, a rejection is always
        # transactional in every mode; otherwise consumers can retain mixed
        # revisions of the same procedure bundle.
        live_required_clients = {
            "/or_digital_twin",
            "/voice_command_resolver",
            "/surgical_interop_execution_bridge",
        }
        if not bool(getattr(self, "_require_integration_preflight", False)):
            live_required_clients = {"/or_digital_twin"}
        required_clients = [
            (name, client)
            for name, client in self._parameter_clients.items()
            if name in live_required_clients
        ]
        optional_parameter_clients = [
            (name, client)
            for name, client in self._parameter_clients.items()
            if name not in live_required_clients
        ]
        updated_clients: set[str] = set()

        def parameter_for(name: str, value: Path) -> Parameter:
            # ``procedure_bundle`` is the resolver's active catalog source;
            # all other spec-aware nodes use the common ``spec_dir`` name.
            parameter_name = (
                "procedure_bundle"
                if name == "/voice_command_resolver"
                else "spec_dir"
            )
            return Parameter(name=parameter_name, value=str(value))

        def update_client(name, client, value: Path, wait_sec: float) -> bool:
            ready = client.services_are_ready()
            if not ready and wait_sec > 0:
                ready = client.wait_for_services(timeout_sec=wait_sec)
            if ready:
                future = client.set_parameters([parameter_for(name, value)])
                response = self._wait_future(future, timeout_sec=10.0)
                results = getattr(response, "results", response or [])
                failed = [
                    result
                    for result in results
                    if not bool(getattr(result, "successful", False))
                ]
                if failed:
                    reason = "; ".join(
                        str(getattr(result, "reason", "")) for result in failed
                    ).strip()
                    raise RuntimeError(
                        f"spec update rejected by {name}: {reason or 'unknown reason'}"
                    )
                updated_clients.add(name)
                return True
            return False

        def rollback_updated_clients() -> list[str]:
            if rollback_spec_dir is None:
                return []
            rollback_failures: list[str] = []
            for name in sorted(updated_clients, reverse=True):
                client = self._parameter_clients.get(name)
                if client is None:
                    continue
                try:
                    if not update_client(name, client, rollback_spec_dir, wait_sec=1.0):
                        rollback_failures.append(f"{name}:parameter_service_unavailable")
                except Exception as exc:
                    rollback_failures.append(f"{name}:{exc}")
            return rollback_failures

        try:
            deadline = time.time() + 8.0
            pending_clients = required_clients
            while pending_clients and time.time() < deadline:
                still_pending = []
                for name, client in pending_clients:
                    if not update_client(name, client, spec_dir, wait_sec=0.25):
                        still_pending.append((name, client))
                pending_clients = still_pending
            if pending_clients:
                missing = ", ".join(name for name, _ in pending_clients)
                raise TimeoutError(f"parameter services not ready for: {missing}")

            for name, client in optional_parameter_clients:
                try:
                    if not update_client(name, client, spec_dir, wait_sec=0.05):
                        self.get_logger().info(
                            "optional parameter service unavailable, skipping "
                            f"spec update for {name}"
                        )
                except Exception as exc:
                    raise RuntimeError(
                        f"spec update rejected by present participant {name}: {exc}"
                    ) from exc
        except Exception as exc:
            rollback_failures = rollback_updated_clients()
            if rollback_failures:
                raise RuntimeError(
                    "procedure reload failed and rollback was incomplete: "
                    + "; ".join(rollback_failures[:4])
                ) from exc
            raise
        if not updated_clients.intersection({"/mock_vlm_node", "/real_vlm_node"}):
            self.get_logger().info(
                "no VLM parameter service was available during bundle switch; "
                "continuing without direct spec update"
            )
        return updated_clients

    def _start_behavior(self, clear_blackboard: bool) -> tuple[bool, str]:
        if not self._start_client.wait_for_service(timeout_sec=5.0):
            return False, "btops start_behavior service is unavailable"
        last_message = "btops start_behavior returned no response"
        for attempt in range(5):
            self._raise_if_operation_cancelled()
            self._wait_for_executor_idle(timeout_sec=3.0)
            requested_groot2_port = self._reserve_groot2_port()
            request = StartBehavior.Request()
            request.executor_name = self._executor_name
            request.mode = "resource"
            request.category = "tree"
            request.resource_identity = RESOURCE_ID
            request.inline_source = ""
            request.source_format = ""
            request.build_handler = ""
            request.entry_point = ENTRY_POINT
            request.node_manifest_identities = list(NODE_MANIFESTS)
            request.attach = False
            request.clear_blackboard = clear_blackboard
            request.enable_monitoring = True
            request.tick_rate_hz = float(self._tick_rate_hz)
            request.requested_groot2_port = int(requested_groot2_port)
            request.parameter_assignments = []
            # From dispatch until a terminal executor snapshot is observed, a
            # mode transition must fail closed even if the digital twin has
            # already published an inactive-looking frame.
            self._executor_settled_confirmed = False
            future = self._start_client.call_async(request)
            deadline = time.time() + 15.0
            response = None
            while time.time() < deadline:
                self._raise_if_operation_cancelled()
                if future.done():
                    response = future.result()
                    break
                time.sleep(0.05)
            if response is None:
                if self._wait_for_executor_running(timeout_sec=4.0):
                    return True, "simulation started after delayed start_behavior response"
                time.sleep(0.25 * (attempt + 1))
                continue
            if response.success:
                return True, str(response.message)
            last_message = str(response.message)
            self._restore_settled_after_explicit_start_rejection()
            lowered_message = last_message.lower()
            if any(
                marker in lowered_message
                for marker in RETRYABLE_START_ERROR_MARKERS
            ):
                self.get_logger().warn(
                    "retrying BT start after transient executor failure "
                    f"(attempt={attempt + 1}, groot2_port={requested_groot2_port}): "
                    f"{last_message}"
                )
                if self._wait_for_executor_running(timeout_sec=4.0):
                    return True, last_message
                self._command_executor("terminate")
                self._wait_for_executor_idle(timeout_sec=8.0)
                time.sleep(0.35 * (attempt + 1))
                continue
            return False, last_message
        return False, last_message

    def _restore_settled_after_explicit_start_rejection(self) -> bool:
        """Recover the initial no-session invariant after an explicit reject.

        A timed-out StartBehavior call may still have dispatched work, so it
        deliberately leaves the executor fail-closed.  Only an actual failed
        response followed by BTops' exact no-snapshot result proves that the
        rejected request created no executor session.
        """

        try:
            success, _state, detail = self._get_runtime_state_detail(
                service_timeout_sec=0.25,
                response_timeout_sec=0.75,
            )
        except Exception:
            return False
        if not success and str(detail).startswith(NO_RUNTIME_SNAPSHOT_PREFIX):
            self._executor_settled_confirmed = True
            return True
        return False

    def _reserve_groot2_port(self) -> int:
        if self._groot2_port > 0:
            return int(self._groot2_port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            return int(sock.getsockname()[1])

    def _command_executor(self, command: str) -> tuple[bool, str]:
        if not self._command_client.wait_for_service(timeout_sec=5.0):
            return False, "btops command_executor service is unavailable"
        request = CommandExecutor.Request()
        request.executor_name = self._executor_name
        request.command = command
        future = self._command_client.call_async(request)
        response = self._wait_future(future, timeout_sec=10.0)
        if response is None:
            return False, "btops command_executor returned no response"
        return bool(response.success), str(response.message)

    def _get_runtime_state_detail(
        self,
        *,
        service_timeout_sec: float = 5.0,
        response_timeout_sec: float = 10.0,
    ) -> tuple[bool, str, str]:
        if not self._runtime_client.wait_for_service(
            timeout_sec=service_timeout_sec
        ):
            return False, "unknown", "btops runtime-state service is unavailable"
        request = GetRuntimeState.Request()
        request.executor_name = self._executor_name
        future = self._runtime_client.call_async(request)
        response = self._wait_future(future, timeout_sec=response_timeout_sec)
        if response is None:
            return False, "unknown", "btops runtime-state returned no response"
        if not response.success:
            return False, "unknown", str(response.message or "runtime state unavailable")
        return (
            True,
            str(response.snapshot.execution_state).strip().lower(),
            str(response.message or "ok"),
        )

    def _get_runtime_state(self) -> tuple[bool, str]:
        success, state, _detail = self._get_runtime_state_detail()
        return success, state

    def _wait_for_executor_idle(self, timeout_sec: float = 8.0) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                success, state, detail = self._get_runtime_state_detail()
                if success and state in TRANSITION_READY_EXECUTOR_STATES:
                    self._executor_settled_confirmed = True
                    return True
                if (
                    not success
                    and str(detail).startswith(NO_RUNTIME_SNAPSHOT_PREFIX)
                    and self._executor_settled_confirmed
                ):
                    return True
            except Exception:
                pass
            time.sleep(0.15)
        return False

    def _transition_reservation_active_locked(self) -> bool:
        deadline = float(getattr(self, "_transition_reservation_until_monotonic", 0.0))
        if deadline <= 0.0:
            return False
        if time.monotonic() >= deadline:
            self._transition_reservation_until_monotonic = 0.0
            return False
        return True

    def _local_transition_ready_status_locked(
        self,
        *,
        allow_current_bundle_transition: bool = False,
    ) -> tuple[bool, str]:
        """Check all cheap local and digital-twin gates under ``_operation_lock``."""

        if self._transition_reservation_active_locked():
            return False, "runtime mode transition is already reserved"
        operation = str(self._operation_name or "")
        if operation:
            return False, f"simulation operation is still pending: {operation}"
        if (
            bool(getattr(self, "_bundle_transition_in_progress", False))
            and not allow_current_bundle_transition
        ):
            return False, "simulation bundle selection is still pending"
        if bool(getattr(self, "_override_in_progress", False)):
            return False, "surgeon override publication is still pending"
        if self._operation_cancel.is_set():
            return False, "simulation operation cancellation is still settling"
        if self._completion_terminate_started:
            return False, "completion-triggered executor termination is still pending"
        if self._running or self._execution_state not in TRANSITION_READY_MANAGER_STATES:
            return False, f"simulation manager is not inactive: {self._execution_state}"

        with self._latest_state_lock:
            state = self._latest_state
            received_monotonic = float(
                getattr(self, "_latest_state_received_monotonic", 0.0)
            )
        if state is None:
            return False, "digital twin state is unavailable"
        age = time.monotonic() - received_monotonic
        if received_monotonic <= 0.0 or age > TRANSITION_STATE_MAX_AGE_SEC:
            return False, "digital twin state receipt is stale"
        if bool(state.running) or str(state.execution_state).strip().lower() not in (
            TRANSITION_READY_MANAGER_STATES
        ):
            return False, "digital twin has not reached an inactive state"
        if str(getattr(state, "active_robot_task_id", "") or "").strip():
            return False, "a robot task is still active"
        if bool(getattr(state, "cleaner_busy", False)):
            return False, "cleaner activity is still pending"
        if list(getattr(state, "pending_transition_tools", []) or []):
            return False, "tool transitions are still pending"
        if list(getattr(state, "active_recovery_tools", []) or []):
            return False, "tool recovery is still active"

        return True, "local runtime state is transition ready"

    def _transition_ready_status_locked(
        self,
        *,
        allow_current_bundle_transition: bool = False,
    ) -> tuple[bool, str]:
        """Return readiness while the caller owns ``_operation_lock``."""

        ready, message = self._local_transition_ready_status_locked(
            allow_current_bundle_transition=allow_current_bundle_transition
        )
        if not ready:
            return False, message

        try:
            success, executor_state, detail = self._get_runtime_state_detail(
                service_timeout_sec=0.5,
                response_timeout_sec=1.5,
            )
        except Exception as exc:
            return False, f"executor state check failed: {exc}"

        # The executor query may take up to two seconds while SimulationState
        # callbacks continue in the reentrant group.  Recheck every cheap local
        # gate and the receipt deadline immediately before accepting/reserving.
        ready, message = self._local_transition_ready_status_locked(
            allow_current_bundle_transition=allow_current_bundle_transition
        )
        if not ready:
            return False, message
        if success and executor_state in TRANSITION_READY_EXECUTOR_STATES:
            self._executor_settled_confirmed = True
            return True, (
                f"{TRANSITION_PROTOCOL_MARKER} "
                f"transition ready; executor={executor_state}"
            )
        if (
            not success
            and str(detail).startswith(NO_RUNTIME_SNAPSHOT_PREFIX)
            and self._executor_settled_confirmed
        ):
            return True, (
                f"{TRANSITION_PROTOCOL_MARKER} "
                "transition ready; executor has no active session"
            )
        if not success:
            return False, f"executor state is unavailable: {detail}"
        return False, f"executor is not settled: {executor_state or 'unknown'}"

    def _live_bundle_switch_safe_status_locked(
        self,
        *,
        allow_current_bundle_transition: bool = False,
    ) -> tuple[bool, str]:
        """Require a completely quiescent Live runtime before a spec swap.

        This is deliberately independent of integration *readiness*: an ASR,
        camera, or remote controller may be unavailable while the operator is
        choosing the next procedure.  The only admission here is that no
        execution-capable local state remains and the latest twin frame says
        the robot is idle, clean-up/recovery is complete, and no fault exists.
        A later ``start`` still runs the complete integration preflight.
        """

        ready, message = self._transition_ready_status_locked(
            allow_current_bundle_transition=allow_current_bundle_transition
        )
        if not ready:
            return False, message
        with self._latest_state_lock:
            state = self._latest_state
        if state is None:
            return False, "digital twin state is unavailable"
        robot_state = str(getattr(state, "robot_state", "") or "").strip().casefold()
        if robot_state in _FAULTED_ROBOT_STATES:
            return False, f"robot is faulted: {robot_state}"
        if robot_state not in _LIVE_SWITCH_SAFE_ROBOT_STATES:
            return False, f"robot is not idle: {robot_state or 'unknown'}"
        for group in list(getattr(state, "bed_robot_arm_groups", []) or []):
            group_state = str(getattr(group, "state", "") or "").strip().casefold()
            group_error = str(getattr(group, "error_code", "") or "").strip()
            if group_state in _FAULTED_ROBOT_STATES or group_error:
                return False, "bed-robot arm group reports a fault"
        return True, "live runtime is stopped and safe for bundle selection"

    def _transition_ready_status(self) -> tuple[bool, str]:
        """Return manager-authoritative readiness without reserving it."""

        with self._operation_lock:
            return self._transition_ready_status_locked()

    def _handle_check_transition_ready(self, _request, response):
        response.success, response.message = self._transition_ready_status()
        return response

    def _handle_reserve_transition(self, _request, response):
        with self._operation_lock:
            # Idempotent callers share the original deadline.  Never renew it:
            # an abandoned launcher must not strand the runtime indefinitely.
            if self._transition_reservation_active_locked():
                response.success = True
                response.message = (
                    f"{TRANSITION_PROTOCOL_MARKER} "
                    "runtime transition is already reserved"
                )
                return response
            ready, message = self._transition_ready_status_locked()
            if not ready:
                response.success = False
                response.message = message
                return response
            self._transition_reservation_until_monotonic = (
                time.monotonic()
                + float(getattr(self, "_transition_reservation_ttl_sec", 75.0))
            )
            response.success = True
            response.message = (
                f"{TRANSITION_PROTOCOL_MARKER} "
                "runtime transition reserved; mutation commands are blocked "
                "for up to "
                f"{float(getattr(self, '_transition_reservation_ttl_sec', 75.0)):.1f}s"
            )
            return response

    def _wait_for_executor_running(self, timeout_sec: float = 10.0) -> bool:
        deadline = time.time() + timeout_sec
        # AutoAPMS reports a successful StartTreeExecutor dispatch as
        # "accepted" and later often "succeeded" while the tree keeps ticking.
        # Treat those as live executor states so startup and bundle restart do
        # not burn the full timeout waiting for a state string that this backend
        # does not emit.
        running_states = {"running", "active", "executing", "starting", "accepted", "succeeded"}
        while time.time() < deadline:
            try:
                success, state = self._get_runtime_state()
                if success and state in running_states:
                    return True
            except Exception:
                pass
            time.sleep(0.15)
        return False

    def _prepare_executor_for_restart(self) -> None:
        if not self._running and self._execution_state in {"idle", "terminated", "halted"}:
            try:
                success, state = self._get_runtime_state()
                if not success or state in {"", "idle", "terminated", "halted", "unknown"}:
                    return
            except Exception:
                return
        try:
            self._command_executor("terminate")
            self._wait_for_executor_idle(timeout_sec=8.0)
        except Exception:
            # It is fine if the executor was not running yet.
            pass

    def _reset_digital_twin_to_idle(self, *, expected_bundle: str | None = None) -> None:
        bundle = self._active_bundle if expected_bundle is None else expected_bundle
        if not self._publish_control_until(
            "reset",
            lambda state: (
                (not bundle or state.active_bundle == bundle)
                and (not state.running)
                and state.execution_state == "idle"
                and self._all_instruments_at_initial_layout(state)
            ),
            timeout_sec=8.0,
            description="idle digital twin frame after reset",
        ):
            raise RuntimeError("digital twin did not publish an idle home frame after reset")
        self._set_idle_state()

    def _quiesce_runtime_for_bundle_change(self) -> None:
        """Close every execution gate before any runtime spec is replaced."""

        # Reset first so actors, group orchestrator/bridge, and downstream
        # action servers stop accepting work even while BT termination is in
        # progress.  Only then is it safe to mutate the shared spec_dir.
        # The launch-time digital twin may already use the requested spec while
        # the manager still holds its default bundle name. This first reset is
        # only a quiescence barrier; the post-update reset verifies the exact
        # selected bundle.
        self._reset_digital_twin_to_idle(expected_bundle="")
        self._prepare_executor_for_restart()

    def _stop_digital_twin_to_halted(self) -> None:
        if not self._publish_control_until(
            "stop",
            lambda state: (
                state.active_bundle == self._active_bundle
                and (not state.running)
                and state.execution_state == "halted"
            ),
            timeout_sec=5.0,
            description="halted digital twin frame after stop",
        ):
            self.get_logger().warn("digital twin did not publish a halted frame after stop")

    def _begin_operation(self, name: str) -> bool:
        with self._operation_lock:
            self._operation_rejection_message = ""
            if (
                name in {"start", "resume", "pause", "reset", "bundle-switch"}
                and self._transition_reservation_active_locked()
            ):
                self._operation_rejection_message = (
                    "runtime mode transition is reserved; mutation command rejected"
                )
                return False
            if self._operation_name:
                self._operation_rejection_message = (
                    f"{self._operation_name} already in progress"
                )
                return False
            self._operation_cancel.clear()
            self._operation_name = name
            return True

    def _finish_operation(self, name: str) -> None:
        with self._operation_lock:
            if self._operation_name == name:
                self._operation_name = ""
                self._operation_cancel.clear()

    def _wait_for_operation_clear(self, timeout_sec: float = 10.0) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            with self._operation_lock:
                if not self._operation_name:
                    return True
            time.sleep(0.1)
        return False

    def _run_async(self, name: str, target) -> tuple[bool, str]:
        if not self._begin_operation(name):
            return False, self._operation_rejection_message or "operation rejected"

        def runner() -> None:
            try:
                target()
            except Exception as exc:
                self.get_logger().error(f"{name} operation failed: {exc}")
                if name == "start" and not self._operation_cancel.is_set():
                    self._publish_control("stop")
                    self._set_idle_state()
            finally:
                self._finish_operation(name)

        thread = threading.Thread(target=runner, name=f"simulation-{name}", daemon=True)
        thread.start()
        return True, f"{name} requested"

    def _run_sync(self, name: str, target) -> tuple[bool, str]:
        if not self._begin_operation(name):
            return False, self._operation_rejection_message or "operation rejected"
        try:
            message = target()
            return True, str(message or f"{name} completed")
        except Exception as exc:
            self.get_logger().error(f"{name} operation failed: {exc}")
            return False, str(exc)
        finally:
            self._finish_operation(name)

    def _start_sequence(self, start_phase_id: str = "", *, prepare_executor: bool = True) -> str:
        start_phase_id = self._normalize_start_phase(start_phase_id)
        target_phase_id = start_phase_id or self._active_spec.default_phase_id
        self._running = False
        self._execution_state = "starting"
        self._completion_terminate_started = False
        self._check_integration_preflight()
        if prepare_executor:
            self._prepare_executor_for_restart()
        self._raise_if_operation_cancelled()
        if not self._publish_control_until(
            "reset",
            lambda state: (
                state.active_bundle == self._active_bundle
                and (not state.running)
                and state.execution_state == "idle"
                and self._all_instruments_at_initial_layout(state)
            ),
            timeout_sec=8.0,
            description="initial idle digital twin frame",
        ):
            self._raise_if_operation_cancelled()
            raise RuntimeError("digital twin did not publish an idle home frame after reset")
        self._raise_if_operation_cancelled()
        if not self._publish_control_until(
            self._control_with_phase("start_runtime", start_phase_id),
            lambda state: (
                state.active_bundle == self._active_bundle
                and state.running
                and state.execution_state == "running"
                and state.filtered_phase == target_phase_id
                and self._all_instruments_at_initial_layout(state)
            ),
            timeout_sec=8.0,
            description=f"initial home running digital twin frame at {target_phase_id}",
        ):
            self._raise_if_operation_cancelled()
            raise RuntimeError("digital twin did not enter running state before BT start")
        self._raise_if_operation_cancelled()
        success, message = self._start_behavior(clear_blackboard=True)
        if success:
            self._raise_if_operation_cancelled()
            if not self._wait_for_executor_running(timeout_sec=10.0):
                self._publish_control("stop")
                self._set_idle_state()
                raise RuntimeError("behavior tree executor did not enter a running state")
            # This is also the bed-group dispatch gate.  No surgeon actor or
            # external group request can cause motion before BT startup is
            # positively confirmed.
            self._commit_start_actors(start_phase_id)
            self.get_logger().info(message or "simulation started")
            phase_suffix = f" from {target_phase_id}" if start_phase_id else ""
            return message or f"simulation running on {self._active_bundle}{phase_suffix}"
        self._publish_control("stop")
        self._set_idle_state()
        raise RuntimeError(message or "failed to start simulation")

    def _configure_integration_preflight(
        self,
        bundle_name: str,
        spec,
        *,
        transitioning: bool,
    ) -> None:
        if not bool(getattr(self, "_require_integration_preflight", False)):
            return
        client = getattr(self, "_integration_preflight_parameter_client", None)
        if client is None or not client.wait_for_services(timeout_sec=2.0):
            raise RuntimeError("integration preflight parameter service is unavailable")
        contract = external_robot_contract_for_spec(spec)
        spec_dir = self._spec_dir_for_bundle(bundle_name)
        response = self._wait_future(
            client.set_parameters_atomically(
                [
                    Parameter(name="active_bundle", value=str(bundle_name)),
                    Parameter(name="spec_dir", value=str(spec_dir)),
                    Parameter(
                        name="procedure_type",
                        value=contract.procedure_type,
                    ),
                    Parameter(
                        name="require_retraction_service",
                        value=contract.require_retraction_service,
                    ),
                    Parameter(
                        name="require_tool_handover_action_server",
                        value=contract.require_tool_handover_action_server,
                    ),
                    Parameter(
                        name="require_bed_robot_arm_status",
                        value=contract.require_bed_robot_arm_status,
                    ),
                    Parameter(
                        name="contract_transitioning",
                        value=bool(transitioning),
                    ),
                ]
            ),
            timeout_sec=5.0,
        )
        result = getattr(response, "result", None)
        if result is None or not bool(getattr(result, "successful", False)):
            reason = str(getattr(result, "reason", "") or "unknown reason")
            raise RuntimeError(f"integration preflight contract update rejected: {reason}")

    def _check_integration_preflight(self) -> None:
        if not bool(getattr(self, "_require_integration_preflight", False)):
            return
        self._configure_integration_preflight(
            self._active_bundle,
            self._active_spec,
            transitioning=False,
        )
        client = getattr(self, "_integration_preflight_client", None)
        if client is None or not client.wait_for_service(timeout_sec=2.0):
            raise RuntimeError("integration preflight service is unavailable")
        deadline = time.monotonic() + float(
            getattr(self, "_integration_preflight_timeout_sec", 5.0)
        )
        detail = "readiness check failed"
        while time.monotonic() < deadline:
            self._raise_if_operation_cancelled()
            remaining = deadline - time.monotonic()
            response = self._wait_future(
                client.call_async(Trigger.Request()),
                timeout_sec=max(0.1, min(1.0, remaining)),
            )
            if response is not None and bool(response.success):
                return
            detail = str(
                getattr(response, "message", "") or "readiness check failed"
            )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        raise RuntimeError(detail)

    def _commit_start_actors(self, start_phase_id: str = "") -> None:
        """Atomically commit the final start gate against stop/reset.

        Holding the operation lock across the cancellation check, control
        publication, and local state commit guarantees one of two orders:
        start_actors then reset, or reset with no later start_actors.
        """

        with self._operation_lock:
            if self._operation_cancel.is_set():
                raise RuntimeError("operation interrupted by newer control command")
            self._publish_control(
                self._control_with_phase("start_actors", start_phase_id)
            )
            self._running = True
            self._execution_state = "running"
            self._bundle_dirty = False

    def _interrupt_start_sequence(self, command: str) -> str:
        with self._operation_lock:
            self._operation_cancel.set()
            self._running = False
            self._execution_state = "resetting" if command == "reset" else "stopping"
        self._publish_control("reset" if command == "reset" else "stop")
        try:
            success, message = self._command_executor("terminate")
            if not success:
                raise RuntimeError(message or "failed to request executor termination")
            if not self._wait_for_executor_idle(timeout_sec=6.0):
                raise RuntimeError("behavior tree executor did not settle after termination")
        except Exception as exc:
            self.get_logger().warn(f"failed to terminate executor while interrupting start: {exc}")
            raise
        if command == "reset":
            self._publish_control("reset")
            self._set_idle_state()
            return "start interrupted; simulation runtime reset to idle"
        self._execution_state = "halted"
        return "start interrupted; simulation stopped"

    def _pause_sequence(self) -> str:
        if self._execution_state == "paused":
            return "simulation already paused"
        if not self._running or self._execution_state != "running":
            raise RuntimeError("simulation is not running")
        success, message = self._command_executor("pause")
        if not success:
            raise RuntimeError(message or "failed to pause simulation")
        self._publish_control("pause")
        self._running = True
        self._execution_state = "paused"
        self.get_logger().info(message or "simulation paused")
        return message or "simulation paused"

    def _resume_sequence(self) -> str:
        if self._running and self._execution_state == "running":
            return "simulation already running"
        if self._execution_state != "paused":
            raise RuntimeError("simulation is not paused")
        # A pause is a potentially long loss-of-control interval.  Live
        # integration must therefore re-admit every execution dependency
        # before publishing a resume frame or waking the behavior tree.  Keep
        # the local twin and BT paused when admission fails; publishing resume
        # first would create a partial restart that cannot be rolled back
        # safely if ASR, perception, or the controller route disappeared.
        self._check_integration_preflight()
        self._publish_control("resume")
        self._wait_for_simulation_state(
            lambda state: state.running and state.execution_state == "running" and len(state.instrument_states) > 0,
            timeout_sec=5.0,
            description="resumed digital twin frame",
        )
        success, message = self._command_executor("resume")
        if not success:
            self._publish_control("pause")
            raise RuntimeError(message or "failed to resume simulation")
        self._wait_for_executor_running(timeout_sec=8.0)
        self._running = True
        self._execution_state = "running"
        self.get_logger().info(message or "simulation resumed")
        return message or "simulation resumed"

    def _stop_sequence(self) -> str:
        self._running = False
        self._execution_state = "stopping"
        self._stop_digital_twin_to_halted()
        success, message = self._command_executor("terminate")
        if not success:
            raise RuntimeError(message or "failed to request executor termination")
        if not self._wait_for_executor_idle(timeout_sec=8.0):
            raise RuntimeError("behavior tree executor did not settle after termination")
        if success:
            self._execution_state = "halted"
            self.get_logger().info(message or "simulation stopped")
            return message or "simulation stopped"
        self._execution_state = "halted"
        raise RuntimeError(message or "failed to stop simulation")

    def _reset_sequence(self) -> str:
        self._bundle_dirty = False
        self._running = False
        self._execution_state = "resetting"
        self._completion_terminate_started = False
        self._prepare_executor_for_restart()
        self._reset_digital_twin_to_idle()
        return "simulation runtime reset to idle"

    def _set_idle_state(self) -> None:
        self._running = False
        self._execution_state = "idle"

    def _handle_select_bundle(self, request, response):
        self._set_bundle_response_metadata(response)
        if bool(getattr(request, "preview_only", False)):
            # Preview is read-only: it remains available while a procedure or
            # runtime-mode transition is in progress.
            return self._handle_select_bundle_impl(request, response)
        with self._operation_lock:
            if self._transition_reservation_active_locked():
                response.success = False
                response.message = (
                    "runtime mode transition is reserved; bundle selection rejected"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                return response
            if bool(getattr(self, "_bundle_transition_in_progress", False)):
                response.success = False
                response.message = "another bundle selection is already in progress"
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                return response
            requested_bundle = str(getattr(request, "bundle_name", "")).strip()
            if requested_bundle:
                try:
                    requested_bundle = validate_bundle_name(requested_bundle)
                except ValueError as exc:
                    response.success = False
                    response.message = str(exc)
                    response.active_bundle = self._active_bundle
                    response.spec_dir = str(self._active_spec_dir)
                    return response
            # A Live selection never interrupts or restarts a procedure. It
            # is available while stopped even if ASR/perception/controller
            # readiness is red, but only after the manager's authoritative
            # twin+executor safety proof succeeds.
            if (
                bool(getattr(self, "_require_integration_preflight", False))
                and requested_bundle
                and requested_bundle != self._active_bundle
            ):
                ready, message = self._live_bundle_switch_safe_status_locked()
                if not ready:
                    active_procedure = (
                        self._operation_name == "start"
                        or self._running
                        or self._execution_state
                        in {"starting", "running", "paused"}
                    )
                    response.success = False
                    response.message = (
                        (
                            "live bundle switch deferred (not queued): "
                            if active_procedure
                            else "live bundle selection is blocked: "
                        )
                        + message
                    )
                    response.active_bundle = self._active_bundle
                    response.spec_dir = str(self._active_spec_dir)
                    self._set_bundle_response_metadata(
                        response,
                        candidate_revision="",
                        changed=True,
                        disposition=(
                            "deferred" if active_procedure else "blocked"
                        ),
                    )
                    return response
            self._bundle_transition_in_progress = True
        try:
            return self._handle_select_bundle_impl(request, response)
        finally:
            with self._operation_lock:
                self._bundle_transition_in_progress = False

    def _handle_select_bundle_impl(self, request, response):
        candidate_revision = ""
        changed = False
        self._set_bundle_response_metadata(response)
        try:
            requested_bundle = str(getattr(request, "bundle_name", "")).strip()
            is_live = bool(getattr(self, "_require_integration_preflight", False))
            preview_only = bool(getattr(request, "preview_only", False))
            reload_if_changed = bool(
                getattr(request, "reload_if_changed", False)
            )
            if not requested_bundle:
                response.success = False
                response.message = "bundle name is required"
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                return response
            requested_bundle = validate_bundle_name(requested_bundle)

            spec_dir, spec, candidate_revision = self._load_spec_candidate(
                requested_bundle
            )
            same_bundle = requested_bundle == self._active_bundle
            changed = (
                not same_bundle
                or candidate_revision
                != str(getattr(self, "_active_config_revision", "") or "")
            )
            self._set_bundle_response_metadata(
                response,
                candidate_revision=candidate_revision,
                changed=changed,
            )

            expected_candidate_revision = str(
                getattr(request, "expected_candidate_revision", "") or ""
            ).strip()
            if len(expected_candidate_revision) > 256:
                response.success = False
                response.message = "expected candidate revision is invalid"
                response.active_bundle = self._active_bundle
                response.spec_dir = str(spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=changed,
                    disposition="rejected",
                )
                return response
            if (
                not preview_only
                and expected_candidate_revision
                and expected_candidate_revision != candidate_revision
            ):
                response.success = False
                response.message = (
                    "bundle configuration changed since preview; preview the "
                    "candidate revision again before applying it"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=changed,
                    disposition="revision_changed",
                )
                return response

            target_contract = external_robot_contract_for_spec(spec)
            runtime_contract = getattr(
                self,
                "_runtime_external_robot_contract",
                external_robot_contract_for_spec(self._active_spec),
            )
            contracts_compatible = (
                runtime_contract.supports(target_contract)
                if is_live
                else target_contract == runtime_contract
            )

            if preview_only:
                response.success = True
                response.active_bundle = self._active_bundle
                response.spec_dir = str(spec_dir)
                if not contracts_compatible:
                    response.message = (
                        "bundle configuration is valid but changes the "
                        "launch-time endpoint shape; a runtime restart is required"
                    )
                    self._set_bundle_response_metadata(
                        response,
                        candidate_revision=candidate_revision,
                        changed=changed,
                        disposition="restart_required",
                    )
                elif changed:
                    response.message = (
                        f"bundle preview detected configuration changes for "
                        f"{requested_bundle}"
                    )
                    self._set_bundle_response_metadata(
                        response,
                        candidate_revision=candidate_revision,
                        changed=True,
                        disposition="preview_change_available",
                    )
                else:
                    response.message = (
                        f"bundle preview found no configuration changes for "
                        f"{requested_bundle}"
                    )
                    self._set_bundle_response_metadata(
                        response,
                        candidate_revision=candidate_revision,
                        disposition="preview_unchanged",
                    )
                return response

            if same_bundle and not changed:
                response.success = True
                response.message = (
                    f"active bundle {requested_bundle} has no configuration changes"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    disposition="unchanged",
                )
                return response

            if same_bundle and not reload_if_changed:
                response.success = True
                response.message = (
                    f"active bundle {requested_bundle} has configuration changes; "
                    "set reload_if_changed=true to apply them"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=True,
                    disposition="change_available",
                )
                return response

            same_bundle_reload = same_bundle and reload_if_changed
            if same_bundle_reload and (
                self._operation_name == "start"
                or self._running
                or self._execution_state in {"starting", "running", "paused"}
            ):
                response.success = False
                response.message = (
                    "bundle reload deferred (not queued): the current full-spec "
                    "reload resets procedure state, so apply it only while the "
                    "simulation is idle or fully stopped"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=True,
                    disposition="deferred",
                )
                return response

            if same_bundle_reload and is_live:
                with self._operation_lock:
                    ready, message = self._live_bundle_switch_safe_status_locked(
                        allow_current_bundle_transition=True
                    )
                if not ready:
                    response.success = False
                    response.message = (
                        "live bundle reload requires a fully stopped safe "
                        f"runtime: {message}"
                    )
                    response.active_bundle = self._active_bundle
                    response.spec_dir = str(self._active_spec_dir)
                    self._set_bundle_response_metadata(
                        response,
                        candidate_revision=candidate_revision,
                        changed=True,
                        disposition="blocked",
                    )
                    return response

            if not same_bundle and (
                self._operation_name == "start"
                or self._execution_state in {"starting", "running"}
                or (self._running and self._execution_state != "paused")
            ):
                response.success = False
                response.message = (
                    "bundle switch deferred (not queued): pause or fully stop "
                    "the simulation before changing scenarios"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=changed,
                    disposition="deferred",
                )
                return response

            if self._operation_name == "start":
                # Recheck after candidate validation in case a concurrent start
                # claimed the operation between the first admission check and
                # this point.  Scenario selection never interrupts a start.
                response.success = False
                response.message = (
                    "bundle switch deferred (not queued): simulation start is "
                    "already in progress"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=changed,
                    disposition="deferred",
                )
                return response
            elif self._operation_name:
                if not self._wait_for_operation_clear(timeout_sec=25.0):
                    response.success = False
                    response.message = f"{self._operation_name} already in progress"
                    response.active_bundle = self._active_bundle
                    response.spec_dir = str(self._active_spec_dir)
                    return response

            was_running = (
                self._running
                or self._execution_state in {"starting", "running", "paused"}
            )
            if is_live and was_running:
                response.success = False
                response.message = (
                    "live bundle selection requires a fully stopped safe "
                    "runtime; it does not stop or restart a procedure"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                return response
            if was_running and not request.restart_if_running:
                response.success = False
                response.message = (
                    "bundle switch deferred (not queued): a paused simulation "
                    "requires restart_if_running=true for an explicit scenario "
                    "change"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=changed,
                    disposition="deferred",
                )
                return response

            if not contracts_compatible:
                response.success = False
                response.message = (
                    "bundle switch changes the launch-time endpoint shape; "
                    f"restart the runtime with default_bundle={requested_bundle}"
                )
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                self._set_bundle_response_metadata(
                    response,
                    candidate_revision=candidate_revision,
                    changed=changed,
                    disposition="restart_required",
                )
                return response

            if not self._begin_operation("bundle-switch"):
                response.success = False
                response.message = self._operation_rejection_message or "bundle switch rejected"
                response.active_bundle = self._active_bundle
                response.spec_dir = str(self._active_spec_dir)
                return response

            old_bundle = self._active_bundle
            old_spec_dir = self._active_spec_dir
            old_spec = self._active_spec
            old_revision = str(
                getattr(self, "_active_config_revision", "") or ""
            )
            runtime_spec_update_attempted = False
            runtime_spec_updated = False
            revision_mismatch_after_apply = False
            try:
                self._configure_integration_preflight(
                    old_bundle,
                    old_spec,
                    transitioning=True,
                )
                self._quiesce_runtime_for_bundle_change()
                # Validate the target procedure's preflight shape while the
                # admission lease is still closed. This does not require the
                # target to be ready; it only prevents a split bundle/spec
                # identity from being released later.
                self._configure_integration_preflight(
                    requested_bundle,
                    spec,
                    transitioning=True,
                )
                if self._bundle_config_revision(spec_dir) != candidate_revision:
                    raise RuntimeError(
                        "bundle configuration changed after candidate validation "
                        "and before participant apply; reload was not applied"
                    )
                runtime_spec_update_attempted = True
                self._set_spec_dir_on_runtime(
                    spec_dir,
                    # A same-path edit cannot be rolled back by setting the
                    # same path again: it would reload the new bytes.  Keep
                    # admission closed and require recovery if a participant
                    # rejects such a reload after partial application.
                    rollback_spec_dir=(
                        None if same_bundle_reload else old_spec_dir
                    ),
                )
                runtime_spec_updated = True
                if self._bundle_config_revision(spec_dir) != candidate_revision:
                    revision_mismatch_after_apply = True
                    raise RuntimeError(
                        "bundle configuration changed during participant apply; "
                        "reload commit was rejected"
                    )
                self._active_bundle = requested_bundle
                self._active_spec_dir = spec_dir
                self._active_spec = spec
                self._active_config_revision = candidate_revision
                # In Live this object records immutable launch-time endpoint
                # capacity, not the currently selected procedure requirement.
                # Preserve the superset so a later stopped switch can return
                # to a procedure that uses an endpoint omitted by this target.
                if not is_live:
                    self._runtime_external_robot_contract = target_contract
                self._bundle_dirty = True
                if was_running and request.restart_if_running:
                    if self._bundle_config_revision(spec_dir) != candidate_revision:
                        revision_mismatch_after_apply = True
                        raise RuntimeError(
                            "bundle configuration changed before scenario restart; "
                            "reload commit was rejected"
                        )
                    response.message = self._start_sequence(prepare_executor=False)
                    response.success = True
                else:
                    # Publish a fresh idle frame from the newly selected spec.
                    self._reset_digital_twin_to_idle()
                    if self._bundle_config_revision(spec_dir) != candidate_revision:
                        revision_mismatch_after_apply = True
                        raise RuntimeError(
                            "bundle configuration changed before reload commit; "
                            "reload commit was rejected"
                        )
                    response.success = True
                    response.message = (
                        f"active bundle {requested_bundle} reloaded without a "
                        "runtime restart"
                        if same_bundle_reload
                        else f"active bundle set to {requested_bundle}"
                    )
                # Reopen Live admission only after the new Digital Twin frame
                # has been observed.  A reset/reload failure therefore cannot
                # expose a partially applied procedure configuration.
                self._configure_integration_preflight(
                    requested_bundle,
                    spec,
                    transitioning=False,
                )
                self._bundle_reload_recovery_required = False
            except Exception as exc:
                self._active_bundle = old_bundle
                self._active_spec_dir = old_spec_dir
                self._active_spec = old_spec
                self._active_config_revision = old_revision
                rollback_failures: list[str] = []
                same_path_recovery_required = (
                    same_bundle_reload and runtime_spec_update_attempted
                )
                if same_path_recovery_required and not is_live:
                    self._bundle_dirty = True
                    self._bundle_reload_recovery_required = True
                    detail = (
                        "configuration changed after participant apply"
                        if revision_mismatch_after_apply
                        else "participant reload did not complete"
                    )
                    raise RuntimeError(
                        "same-bundle reload was not fully applied; runtime "
                        f"recovery is required ({detail})"
                    ) from exc
                if not is_live and runtime_spec_updated:
                    try:
                        self._set_spec_dir_on_runtime(old_spec_dir)
                        self._reset_digital_twin_to_idle()
                    except Exception as rollback_exc:
                        self._bundle_dirty = True
                        self._bundle_reload_recovery_required = True
                        raise RuntimeError(
                            "bundle reload commit failed and non-Live rollback "
                            f"was incomplete: {rollback_exc}"
                        ) from exc
                if is_live:
                    incomplete_participant_rollback = (
                        "rollback was incomplete" in str(exc).casefold()
                    )
                    if (
                        same_path_recovery_required
                        or incomplete_participant_rollback
                    ):
                        self._bundle_reload_recovery_required = True
                        try:
                            self._configure_integration_preflight(
                                old_bundle,
                                old_spec,
                                transitioning=True,
                            )
                        except Exception as close_exc:
                            rollback_failures.append(
                                f"preflight_close:{close_exc}"
                            )
                        detail = (
                            "; ".join(rollback_failures[:2])
                            if rollback_failures
                            else "admission remains closed"
                        )
                        raise RuntimeError(
                            "same-bundle reload was not fully applied; "
                            f"runtime recovery is required ({detail})"
                        ) from exc
                    if runtime_spec_updated:
                        try:
                            self._set_spec_dir_on_runtime(old_spec_dir)
                        except Exception as rollback_exc:
                            rollback_failures.append(
                                f"participant_reload:{rollback_exc}"
                            )
                    if not rollback_failures:
                        try:
                            self._reset_digital_twin_to_idle()
                            self._configure_integration_preflight(
                                old_bundle,
                                old_spec,
                                transitioning=False,
                            )
                        except Exception as rollback_exc:
                            rollback_failures.append(
                                f"preflight_restore:{rollback_exc}"
                            )
                if rollback_failures:
                    self._bundle_reload_recovery_required = True
                    raise RuntimeError(
                        "bundle selection failed; runtime remains admission-blocked: "
                        + "; ".join(rollback_failures[:2])
                    ) from exc
                raise
            finally:
                self._finish_operation("bundle-switch")
            response.active_bundle = self._active_bundle
            response.spec_dir = str(self._active_spec_dir)
            self._set_bundle_response_metadata(
                response,
                candidate_revision=candidate_revision,
                changed=changed,
                applied=True,
                disposition="applied",
            )
            return response
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            response.active_bundle = self._active_bundle
            response.spec_dir = str(getattr(self, "_active_spec_dir", "") or "")
            self._set_bundle_response_metadata(
                response,
                candidate_revision=candidate_revision,
                changed=changed,
                disposition="rejected",
            )
            return response

    def _handle_control(self, request, response):
        command = request.command.strip().lower()
        requested_start_phase = str(getattr(request, "start_phase_id", "") or "").strip()
        try:
            if command in {"start", "resume"} and bool(
                getattr(self, "_bundle_reload_recovery_required", False)
            ):
                response.success = False
                response.message = (
                    "procedure configuration recovery is required before "
                    f"simulation {command}"
                )
                response.running = self._running
                response.execution_state = self._execution_state
                return response
            if command not in {"status", "stop"}:
                with self._operation_lock:
                    if self._transition_reservation_active_locked():
                        response.success = False
                        response.message = (
                            "runtime mode transition is reserved; "
                            f"{command or 'mutation'} command rejected"
                        )
                        response.running = self._running
                        response.execution_state = self._execution_state
                        return response
            if self._operation_name == "start":
                if command in {"stop", "reset"}:
                    message = self._interrupt_start_sequence(command)
                    response.success = True
                    response.message = message
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                if command == "start":
                    response.success = True
                    response.message = "start already in progress"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                if command in {"pause", "resume"}:
                    response.success = False
                    response.message = "simulation is still starting"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
            elif command in {"start", "resume"} and self._operation_name in {"stop", "reset", "pause"}:
                if not self._wait_for_operation_clear(timeout_sec=25.0):
                    response.success = False
                    response.message = f"{self._operation_name} already in progress"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
            elif command == self._operation_name and command in {
                "pause",
                "resume",
                "reset",
                "stop",
            }:
                response.success = True
                response.message = f"{command} already in progress"
                response.running = self._running
                response.execution_state = self._execution_state
                return response

            if command == "start":
                try:
                    normalized_start_phase = self._normalize_start_phase(requested_start_phase)
                except ValueError as exc:
                    response.success = False
                    response.message = str(exc)
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                if self._running and self._execution_state == "running":
                    response.success = True
                    response.message = "simulation already running"
                else:
                    success, message = self._run_async(
                        "start",
                        lambda: self._start_sequence(normalized_start_phase),
                    )
                    response.success = success
                    response.message = message
            elif command == "pause":
                success, message = self._run_sync("pause", self._pause_sequence)
                response.success = success
                response.message = message
            elif command == "resume":
                success, message = self._run_sync("resume", self._resume_sequence)
                response.success = success
                response.message = message
            elif command == "status":
                response.success = True
                response.message = "simulation status"
            elif command == "reset":
                success, message = self._run_async("reset", self._reset_sequence)
                response.success = success
                response.message = message
            elif command == "stop":
                if not self._running and self._execution_state in {"idle", "halted", "terminated"}:
                    response.success = True
                    response.message = "simulation already stopped"
                    response.running = self._running
                    response.execution_state = self._execution_state
                    return response
                success, message = self._run_sync("stop", self._stop_sequence)
                response.success = success
                response.message = message
            else:
                response.success = False
                response.message = f"unsupported simulation control command '{request.command}'"
            response.running = self._running
            response.execution_state = self._execution_state
            return response
        except Exception as exc:
            response.success = False
            response.message = str(exc)
            response.running = self._running
            response.execution_state = self._execution_state
            return response

    def _handle_override(self, request, response):
        with self._operation_lock:
            if self._transition_reservation_active_locked():
                response.success = False
                response.message = (
                    "runtime mode transition is reserved; surgeon override rejected"
                )
                return response
            if bool(getattr(self, "_override_in_progress", False)):
                response.success = False
                response.message = "another surgeon override is already in progress"
                return response
            self._override_in_progress = True
        try:
            return self._handle_override_impl(request, response)
        finally:
            with self._operation_lock:
                self._override_in_progress = False

    def _handle_override_impl(self, request, response):
        event_type = request.event_type.strip()
        if event_type not in ALLOWED_OVERRIDE_EVENTS:
            response.success = False
            response.message = (
                f"unsupported surgeon override event_type '{request.event_type}'; "
                f"expected one of {sorted(ALLOWED_OVERRIDE_EVENTS)}"
            )
            return response
        if self._execution_state == "paused":
            response.success = False
            response.message = "simulation paused; resume before injecting surgeon override"
            return response

        requested_tool = request.requested_tool.strip()
        canonical_tool = ""
        if requested_tool:
            canonical_tool = self._active_spec.resolve_instrument_alias(requested_tool) or ""
            if not canonical_tool:
                response.success = False
                response.message = (
                    f"unknown tool '{requested_tool}' for active bundle '{self._active_bundle}'"
                )
                return response
        elif event_type in TOOL_REQUIRED_OVERRIDE_EVENTS:
            response.success = False
            response.message = f"event_type '{event_type}' requires requested_tool"
            return response

        # Manual input has one authoritative route. The legacy rule actor consumes
        # /simulation/surgeon_override; LLM/disabled actor modes use the public
        # /surgeon/request topic directly.
        mute_msg = String()
        mute_msg.data = f"mute_actor:{self._manual_override_actor_mute_sec:.1f}"
        self._control_pub.publish(mute_msg)

        def publish_manual_request(msg: SurgeonRequest) -> None:
            if self._surgeon_actor_mode == "rule":
                self._override_pub.publish(msg)
            else:
                self._direct_request_pub.publish(msg)

        if request.clear_pending_requests:
            cancel = SurgeonRequest()
            cancel.stamp = self.get_clock().now().to_msg()
            cancel.event_type = "cancel_request"
            cancel.override = True
            cancel.note = "clear pending requests before manual override"
            publish_manual_request(cancel)

        msg = SurgeonRequest()
        msg.stamp = self.get_clock().now().to_msg()
        msg.event_type = event_type
        msg.requested_tool = canonical_tool
        msg.voice_text = request.voice_text
        msg.ready_for_handover = bool(request.ready_for_handover)
        msg.ready_for_retrieval = bool(request.ready_for_retrieval)
        msg.override = True
        msg.note = (
            "simulation_manager manual_override "
            f"actor_muted_sec={self._manual_override_actor_mute_sec:.1f}"
        )
        publish_manual_request(msg)

        response.success = True
        response.message = (
            "manual surgeon override published; autonomous actor muted for "
            f"{self._manual_override_actor_mute_sec:.1f}s"
        )
        return response


def main() -> None:
    rclpy.init()
    node = SimulationManagerNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
