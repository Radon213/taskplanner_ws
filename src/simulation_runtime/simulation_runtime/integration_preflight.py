"""Fail-closed readiness gate for the external Taskplanner runtime."""

from __future__ import annotations

import json
import time
from typing import Any

import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger
from surgical_interop_msgs.action import (
    ExecuteRetraction,
    ExecuteToolHandover,
)
from surgical_interop_msgs.srv import SetSuction


def evaluate_readiness(
    *,
    sentence_publisher_count: int,
    require_sentence_publisher: bool,
    tool_handover_server_ready: bool,
    suction_service_ready: bool,
    retraction_server_ready: bool,
    require_perception: bool,
    rfdetr_health: dict[str, Any] | None,
    rfdetr_age_sec: float,
    perception_max_age_sec: float,
) -> dict[str, Any]:
    checks = {
        "surgeon_sentence_publisher": (
            not require_sentence_publisher or sentence_publisher_count > 0
        ),
        "tool_handover_action_server": bool(tool_handover_server_ready),
        "suction_control_service": bool(suction_service_ready),
        "retraction_action_server": bool(retraction_server_ready),
        "perception_input": True,
    }
    details: dict[str, Any] = {
        "sentence_publisher_count": max(0, int(sentence_publisher_count)),
        "rfdetr_age_sec": (
            round(float(rfdetr_age_sec), 3) if rfdetr_age_sec >= 0.0 else -1.0
        ),
    }

    if require_perception:
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
        self.declare_parameter("perception_max_age_sec", 3.0)
        self.declare_parameter(
            "tool_handover_action_name",
            "/surgery/tool_handover",
        )
        self.declare_parameter(
            "suction_service_name",
            "/surgery/suction/set",
        )
        self.declare_parameter(
            "retraction_action_name",
            "/surgery/retraction",
        )

        self._sentence_topic = str(self.get_parameter("sentence_topic").value)
        self._require_sentence_publisher = bool(
            self.get_parameter("require_sentence_publisher").value
        )
        self._require_perception = bool(
            self.get_parameter("require_perception").value
        )
        self._perception_max_age_sec = max(
            0.1,
            float(self.get_parameter("perception_max_age_sec").value),
        )
        self._latest_rfdetr_health: dict[str, Any] | None = None
        self._latest_rfdetr_monotonic = 0.0

        self._tool_handover_client = ActionClient(
            self,
            ExecuteToolHandover,
            str(self.get_parameter("tool_handover_action_name").value),
        )
        self._suction_client = self.create_client(
            SetSuction,
            str(self.get_parameter("suction_service_name").value),
        )
        self._retraction_client = ActionClient(
            self,
            ExecuteRetraction,
            str(self.get_parameter("retraction_action_name").value),
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
        self.create_service(
            Trigger,
            str(self.get_parameter("readiness_service").value),
            self._handle_readiness,
        )
        self.create_timer(1.0, self._publish_readiness)

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

    def _snapshot(self) -> dict[str, Any]:
        rfdetr_age_sec = (
            time.monotonic() - self._latest_rfdetr_monotonic
            if self._latest_rfdetr_monotonic > 0.0
            else -1.0
        )
        snapshot = evaluate_readiness(
            sentence_publisher_count=self.count_publishers(self._sentence_topic),
            require_sentence_publisher=self._require_sentence_publisher,
            tool_handover_server_ready=self._tool_handover_client.server_is_ready(),
            suction_service_ready=self._suction_client.service_is_ready(),
            retraction_server_ready=self._retraction_client.server_is_ready(),
            require_perception=self._require_perception,
            rfdetr_health=self._latest_rfdetr_health,
            rfdetr_age_sec=rfdetr_age_sec,
            perception_max_age_sec=self._perception_max_age_sec,
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
