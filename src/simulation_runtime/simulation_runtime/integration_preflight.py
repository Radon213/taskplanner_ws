"""Fail-closed readiness gate for the external Taskplanner runtime."""

from __future__ import annotations

import json
import time
from typing import Any

import rclpy
from rclpy.action import ActionClient
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger
from surgical_interop_msgs.action import ExecuteToolHandover
from surgical_interop_msgs.msg import BedRobotArmStateArray
from surgical_interop_msgs.srv import ExecuteRetractionCommand


_BED_ROBOT_LAYOUTS = {
    "thyroidectomy": {"army_navy"},
    "nephrectomy": {"left_malleable", "right_malleable"},
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


def expected_contract_for_bundle(bundle_name: str) -> tuple[str, bool, bool]:
    """Return procedure, retraction Service, and status requirements."""

    normalized = str(bundle_name).strip().casefold()
    if normalized in {"thyroidectomy", "thyroidectomy_demo"}:
        return "thyroidectomy", True, True
    if normalized == "nephrectomy":
        return "nephrectomy", True, True
    return "", False, False


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


def evaluate_readiness(
    *,
    sentence_publisher_count: int,
    require_sentence_publisher: bool,
    tool_handover_server_ready: bool,
    retraction_service_ready: bool,
    require_retraction_service: bool,
    bed_robot_arm_status_valid: bool,
    bed_robot_arm_status_age_sec: float,
    bed_robot_arm_status_max_age_sec: float,
    require_bed_robot_arm_status: bool,
    require_perception: bool,
    rfdetr_health: dict[str, Any] | None,
    rfdetr_age_sec: float,
    perception_max_age_sec: float,
    contract_configuration_valid: bool = True,
    perception_backend: str = "local",
    cv_contract_status: dict[str, Any] | None = None,
    cv_contract_age_sec: float = -1.0,
    require_metric_3d: bool = False,
) -> dict[str, Any]:
    checks = {
        "contract_configuration": bool(contract_configuration_valid),
        "surgeon_sentence_publisher": (
            not require_sentence_publisher or sentence_publisher_count > 0
        ),
        "tool_handover_action_server": bool(tool_handover_server_ready),
        "retraction_command_service": (
            not require_retraction_service or bool(retraction_service_ready)
        ),
        "bed_robot_arm_status": (
            not require_bed_robot_arm_status
            or (
                bool(bed_robot_arm_status_valid)
                and 0.0 <= float(bed_robot_arm_status_age_sec)
                <= float(bed_robot_arm_status_max_age_sec)
            )
        ),
        "perception_input": True,
    }
    details: dict[str, Any] = {
        "sentence_publisher_count": max(0, int(sentence_publisher_count)),
        "bed_robot_arm_status_age_sec": (
            round(float(bed_robot_arm_status_age_sec), 3)
            if bed_robot_arm_status_age_sec >= 0.0
            else -1.0
        ),
        "rfdetr_age_sec": (
            round(float(rfdetr_age_sec), 3) if rfdetr_age_sec >= 0.0 else -1.0
        ),
    }

    normalized_backend = str(perception_backend).strip().casefold()
    if normalized_backend not in {"local", "external", "disabled"}:
        normalized_backend = "invalid"
    details["perception_backend"] = normalized_backend

    if require_perception and normalized_backend == "local":
        health = rfdetr_health if isinstance(rfdetr_health, dict) else {}
        perception_ready = (
            bool(health.get("connected"))
            and str(health.get("status", "")).strip().lower() == "ready"
            and bool(health.get("cam4_aligned"))
            and 0.0 <= float(rfdetr_age_sec) <= float(perception_max_age_sec)
        )
        checks["perception_input"] = perception_ready
        details["rfdetr_status"] = str(health.get("status", "missing"))
        details["cam4_aligned"] = bool(health.get("cam4_aligned"))
    elif require_perception and normalized_backend == "external":
        health = rfdetr_health if isinstance(rfdetr_health, dict) else {}
        contract = (
            cv_contract_status if isinstance(cv_contract_status, dict) else {}
        )
        # The PNU bridge deliberately reuses the existing Taskplanner health
        # topic/schema.  Require its provider identity and semantic readiness,
        # so a reachable worker or a successful Blood/Hand-only request cannot
        # accidentally authorize planner-facing Tool evidence.  A valid empty
        # Tool result remains ready: detection_count is intentionally not a
        # gate.
        is_pnu_health = health.get("provider") == "pnu_hand_blood"
        if is_pnu_health:
            metric_3d_ready = bool(health.get("metric_3d_ready"))
            perception_ready = (
                bool(health.get("connected"))
                and str(health.get("status", "")).strip().lower() == "ready"
                and bool(health.get("semantic_ready"))
                and bool(health.get("cam4_aligned"))
                and (not require_metric_3d or metric_3d_ready)
                and 0.0 <= float(rfdetr_age_sec) <= float(perception_max_age_sec)
            )
            details["perception_evidence_source"] = "pnu_bridge_health"
            details["rfdetr_status"] = str(health.get("status", "missing"))
            details["semantic_ready"] = bool(health.get("semantic_ready"))
            details["cam4_aligned"] = bool(health.get("cam4_aligned"))
            details["metric_3d_required"] = bool(require_metric_3d)
            details["metric_3d_ready"] = metric_3d_ready
            details["metric_3d_reasons"] = list(
                health.get("metric_3d_reasons", [])
                if isinstance(health.get("metric_3d_reasons", []), list)
                else []
            )
            details["empty_detection_result"] = bool(
                health.get("empty_detection_result")
            )
        else:
            # Retain the generic external-CV authorization path for a future
            # custom-IDL adapter.  Topic-name presence alone never passes it.
            perception_ready = (
                contract.get("schema") == "taskplanner.cv_external_contract.v1"
                and bool(contract.get("ready_for_external_evidence"))
                and 0.0 <= float(cv_contract_age_sec)
                <= float(perception_max_age_sec)
            )
            details["perception_evidence_source"] = "cv_contract_monitor"
        checks["perception_input"] = perception_ready
        details["cv_contract_state"] = str(
            contract.get("readiness_state", "missing")
        )
        details["cv_contract_age_sec"] = (
            round(float(cv_contract_age_sec), 3)
            if cv_contract_age_sec >= 0.0
            else -1.0
        )
    elif require_perception:
        checks["perception_input"] = False
        details["perception_backend_error"] = (
            "perception backend is disabled or invalid"
        )

    missing = [name for name, passed in checks.items() if not passed]
    return {
        "schema": "taskplanner.integration_readiness.v1",
        "ready": not missing,
        "checks": checks,
        "missing": missing,
        "details": details,
    }


class IntegrationPreflightNode(Node):
    def __init__(self) -> None:
        super().__init__("integration_preflight")
        self.declare_parameter("sentence_topic", "/sensors/surgeon/sentence")
        self.declare_parameter("readiness_topic", "/integration/readiness")
        self.declare_parameter(
            "readiness_service",
            "/integration/check_readiness",
        )
        self.declare_parameter(
            "rfdetr_health_topic",
            "/surgery/perception/rfdetr/health",
        )
        self.declare_parameter("require_sentence_publisher", True)
        self.declare_parameter("require_perception", False)
        self.declare_parameter("require_metric_3d", False)
        self.declare_parameter("perception_max_age_sec", 3.0)
        self.declare_parameter("perception_backend", "local")
        self.declare_parameter(
            "cv_contract_status_topic",
            "/integration/cv_contract/status",
        )
        self.declare_parameter(
            "tool_handover_action_name",
            "/surgery/tool_handover",
        )
        self.declare_parameter(
            "retraction_service_name",
            "/surgery/retraction/command",
        )
        self.declare_parameter("require_retraction_service", True)
        self.declare_parameter(
            "bed_robot_arm_status_topic",
            "/external/bed_robot_arms/status",
        )
        self.declare_parameter("require_bed_robot_arm_status", True)
        self.declare_parameter("bed_robot_arm_status_max_age_sec", 3.0)
        self.declare_parameter("active_bundle", "")
        self.declare_parameter("procedure_type", "")
        self.declare_parameter("contract_transitioning", False)

        self._sentence_topic = str(self.get_parameter("sentence_topic").value)
        self._require_sentence_publisher = bool(
            self.get_parameter("require_sentence_publisher").value
        )
        self._require_perception = bool(
            self.get_parameter("require_perception").value
        )
        self._require_metric_3d = bool(
            self.get_parameter("require_metric_3d").value
        )
        self._perception_backend = str(
            self.get_parameter("perception_backend").value
        ).strip().casefold()
        self._require_retraction_service = bool(
            self.get_parameter("require_retraction_service").value
        )
        self._require_bed_robot_arm_status = bool(
            self.get_parameter("require_bed_robot_arm_status").value
        )
        self._active_bundle = str(
            self.get_parameter("active_bundle").value
        ).strip()
        self._contract_transitioning = bool(
            self.get_parameter("contract_transitioning").value
        )
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
        self._latest_rfdetr_health: dict[str, Any] | None = None
        self._latest_rfdetr_monotonic = 0.0
        self._latest_cv_contract_status: dict[str, Any] | None = None
        self._latest_cv_contract_monotonic = 0.0
        self.add_on_set_parameters_callback(self._on_contract_parameters_changed)

        self._tool_handover_client = ActionClient(
            self,
            ExecuteToolHandover,
            str(self.get_parameter("tool_handover_action_name").value),
        )
        self._retraction_client = self.create_client(
            ExecuteRetractionCommand,
            str(self.get_parameter("retraction_service_name").value),
        )
        self._bed_robot_arm_status_topic = str(
            self.get_parameter("bed_robot_arm_status_topic").value
        )
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
        self.create_subscription(
            String,
            str(self.get_parameter("rfdetr_health_topic").value),
            self._on_rfdetr_health,
            10,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("cv_contract_status_topic").value),
            self._on_cv_contract_status,
            10,
        )
        self.create_service(
            Trigger,
            str(self.get_parameter("readiness_service").value),
            self._handle_readiness,
        )
        self.create_timer(1.0, self._publish_readiness)

    def _on_contract_parameters_changed(self, parameters) -> SetParametersResult:
        candidate = {
            "active_bundle": self._active_bundle,
            "procedure_type": self._procedure_type,
            "require_retraction_service": self._require_retraction_service,
            "require_bed_robot_arm_status": self._require_bed_robot_arm_status,
            "contract_transitioning": self._contract_transitioning,
        }
        for parameter in parameters:
            if parameter.name in candidate:
                candidate[parameter.name] = parameter.value

        active_bundle = str(candidate["active_bundle"]).strip()
        expected = expected_contract_for_bundle(active_bundle)
        supplied = (
            str(candidate["procedure_type"]).strip().casefold(),
            bool(candidate["require_retraction_service"]),
            bool(candidate["require_bed_robot_arm_status"]),
        )
        if not active_bundle:
            return SetParametersResult(
                successful=False,
                reason="active_bundle must be set before readiness can be evaluated",
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
            self._procedure_type,
            self._require_retraction_service,
            self._require_bed_robot_arm_status,
            self._contract_transitioning,
        )
        next_identity = (
            active_bundle,
            supplied[0],
            supplied[1],
            supplied[2],
            bool(candidate["contract_transitioning"]),
        )
        self._active_bundle = active_bundle
        self._procedure_type = supplied[0]
        self._require_retraction_service = supplied[1]
        self._require_bed_robot_arm_status = supplied[2]
        self._contract_transitioning = next_identity[4]
        if next_identity != previous_identity:
            self._invalidate_bed_robot_status()
        return SetParametersResult(successful=True)

    def _invalidate_bed_robot_status(self) -> None:
        self._bed_robot_status_valid = False
        self._bed_robot_status_received_monotonic = 0.0
        self._bed_robot_status_source_stamp_sec = 0.0
        self._bed_robot_status_revision = None

    def _contract_configuration_valid(self) -> bool:
        expected = expected_contract_for_bundle(self._active_bundle)
        supplied = (
            self._procedure_type,
            self._require_retraction_service,
            self._require_bed_robot_arm_status,
        )
        return bool(
            self._active_bundle
            and not self._contract_transitioning
            and supplied == expected
        )

    def _on_rfdetr_health(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("schema") != "taskplanner.rfdetr_health.v1":
            return
        self._latest_rfdetr_health = payload
        self._latest_rfdetr_monotonic = time.monotonic()

    def _on_cv_contract_status(self, msg: String) -> None:
        try:
            payload = json.loads(msg.data)
        except (TypeError, ValueError):
            return
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "taskplanner.cv_external_contract.v1"
        ):
            return
        self._latest_cv_contract_status = payload
        self._latest_cv_contract_monotonic = time.monotonic()

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
        rfdetr_age_sec = (
            time.monotonic() - self._latest_rfdetr_monotonic
            if self._latest_rfdetr_monotonic > 0.0
            else -1.0
        )
        cv_contract_age_sec = (
            time.monotonic() - self._latest_cv_contract_monotonic
            if self._latest_cv_contract_monotonic > 0.0
            else -1.0
        )
        reception_age_sec = (
            time.monotonic() - self._bed_robot_status_received_monotonic
            if self._bed_robot_status_received_monotonic > 0.0
            else -1.0
        )
        source_age_sec = (
            time.time() - self._bed_robot_status_source_stamp_sec
            if self._bed_robot_status_source_stamp_sec > 0.0
            else -1.0
        )
        bed_robot_status_age_sec = (
            max(reception_age_sec, source_age_sec)
            if reception_age_sec >= 0.0 and source_age_sec >= 0.0
            else -1.0
        )
        snapshot = evaluate_readiness(
            sentence_publisher_count=self.count_publishers(self._sentence_topic),
            require_sentence_publisher=self._require_sentence_publisher,
            tool_handover_server_ready=self._tool_handover_client.server_is_ready(),
            retraction_service_ready=self._retraction_client.service_is_ready(),
            require_retraction_service=self._require_retraction_service,
            bed_robot_arm_status_valid=self._bed_robot_status_valid,
            bed_robot_arm_status_age_sec=bed_robot_status_age_sec,
            bed_robot_arm_status_max_age_sec=(
                self._bed_robot_arm_status_max_age_sec
            ),
            require_bed_robot_arm_status=self._require_bed_robot_arm_status,
            require_perception=self._require_perception,
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
        )
        snapshot["details"].update(
            {
                "active_bundle": self._active_bundle,
                "procedure_type": self._procedure_type,
                "contract_transitioning": self._contract_transitioning,
            }
        )
        snapshot["stamp_sec"] = round(
            self.get_clock().now().nanoseconds / 1_000_000_000.0,
            6,
        )
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
        if response.success:
            response.message = "integration ready"
        else:
            response.message = (
                "integration not ready: "
                + ", ".join(str(item) for item in snapshot["missing"])
            )
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
