"""Read-only integration-readiness observer for the Taskplanner runtime.

The observer reports missing dependencies for diagnosis.  It does not own
scenario admission, lifecycle transitions, or endpoint dispatch: those remain
with ScenarioStore, the state core, and the typed endpoint adapter.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import rclpy
from rclpy.action import ActionClient
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.qos import (
    QoSDurabilityPolicy,
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
    qos_profile_sensor_data,
)
from surgical_perception_msgs.msg import ToolObservation2DArray
from surgical_msgs.msg import VLMRequestContext
from std_msgs.msg import String
from std_srvs.srv import Trigger
from surgical_interop_msgs.action import ExecuteToolHandover
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand

from procedure_spec import (
    DEFAULT_SCENARIO_RUNTIME_REQUIREMENTS,
    ScenarioRuntimeRequirements,
    get_default_spec_dir,
    load_bundle,
    load_scenario_consumer_bundle,
    parse_scenario_config,
)
from surgical_interop_execution.controller_contract import (
    EIR_NUC_CAPABILITY_POLICY_ID,
    EIR_NUC_EXTERNAL_CONTRACT_ID,
    EIR_NUC_VIRTUAL_CONTRACT_ID,
    VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID,
    CONTROLLER_CONTRACT_SCHEMA,
    controller_contract_mismatches,
    required_tool_instances_for_spec,
    validate_source_stamp,
)
from surgical_interop_execution.virtual_endpoints import (
    EXECUTION_ROUTE_STATE_TOPIC,
    EXTERNAL_CONTROLLER_CONTRACT_TOPIC,
    EXTERNAL_ENDPOINT_SOURCE,
    EXTERNAL_RETRACTION_SERVICE_ENDPOINT,
    EXTERNAL_TOOL_HANDOVER_ENDPOINT,
    VIRTUAL_CONTROLLER_CONTRACT_TOPIC,
    VIRTUAL_ENDPOINT_SOURCE,
    VIRTUAL_RETRACTION_SERVICE_ENDPOINT,
    VIRTUAL_TOOL_HANDOVER_ENDPOINT,
    parse_execution_route_state,
)
from .perception_readiness import (
    RFDETR_VIEWS as _RFDETR_VIEWS,
    RFDETR_VLM_ALIGNMENT_STATUSES as _RFDETR_VLM_ALIGNMENT_STATUSES,
    nonempty_message_text as _nonempty_message_text,
    requires_rfdetr_tool_location_context,
    rfdetr_tool_observation_source_stamp,
)
from .readiness_evaluator import (
    _CV_CONTRACT_STATUS_SCHEMA,
    _bounded_reason,
    build_readiness_checklist,
    evaluate_readiness,
    readiness_service_message,
)


_BED_ROBOT_LAYOUTS = {
    "thyroidectomy": {"army_navy"},
    "nephrectomy": {"left_malleable", "right_malleable"},
    "inguinal_hernia_repair": {"left_army_navy", "right_army_navy"},
}
_BED_ROBOT_STATES = {
    "standby",
    "direct_teach",
    "retracting",
    "changing_tool",
    "moving_to_standby",
    "fault",
    "protective_stop",
    "unknown",
}
_ROBOT_ENDPOINT_SOURCES = frozenset({"external", "virtual"})
_VIRTUAL_ENDPOINT_PREFIX = "/integration/virtual/"
_ASR_RUNTIME_STATUS_SCHEMA = "taskplanner.asr.status.v1"
_ASR_RUNTIME_READY_STATES = frozenset({"IDLE", "LISTENING", "STREAMING"})
_ASR_RUNTIME_READY_DEVICE_STATES = frozenset(
    {"READY", "AVAILABLE", "IDLE", "STREAMING", "CONNECTED"}
)
_RFDETR_HEALTH_SCHEMAS = frozenset(
    {
        "taskplanner.rfdetr_health.v1",
        # The built-in RF-DETR adapter's current canonical health contract.
        # It carries a source timestamp and intentionally has different ready
        # field names from the legacy/PNU v1 payload.
        "pnu.rfdetr_health.v2",
    }
)


def evaluate_asr_runtime_status(
    payload: object,
    *,
    now_sec: float,
    max_age_sec: float,
    future_tolerance_sec: float,
) -> tuple[bool, float, str]:
    """Return whether a published operational ASR status can admit Live input.

    A DDS publisher count is only graph presence.  This requires the fixed
    operational status schema, a fresh wall-clock stamp, runtime availability,
    and either a live connection or an explicitly ready microphone/runtime.
    """

    stamp_sec, stamp_error = validate_source_stamp(
        payload,
        now_sec=now_sec,
        max_age_sec=max_age_sec,
        future_tolerance_sec=future_tolerance_sec,
        source_name="asr_runtime_status",
    )
    if stamp_error:
        return False, -1.0, stamp_error
    assert stamp_sec is not None
    if not isinstance(payload, dict):
        return False, -1.0, "asr_runtime_status_missing"
    if payload.get("schema") != _ASR_RUNTIME_STATUS_SCHEMA:
        return False, now_sec - stamp_sec, "asr_runtime_status_schema_mismatch"
    asr = payload.get("asr")
    if not isinstance(asr, dict):
        return False, now_sec - stamp_sec, "asr_runtime_status_payload_invalid"
    if asr.get("available") is not True:
        return False, now_sec - stamp_sec, "asr_runtime_unavailable"
    connected = asr.get("connected") is True
    state = str(asr.get("state", "")).strip().upper()
    device_status = str(asr.get("device_status", "")).strip().upper()
    if not (
        connected
        or state in _ASR_RUNTIME_READY_STATES
        or device_status in _ASR_RUNTIME_READY_DEVICE_STATES
    ):
        return False, now_sec - stamp_sec, "asr_runtime_not_execution_capable"
    return True, now_sec - stamp_sec, ""


def _is_valid_robot_endpoint_configuration(
    *,
    robot_endpoint_source: str,
    retraction_endpoint_source: str | None = None,
    tool_handover_action_name: str,
    retraction_service_name: str,
    require_bed_robot_arm_status: bool,
    retraction_state_machine_suppressed: bool | None = None,
    active_bundle: str = "",
    scenario_runtime_requirements: ScenarioRuntimeRequirements | None = None,
    controller_contract_topic: str = "",
    require_controller_contract: bool = False,
) -> bool:
    """Validate immutable endpoint routing before a Live run can start."""

    tool_source = str(robot_endpoint_source).strip().casefold()
    retraction_source = str(
        retraction_endpoint_source or tool_source
    ).strip().casefold()
    if (
        tool_source not in _ROBOT_ENDPOINT_SOURCES
        or retraction_source not in _ROBOT_ENDPOINT_SOURCES
    ):
        return False
    runtime_requirements = scenario_runtime_requirements
    if runtime_requirements is None:
        runtime_requirements = _runtime_requirements_for_bundle(active_bundle)
    workflow_suppression = not bool(
        runtime_requirements.retraction_workflow_state_enforced
    )
    suppressed = (
        retraction_source == "virtual" or workflow_suppression
        if retraction_state_machine_suppressed is None
        else bool(retraction_state_machine_suppressed)
    )
    if suppressed != (retraction_source == "virtual" or workflow_suppression):
        return False
    tool_virtual = str(tool_handover_action_name).strip().startswith(
        _VIRTUAL_ENDPOINT_PREFIX
    )
    retraction_virtual = str(retraction_service_name).strip().startswith(
        _VIRTUAL_ENDPOINT_PREFIX
    )
    # Controller-contract messages remain diagnostic-only here. Bed-robot
    # status is not an endpoint-routing identity; when a scenario explicitly
    # requires it, freshness and validity are evaluated by the readiness lease.
    # The selected Action and Service endpoints remain immutable and are still
    # discovered below by the readiness node.
    _ = (
        controller_contract_topic,
        require_controller_contract,
        require_bed_robot_arm_status,
    )
    return bool(
        tool_virtual == (tool_source == "virtual")
        and retraction_virtual == (retraction_source == "virtual")
    )


def _runtime_requirements_for_bundle(
    bundle_name: str,
    *,
    spec_dir: str = "",
) -> ScenarioRuntimeRequirements:
    """Resolve one procedure bundle without reproducing scenario ID switches."""

    normalized = str(bundle_name).strip()
    configured_spec_dir = str(spec_dir).strip()
    if not configured_spec_dir and not normalized:
        return DEFAULT_SCENARIO_RUNTIME_REQUIREMENTS
    bundle_dir = (
        Path(configured_spec_dir)
        if configured_spec_dir
        else get_default_spec_dir().parent / normalized
    )
    return load_bundle(bundle_dir).get_scenario_runtime_requirements()


def _expected_contract_for_runtime_requirements(
    runtime: ScenarioRuntimeRequirements,
) -> tuple[str, bool, bool, bool]:
    return (
        str(runtime.procedure_type).strip().casefold(),
        bool(runtime.tool_handover_action_required),
        bool(runtime.retraction_service_required),
        bool(runtime.bed_robot_status_required),
    )


def expected_contract_for_bundle(
    bundle_name: str,
    *,
    spec_dir: str = "",
) -> tuple[str, bool, bool, bool]:
    """Return procedure, tool Action, retraction Service, and status requirements.

    Retraction is now a Service-only controller contract.  The optional
    ``BedRobotArmStateArray`` remains observable telemetry, but no live bundle
    may require its publisher as a start-admission prerequisite: a Service
    server is the sole reviewed execution endpoint.
    """

    return _expected_contract_for_runtime_requirements(
        _runtime_requirements_for_bundle(bundle_name, spec_dir=spec_dir)
    )


def validate_bed_robot_status_layout(
    procedure_type: str,
    arms: list[Any],
) -> bool:
    """Validate only controller-owned fields present in the public contract."""

    expected_roles = _BED_ROBOT_LAYOUTS.get(str(procedure_type).strip().casefold())
    if expected_roles is None or len(arms) != len(expected_roles):
        return False
    arm_ids: set[str] = set()
    roles: set[str] = set()
    for arm in arms:
        arm_id = str(getattr(arm, "arm_id", "")).strip()
        role = str(getattr(arm, "role", "")).strip()
        role_instance = str(getattr(arm, "role_instance_id", "")).strip()
        state = str(getattr(arm, "state", "")).strip()
        direct_teach_active = bool(getattr(arm, "direct_teach_active", False))
        if (
            arm_id not in {"arm_1", "arm_2"}
            or arm_id in arm_ids
            or role != "retraction"
            or role_instance not in expected_roles
            or role_instance in roles
            or state not in _BED_ROBOT_STATES
            or direct_teach_active != (state == "direct_teach")
        ):
            return False
        arm_ids.add(arm_id)
        roles.add(role_instance)
    return roles == expected_roles


class IntegrationPreflightNode(Node):
    def __init__(self) -> None:
        super().__init__("integration_preflight")
        self.declare_parameter("sentence_topic", "/sensors/surgeon/sentence")
        # ``sentence_topic`` is retained as the legacy String-route alias.
        # A Live typed-ASR route supplies its actual SpeechUtterance source
        # here so readiness does not count an unrelated legacy publisher.
        self.declare_parameter("speech_source_topic", "")
        self.declare_parameter(
            "asr_runtime_status_topic", "/input/asr/runtime_status"
        )
        self.declare_parameter("require_asr_runtime_status", False)
        self.declare_parameter("asr_runtime_status_max_age_sec", 3.0)
        self.declare_parameter(
            "asr_runtime_status_source_future_tolerance_sec", 0.5
        )
        self.declare_parameter("readiness_topic", "/integration/readiness")
        self.declare_parameter(
            "readiness_service",
            "/integration/check_readiness",
        )
        self.declare_parameter(
            "rfdetr_health_topic",
            "/surgery/perception/rfdetr/health",
        )
        # This is the launch-time RF-DETR contract switch. The active bundle
        # decides whether the selected procedure requires the evidence;
        # subscriptions stay alive across a stopped-state bundle switch so no
        # process restart or stale prior frame can weaken that later check.
        self.declare_parameter("require_rfdetr_tool_observations", False)
        self.declare_parameter("cam3_tool_observations_topic", "")
        self.declare_parameter("cam4_tool_observations_topic", "")
        self.declare_parameter(
            "cam3_tool_observations_expected_model_version", ""
        )
        self.declare_parameter(
            "cam4_tool_observations_expected_model_version", ""
        )
        self.declare_parameter(
            "rfdetr_vlm_request_context_topic",
            "/context/vlm_request_context",
        )
        self.declare_parameter("require_sentence_publisher", True)
        self.declare_parameter("require_perception", False)
        self.declare_parameter("require_metric_3d", False)
        self.declare_parameter("perception_max_age_sec", 3.0)
        self.declare_parameter(
            "perception_source_future_tolerance_sec", 0.5
        )
        self.declare_parameter("perception_backend", "local")
        self.declare_parameter(
            "cv_contract_status_topic",
            "/integration/cv_contract/status",
        )
        self.declare_parameter(
            "tool_handover_action_name",
            "/surgery/tool_handover",
        )
        self.declare_parameter("require_tool_handover_action_server", True)
        self.declare_parameter(
            "retraction_service_name",
            "/surgery/retraction/command",
        )
        self.declare_parameter("require_retraction_service", True)
        self.declare_parameter("robot_endpoint_source", "external")
        # Empty preserves the legacy atomic route: use robot_endpoint_source.
        self.declare_parameter("retraction_endpoint_source", "")
        # Live-only route control keeps both endpoint families available and
        # follows the bridge's latched read-only state. It is disabled for
        # ordinary mock/debug launches so this node retains its small legacy
        # surface there.
        self.declare_parameter("enable_runtime_route_control", False)
        self.declare_parameter(
            "execution_route_state_topic", EXECUTION_ROUTE_STATE_TOPIC
        )
        self.declare_parameter("retraction_state_machine_suppressed", False)
        # Deprecated compatibility input. Controller-contract manifests are
        # diagnostic telemetry only and never participate in start admission.
        self.declare_parameter("require_controller_contract", False)
        self.declare_parameter(
            "controller_contract_topic", "/surgery/controller_contract"
        )
        self.declare_parameter(
            "expected_controller_contract_id", EIR_NUC_EXTERNAL_CONTRACT_ID
        )
        self.declare_parameter(
            "expected_capability_policy_id", EIR_NUC_CAPABILITY_POLICY_ID
        )
        self.declare_parameter("require_physical_stop_confirmation", True)
        self.declare_parameter("controller_contract_max_age_sec", 3.0)
        self.declare_parameter("spec_dir", "")
        self.declare_parameter(
            "bed_robot_arm_status_topic",
            "/external/bed_robot_arms/status",
        )
        self.declare_parameter("require_bed_robot_arm_status", True)
        self.declare_parameter("bed_robot_arm_status_max_age_sec", 3.0)
        self.declare_parameter("active_bundle", "")
        self.declare_parameter("procedure_type", "")
        self.declare_parameter("contract_transitioning", False)
        self.declare_parameter("scenario_config_topic", "/simulation/scenario_config")

        self._sentence_topic = str(self.get_parameter("sentence_topic").value)
        self._speech_source_topic = str(
            self.get_parameter("speech_source_topic").value
        ).strip() or self._sentence_topic
        self._asr_runtime_status_topic = str(
            self.get_parameter("asr_runtime_status_topic").value
        ).strip()
        self._require_asr_runtime_status = bool(
            self.get_parameter("require_asr_runtime_status").value
        )
        self._asr_runtime_status_max_age_sec = max(
            0.1,
            float(self.get_parameter("asr_runtime_status_max_age_sec").value),
        )
        self._asr_runtime_status_source_future_tolerance_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "asr_runtime_status_source_future_tolerance_sec"
                ).value
            ),
        )
        self._require_sentence_publisher = bool(
            self.get_parameter("require_sentence_publisher").value
        )
        self._require_perception = bool(
            self.get_parameter("require_perception").value
        )
        self._require_rfdetr_tool_observations = bool(
            self.get_parameter("require_rfdetr_tool_observations").value
        )
        self._cam3_tool_observations_topic = str(
            self.get_parameter("cam3_tool_observations_topic").value
        ).strip()
        self._cam4_tool_observations_topic = str(
            self.get_parameter("cam4_tool_observations_topic").value
        ).strip()
        self._rfdetr_expected_model_versions = {
            "cam_3": str(
                self.get_parameter(
                    "cam3_tool_observations_expected_model_version"
                ).value
            ).strip(),
            "cam_4": str(
                self.get_parameter(
                    "cam4_tool_observations_expected_model_version"
                ).value
            ).strip(),
        }
        self._rfdetr_vlm_request_context_topic = str(
            self.get_parameter("rfdetr_vlm_request_context_topic").value
        ).strip()
        self._require_metric_3d = bool(
            self.get_parameter("require_metric_3d").value
        )
        self._perception_backend = str(
            self.get_parameter("perception_backend").value
        ).strip().casefold()
        self._require_retraction_service = bool(
            self.get_parameter("require_retraction_service").value
        )
        self._require_tool_handover_action_server = bool(
            self.get_parameter("require_tool_handover_action_server").value
        )
        self._require_bed_robot_arm_status = bool(
            self.get_parameter("require_bed_robot_arm_status").value
        )
        self._robot_endpoint_source = str(
            self.get_parameter("robot_endpoint_source").value
        ).strip().casefold()
        self._retraction_endpoint_source = str(
            self.get_parameter("retraction_endpoint_source").value
        ).strip().casefold() or self._robot_endpoint_source
        if self._robot_endpoint_source not in _ROBOT_ENDPOINT_SOURCES:
            raise RuntimeError("robot_endpoint_source is invalid")
        if self._retraction_endpoint_source not in _ROBOT_ENDPOINT_SOURCES:
            raise RuntimeError("retraction_endpoint_source is invalid")
        self._retraction_state_machine_suppressed = bool(
            self.get_parameter("retraction_state_machine_suppressed").value
        )
        self._require_controller_contract = bool(
            self.get_parameter("require_controller_contract").value
        )
        self._controller_contract_topic = str(
            self.get_parameter("controller_contract_topic").value
        ).strip()
        self._expected_controller_contract_id = str(
            self.get_parameter("expected_controller_contract_id").value
        ).strip()
        self._expected_capability_policy_id = str(
            self.get_parameter("expected_capability_policy_id").value
        ).strip()
        self._require_physical_stop_confirmation = bool(
            self.get_parameter("require_physical_stop_confirmation").value
        )
        self._enable_runtime_route_control = bool(
            self.get_parameter("enable_runtime_route_control").value
        )
        self._execution_route_state_topic = str(
            self.get_parameter("execution_route_state_topic").value
        ).strip()
        initial_tool_handover_action_name = str(
            self.get_parameter("tool_handover_action_name").value
        ).strip()
        initial_retraction_service_name = str(
            self.get_parameter("retraction_service_name").value
        ).strip()
        self._external_tool_handover_action_name = str(
            self.declare_parameter(
                "external_tool_handover_action_name",
                (
                    initial_tool_handover_action_name
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EXTERNAL_TOOL_HANDOVER_ENDPOINT
                ),
            ).value
        ).strip()
        self._virtual_tool_handover_action_name = str(
            self.declare_parameter(
                "virtual_tool_handover_action_name",
                (
                    initial_tool_handover_action_name
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_TOOL_HANDOVER_ENDPOINT
                ),
            ).value
        ).strip()
        self._external_retraction_service_name = str(
            self.declare_parameter(
                "external_retraction_service_name",
                (
                    initial_retraction_service_name
                    if self._retraction_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EXTERNAL_RETRACTION_SERVICE_ENDPOINT
                ),
            ).value
        ).strip()
        self._virtual_retraction_service_name = str(
            self.declare_parameter(
                "virtual_retraction_service_name",
                (
                    initial_retraction_service_name
                    if self._retraction_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_RETRACTION_SERVICE_ENDPOINT
                ),
            ).value
        ).strip()
        self._external_controller_contract_topic = str(
            self.declare_parameter(
                "external_controller_contract_topic",
                (
                    self._controller_contract_topic
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EXTERNAL_CONTROLLER_CONTRACT_TOPIC
                ),
            ).value
        ).strip()
        self._virtual_controller_contract_topic = str(
            self.declare_parameter(
                "virtual_controller_contract_topic",
                (
                    self._controller_contract_topic
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_CONTROLLER_CONTRACT_TOPIC
                ),
            ).value
        ).strip()
        self._external_expected_controller_contract_id = str(
            self.declare_parameter(
                "external_expected_controller_contract_id",
                (
                    self._expected_controller_contract_id
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EIR_NUC_EXTERNAL_CONTRACT_ID
                ),
            ).value
        ).strip()
        self._virtual_expected_controller_contract_id = str(
            self.declare_parameter(
                "virtual_expected_controller_contract_id",
                (
                    self._expected_controller_contract_id
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else EIR_NUC_VIRTUAL_CONTRACT_ID
                ),
            ).value
        ).strip()
        self._external_expected_capability_policy_id = str(
            self.declare_parameter(
                "external_expected_capability_policy_id",
                (
                    self._expected_capability_policy_id
                    if self._robot_endpoint_source == EXTERNAL_ENDPOINT_SOURCE
                    else EIR_NUC_CAPABILITY_POLICY_ID
                ),
            ).value
        ).strip()
        self._virtual_expected_capability_policy_id = str(
            self.declare_parameter(
                "virtual_expected_capability_policy_id",
                (
                    self._expected_capability_policy_id
                    if self._robot_endpoint_source == VIRTUAL_ENDPOINT_SOURCE
                    else VIRTUAL_EMULATOR_CAPABILITY_POLICY_ID
                ),
            ).value
        ).strip()
        self._external_require_bed_robot_arm_status = bool(
            self.declare_parameter(
                "external_require_bed_robot_arm_status",
                self._require_bed_robot_arm_status,
            ).value
        )
        self._external_require_physical_stop_confirmation = bool(
            self.declare_parameter(
                "external_require_physical_stop_confirmation",
                self._require_physical_stop_confirmation,
            ).value
        )
        self._controller_contract_max_age_sec = max(
            0.1,
            float(self.get_parameter("controller_contract_max_age_sec").value),
        )
        self._spec_dir = str(self.get_parameter("spec_dir").value).strip()
        self._scenario_config_root = Path(self._spec_dir).resolve().parent
        self._active_bundle = str(
            self.get_parameter("active_bundle").value
        ).strip()
        self._scenario_runtime_requirements = _runtime_requirements_for_bundle(
            self._active_bundle,
            spec_dir=self._spec_dir,
        )
        self._contract_transitioning = bool(
            self.get_parameter("contract_transitioning").value
        )
        self._scenario_config_topic = str(
            self.get_parameter("scenario_config_topic").value
        ).strip()
        self._bed_robot_arm_status_max_age_sec = max(
            0.1,
            float(self.get_parameter("bed_robot_arm_status_max_age_sec").value),
        )
        self._procedure_type = str(
            self.get_parameter("procedure_type").value
        ).strip().casefold()
        self._bed_robot_status_valid = False
        self._bed_robot_status_received_monotonic = 0.0
        self._bed_robot_status_source_stamp_sec = 0.0
        self._bed_robot_status_revision: int | None = None
        self._perception_max_age_sec = max(
            0.1,
            float(self.get_parameter("perception_max_age_sec").value),
        )
        self._perception_source_future_tolerance_sec = max(
            0.0,
            float(
                self.get_parameter(
                    "perception_source_future_tolerance_sec"
                ).value
            ),
        )
        self._latest_rfdetr_health: dict[str, Any] | None = None
        self._latest_rfdetr_monotonic = 0.0
        self._latest_rfdetr_source_error = ""
        self._last_accepted_rfdetr_source_stamp_sec = 0.0
        self._latest_rfdetr_tool_observations: dict[
            str, dict[str, float] | None
        ] = {"cam_3": None, "cam_4": None}
        self._latest_rfdetr_tool_observations_monotonic = {
            "cam_3": 0.0,
            "cam_4": 0.0,
        }
        self._latest_rfdetr_tool_observations_source_error = {
            "cam_3": "",
            "cam_4": "",
        }
        self._last_accepted_rfdetr_tool_observations_source_stamp_sec = {
            "cam_3": 0.0,
            "cam_4": 0.0,
        }
        # Alignment is optional read-only observability from the VLM's own
        # context projection. It never authorizes start: CAM3 is an
        # independent view and a valid typed detector frame remains usable
        # location evidence even when FLIR is absent or temporally unrelated.
        self._latest_rfdetr_vlm_alignment: dict[str, str] = {}
        self._latest_rfdetr_vlm_alignment_monotonic = 0.0
        self._latest_rfdetr_vlm_alignment_error = ""
        self._latest_cv_contract_status: dict[str, Any] | None = None
        self._latest_cv_contract_monotonic = 0.0
        self._latest_cv_contract_source_error = ""
        self._last_accepted_cv_contract_source_stamp_sec = 0.0
        self._latest_controller_contract: dict[str, Any] | None = None
        self._latest_controller_contract_monotonic = 0.0
        self._latest_controller_contract_source_stamp_sec = 0.0
        self._latest_controller_contract_source_error = ""
        self._last_accepted_controller_contract_source_stamp_sec = 0.0
        self._controller_contract_leases: dict[str, dict[str, Any]] = {
            source: {
                "payload": None,
                "monotonic": 0.0,
                "source_stamp_sec": 0.0,
                "source_error": "",
                "last_accepted_stamp_sec": 0.0,
            }
            for source in _ROBOT_ENDPOINT_SOURCES
        }
        self._latest_asr_runtime_status: dict[str, Any] | None = None
        self._latest_asr_runtime_status_monotonic = 0.0
        self._latest_asr_runtime_status_source_stamp_sec = 0.0
        self._latest_asr_runtime_status_source_error = ""
        self._last_accepted_asr_runtime_status_source_stamp_sec = 0.0
        self.add_on_set_parameters_callback(self._on_contract_parameters_changed)

        self._route_state_revision = -1
        self._route_state_initialization_revision = -1
        self._route_state_initialized = not self._enable_runtime_route_control
        self._route_state_selected_source = (
            self._robot_endpoint_source
            if not self._enable_runtime_route_control
            else ""
        )
        self._route_state_retraction_source = (
            self._retraction_endpoint_source
            if not self._enable_runtime_route_control
            else ""
        )
        self._route_state_initialization_state = (
            "initialized" if not self._enable_runtime_route_control else ""
        )
        self._external_tool_handover_client = ActionClient(
            self,
            ExecuteToolHandover,
            self._external_tool_handover_action_name,
        )
        self._virtual_tool_handover_client = ActionClient(
            self,
            ExecuteToolHandover,
            self._virtual_tool_handover_action_name,
        )
        self._external_retraction_client = self.create_client(
            ExecuteRetractionCommand, self._external_retraction_service_name
        )
        self._virtual_retraction_client = self.create_client(
            ExecuteRetractionCommand, self._virtual_retraction_service_name
        )
        self._apply_execution_route_source(
            self._robot_endpoint_source,
            retraction_source=self._retraction_endpoint_source,
            invalidate=False,
        )
        self._bed_robot_arm_status_topic = str(
            self.get_parameter("bed_robot_arm_status_topic").value
        )
        self._bed_robot_arm_status_subscription = None
        if self._external_require_bed_robot_arm_status:
            self._bed_robot_arm_status_subscription = self.create_subscription(
                BedRobotArmStateArray,
                self._bed_robot_arm_status_topic,
                self._on_bed_robot_arm_status,
                10,
            )
        self._readiness_pub = self.create_publisher(
            String,
            str(self.get_parameter("readiness_topic").value),
            10,
        )
        if self._scenario_config_topic:
            # ScenarioStore publishes a latched, read-only identity.  The
            # readiness observer follows it directly instead of making the
            # lifecycle manager reconfigure a second scenario contract.
            self.create_subscription(
                String,
                self._scenario_config_topic,
                self._on_scenario_config,
                QoSProfile(
                    history=QoSHistoryPolicy.KEEP_LAST,
                    depth=1,
                    reliability=QoSReliabilityPolicy.RELIABLE,
                    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
                ),
            )
        self.create_subscription(
            String,
            str(self.get_parameter("rfdetr_health_topic").value),
            self._on_rfdetr_health,
            10,
        )
        # Subscribe whenever typed topics are configured, rather than only
        # when the launch default happens to be the demo. After a safe
        # stopped-state selection, the demo must receive fresh CAM3/CAM4 facts
        # before diagnostics report them current; no detector image or mask is
        # retained.
        if self._cam3_tool_observations_topic:
            self.create_subscription(
                ToolObservation2DArray,
                self._cam3_tool_observations_topic,
                lambda msg: self._on_rfdetr_tool_observations(
                    msg,
                    expected_view="cam_3",
                ),
                qos_profile_sensor_data,
            )
        if self._cam4_tool_observations_topic:
            self.create_subscription(
                ToolObservation2DArray,
                self._cam4_tool_observations_topic,
                lambda msg: self._on_rfdetr_tool_observations(
                    msg,
                    expected_view="cam_4",
                ),
                qos_profile_sensor_data,
            )
        if self._rfdetr_vlm_request_context_topic:
            self.create_subscription(
                VLMRequestContext,
                self._rfdetr_vlm_request_context_topic,
                self._on_rfdetr_vlm_request_context,
                10,
            )
        self.create_subscription(
            String,
            str(self.get_parameter("cv_contract_status_topic").value),
            self._on_cv_contract_status,
            10,
        )
        self.create_subscription(
            String,
            self._external_controller_contract_topic,
            lambda msg: self._on_controller_contract(
                msg, EXTERNAL_ENDPOINT_SOURCE
            ),
            10,
        )
        self.create_subscription(
            String,
            self._virtual_controller_contract_topic,
            lambda msg: self._on_controller_contract(
                msg, VIRTUAL_ENDPOINT_SOURCE
            ),
            10,
        )
        if self._enable_runtime_route_control:
            self.create_subscription(
                String,
                self._execution_route_state_topic,
                self._on_execution_route_state,
                QoSProfile(
                    history=QoSHistoryPolicy.KEEP_LAST,
                    depth=1,
                    reliability=QoSReliabilityPolicy.RELIABLE,
                    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
                ),
            )
        self.create_subscription(
            String,
            self._asr_runtime_status_topic,
            self._on_asr_runtime_status,
            10,
        )
        self.create_service(
            Trigger,
            str(self.get_parameter("readiness_service").value),
            self._handle_readiness,
        )
        self.create_timer(1.0, self._publish_readiness)

    def _current_scenario_runtime_requirements(
        self,
    ) -> ScenarioRuntimeRequirements:
        runtime = getattr(self, "_scenario_runtime_requirements", None)
        if isinstance(runtime, ScenarioRuntimeRequirements):
            return runtime
        return _runtime_requirements_for_bundle(
            getattr(self, "_active_bundle", ""),
            spec_dir=getattr(self, "_spec_dir", ""),
        )

    def _on_scenario_config(self, message: String) -> None:
        """Follow ScenarioStore's selected bundle as a diagnostics consumer.

        This callback has no Action/Service calls and never pauses, resets, or
        dispatches a procedure.  ScenarioStore has already limited selection
        to a stopped lifecycle; this observer only refreshes which optional
        checks it displays for the selected local bundle.
        """

        try:
            published = parse_scenario_config(message.data)
            bundle = load_scenario_consumer_bundle(
                published,
                fixed_spec_root=self._scenario_config_root,
            )
            spec_dir = Path(bundle.spec_dir)
            runtime_requirements = _runtime_requirements_for_bundle(
                published.bundle_name,
                spec_dir=str(spec_dir),
            )
            procedure_type, require_tool_handover, require_retraction, require_bed = (
                _expected_contract_for_runtime_requirements(runtime_requirements)
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.get_logger().warning(
                f"ignoring invalid ScenarioStore readiness snapshot: {exc}"
            )
            return

        previous_identity = (
            self._active_bundle,
            self._spec_dir,
            self._procedure_type,
            self._require_tool_handover_action_server,
            self._require_retraction_service,
            self._require_bed_robot_arm_status,
        )
        self._active_bundle = published.bundle_name
        self._spec_dir = str(spec_dir)
        self._procedure_type = procedure_type
        self._require_tool_handover_action_server = require_tool_handover
        self._require_retraction_service = require_retraction
        self._require_bed_robot_arm_status = require_bed
        self._scenario_runtime_requirements = runtime_requirements
        self._contract_transitioning = False
        current_identity = (
            self._active_bundle,
            self._spec_dir,
            self._procedure_type,
            self._require_tool_handover_action_server,
            self._require_retraction_service,
            self._require_bed_robot_arm_status,
        )
        if current_identity != previous_identity:
            self._invalidate_bed_robot_status()
        if current_identity[:2] != previous_identity[:2]:
            self._invalidate_rfdetr_tool_location_leases()

    def _apply_execution_route_source(
        self,
        source: object,
        *,
        retraction_source: object | None = None,
        invalidate: bool,
    ) -> None:
        """Select independently reviewed Action and Service clients."""

        normalized = str(source).strip().casefold()
        normalized_retraction = str(
            retraction_source or normalized
        ).strip().casefold()
        if (
            normalized not in _ROBOT_ENDPOINT_SOURCES
            or normalized_retraction not in _ROBOT_ENDPOINT_SOURCES
        ):
            raise ValueError("execution route source is invalid")
        self._robot_endpoint_source = normalized
        self._retraction_endpoint_source = normalized_retraction
        tool_virtual = normalized == VIRTUAL_ENDPOINT_SOURCE
        retraction_virtual = normalized_retraction == VIRTUAL_ENDPOINT_SOURCE
        self._tool_handover_action_name = (
            self._virtual_tool_handover_action_name
            if tool_virtual
            else self._external_tool_handover_action_name
        )
        self._tool_handover_client = (
            self._virtual_tool_handover_client
            if tool_virtual
            else self._external_tool_handover_client
        )
        self._controller_contract_topic = (
            self._virtual_controller_contract_topic
            if tool_virtual
            else self._external_controller_contract_topic
        )
        self._expected_controller_contract_id = (
            self._virtual_expected_controller_contract_id
            if tool_virtual
            else self._external_expected_controller_contract_id
        )
        self._expected_capability_policy_id = (
            self._virtual_expected_capability_policy_id
            if tool_virtual
            else self._external_expected_capability_policy_id
        )
        self._retraction_service_name = (
            self._virtual_retraction_service_name
            if retraction_virtual
            else self._external_retraction_service_name
        )
        self._retraction_client = (
            self._virtual_retraction_client
            if retraction_virtual
            else self._external_retraction_client
        )
        self._retraction_controller_contract_topic = (
            self._virtual_controller_contract_topic
            if retraction_virtual
            else self._external_controller_contract_topic
        )
        self._retraction_expected_controller_contract_id = (
            self._virtual_expected_controller_contract_id
            if retraction_virtual
            else self._external_expected_controller_contract_id
        )
        self._require_bed_robot_arm_status = (
            self._external_require_bed_robot_arm_status
            if not retraction_virtual
            else False
        )
        self._require_physical_stop_confirmation = (
            self._external_require_physical_stop_confirmation
            if not retraction_virtual
            else False
        )
        self._retraction_state_machine_suppressed = bool(
            retraction_virtual
            or not self._current_scenario_runtime_requirements().retraction_workflow_state_enforced
        )
        if invalidate:
            self._invalidate_bed_robot_status()
            self._latest_controller_contract = None
            self._latest_controller_contract_monotonic = 0.0
            self._latest_controller_contract_source_stamp_sec = 0.0
            self._latest_controller_contract_source_error = ""
            self._last_accepted_controller_contract_source_stamp_sec = 0.0
            for lease in self._controller_contract_leases.values():
                lease.update(
                    {
                        "payload": None,
                        "monotonic": 0.0,
                        "source_stamp_sec": 0.0,
                        "source_error": "",
                        "last_accepted_stamp_sec": 0.0,
                    }
                )

    def _on_execution_route_state(self, msg: String) -> None:
        """Apply only the bridge's strict, latched route projection.

        This subscription has no command behavior.  It replaces both client
        references together and invalidates prior controller evidence, so the
        next start must re-admit the selected Action *and* Service family.
        """

        workflow_suppression = not bool(
            self._current_scenario_runtime_requirements()
            .retraction_workflow_state_enforced
        )
        try:
            payload = json.loads(msg.data)
            state = parse_execution_route_state(
                payload,
                allow_external_retraction_state_machine_suppression=(
                    workflow_suppression
                ),
            )
        except (TypeError, ValueError) as exc:
            self._route_state_initialized = False
            self.get_logger().warning(f"ignored invalid execution route state: {exc}")
            return
        tool_expected = (
            self._virtual_tool_handover_action_name
            if state.selected_source == VIRTUAL_ENDPOINT_SOURCE
            else self._external_tool_handover_action_name
        )
        retraction_expected = (
            (
                self._virtual_retraction_service_name,
                False,
                True,
            )
            if state.retraction_source == VIRTUAL_ENDPOINT_SOURCE
            else (
                self._external_retraction_service_name,
                self._external_require_physical_stop_confirmation,
                workflow_suppression,
            )
        )
        received = (
            state.tool_handover_endpoint,
            state.retraction_service_name,
            state.require_physical_stop_confirmation,
            state.retraction_state_machine_suppressed,
        )
        if received != (tool_expected,) + retraction_expected:
            self._route_state_initialized = False
            self.get_logger().warning(
                "ignored execution route state with a launch-contract mismatch"
            )
            return
        state_key = (state.revision, state.initialization_revision)
        previous_key = (
            getattr(self, "_route_state_revision", -1),
            getattr(self, "_route_state_initialization_revision", -1),
        )
        if state_key < previous_key:
            return
        state_is_initialized = state.initialization_state != "initializing"
        source_changed = (
            state.selected_source
            != str(getattr(self, "_route_state_selected_source", ""))
            or state.retraction_source
            != str(getattr(self, "_route_state_retraction_source", ""))
        )
        suppression_changed = (
            bool(state.retraction_state_machine_suppressed)
            != bool(
                getattr(
                    self,
                    "_retraction_state_machine_suppressed",
                    False,
                )
            )
        )
        if (
            state_key == previous_key
            and not source_changed
            and not suppression_changed
            and self._route_state_initialized == state_is_initialized
            and self._route_state_initialization_state
            == state.initialization_state
        ):
            return
        # An ``initializing`` -> ``initialized`` publication uses the same
        # revision and already-applied client pair.  Do not invalidate the
        # just-applied route again, otherwise a benign acknowledgement update
        # would erase newly received controller evidence.
        if state_key != previous_key or source_changed:
            self._apply_execution_route_source(
                state.selected_source,
                retraction_source=state.retraction_source,
                invalidate=True,
            )
        # A stopped procedure switch can change only the authored local
        # workflow-ordering choice.  The bridge then republishes the same
        # authoritative route revision with the newly resolved suppression
        # bit.  Refresh it only after the complete source/endpoint/
        # initialization projection above has matched; otherwise the
        # unchanged-route fast path would retain the prior procedure's policy
        # and keep readiness permanently closed.
        self._retraction_state_machine_suppressed = bool(
            state.retraction_state_machine_suppressed
        )
        self._route_state_revision = state.revision
        self._route_state_initialization_revision = state.initialization_revision
        self._route_state_selected_source = state.selected_source
        self._route_state_retraction_source = state.retraction_source
        self._route_state_initialization_state = state.initialization_state
        self._route_state_initialized = state_is_initialized

    def _on_contract_parameters_changed(self, parameters) -> SetParametersResult:
        candidate = {
            "active_bundle": self._active_bundle,
            "spec_dir": self._spec_dir,
            "procedure_type": self._procedure_type,
            "require_tool_handover_action_server": (
                self._require_tool_handover_action_server
            ),
            "require_retraction_service": self._require_retraction_service,
            "require_bed_robot_arm_status": self._require_bed_robot_arm_status,
            "robot_endpoint_source": self._robot_endpoint_source,
            "retraction_endpoint_source": getattr(
                self, "_retraction_endpoint_source", self._robot_endpoint_source
            ),
            "retraction_state_machine_suppressed": (
                self._retraction_state_machine_suppressed
            ),
            "contract_transitioning": self._contract_transitioning,
        }
        for parameter in parameters:
            launch_lifetime_values = {
                "robot_endpoint_source": self._robot_endpoint_source,
                "retraction_endpoint_source": getattr(
                    self, "_retraction_endpoint_source", self._robot_endpoint_source
                ),
                "retraction_state_machine_suppressed": (
                    self._retraction_state_machine_suppressed
                ),
                "require_controller_contract": self._require_controller_contract,
                "controller_contract_topic": self._controller_contract_topic,
                "retraction_controller_contract_topic": (
                    getattr(
                        self,
                        "_retraction_controller_contract_topic",
                        self._controller_contract_topic,
                    )
                ),
                "expected_controller_contract_id": (
                    self._expected_controller_contract_id
                ),
                "retraction_expected_controller_contract_id": (
                    getattr(
                        self,
                        "_retraction_expected_controller_contract_id",
                        self._expected_controller_contract_id,
                    )
                ),
                "expected_capability_policy_id": (
                    self._expected_capability_policy_id
                ),
                "require_physical_stop_confirmation": (
                    self._require_physical_stop_confirmation
                ),
                "controller_contract_max_age_sec": (
                    self._controller_contract_max_age_sec
                ),
                "speech_source_topic": self._speech_source_topic,
                "asr_runtime_status_topic": self._asr_runtime_status_topic,
                "require_asr_runtime_status": (
                    self._require_asr_runtime_status
                ),
                "asr_runtime_status_max_age_sec": (
                    self._asr_runtime_status_max_age_sec
                ),
                "asr_runtime_status_source_future_tolerance_sec": (
                    self._asr_runtime_status_source_future_tolerance_sec
                ),
                "perception_source_future_tolerance_sec": (
                    self._perception_source_future_tolerance_sec
                ),
                "require_rfdetr_tool_observations": (
                    bool(
                        getattr(
                            self,
                            "_require_rfdetr_tool_observations",
                            False,
                        )
                    )
                ),
                "cam3_tool_observations_topic": (
                    str(getattr(self, "_cam3_tool_observations_topic", ""))
                ),
                "cam4_tool_observations_topic": (
                    str(getattr(self, "_cam4_tool_observations_topic", ""))
                ),
                "rfdetr_vlm_request_context_topic": (
                    str(
                        getattr(
                            self,
                            "_rfdetr_vlm_request_context_topic",
                            "",
                        )
                    )
                ),
                "enable_runtime_route_control": (
                    bool(getattr(self, "_enable_runtime_route_control", False))
                ),
                "execution_route_state_topic": (
                    str(getattr(self, "_execution_route_state_topic", ""))
                ),
                "external_tool_handover_action_name": (
                    str(
                        getattr(
                            self,
                            "_external_tool_handover_action_name",
                            "",
                        )
                    )
                ),
                "virtual_tool_handover_action_name": (
                    str(
                        getattr(
                            self,
                            "_virtual_tool_handover_action_name",
                            "",
                        )
                    )
                ),
                "external_retraction_service_name": (
                    str(
                        getattr(
                            self,
                            "_external_retraction_service_name",
                            "",
                        )
                    )
                ),
                "virtual_retraction_service_name": (
                    str(
                        getattr(
                            self,
                            "_virtual_retraction_service_name",
                            "",
                        )
                    )
                ),
                "external_controller_contract_topic": (
                    str(
                        getattr(
                            self,
                            "_external_controller_contract_topic",
                            "",
                        )
                    )
                ),
                "virtual_controller_contract_topic": (
                    str(
                        getattr(
                            self,
                            "_virtual_controller_contract_topic",
                            "",
                        )
                    )
                ),
                "external_expected_controller_contract_id": (
                    str(
                        getattr(
                            self,
                            "_external_expected_controller_contract_id",
                            "",
                        )
                    )
                ),
                "virtual_expected_controller_contract_id": (
                    str(
                        getattr(
                            self,
                            "_virtual_expected_controller_contract_id",
                            "",
                        )
                    )
                ),
                "external_expected_capability_policy_id": (
                    str(
                        getattr(
                            self,
                            "_external_expected_capability_policy_id",
                            "",
                        )
                    )
                ),
                "virtual_expected_capability_policy_id": (
                    str(
                        getattr(
                            self,
                            "_virtual_expected_capability_policy_id",
                            "",
                        )
                    )
                ),
                "external_require_bed_robot_arm_status": (
                    bool(
                        getattr(
                            self,
                            "_external_require_bed_robot_arm_status",
                            False,
                        )
                    )
                ),
                "external_require_physical_stop_confirmation": (
                    bool(
                        getattr(
                            self,
                            "_external_require_physical_stop_confirmation",
                            False,
                        )
                    )
                ),
            }
            if parameter.name in launch_lifetime_values:
                current = launch_lifetime_values[parameter.name]
                requested = parameter.value
                if parameter.name in {
                    "robot_endpoint_source",
                    "retraction_endpoint_source",
                    "controller_contract_topic",
                    "expected_controller_contract_id",
                    "expected_capability_policy_id",
                    "speech_source_topic",
                    "asr_runtime_status_topic",
                    "cam3_tool_observations_topic",
                    "cam4_tool_observations_topic",
                    "rfdetr_vlm_request_context_topic",
                }:
                    matches = str(requested).strip().casefold() == str(current).strip().casefold()
                else:
                    matches = requested == current
                if not matches:
                    return SetParametersResult(
                        successful=False,
                        reason=(
                            f"{parameter.name} is launch-lifetime and cannot be "
                            "changed while preflight is running"
                        ),
                    )
            if parameter.name == "robot_endpoint_source" and str(
                parameter.value
            ).strip().casefold() != self._robot_endpoint_source:
                return SetParametersResult(
                    successful=False,
                    reason=(
                        "robot_endpoint_source is launch-lifetime and cannot "
                        "be changed while preflight is running"
                    ),
                )
            if parameter.name == "retraction_endpoint_source" and str(
                parameter.value
            ).strip().casefold() != self._retraction_endpoint_source:
                return SetParametersResult(
                    successful=False,
                    reason=(
                        "retraction_endpoint_source is launch-lifetime and cannot "
                        "be changed while preflight is running"
                    ),
                )
            if parameter.name == "retraction_state_machine_suppressed" and bool(
                parameter.value
            ) != self._retraction_state_machine_suppressed:
                return SetParametersResult(
                    successful=False,
                    reason=(
                        "retraction_state_machine_suppressed is launch-lifetime "
                        "and cannot be changed while preflight is running"
                    ),
                )
            if parameter.name in candidate:
                candidate[parameter.name] = parameter.value

        active_bundle = str(candidate["active_bundle"]).strip()
        spec_dir = str(candidate["spec_dir"]).strip()
        supplied = (
            str(candidate["procedure_type"]).strip().casefold(),
            bool(candidate["require_tool_handover_action_server"]),
            bool(candidate["require_retraction_service"]),
            bool(candidate["require_bed_robot_arm_status"]),
        )
        if not active_bundle:
            return SetParametersResult(
                successful=False,
                reason="active_bundle must be set before readiness can be evaluated",
            )
        # The manager updates active_bundle and spec_dir atomically while the
        # readiness lease is closed.  Refuse a split identity: otherwise the
        # controller contract could be checked against tools from an old
        # procedure during a Live bundle change.
        if not spec_dir or Path(spec_dir).name != active_bundle:
            return SetParametersResult(
                successful=False,
                reason=(
                    "spec_dir must name the active_bundle during an "
                    "integration contract update"
                ),
            )
        try:
            runtime_requirements = _runtime_requirements_for_bundle(
                active_bundle,
                spec_dir=spec_dir,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return SetParametersResult(
                successful=False,
                reason=f"failed to load active scenario runtime policy: {exc}",
            )
        expected = _expected_contract_for_runtime_requirements(
            runtime_requirements
        )
        if supplied != expected:
            return SetParametersResult(
                successful=False,
                reason=(
                    f"external robot contract mismatch for bundle '{active_bundle}': "
                    f"expected {expected}, received {supplied}"
                ),
            )

        previous_identity = (
            self._active_bundle,
            self._spec_dir,
            self._procedure_type,
            self._require_tool_handover_action_server,
            self._require_retraction_service,
            self._require_bed_robot_arm_status,
            self._contract_transitioning,
        )
        next_identity = (
            active_bundle,
            spec_dir,
            supplied[0],
            supplied[1],
            supplied[2],
            supplied[3],
            bool(candidate["contract_transitioning"]),
        )
        self._active_bundle = active_bundle
        self._spec_dir = spec_dir
        self._procedure_type = supplied[0]
        self._require_tool_handover_action_server = supplied[1]
        self._require_retraction_service = supplied[2]
        self._require_bed_robot_arm_status = supplied[3]
        self._scenario_runtime_requirements = runtime_requirements
        self._contract_transitioning = next_identity[6]
        if next_identity != previous_identity:
            self._invalidate_bed_robot_status()
        # CAM3/CAM4 detector facts describe the current operating scene.
        # Never let a frame accepted for the previous procedure identity
        # satisfy the newly selected demo: it must receive fresh typed
        # geometry after the stopped-state bundle transition.  This does not
        # retain or invalidate any raster image/mask because those are not
        # tool-location evidence for VLM admission.
        if next_identity[:2] != previous_identity[:2]:
            self._invalidate_rfdetr_tool_location_leases()
        return SetParametersResult(successful=True)

    def _invalidate_bed_robot_status(self) -> None:
        self._bed_robot_status_valid = False
        self._bed_robot_status_received_monotonic = 0.0
        self._bed_robot_status_source_stamp_sec = 0.0
        self._bed_robot_status_revision = None

    def _invalidate_rfdetr_tool_location_leases(self) -> None:
        """Require fresh, structured CAM3/CAM4 observations after a switch."""

        self._latest_rfdetr_tool_observations = {
            view: None for view in _RFDETR_VIEWS
        }
        self._latest_rfdetr_tool_observations_monotonic = {
            view: 0.0 for view in _RFDETR_VIEWS
        }
        self._latest_rfdetr_tool_observations_source_error = {
            view: "" for view in _RFDETR_VIEWS
        }
        self._last_accepted_rfdetr_tool_observations_source_stamp_sec = {
            view: 0.0 for view in _RFDETR_VIEWS
        }
        # Alignment is display-only, but clearing it prevents the operator
        # from seeing a previous procedure's VLM alignment diagnosis while
        # the new one waits for its first structured detector update.
        self._latest_rfdetr_vlm_alignment = {}
        self._latest_rfdetr_vlm_alignment_monotonic = 0.0
        self._latest_rfdetr_vlm_alignment_error = ""

    def _contract_configuration_valid(self) -> bool:
        runtime_requirements = self._current_scenario_runtime_requirements()
        expected = _expected_contract_for_runtime_requirements(
            runtime_requirements
        )
        supplied = (
            self._procedure_type,
            self._require_tool_handover_action_server,
            self._require_retraction_service,
            self._require_bed_robot_arm_status,
        )
        endpoints_valid = _is_valid_robot_endpoint_configuration(
            robot_endpoint_source=self._robot_endpoint_source,
            retraction_endpoint_source=getattr(
                self, "_retraction_endpoint_source", self._robot_endpoint_source
            ),
            tool_handover_action_name=self._tool_handover_action_name,
            retraction_service_name=self._retraction_service_name,
            require_bed_robot_arm_status=self._require_bed_robot_arm_status,
            retraction_state_machine_suppressed=(
                self._retraction_state_machine_suppressed
            ),
            active_bundle=self._active_bundle,
            scenario_runtime_requirements=runtime_requirements,
            controller_contract_topic=self._controller_contract_topic,
            require_controller_contract=self._require_controller_contract,
        )
        asr_runtime_status_configured = (
            not self._require_asr_runtime_status
            or bool(self._speech_source_topic and self._asr_runtime_status_topic)
        )
        structured_rfdetr_configured = (
            not self._active_requires_rfdetr_tool_observations()
            or bool(
                getattr(self, "_require_rfdetr_tool_observations", False)
                and str(
                    getattr(self, "_perception_backend", "")
                ).strip().casefold()
                in {"local", "external"}
                and str(
                    getattr(self, "_cam3_tool_observations_topic", "")
                ).strip()
                and str(
                    getattr(self, "_cam4_tool_observations_topic", "")
                ).strip()
            )
        )
        return bool(
            self._active_bundle
            and not self._contract_transitioning
            and (
                not bool(getattr(self, "_enable_runtime_route_control", False))
                or bool(getattr(self, "_route_state_initialized", False))
            )
            and supplied == expected
            and endpoints_valid
            and asr_runtime_status_configured
            and structured_rfdetr_configured
        )

    def _active_requires_rfdetr_tool_observations(self) -> bool:
        """Bind typed tool-location admission to the selected procedure."""

        return bool(
            self._current_scenario_runtime_requirements()
            .rfdetr_tool_observations_required
        )

    def _active_requires_perception(self) -> bool:
        """Apply the perception health gate to the active procedure."""

        return bool(
            self._require_perception
            or self._active_requires_rfdetr_tool_observations()
        )

    def _on_rfdetr_health(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("schema") not in _RFDETR_HEALTH_SCHEMAS:
            return
        source_stamp_sec, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=self._perception_max_age_sec,
            future_tolerance_sec=(
                self._perception_source_future_tolerance_sec
            ),
            source_name="rfdetr_health",
            previous_stamp_sec=(
                self._last_accepted_rfdetr_source_stamp_sec or None
            ),
        )
        self._latest_rfdetr_health = payload
        self._latest_rfdetr_monotonic = time.monotonic()
        self._latest_rfdetr_source_error = source_error
        if not source_error and source_stamp_sec is not None:
            self._last_accepted_rfdetr_source_stamp_sec = source_stamp_sec

    def _on_rfdetr_tool_observations(
        self,
        msg: ToolObservation2DArray,
        *,
        expected_view: str,
    ) -> None:
        """Record a bounded typed detector lease for one VLM camera view."""

        if expected_view not in _RFDETR_VIEWS:
            return
        source_name = (
            f"rfdetr_{expected_view.replace('_', '')}_tool_observations"
        )
        source_stamp_sec, source_error = rfdetr_tool_observation_source_stamp(
            msg,
            expected_view=expected_view,
            expected_model_version=str(
                getattr(self, "_rfdetr_expected_model_versions", {}).get(
                    expected_view, ""
                )
            ),
        )
        if source_error:
            source_error = source_error.replace(
                "rfdetr_tool_observations",
                source_name,
                1,
            )
        observations = getattr(self, "_latest_rfdetr_tool_observations", None)
        receipts = getattr(
            self,
            "_latest_rfdetr_tool_observations_monotonic",
            None,
        )
        errors = getattr(
            self,
            "_latest_rfdetr_tool_observations_source_error",
            None,
        )
        accepted_stamps = getattr(
            self,
            "_last_accepted_rfdetr_tool_observations_source_stamp_sec",
            None,
        )
        if not isinstance(observations, dict):
            observations = {}
            self._latest_rfdetr_tool_observations = observations
        if not isinstance(receipts, dict):
            receipts = {}
            self._latest_rfdetr_tool_observations_monotonic = receipts
        if not isinstance(errors, dict):
            errors = {}
            self._latest_rfdetr_tool_observations_source_error = errors
        if not isinstance(accepted_stamps, dict):
            accepted_stamps = {}
            self._last_accepted_rfdetr_tool_observations_source_stamp_sec = (
                accepted_stamps
            )

        if not source_error and source_stamp_sec is not None:
            _, source_error = validate_source_stamp(
                {"stamp_sec": source_stamp_sec},
                now_sec=time.time(),
                max_age_sec=self._perception_max_age_sec,
                future_tolerance_sec=(
                    self._perception_source_future_tolerance_sec
                ),
                source_name=source_name,
                previous_stamp_sec=accepted_stamps.get(expected_view) or None,
            )

        # A malformed or replayed newest frame must disarm this view rather
        # than leaving an earlier healthy detector lease active.  The retained
        # payload has only a timestamp and no image/instance data.
        observations[expected_view] = (
            {
                "stamp_sec": float(source_stamp_sec),
                "no_detections": not bool(getattr(msg, "instances", ())),
            }
            if source_stamp_sec is not None and not source_error
            else None
        )
        receipts[expected_view] = time.monotonic()
        errors[expected_view] = source_error or ""
        if not source_error and source_stamp_sec is not None:
            accepted_stamps[expected_view] = source_stamp_sec

    def _on_rfdetr_vlm_request_context(self, msg: VLMRequestContext) -> None:
        """Expose the VLM's optional view-alignment result without gating.

        This observes the already-bounded VLM request context. It does not
        make preflight depend on VLM inference or force CAM3 to align with a
        FLIR image; those are distinct views and only typed detector
        freshness/provenance authorizes the CAM3/CAM4 data plane.
        """

        try:
            payload = json.loads(str(getattr(msg, "compact_json", "")))
        except (TypeError, ValueError):
            payload = None
        alignment: dict[str, str] = {}
        error = ""
        perception = (
            payload.get("observable_perception")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(perception, dict):
            error = "rfdetr_vlm_alignment_context_invalid"
        else:
            candidates: list[object] = [
                perception.get("visual_alignment"),
                perception.get("alignment"),
            ]
            views = perception.get("tool_detection_views", [])
            if isinstance(views, list):
                candidates.extend(views[:2])
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if "view" in candidate:
                    view = _nonempty_message_text(candidate.get("view"))
                    view_alignment = candidate.get("visual_alignment")
                    if not isinstance(view_alignment, dict):
                        view_alignment = candidate.get("alignment")
                    status = (
                        _nonempty_message_text(view_alignment.get("status"))
                        if isinstance(view_alignment, dict)
                        else ""
                    )
                    if view in _RFDETR_VIEWS and status in _RFDETR_VLM_ALIGNMENT_STATUSES:
                        alignment[view] = status
                    continue
                for view in _RFDETR_VIEWS:
                    item = candidate.get(view)
                    status = (
                        _nonempty_message_text(item.get("status"))
                        if isinstance(item, dict)
                        else ""
                    )
                    if status in _RFDETR_VLM_ALIGNMENT_STATUSES:
                        alignment[view] = status
            if not alignment:
                error = "rfdetr_vlm_alignment_not_observed"
        self._latest_rfdetr_vlm_alignment = alignment
        self._latest_rfdetr_vlm_alignment_monotonic = time.monotonic()
        self._latest_rfdetr_vlm_alignment_error = error

    def _on_cv_contract_status(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != _CV_CONTRACT_STATUS_SCHEMA
        ):
            return
        source_stamp_sec, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=self._perception_max_age_sec,
            future_tolerance_sec=(
                self._perception_source_future_tolerance_sec
            ),
            source_name="cv_contract_status",
            previous_stamp_sec=(
                self._last_accepted_cv_contract_source_stamp_sec or None
            ),
        )
        self._latest_cv_contract_status = payload
        self._latest_cv_contract_monotonic = time.monotonic()
        self._latest_cv_contract_source_error = source_error
        if not source_error and source_stamp_sec is not None:
            self._last_accepted_cv_contract_source_stamp_sec = source_stamp_sec

    def _on_controller_contract(
        self,
        msg: String,
        source: str | None = None,
    ) -> None:
        """Record a read-only controller manifest without issuing any Goal."""

        normalized_source = self._robot_endpoint_source
        if source is not None:
            normalized_source = str(source).strip().casefold()
            if normalized_source not in {
                self._robot_endpoint_source,
                self._retraction_endpoint_source,
            }:
                return

        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        # Keep malformed/replayed schemas as observations so readiness reports
        # a stable mismatch rather than retaining a prior healthy controller
        # lease. Receipt time is insufficient: an old contract can be replayed
        # with a new DDS arrival time.
        source_stamp_sec, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=self._controller_contract_max_age_sec,
            future_tolerance_sec=0.5,
            source_name="controller_contract",
            previous_stamp_sec=(
                self._controller_contract_leases[normalized_source][
                    "last_accepted_stamp_sec"
                ]
                or None
            ),
        )
        received_monotonic = time.monotonic()
        lease = self._controller_contract_leases[normalized_source]
        lease["payload"] = payload
        lease["monotonic"] = received_monotonic
        lease["source_stamp_sec"] = float(source_stamp_sec or 0.0)
        lease["source_error"] = source_error
        if not source_error and source_stamp_sec is not None:
            lease["last_accepted_stamp_sec"] = source_stamp_sec
        if normalized_source == self._robot_endpoint_source:
            self._latest_controller_contract = payload
            self._latest_controller_contract_monotonic = received_monotonic
            self._latest_controller_contract_source_stamp_sec = float(
                source_stamp_sec or 0.0
            )
            self._latest_controller_contract_source_error = source_error
            if not source_error and source_stamp_sec is not None:
                self._last_accepted_controller_contract_source_stamp_sec = (
                    source_stamp_sec
                )

    def _on_asr_runtime_status(self, msg: String) -> None:
        """Track the operational ASR status as a freshness-bound Live lease."""

        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        source_stamp_sec, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=self._asr_runtime_status_max_age_sec,
            future_tolerance_sec=(
                self._asr_runtime_status_source_future_tolerance_sec
            ),
            source_name="asr_runtime_status",
            previous_stamp_sec=(
                self._last_accepted_asr_runtime_status_source_stamp_sec or None
            ),
        )
        self._latest_asr_runtime_status = payload
        self._latest_asr_runtime_status_monotonic = time.monotonic()
        self._latest_asr_runtime_status_source_stamp_sec = float(
            source_stamp_sec or 0.0
        )
        self._latest_asr_runtime_status_source_error = source_error
        if not source_error and source_stamp_sec is not None:
            self._last_accepted_asr_runtime_status_source_stamp_sec = source_stamp_sec

    @staticmethod
    def _expected_controller_execution_mode(source: str) -> str:
        return "virtual" if source == VIRTUAL_ENDPOINT_SOURCE else "real"

    def _controller_contract_spec(self):
        if not self._spec_dir or not self._active_bundle:
            return None
        configured = Path(self._spec_dir)
        bundle_dir = (
            configured
            if configured.name == self._active_bundle
            else configured.parent / self._active_bundle
        )
        try:
            return load_bundle(bundle_dir)
        except Exception:
            return None

    @staticmethod
    def _spec_requires_retraction_profile_identity(spec: object) -> bool:
        getter = getattr(spec, "get_bed_robot_arm_group_spec", None)
        group_spec = getter() if callable(getter) else None
        if group_spec is None:
            return False
        return any(
            bool(group.enabled)
            and "change_end_effector" in {
                str(operation).strip().casefold()
                for operation in group.allowed_operations
            }
            for group in group_spec.groups
        )

    def _controller_contract_readiness(self) -> tuple[bool, float, tuple[str, ...]]:
        if not self._require_controller_contract:
            return True, -1.0, ()
        spec = self._controller_contract_spec()
        if spec is None:
            return False, -1.0, ("controller_contract_spec_unavailable",)

        def validate_operation_contract(
            *,
            operation: str,
            source: str,
            expected_contract_id: str,
            require_tool_handover: bool,
            require_retraction_service: bool,
        ) -> tuple[float, tuple[str, ...]]:
            lease = self._controller_contract_leases[source]
            payload = lease["payload"]
            received_monotonic = float(lease["monotonic"])
            if not isinstance(payload, dict) or received_monotonic <= 0.0:
                return -1.0, (f"{operation}_controller_contract_missing",)
            age_sec = time.monotonic() - received_monotonic
            if age_sec < 0.0 or age_sec > self._controller_contract_max_age_sec:
                return age_sec, (f"{operation}_controller_contract_stale",)
            source_error = str(lease["source_error"])
            if source_error:
                return age_sec, (f"{operation}_{source_error}",)
            _, source_error = validate_source_stamp(
                payload,
                now_sec=time.time(),
                max_age_sec=self._controller_contract_max_age_sec,
                future_tolerance_sec=0.5,
                source_name="controller_contract",
            )
            if source_error:
                return age_sec, (f"{operation}_{source_error}",)
            require_profile_identity = bool(
                require_retraction_service
                and source == EXTERNAL_ENDPOINT_SOURCE
                and self._spec_requires_retraction_profile_identity(spec)
            )
            mismatches = controller_contract_mismatches(
                payload,
                expected_contract_id=expected_contract_id,
                expected_endpoint_source=source,
                expected_execution_mode=self._expected_controller_execution_mode(
                    source
                ),
                expected_tool_handover_endpoint=self._tool_handover_action_name,
                expected_retraction_service_name=self._retraction_service_name,
                expected_capability_policy_id=(
                    self._expected_capability_policy_id
                ),
                require_tool_handover=require_tool_handover,
                require_retraction_service=require_retraction_service,
                require_retraction_profile_identity=require_profile_identity,
                require_physical_stop_confirmation=(
                    require_retraction_service
                    and self._require_physical_stop_confirmation
                ),
                required_tool_instances=(
                    required_tool_instances_for_spec(spec)
                    if require_tool_handover
                    else ()
                ),
            )
            return age_sec, tuple(
                f"{operation}_{reason}" for reason in mismatches
            )

        # Validate only the operations that this runtime has actually admitted.
        # A disabled operation must not prevent the independent counterpart from
        # using its selected endpoint source and controller contract.
        if self._require_tool_handover_action_server:
            tool_age_sec, tool_reasons = validate_operation_contract(
                operation="tool_handover",
                source=self._robot_endpoint_source,
                expected_contract_id=self._expected_controller_contract_id,
                require_tool_handover=True,
                require_retraction_service=False,
            )
        else:
            tool_age_sec, tool_reasons = -1.0, ()

        if self._require_retraction_service:
            retraction_age_sec, retraction_reasons = validate_operation_contract(
                operation="retraction",
                source=self._retraction_endpoint_source,
                expected_contract_id=self._retraction_expected_controller_contract_id,
                require_tool_handover=False,
                require_retraction_service=True,
            )
        else:
            retraction_age_sec, retraction_reasons = -1.0, ()
        reasons = tool_reasons + retraction_reasons
        ages = [age for age in (tool_age_sec, retraction_age_sec) if age >= 0.0]
        return not reasons, (max(ages) if ages else -1.0), reasons

    def _asr_runtime_status_readiness(self) -> tuple[bool, float, str]:
        if not self._require_asr_runtime_status:
            return True, -1.0, ""
        if self._latest_asr_runtime_status_monotonic <= 0.0:
            return False, -1.0, "asr_runtime_status_missing"
        receipt_age_sec = (
            time.monotonic() - self._latest_asr_runtime_status_monotonic
        )
        if (
            receipt_age_sec < 0.0
            or receipt_age_sec > self._asr_runtime_status_max_age_sec
        ):
            return False, receipt_age_sec, "asr_runtime_status_stale"
        if self._latest_asr_runtime_status_source_error:
            return (
                False,
                receipt_age_sec,
                self._latest_asr_runtime_status_source_error,
            )
        valid, source_age_sec, reason = evaluate_asr_runtime_status(
            self._latest_asr_runtime_status,
            now_sec=time.time(),
            max_age_sec=self._asr_runtime_status_max_age_sec,
            future_tolerance_sec=(
                self._asr_runtime_status_source_future_tolerance_sec
            ),
        )
        return valid, source_age_sec, reason

    def _perception_payload_source_readiness(
        self,
        *,
        payload: dict[str, Any] | None,
        received_monotonic: float,
        latest_source_error: str,
        source_name: str,
    ) -> tuple[bool, float, str]:
        """Require source-time freshness, not merely a recent DDS receipt."""

        if not self._active_requires_perception():
            return True, -1.0, ""
        if not isinstance(payload, dict) or received_monotonic <= 0.0:
            return False, -1.0, f"{source_name}_missing"
        receipt_age_sec = time.monotonic() - received_monotonic
        if (
            receipt_age_sec < 0.0
            or receipt_age_sec > self._perception_max_age_sec
        ):
            return False, receipt_age_sec, f"{source_name}_receipt_stale"
        if latest_source_error:
            return False, receipt_age_sec, latest_source_error
        source_stamp_sec, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=self._perception_max_age_sec,
            future_tolerance_sec=(
                self._perception_source_future_tolerance_sec
            ),
            source_name=source_name,
        )
        if source_error or source_stamp_sec is None:
            return False, receipt_age_sec, source_error or f"{source_name}_missing"
        source_age_sec = time.time() - source_stamp_sec
        return True, max(receipt_age_sec, source_age_sec), ""

    def _rfdetr_tool_observation_readiness(
        self,
        *,
        view: str,
    ) -> tuple[bool, float, str]:
        """Return one typed VLM detector-view lease, without FLIR coupling."""

        if not self._active_requires_rfdetr_tool_observations():
            return True, -1.0, ""
        source_name = f"rfdetr_{view.replace('_', '')}_tool_observations"
        observations = getattr(self, "_latest_rfdetr_tool_observations", {})
        receipts = getattr(
            self,
            "_latest_rfdetr_tool_observations_monotonic",
            {},
        )
        errors = getattr(
            self,
            "_latest_rfdetr_tool_observations_source_error",
            {},
        )
        payload = observations.get(view) if isinstance(observations, dict) else None
        received_monotonic = (
            float(receipts.get(view, 0.0)) if isinstance(receipts, dict) else 0.0
        )
        latest_error = (
            str(errors.get(view, "")) if isinstance(errors, dict) else ""
        )
        if received_monotonic <= 0.0:
            return False, -1.0, f"{source_name}_missing"
        receipt_age_sec = time.monotonic() - received_monotonic
        if (
            receipt_age_sec < 0.0
            or receipt_age_sec > self._perception_max_age_sec
        ):
            return False, receipt_age_sec, f"{source_name}_receipt_stale"
        if latest_error:
            return False, receipt_age_sec, latest_error
        if not isinstance(payload, dict):
            return False, receipt_age_sec, f"{source_name}_missing"
        source_stamp_sec, source_error = validate_source_stamp(
            payload,
            now_sec=time.time(),
            max_age_sec=self._perception_max_age_sec,
            future_tolerance_sec=(
                self._perception_source_future_tolerance_sec
            ),
            source_name=source_name,
        )
        if source_error or source_stamp_sec is None:
            return (
                False,
                receipt_age_sec,
                source_error or f"{source_name}_missing",
            )
        source_age_sec = time.time() - source_stamp_sec
        return True, max(receipt_age_sec, source_age_sec), ""

    def _rfdetr_vlm_alignment_details(self) -> dict[str, dict[str, object]]:
        """Return a bounded, non-gating snapshot of VLM view alignment."""

        received_monotonic = float(
            getattr(self, "_latest_rfdetr_vlm_alignment_monotonic", 0.0)
        )
        latest_error = str(
            getattr(self, "_latest_rfdetr_vlm_alignment_error", "")
        )
        values = getattr(self, "_latest_rfdetr_vlm_alignment", {})
        alignment = values if isinstance(values, dict) else {}
        age_sec = (
            time.monotonic() - received_monotonic
            if received_monotonic > 0.0
            else -1.0
        )
        if age_sec < 0.0:
            status = "not_observed"
        elif age_sec > self._perception_max_age_sec:
            status = "context_stale"
        elif latest_error == "rfdetr_vlm_alignment_not_observed":
            status = "not_observed"
        elif latest_error:
            status = "context_invalid"
        else:
            status = ""
        return {
            view: {
                "status": (
                    status
                    or str(alignment.get(view, "not_observed"))[:64]
                ),
                "context_age_sec": round(age_sec, 3) if age_sec >= 0.0 else -1.0,
            }
            for view in sorted(_RFDETR_VIEWS)
        }

    def _on_bed_robot_arm_status(self, msg: BedRobotArmStateArray) -> None:
        source_stamp_sec = float(msg.stamp.sec) + float(msg.stamp.nanosec) / 1e9
        now_sec = time.time()
        revision = int(msg.revision)
        source_is_strictly_newer = (
            source_stamp_sec > self._bed_robot_status_source_stamp_sec
        )
        valid = bool(
            str(msg.procedure_type).strip().casefold() == self._procedure_type
            and validate_bed_robot_status_layout(self._procedure_type, list(msg.arms))
            and source_stamp_sec > 0.0
            and source_stamp_sec <= now_sec + 0.5
            and source_is_strictly_newer
        )
        if not valid:
            self._bed_robot_status_valid = False
            return
        self._bed_robot_status_valid = True
        self._bed_robot_status_received_monotonic = time.monotonic()
        self._bed_robot_status_source_stamp_sec = source_stamp_sec
        self._bed_robot_status_revision = revision

    def _snapshot(self) -> dict[str, Any]:
        require_perception = self._active_requires_perception()
        require_rfdetr_tool_observations = (
            self._active_requires_rfdetr_tool_observations()
        )
        (
            rfdetr_source_valid,
            rfdetr_age_sec,
            rfdetr_source_reason,
        ) = self._perception_payload_source_readiness(
            payload=self._latest_rfdetr_health,
            received_monotonic=self._latest_rfdetr_monotonic,
            latest_source_error=self._latest_rfdetr_source_error,
            source_name="rfdetr_health",
        )
        (
            cv_contract_source_valid,
            cv_contract_age_sec,
            cv_contract_source_reason,
        ) = self._perception_payload_source_readiness(
            payload=self._latest_cv_contract_status,
            received_monotonic=self._latest_cv_contract_monotonic,
            latest_source_error=self._latest_cv_contract_source_error,
            source_name="cv_contract_status",
        )
        # Preserve contract mismatch diagnostics for operators, but do not
        # feed them back into scenario, voice, or dispatch admission.
        _, _, controller_contract_mismatches = (
            self._controller_contract_readiness()
        )
        (
            asr_runtime_status_valid,
            asr_runtime_status_age_sec,
            asr_runtime_status_reason,
        ) = self._asr_runtime_status_readiness()
        (
            rfdetr_cam3_tool_observations_valid,
            rfdetr_cam3_tool_observations_age_sec,
            rfdetr_cam3_tool_observations_reason,
        ) = self._rfdetr_tool_observation_readiness(view="cam_3")
        (
            rfdetr_cam4_tool_observations_valid,
            rfdetr_cam4_tool_observations_age_sec,
            rfdetr_cam4_tool_observations_reason,
        ) = self._rfdetr_tool_observation_readiness(view="cam_4")
        bed_robot_status_reception_age_sec = (
            time.monotonic() - self._bed_robot_status_received_monotonic
            if self._bed_robot_status_received_monotonic > 0.0
            else -1.0
        )
        bed_robot_status_source_age_sec = (
            time.time() - self._bed_robot_status_source_stamp_sec
            if self._bed_robot_status_source_stamp_sec > 0.0
            else -1.0
        )
        bed_robot_status_age_sec = (
            max(
                bed_robot_status_reception_age_sec,
                bed_robot_status_source_age_sec,
            )
            if bed_robot_status_reception_age_sec >= 0.0
            and bed_robot_status_source_age_sec >= 0.0
            else -1.0
        )
        snapshot = evaluate_readiness(
            sentence_publisher_count=self.count_publishers(
                self._speech_source_topic
            ),
            require_sentence_publisher=self._require_sentence_publisher,
            tool_handover_server_ready=self._tool_handover_client.server_is_ready(),
            require_tool_handover_action_server=(
                self._require_tool_handover_action_server
            ),
            retraction_service_ready=self._retraction_client.service_is_ready(),
            require_retraction_service=self._require_retraction_service,
            bed_robot_arm_status_valid=self._bed_robot_status_valid,
            bed_robot_arm_status_age_sec=bed_robot_status_age_sec,
            bed_robot_arm_status_max_age_sec=(
                self._bed_robot_arm_status_max_age_sec
            ),
            require_bed_robot_arm_status=self._require_bed_robot_arm_status,
            require_perception=require_perception,
            rfdetr_health=self._latest_rfdetr_health,
            rfdetr_age_sec=rfdetr_age_sec,
            perception_max_age_sec=self._perception_max_age_sec,
            contract_configuration_valid=self._contract_configuration_valid(),
            perception_backend=self._perception_backend,
            cv_contract_status=self._latest_cv_contract_status,
            cv_contract_age_sec=cv_contract_age_sec,
            require_metric_3d=bool(
                getattr(self, "_require_metric_3d", False)
            ),
            robot_endpoint_source=self._robot_endpoint_source,
            retraction_endpoint_source=getattr(
                self, "_retraction_endpoint_source", self._robot_endpoint_source
            ),
            retraction_state_machine_suppressed=(
                self._retraction_state_machine_suppressed
            ),
            controller_contract_valid=True,
            require_controller_contract=False,
            controller_contract_age_sec=-1.0,
            asr_runtime_status_valid=asr_runtime_status_valid,
            require_asr_runtime_status=self._require_asr_runtime_status,
            asr_runtime_status_age_sec=asr_runtime_status_age_sec,
            rfdetr_source_valid=rfdetr_source_valid,
            cv_contract_source_valid=cv_contract_source_valid,
            require_rfdetr_tool_observations=require_rfdetr_tool_observations,
            rfdetr_cam3_tool_observations_valid=(
                rfdetr_cam3_tool_observations_valid
            ),
            rfdetr_cam3_tool_observations_age_sec=(
                rfdetr_cam3_tool_observations_age_sec
            ),
            rfdetr_cam3_tool_observations_reason=(
                rfdetr_cam3_tool_observations_reason
            ),
            rfdetr_cam4_tool_observations_valid=(
                rfdetr_cam4_tool_observations_valid
            ),
            rfdetr_cam4_tool_observations_age_sec=(
                rfdetr_cam4_tool_observations_age_sec
            ),
            rfdetr_cam4_tool_observations_reason=(
                rfdetr_cam4_tool_observations_reason
            ),
            contract_transitioning=self._contract_transitioning,
            controller_contract_reasons=(),
            asr_runtime_status_reason=asr_runtime_status_reason,
            rfdetr_source_reason=rfdetr_source_reason,
            cv_contract_source_reason=cv_contract_source_reason,
        )
        snapshot["details"].update(
            {
                "active_bundle": self._active_bundle,
                "procedure_type": self._procedure_type,
                "contract_transitioning": self._contract_transitioning,
                "controller_contract_topic": self._controller_contract_topic,
                "expected_controller_contract_id": (
                    self._expected_controller_contract_id
                ),
                "expected_capability_policy_id": (
                    self._expected_capability_policy_id
                ),
                "require_physical_stop_confirmation": (
                    self._require_physical_stop_confirmation
                ),
                "execution_route_state_required": (
                    bool(getattr(self, "_enable_runtime_route_control", False))
                ),
                "execution_route_state_initialized": (
                    bool(getattr(self, "_route_state_initialized", True))
                ),
                "execution_route_revision": int(
                    getattr(self, "_route_state_revision", -1)
                ),
                "execution_route_initialization_revision": int(
                    getattr(self, "_route_state_initialization_revision", -1)
                ),
                "execution_route_selected_source": str(
                    getattr(self, "_route_state_selected_source", "")
                ),
                "execution_route_retraction_source": str(
                    getattr(
                        self,
                        "_route_state_retraction_source",
                        self._robot_endpoint_source,
                    )
                ),
                "execution_route_initialization_state": str(
                    getattr(self, "_route_state_initialization_state", "")
                ),
                "controller_contract_mismatches": [
                    _bounded_reason(reason, fallback="contract_mismatch")
                    for reason in controller_contract_mismatches[:8]
                ],
                "speech_source_topic": self._speech_source_topic,
                "asr_runtime_status_topic": self._asr_runtime_status_topic,
                "asr_runtime_status_reason": asr_runtime_status_reason,
                "rfdetr_source_reason": rfdetr_source_reason,
                "cv_contract_source_reason": cv_contract_source_reason,
                "rfdetr_tool_observations_required": (
                    require_rfdetr_tool_observations
                ),
                "perception_input_required": require_perception,
                "rfdetr_cam3_tool_observations_reason": (
                    rfdetr_cam3_tool_observations_reason
                ),
                "rfdetr_cam4_tool_observations_reason": (
                    rfdetr_cam4_tool_observations_reason
                ),
                "rfdetr_vlm_visual_alignment": (
                    self._rfdetr_vlm_alignment_details()
                ),
            }
        )
        # Publish a wall-clock lease stamp rather than a replay /clock stamp.
        # The execution bridge validates this independently before every Goal
        # or Service request.
        snapshot["stamp_sec"] = round(time.time(), 6)
        return snapshot

    def _publish_readiness(self) -> None:
        msg = String()
        msg.data = json.dumps(
            self._snapshot(),
            separators=(",", ":"),
            sort_keys=True,
        )
        self._readiness_pub.publish(msg)

    def _handle_readiness(
        self,
        _request: Trigger.Request,
        response: Trigger.Response,
    ) -> Trigger.Response:
        snapshot = self._snapshot()
        response.success = bool(snapshot["ready"])
        response.message = readiness_service_message(snapshot)
        self._publish_readiness()
        return response


def main() -> None:
    rclpy.init()
    node = IntegrationPreflightNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
