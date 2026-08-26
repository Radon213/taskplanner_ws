"""Pure integration-readiness snapshot evaluation.

This module deliberately has no ROS imports and owns no mutable runtime state.
Callers collect graph, service, Action, and evidence leases elsewhere and pass
their already-observed values into :func:`evaluate_readiness`.
"""

from __future__ import annotations

from typing import Any


_CV_CONTRACT_STATUS_SCHEMA = "taskplanner.cv_external_contract.v1"
_INTEGRATION_READINESS_SCHEMA = "taskplanner.integration_readiness.v1"
_READINESS_CHECK_IDS = (
    "contract_configuration",
    "surgeon_sentence_publisher",
    "tool_handover_action_server",
    "retraction_command_service",
    "asr_runtime_status",
    "perception_input",
    "rfdetr_cam3_tool_observations",
    "rfdetr_cam4_tool_observations",
)
_BED_ROBOT_ARM_STATUS_CHECK_ID = "bed_robot_arm_status"
_READINESS_CHECKLIST_MAX_ITEMS = len(_READINESS_CHECK_IDS) + 1
_READINESS_REASON_MAX_CHARS = 96


def _bounded_reason(value: object, *, fallback: str) -> str:
    """Return a compact machine-readable reason without external payload text."""

    raw = str(value or "").strip().lower()
    if not raw:
        return fallback
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_.:-"
    compact = "".join(character for character in raw if character in allowed)
    return (compact or fallback)[:_READINESS_REASON_MAX_CHARS]


def _checklist_status(*, required: bool, passed: bool, reason: str) -> str:
    if not required or passed:
        return "pass"
    # These conditions are expected to become available after a fresh health
    # heartbeat or server discovery. All malformed/mismatched evidence stays
    # a fail, rather than looking like a harmless spinner in the operation UI.
    pending_markers = (
        "_missing",
        "_not_ready",
        "_not_observed",
        "_stale",
        "_pending",
    )
    return "pending" if reason.endswith(pending_markers) else "fail"


def _checklist_detail(check_id: str, *, status: str, reason: str) -> str:
    """Return a bounded human-readable explanation for the operation view."""

    if status == "pass":
        return (
            "Not required for the active procedure"
            if reason == "not_required"
            else "Ready"
        )
    known = {
        "contract_transitioning": "Procedure contract update is in progress",
        "contract_configuration_invalid": "Active procedure contract is invalid",
        "surgeon_sentence_publisher_not_observed": (
            "Waiting for the admitted ASR source"
        ),
        "tool_handover_action_server_not_ready": (
            "Waiting for the tool-handover Action server"
        ),
        "retraction_command_service_not_ready": "Waiting for the retraction Service",
        "bed_robot_arm_status_missing": "Waiting for bed-robot arm status",
        "bed_robot_arm_status_invalid": "Bed-robot arm status is invalid",
        "bed_robot_arm_status_stale": "Bed-robot arm status is stale",
        "asr_runtime_status_not_ready": "Waiting for operational ASR status",
        "rfdetr_health_missing": "Waiting for RF-DETR health",
        "rfdetr_cam3_tool_observations_missing": (
            "Waiting for a fresh typed CAM3 RF-DETR frame"
        ),
        "rfdetr_cam4_tool_observations_missing": (
            "Waiting for a fresh typed CAM4 RF-DETR frame"
        ),
        "rfdetr_cam3_tool_observations_provenance_invalid": (
            "CAM3 RF-DETR provenance is invalid"
        ),
        "rfdetr_cam4_tool_observations_provenance_invalid": (
            "CAM4 RF-DETR provenance is invalid"
        ),
        "rfdetr_cam3_tool_observations_model_version_mismatch": (
            "CAM3 RF-DETR model version does not match the configured exact pin"
        ),
        "rfdetr_cam4_tool_observations_model_version_mismatch": (
            "CAM4 RF-DETR model version does not match the configured exact pin"
        ),
        "typed_rfdetr_tool_observations_not_ready": (
            "Waiting for both typed CAM3/CAM4 RF-DETR frames"
        ),
        "rfdetr_cam3_tool_observations_view_mismatch": (
            "CAM3 RF-DETR frame declares a different view"
        ),
        "rfdetr_cam4_tool_observations_view_mismatch": (
            "CAM4 RF-DETR frame declares a different view"
        ),
        "cv_contract_status_missing": "Waiting for external CV contract status",
        "perception_not_ready": "Perception input is not ready",
        "perception_backend_invalid": "Perception backend is disabled or invalid",
    }
    if reason in known:
        return known[reason]
    if reason.endswith("_stale"):
        return "Latest observation is stale"
    if reason.endswith("_missing"):
        return "Waiting for a required observation"
    if reason.endswith("_not_monotonic"):
        return "Latest observation was replayed or out of order"
    if reason.endswith("_unavailable"):
        return "Required runtime is unavailable"
    label = check_id.replace("_", " ")
    return f"{label.capitalize()} check failed"


