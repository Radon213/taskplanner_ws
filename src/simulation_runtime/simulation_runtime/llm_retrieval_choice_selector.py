"""LLM-based retrieval choice selector.

This node is not a simulated surgeon actor.
It is only used when the surgeon hand is full and an active requested tool
cannot be handed over. It chooses which already-held tool should be parked
on the Mayo stand to free hand capacity.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests
import rclpy
from rclpy.node import Node
from surgical_msgs.msg import SurgeonActorEvent, WorldState


ACTIVE_REQUEST_INTENTS = {"request_tool", "voice_request", "extend_hand_for_handover"}


class LLMRetrievalChoiceSelector(Node):
    def __init__(self) -> None:
        super().__init__("llm_retrieval_choice_selector")

        self.declare_parameter("base_url", "http://127.0.0.1:1234")
        self.declare_parameter("model_id", "qwen2.5-vl-7b-instruct")
        self.declare_parameter("response_format", "json_object")
        self.declare_parameter("request_timeout_sec", 20.0)
        self.declare_parameter("cooldown_sec", 8.0)
        self.declare_parameter("enabled", True)

        self._base_url = str(self.get_parameter("base_url").value).rstrip("/")
        self._model_id = str(self.get_parameter("model_id").value)
        self._response_format = str(self.get_parameter("response_format").value)
        self._timeout_sec = max(1.0, float(self.get_parameter("request_timeout_sec").value))
        self._cooldown_sec = max(1.0, float(self.get_parameter("cooldown_sec").value))
        self._enabled = bool(self.get_parameter("enabled").value)

        self._last_signature = ""
        self._last_decision_sec = 0.0

        self._event_pub = self.create_publisher(SurgeonActorEvent, "/surgeon/actor_event", 20)
        self.create_subscription(WorldState, "/twin/world_state", self._on_world, 20)

        self.get_logger().info(
            f"LLM retrieval choice selector started. model={self._model_id} base_url={self._base_url}"
        )

    def _on_world(self, msg: WorldState) -> None:
        if not self._enabled:
            return

        active_request_tool = msg.surgeon_request_tool or msg.explicit_request_tool
        if not active_request_tool:
            return

        if msg.surgeon_intent not in ACTIVE_REQUEST_INTENTS:
            return

        if msg.handover_allowed:
            return

        if msg.phase_uncertain:
            return

        if msg.robot_state != "idle":
            return

        if msg.active_robot_task_id:
            return

        if msg.cleaner_busy or msg.left_hand_tool:
            return

        candidates = []
        requested_state = None

        for inst in msg.instrument_states:
            if inst.instrument_id == active_request_tool:
                requested_state = inst

            if inst.instrument_id == active_request_tool:
                continue

            if (
                inst.lifecycle_stage == "surgeon_owned"
                and (inst.location_type == "surgeon_hand" or inst.status == "handed_over")
                and inst.owner == "surgeon"
            ):
                candidates.append(inst)

        if requested_state is None:
            return

        if requested_state.contaminated:
            return

        if requested_state.lifecycle_stage not in {"home_rack", "returned_home", "prepositioned_right"}:
            return

        if len(candidates) < 2:
            return

        signature = "|".join(
            [
                msg.procedure_id,
                msg.filtered_phase,
                active_request_tool,
                ",".join(sorted(inst.instrument_id for inst in candidates)),
            ]
        )

        now = time.monotonic()
        if signature == self._last_signature and now - self._last_decision_sec < self._cooldown_sec:
            return

        self._last_signature = signature
        self._last_decision_sec = now

        context = self._build_context(msg, active_request_tool, requested_state, candidates)
        selected_tool, reason = self._choose_tool_with_llm(context, candidates)

        if not selected_tool:
            return

        self._publish_place_on_mayo(selected_tool, active_request_tool, reason, context)

    def _build_context(self, msg: WorldState, active_request_tool: str, requested_state, candidates) -> dict[str, Any]:
        return {
            "task": "choose_one_currently_held_tool_to_temporarily_place_on_mayo_stand",
            "policy": {
                "do_not_choose_active_requested_tool": active_request_tool,
                "target_zone": "mayo_reuse_zone",
                "purpose": "free surgeon hand capacity before handing over the active requested tool",
                "do_not_send_to_cleaner": True,
            },
            "procedure_id": msg.procedure_id,
            "execution_state": msg.execution_state,
            "filtered_phase": msg.filtered_phase,
            "expected_instruments_current_phase": list(msg.expected_instruments),
            "active_request_tool": active_request_tool,
            "active_request_tool_state": {
                "instrument_id": requested_state.instrument_id,
                "lifecycle_stage": requested_state.lifecycle_stage,
                "location_type": requested_state.location_type,
                "status": requested_state.status,
                "contaminated": bool(requested_state.contaminated),
                "cleanliness_state": requested_state.cleanliness_state,
            },
            "surgeon_hand_candidates": [
                {
                    "instrument_id": inst.instrument_id,
                    "lifecycle_stage": inst.lifecycle_stage,
                    "location_type": inst.location_type,
                    "status": inst.status,
                    "contaminated": bool(inst.contaminated),
                    "cleanliness_state": inst.cleanliness_state,
                    "last_holder": inst.last_holder,
                    "next_required_transition": inst.next_required_transition,
                    "is_expected_in_current_phase": inst.instrument_id in msg.expected_instruments,
                }
                for inst in candidates
            ],
            "instruction": (
                "Return JSON only. Choose exactly one selected_tool from surgeon_hand_candidates. "
                "Choose the tool that is safest and most reasonable to place on the Mayo stand temporarily "
                "so the active requested tool can be handed over. Prefer tools that are less important "
                "for the current phase or likely finished. Do not choose the active requested tool."
            ),
            "required_json_schema": {
                "selected_tool": "tool id from candidates",
                "reason": "short reason",
            },
        }

    def _choose_tool_with_llm(self, context: dict[str, Any], candidates) -> tuple[str, str]:
        candidate_ids = {inst.instrument_id for inst in candidates}

        system_prompt = (
            "You are not acting as the surgeon. "
            "You are a surgical workflow assistant that only selects which already-held instrument "
            "should be temporarily placed on the Mayo stand to free hand capacity. "
            "You must output JSON only."
        )

        user_prompt = json.dumps(context, ensure_ascii=False, indent=2)

        payload: dict[str, Any] = {
            "model": self._model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 180,
        }

        if self._response_format in {"json_object", "json_schema"}:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = requests.post(
                f"{self._base_url}/v1/chat/completions",
                json=payload,
                timeout=self._timeout_sec,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json_content(content)
            selected_tool = str(parsed.get("selected_tool", "")).strip()
            reason = str(parsed.get("reason", "")).strip()

            if selected_tool in candidate_ids:
                self.get_logger().info(
                    f"LLM selected {selected_tool} for Mayo reuse before active request "
                    f"{context['active_request_tool']}: {reason}"
                )
                return selected_tool, reason or "llm_selected_for_mayo_reuse"

            self.get_logger().warn(
                f"LLM selected invalid tool '{selected_tool}'. Falling back to deterministic policy."
            )
        except Exception as exc:
            self.get_logger().warn(f"LLM retrieval choice failed: {exc}. Falling back to deterministic policy.")

        fallback = self._fallback_choice(context, candidates)
        return fallback

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        text = str(content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end >= start:
            text = text[start:end + 1]
        return json.loads(text)

    def _fallback_choice(self, context: dict[str, Any], candidates) -> tuple[str, str]:
        expected = set(context.get("expected_instruments_current_phase", []))

        def key(inst):
            # Prefer a tool not expected in the current phase.
            # Then prefer contaminated/used tool to free hand capacity.
            # If tied, choose lexicographically for deterministic behavior.
            expected_rank = 1 if inst.instrument_id in expected else 0
            contaminated_rank = 0 if inst.contaminated else 1
            return (expected_rank, contaminated_rank, inst.instrument_id)

        selected = sorted(candidates, key=key)[0]
        reason = "fallback_selected_less_expected_or_used_tool_for_mayo_reuse"
        self.get_logger().info(f"Fallback selected {selected.instrument_id}: {reason}")
        return selected.instrument_id, reason

    def _publish_place_on_mayo(
        self,
        selected_tool: str,
        active_request_tool: str,
        reason: str,
        context: dict[str, Any],
    ) -> None:
        msg = SurgeonActorEvent()
        msg.stamp = self.get_clock().now().to_msg()
        msg.event_type = "place_on_mayo_reuse"
        msg.tool_id = selected_tool
        msg.phase_id = str(context.get("filtered_phase", ""))
        msg.voice_text = (
            f"Place {selected_tool} on Mayo stand before handing over {active_request_tool}."
        )
        msg.note = json.dumps(
            {
                "source": "llm_retrieval_choice_selector",
                "active_request_tool": active_request_tool,
                "selected_tool": selected_tool,
                "target_zone": "mayo_reuse_zone",
                "reason": reason,
            },
            ensure_ascii=False,
        )
        msg.ready_for_handover = False
        msg.ready_for_retrieval = False
        msg.override = True

        self._event_pub.publish(msg)
        self.get_logger().info(
            f"Published place_on_mayo_reuse for {selected_tool}; active_request={active_request_tool}"
        )


def main() -> None:
    rclpy.init()
    node = LLMRetrievalChoiceSelector()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