def build_readiness_checklist(
    *,
    checks: dict[str, bool],
    required: dict[str, bool],
    reasons: dict[str, str],
) -> list[dict[str, object]]:
    """Build the fixed-size, read-only UI/API readiness checklist.

    The list is intentionally fixed and ordered. Consumers must never infer
    permission from a missing item; a disabled dependency is represented as a
    passing ``not_required`` item instead.
    """

    checklist: list[dict[str, object]] = []
    check_ids = list(_READINESS_CHECK_IDS)
    if _BED_ROBOT_ARM_STATUS_CHECK_ID in checks:
        check_ids.insert(4, _BED_ROBOT_ARM_STATUS_CHECK_ID)
    for check_id in check_ids:
        is_required = bool(required.get(check_id, True))
        passed = bool(checks.get(check_id, False))
        reason = _bounded_reason(
            reasons.get(check_id),
            fallback=("not_required" if not is_required else "check_unavailable"),
        )
        if not is_required:
            reason = "not_required"
        elif passed:
            reason = "ready"
        status = _checklist_status(
            required=is_required,
            passed=passed,
            reason=reason,
        )
        checklist.append(
            {
                "id": check_id,
                "required": is_required,
                "status": status,
                "reason": reason,
                "detail": _checklist_detail(
                    check_id,
                    status=status,
                    reason=reason,
                )[:160],
            }
        )
    return checklist[:_READINESS_CHECKLIST_MAX_ITEMS]


def readiness_service_message(snapshot: dict[str, Any]) -> str:
    """Make the legacy Trigger response useful without turning it into JSON.

    Full structured data belongs on the read-only readiness topic. This
    compact service summary is deliberately bounded for command-line callers
    and startup logs.
    """

    if bool(snapshot.get("ready")):
        return "integration ready"
    missing = [
        _bounded_reason(item, fallback="check_unavailable")
        for item in list(snapshot.get("missing", []))[:_READINESS_CHECKLIST_MAX_ITEMS]
    ]
    blockers: list[str] = []
    checklist = snapshot.get("checklist", [])
    if isinstance(checklist, list):
        for item in checklist:
            if not isinstance(item, dict) or item.get("status") == "pass":
                continue
            check_id = _bounded_reason(item.get("id"), fallback="check")
            status = _bounded_reason(item.get("status"), fallback="fail")
            reason = _bounded_reason(item.get("reason"), fallback="check_unavailable")
            blockers.append(f"{check_id}={status}:{reason}")
            if len(blockers) >= 4:
                break
    message = "integration not ready: " + ", ".join(missing)
    if blockers:
        message += "; " + "; ".join(blockers)
    return message[:512]


def evaluate_readiness(
    *,
    sentence_publisher_count: int,
    require_sentence_publisher: bool,
    tool_handover_server_ready: bool,
    require_tool_handover_action_server: bool,
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
    robot_endpoint_source: str = "external",
    retraction_endpoint_source: str = "external",
    retraction_state_machine_suppressed: bool = False,
    controller_contract_valid: bool = True,
    require_controller_contract: bool = False,
    controller_contract_age_sec: float = -1.0,
    asr_runtime_status_valid: bool = True,
    require_asr_runtime_status: bool = False,
    asr_runtime_status_age_sec: float = -1.0,
    rfdetr_source_valid: bool = True,
    cv_contract_source_valid: bool = True,
    require_rfdetr_tool_observations: bool = False,
    rfdetr_cam3_tool_observations_valid: bool = True,
    rfdetr_cam3_tool_observations_age_sec: float = -1.0,
    rfdetr_cam3_tool_observations_reason: str = "",
    rfdetr_cam4_tool_observations_valid: bool = True,
    rfdetr_cam4_tool_observations_age_sec: float = -1.0,
    rfdetr_cam4_tool_observations_reason: str = "",
    contract_transitioning: bool = False,
    controller_contract_reasons: tuple[str, ...] = (),
    asr_runtime_status_reason: str = "",
    rfdetr_source_reason: str = "",
    cv_contract_source_reason: str = "",
) -> dict[str, Any]:
    """Compute one fail-closed readiness snapshot from observed input facts."""

    checks = {
        "contract_configuration": bool(contract_configuration_valid),
        "surgeon_sentence_publisher": (
            not require_sentence_publisher or sentence_publisher_count > 0
        ),
        "tool_handover_action_server": (
            not require_tool_handover_action_server
            or bool(tool_handover_server_ready)
        ),
        "retraction_command_service": (
            not require_retraction_service or bool(retraction_service_ready)
        ),
    }
    if require_bed_robot_arm_status:
        checks[_BED_ROBOT_ARM_STATUS_CHECK_ID] = bool(
            bed_robot_arm_status_valid
            and 0.0 <= float(bed_robot_arm_status_age_sec)
            <= float(bed_robot_arm_status_max_age_sec)
        )
    checks.update(
        {
            "asr_runtime_status": (
                not require_asr_runtime_status or bool(asr_runtime_status_valid)
            ),
            "perception_input": True,
            "rfdetr_cam3_tool_observations": (
                not require_rfdetr_tool_observations
                or bool(rfdetr_cam3_tool_observations_valid)
            ),
            "rfdetr_cam4_tool_observations": (
                not require_rfdetr_tool_observations
                or bool(rfdetr_cam4_tool_observations_valid)
            ),
        }
    )
    details: dict[str, Any] = {
        "sentence_publisher_count": max(0, int(sentence_publisher_count)),
        "rfdetr_age_sec": (
            round(float(rfdetr_age_sec), 3) if rfdetr_age_sec >= 0.0 else -1.0
        ),
        "robot_endpoint_source": str(robot_endpoint_source).strip().casefold(),
        "retraction_endpoint_source": str(retraction_endpoint_source)
        .strip()
        .casefold(),
        "retraction_state_machine_suppressed": bool(
            retraction_state_machine_suppressed
        ),
        "asr_runtime_status_age_sec": (
            round(float(asr_runtime_status_age_sec), 3)
            if asr_runtime_status_age_sec >= 0.0
            else -1.0
        ),
        "rfdetr_cam3_tool_observations_age_sec": (
            round(float(rfdetr_cam3_tool_observations_age_sec), 3)
            if rfdetr_cam3_tool_observations_age_sec >= 0.0
            else -1.0
        ),
        "rfdetr_cam4_tool_observations_age_sec": (
            round(float(rfdetr_cam4_tool_observations_age_sec), 3)
            if rfdetr_cam4_tool_observations_age_sec >= 0.0
            else -1.0
        ),
    }
    if require_bed_robot_arm_status:
        details["bed_robot_arm_status_age_sec"] = (
            round(float(bed_robot_arm_status_age_sec), 3)
            if bed_robot_arm_status_age_sec >= 0.0
            else -1.0
        )

    normalized_backend = str(perception_backend).strip().casefold()
    if normalized_backend not in {"local", "external", "disabled"}:
        normalized_backend = "invalid"
    details["perception_backend"] = normalized_backend
    typed_tool_location_ready = bool(
        require_rfdetr_tool_observations
        and rfdetr_cam3_tool_observations_valid
        and rfdetr_cam4_tool_observations_valid
    )
    details["typed_rfdetr_tool_location_ready"] = typed_tool_location_ready

    if require_perception and normalized_backend == "local":
        health = rfdetr_health if isinstance(rfdetr_health, dict) else {}
        is_rfdetr_v2 = health.get("schema") == "pnu.rfdetr_health.v2"
        bridge_perception_ready = (
            bool(rfdetr_source_valid)
            and (
                (
                    bool(health.get("model_ready"))
                    and str(health.get("state", "")).strip().lower()
                    == "ready"
                    and bool(health.get("cam4_rgb_ready"))
                )
                if is_rfdetr_v2
                else (
                    bool(health.get("connected"))
                    and str(health.get("status", "")).strip().lower()
                    == "ready"
                    and bool(health.get("cam4_aligned"))
                )
            )
            and 0.0 <= float(rfdetr_age_sec) <= float(perception_max_age_sec)
        )
        # A scenario can consume typed CAM3/CAM4 ToolObservation2DArray
        # records directly in the VLM request. Its location admission must not
        # depend on an unrelated FLIR overlay/bridge frame becoming ready. The
        # two fresh provenance-checked typed views are the authoritative
        # perception contract whenever that runtime requirement is selected;
        # other procedures retain the bridge-health gate.
        perception_ready = bool(
            typed_tool_location_ready
            or bridge_perception_ready
        )
        checks["perception_input"] = perception_ready
        details["rfdetr_health_schema"] = str(health.get("schema", ""))
        details["rfdetr_status"] = str(
            health.get("state" if is_rfdetr_v2 else "status", "missing")
        )
        details["cam4_aligned"] = bool(
            health.get("cam4_rgb_ready" if is_rfdetr_v2 else "cam4_aligned")
        )
        details["perception_evidence_source"] = (
            "typed_rfdetr_tool_observations"
            if typed_tool_location_ready
            else "rfdetr_bridge_health"
        )
    elif require_perception and normalized_backend == "external":
        health = rfdetr_health if isinstance(rfdetr_health, dict) else {}
        contract = (
            cv_contract_status if isinstance(cv_contract_status, dict) else {}
        )
        # The PNU bridge deliberately reuses the existing Taskplanner health
        # topic/schema. Require its provider identity and semantic readiness,
        # so a reachable worker or a successful Blood/Hand-only request cannot
        # accidentally authorize planner-facing Tool evidence. A valid empty
        # Tool result remains ready: detection_count is intentionally not a
        # gate.
        is_pnu_health = health.get("provider") == "pnu_hand_blood"
        if require_rfdetr_tool_observations:
            # Production has no local/HTTP perception process. The two
            # provenance-checked DDS frames from the external RF-DETR host are
            # the complete provider lease, so unrelated PNU/CV health cannot
            # stand in for a missing camera view.
            perception_ready = typed_tool_location_ready
            details["perception_evidence_source"] = (
                "typed_rfdetr_tool_observations"
            )
        elif is_pnu_health:
            metric_3d_ready = bool(health.get("metric_3d_ready"))
            perception_ready = (
                bool(rfdetr_source_valid)
                and bool(health.get("connected"))
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
            # custom-IDL adapter. Topic-name presence alone never passes it.
            perception_ready = (
                bool(cv_contract_source_valid)
                and contract.get("schema") == _CV_CONTRACT_STATUS_SCHEMA
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
    requirements = {
        "contract_configuration": True,
        "surgeon_sentence_publisher": bool(require_sentence_publisher),
        "tool_handover_action_server": bool(
            require_tool_handover_action_server
        ),
        "retraction_command_service": bool(require_retraction_service),
        "asr_runtime_status": bool(require_asr_runtime_status),
        "perception_input": bool(require_perception),
        "rfdetr_cam3_tool_observations": bool(
            require_rfdetr_tool_observations
        ),
        "rfdetr_cam4_tool_observations": bool(
            require_rfdetr_tool_observations
        ),
    }
    if require_bed_robot_arm_status:
        requirements[_BED_ROBOT_ARM_STATUS_CHECK_ID] = True
    bed_robot_arm_status_reason = "bed_robot_arm_status_ready"
    if require_bed_robot_arm_status and not checks[_BED_ROBOT_ARM_STATUS_CHECK_ID]:
        if float(bed_robot_arm_status_age_sec) < 0.0:
            bed_robot_arm_status_reason = "bed_robot_arm_status_missing"
        elif not bed_robot_arm_status_valid:
            bed_robot_arm_status_reason = "bed_robot_arm_status_invalid"
        else:
            bed_robot_arm_status_reason = "bed_robot_arm_status_stale"
    perception_reason = "perception_ready"
    if not checks["perception_input"]:
        if normalized_backend in {"disabled", "invalid"}:
            perception_reason = "perception_backend_invalid"
        elif (
            normalized_backend == "external"
            and require_rfdetr_tool_observations
        ):
            # The typed external route is admitted exclusively by its two
            # view leases. Do not surface a missing legacy aggregate health
            # topic as the cause when one of those view leases is invalid.
            perception_reason = "typed_rfdetr_tool_observations_not_ready"
        elif rfdetr_source_reason:
            perception_reason = rfdetr_source_reason
        elif cv_contract_source_reason:
            perception_reason = cv_contract_source_reason
        elif float(rfdetr_age_sec) < 0.0 and normalized_backend == "local":
            perception_reason = "rfdetr_health_missing"
        elif float(cv_contract_age_sec) < 0.0 and normalized_backend == "external":
            perception_reason = "cv_contract_status_missing"
        else:
            perception_reason = "perception_not_ready"
    reasons = {
        "contract_configuration": (
            "contract_transitioning"
            if contract_transitioning
            else "contract_configuration_invalid"
        ),
        "surgeon_sentence_publisher": "surgeon_sentence_publisher_not_observed",
        "tool_handover_action_server": "tool_handover_action_server_not_ready",
        "retraction_command_service": "retraction_command_service_not_ready",
        "asr_runtime_status": (
            asr_runtime_status_reason or "asr_runtime_status_not_ready"
        ),
        "perception_input": perception_reason,
        "rfdetr_cam3_tool_observations": (
            rfdetr_cam3_tool_observations_reason
            or "rfdetr_cam3_tool_observations_missing"
        ),
        "rfdetr_cam4_tool_observations": (
            rfdetr_cam4_tool_observations_reason
            or "rfdetr_cam4_tool_observations_missing"
        ),
    }
    if require_bed_robot_arm_status:
        reasons[_BED_ROBOT_ARM_STATUS_CHECK_ID] = bed_robot_arm_status_reason
    checklist = build_readiness_checklist(
        checks=checks,
        required=requirements,
        reasons=reasons,
    )
    return {
        "schema": _INTEGRATION_READINESS_SCHEMA,
        "ready": not missing,
        "checks": checks,
        "missing": missing,
        "checklist": checklist,
        "details": details,
    }
