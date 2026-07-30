"""LLM-driven surgeon actor that is an upstream source of surgical cues.

Unlike the rule actor, this node does not subscribe to the digital twin or
simulation state. It advances from its own internal procedure state and only
uses skill completion messages to keep timing aligned with the humanoid.
"""

from __future__ import annotations

from collections import deque
import json
import os
import random
import re
import time
from typing import Any
import uuid

from procedure_spec import compact_procedure_prompt, get_default_spec_dir, load_bundle
import requests
import rclpy
from model_provider_registry import ModelProviderRegistry, force_disable_thinking
from rcl_interfaces.msg import SetParametersResult
from rclpy.node import Node
from rclpy.parameter import Parameter
from std_msgs.msg import String
from surgical_msgs.msg import (
    BedRobotArmGroupRequest,
    BedRobotArmGroupStatus,
    ModelCatalogEntry,
    ModelProviderStatus,
    SpeechUtterance,
    SkillStatus,
    SurgeonActorEvent,
    SurgeonLLMDecision,
    SurgeonState,
)
from surgical_msgs.srv import (
    ControlModelRuntime,
    ListModelCatalog,
    ListModels,
    SelectModelProvider,
)


HANDOVER_ACTIONS = {
    "direct_handover",
    "pick_up_and_handover",
    "pick_up_from_mayo_and_handover",
    "put_down_and_handover",
    "tool_handover",
    "predicted_tool_handover",
    "replace_and_handover",
}
RETRIEVE_ACTIONS = {"retrieve_from_mayo", "retrieve_from_hand", "tool_retrieve"}
RECOVERY_STARTED_STATES = {
    "dispatching",
    "accepted",
    "executing",
    "retrieving_from_mayo",
    "inserting_into_cleaner",
    "cleaning",
    "returning_to_rack",
    "completed",
}
RECOVERY_FAILURE_STATES = {
    "rejected",
    "dispatch_failed",
    "server_unavailable",
    "skipped_while_busy",
    "cancel_requested",
    "canceled",
    "aborted",
    "result_failed",
}


class LLMSurgeonActorNode(Node):
    def __init__(self) -> None:
        super().__init__("llm_surgeon_actor")
        self.declare_parameter("spec_dir", str(get_default_spec_dir()))
        self.declare_parameter("base_url", "http://127.0.0.1:1234")
        self.declare_parameter("provider_id", os.environ.get("ACTOR_PROVIDER_ID", "auto"))
        self.declare_parameter("api_key", "")
        self.declare_parameter("model_id", "google/gemma-4-12b-qat")
        self.declare_parameter("request_timeout_sec", 20.0)
        self.declare_parameter("decision_period_sec", 0.25)
        self.declare_parameter("temperature", 0.7)
        self.declare_parameter("top_p", 0.9)
        self.declare_parameter("max_output_tokens", 220)
        self.declare_parameter("response_format", "json_schema")
        self.declare_parameter("reasoning_effort", "none")
        self.declare_parameter("seed", 7)
        self.declare_parameter("min_decision_dwell_sec", 1.5)
        self.declare_parameter("max_decision_dwell_sec", 7.0)
        self.declare_parameter("post_mayo_public_cue_min_sec", 4.5)
        self.declare_parameter("post_mayo_public_cue_max_sec", 6.5)
        self.declare_parameter("post_handover_public_cue_min_sec", 5.0)
        self.declare_parameter("post_handover_public_cue_max_sec", 7.0)
        self.declare_parameter("request_mode_voice_weight", 0.35)
        self.declare_parameter("request_mode_hand_weight", 0.25)
        self.declare_parameter("request_mode_voice_hand_weight", 0.40)
        self.declare_parameter("require_voice_for_tool_requests", False)
        self.declare_parameter("small_talk_chance", 0.18)
        self.declare_parameter("same_request_mode_limit", 2)
        self.declare_parameter("autonomous_phase_progression_enabled", True)
        self.declare_parameter("phase_auto_advance_grace_sec", 10.0)
        self.declare_parameter("phase_auto_advance_max_sec", 24.0)
        self.declare_parameter("phase_auto_advance_tool_fraction", 0.6)
        self.declare_parameter("interrupt_events_enabled", True)
        self.declare_parameter("interrupt_event_phase_probability", 0.45)
        self.declare_parameter("interrupt_event_min_phase_elapsed_sec", 7.0)
        self.declare_parameter("interrupt_event_max_phase_elapsed_sec", 22.0)
        self.declare_parameter("interrupt_event_min_gap_sec", 35.0)
        self.declare_parameter("interrupt_event_min_active_sec", 5.0)
        self.declare_parameter("interrupt_event_tool_fraction", 0.5)
        self.declare_parameter("enabled", True)

        self._enabled = True
        self._active = False
        self._control_running = False
        self._max_held_tools = 2
        self._held_tools: list[str] = []
        self._mayo_tools: list[str] = []
        self._pending_action = ""
        self._pending_tool = ""
        self._current_phase_id = ""
        self._phase_entered_sec = 0.0
        self._phase_used_tools: set[str] = set()
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=14)
        self._last_speech = ""
        self._last_hand_overlay = ""
        self._last_decision_raw = ""
        self._next_decision_time = 0.0
        self._request_mode_history: deque[str] = deque(maxlen=4)
        self._scheduled_interrupt_phase_id = ""
        self._scheduled_interrupt_from_phase_id = ""
        self._scheduled_interrupt_at_sec = 0.0
        self._active_interrupt_phase_id = ""
        self._interrupt_return_phase_id = ""
        self._interrupt_entered_sec = 0.0
        self._interrupt_used_tools: set[str] = set()
        self._last_interrupt_completed_sec = -1_000_000_000.0
        self._field_event_lines: list[str] = []
        self._pending_group_requests: dict[str, dict[str, str]] = {}
        self._bed_group_states: dict[str, dict[str, Any]] = {}
        self._bed_group_status_stamp_ns: dict[str, int] = {}
        self._manual_override_mute_until_sec = 0.0

        self._provider_model_selections: dict[str, str] = {}
        initial_base_url = str(self.get_parameter("base_url").value)
        initial_api_key = str(
            self.get_parameter("api_key").value or os.environ.get("ACTOR_API_KEY", "")
        )
        self._provider_registry = ModelProviderRegistry.from_environment(
            legacy_base_url=initial_base_url,
            legacy_api_key=initial_api_key,
        )
        self._load_parameters()
        self._schedule_interrupt_for_phase(self._current_phase_id, "init")
        self.add_on_set_parameters_callback(self._on_parameters_changed)

        self._state_pub = self.create_publisher(SurgeonState, "/surgeon/state", 20)
        self._speech_pub = self.create_publisher(
            SpeechUtterance,
            "/sensors/speech/utterance",
            20,
        )
        self._actor_event_pub = self.create_publisher(SurgeonActorEvent, "/surgeon/actor_event", 20)
        self._overlay_pub = self.create_publisher(String, "/surgeon/actor_overlay", 20)
        self._decision_pub = self.create_publisher(SurgeonLLMDecision, "/surgeon/llm_decision", 20)
        self._model_catalog_service = self.create_service(ListModels, "~/list_models", self._on_list_models)
        self._provider_catalog_service = self.create_service(
            ListModelCatalog,
            "~/list_model_catalog",
            self._on_list_model_catalog,
        )
        self._provider_select_service = self.create_service(
            SelectModelProvider,
            "~/select_model_provider",
            self._on_select_model_provider,
        )
        self._provider_control_service = self.create_service(
            ControlModelRuntime,
            "~/control_model_runtime",
            self._on_control_model_runtime,
        )

        self.create_subscription(String, "/simulation/control_state", self._on_control, 20)
        self.create_subscription(SkillStatus, "/skill/status", self._on_skill_status, 50)
        self.create_subscription(
            BedRobotArmGroupStatus,
            "/bed_robot_arm_group/status",
            self._on_bed_robot_arm_group_status,
            50,
        )
        self.create_subscription(
            BedRobotArmGroupRequest,
            "/surgeon/bed_robot_arm_group_request",
            self._on_bed_robot_arm_group_request,
            20,
        )

        self._timer = self.create_timer(self._decision_period_sec, self._tick)
        self._publish_overlay()
        self._publish_state("idle", "", "", False, False, "actor initialized")

    def _load_parameters(self, overrides: dict[str, Any] | None = None) -> None:
        override_values = overrides or {}

        def param_value(name: str) -> Any:
            return override_values.get(name, self.get_parameter(name).value)

        self._spec_dir = str(param_value("spec_dir"))
        self._spec = load_bundle(self._spec_dir)
        configured_base_url = str(param_value("base_url")).rstrip("/")
        configured_api_key = str(
            param_value("api_key") or os.environ.get("ACTOR_API_KEY", "")
        )
        provider = self._provider_registry.resolve(
            str(param_value("provider_id")),
            fallback_base_url=configured_base_url,
            fallback_api_key=configured_api_key,
        )
        self._provider_id = provider.provider_id
        self._base_url = provider.base_url
        self._api_key = provider.api_key
        self._model_id = str(param_value("model_id"))
        self._provider_model_selections[self._provider_id] = self._model_id
        self._request_timeout_sec = float(param_value("request_timeout_sec"))
        self._decision_period_sec = float(param_value("decision_period_sec"))
        self._temperature = float(param_value("temperature"))
        self._top_p = float(param_value("top_p"))
        self._max_output_tokens = int(param_value("max_output_tokens"))
        self._response_format = str(param_value("response_format")).strip().lower()
        self._reasoning_effort = str(param_value("reasoning_effort")).strip()
        self._seed = int(param_value("seed"))
        self._min_dwell_sec = max(0.1, float(param_value("min_decision_dwell_sec")))
        self._max_dwell_sec = max(self._min_dwell_sec, float(param_value("max_decision_dwell_sec")))
        self._post_mayo_public_cue_min_sec = max(
            self._min_dwell_sec,
            float(param_value("post_mayo_public_cue_min_sec")),
        )
        self._post_mayo_public_cue_max_sec = max(
            self._post_mayo_public_cue_min_sec,
            float(param_value("post_mayo_public_cue_max_sec")),
        )
        self._post_handover_public_cue_min_sec = max(
            self._min_dwell_sec,
            float(param_value("post_handover_public_cue_min_sec")),
        )
        self._post_handover_public_cue_max_sec = max(
            self._post_handover_public_cue_min_sec,
            float(param_value("post_handover_public_cue_max_sec")),
        )
        self._request_mode_weights = {
            "voice": max(0.0, float(param_value("request_mode_voice_weight"))),
            "hand": max(0.0, float(param_value("request_mode_hand_weight"))),
            "voice_hand": max(0.0, float(param_value("request_mode_voice_hand_weight"))),
        }
        self._require_voice_for_tool_requests = bool(
            param_value("require_voice_for_tool_requests")
        )
        self._small_talk_chance = max(0.0, min(1.0, float(param_value("small_talk_chance"))))
        self._same_request_mode_limit = max(1, int(param_value("same_request_mode_limit")))
        self._autonomous_phase_progression_enabled = bool(param_value("autonomous_phase_progression_enabled"))
        self._phase_auto_advance_grace_sec = max(
            0.0,
            float(param_value("phase_auto_advance_grace_sec")),
        )
        self._phase_auto_advance_max_sec = max(
            1.0,
            float(param_value("phase_auto_advance_max_sec")),
        )
        self._phase_auto_advance_tool_fraction = max(
            0.0,
            min(1.0, float(param_value("phase_auto_advance_tool_fraction"))),
        )
        self._interrupt_events_enabled = bool(param_value("interrupt_events_enabled"))
        self._interrupt_event_phase_probability = max(
            0.0,
            min(1.0, float(param_value("interrupt_event_phase_probability"))),
        )
        self._interrupt_event_min_phase_elapsed_sec = max(
            0.0,
            float(param_value("interrupt_event_min_phase_elapsed_sec")),
        )
        self._interrupt_event_max_phase_elapsed_sec = max(
            self._interrupt_event_min_phase_elapsed_sec,
            float(param_value("interrupt_event_max_phase_elapsed_sec")),
        )
        self._interrupt_event_min_gap_sec = max(
            0.0,
            float(param_value("interrupt_event_min_gap_sec")),
        )
        self._interrupt_event_min_active_sec = max(
            0.0,
            float(param_value("interrupt_event_min_active_sec")),
        )
        self._interrupt_event_tool_fraction = max(
            0.0,
            min(1.0, float(param_value("interrupt_event_tool_fraction"))),
        )
        self._enabled = bool(param_value("enabled"))
        self._rng = random.Random(self._seed)
        self._current_phase_id = self._spec.default_phase_id
        self._tool_ids = self._spec.list_instrument_ids()
        self._requestable_tool_ids = [
            instrument.id
            for instrument in self._spec.bundle.instruments
            if bool(getattr(instrument, "requestable", True))
        ]
        self._phase_ids = list(self._spec.phase_ids)
        self._procedure_prompt = compact_procedure_prompt(self._spec_dir)
        self._reset_bed_robot_arm_group_states()
        self._normal_phase_ids = list(getattr(self._spec, "normal_phase_ids", []))
        self._interrupt_phase_ids = set(getattr(self._spec, "interrupt_phase_ids", []))
        if isinstance(self._procedure_prompt, dict):
            phase_labels = self._procedure_prompt.get("phase_labels", {})
            if isinstance(phase_labels, dict):
                if not self._normal_phase_ids:
                    normal_labels = phase_labels.get("normal", {})
                    if isinstance(normal_labels, dict):
                        self._normal_phase_ids = [
                            str(phase_id) for phase_id in normal_labels if str(phase_id) in self._phase_ids
                        ]
                if not self._interrupt_phase_ids:
                    interrupt_labels = phase_labels.get("interrupt", {})
                    if isinstance(interrupt_labels, dict):
                        self._interrupt_phase_ids = set(str(phase_id) for phase_id in interrupt_labels)
        if not self._normal_phase_ids:
            self._normal_phase_ids = [
                phase_id for phase_id in self._phase_ids if phase_id not in self._interrupt_phase_ids
            ]
        else:
            self._normal_phase_ids = [
                phase_id
                for phase_id in self._normal_phase_ids
                if phase_id in self._phase_ids and phase_id not in self._interrupt_phase_ids
            ]
        self._tool_display_by_id = {instrument.id: instrument.display_name or instrument.id for instrument in self._spec.bundle.instruments}
        self._phase_display_by_id = {
            phase.id: phase.display_name or phase.id
            for phase in self._spec.bundle.phases
        }
        self._tool_aliases_by_id = {
            instrument.id: [instrument.id, instrument.display_name, *list(getattr(instrument, "aliases", []))]
            for instrument in self._spec.bundle.instruments
        }
        self._phase_tool_sequences = self._load_phase_tool_sequences()
        self._schema = self._decision_schema()
        self._system_prompt = self._build_system_prompt()
        self._phase_entered_sec = self._now()
        self._phase_used_tools = set()
        self._episode_style = self._rng.choice(
            [
                "concise requests with rare small talk",
                "calm teaching style with occasional check-ins",
                "efficient senior surgeon style",
                "slightly more verbal operating-room coordination",
            ]
        )
        self._clear_interrupt_state(clear_cooldown=True)

    def _on_parameters_changed(self, params):
        reload_required = False
        overrides = {parameter.name: parameter.value for parameter in params}
        spec_changed = "spec_dir" in overrides
        connection_names = {"base_url", "provider_id", "api_key", "model_id"}
        if connection_names.intersection(overrides):
            try:
                configured_base_url = str(
                    overrides.get("base_url", self.get_parameter("base_url").value)
                ).rstrip("/")
                configured_api_key = str(
                    overrides.get("api_key", self.get_parameter("api_key").value)
                    or os.environ.get("ACTOR_API_KEY", "")
                )
                provider = self._provider_registry.resolve(
                    str(
                        overrides.get(
                            "provider_id",
                            self.get_parameter("provider_id").value,
                        )
                    ),
                    fallback_base_url=configured_base_url,
                    fallback_api_key=configured_api_key,
                )
                self._provider_id = provider.provider_id
                self._base_url = provider.base_url
                self._api_key = provider.api_key
                self._model_id = str(overrides.get("model_id", self._model_id))
                self._provider_model_selections[self._provider_id] = self._model_id
            except Exception as exc:
                return SetParametersResult(successful=False, reason=str(exc))
        for parameter in params:
            if parameter.name in connection_names:
                continue
            if parameter.name in {
                "spec_dir",
                "request_timeout_sec",
                "temperature",
                "top_p",
                "max_output_tokens",
                "response_format",
                "reasoning_effort",
                "seed",
                "min_decision_dwell_sec",
                "max_decision_dwell_sec",
                "post_mayo_public_cue_min_sec",
                "post_mayo_public_cue_max_sec",
                "post_handover_public_cue_min_sec",
                "post_handover_public_cue_max_sec",
                "request_mode_voice_weight",
                "request_mode_hand_weight",
                "request_mode_voice_hand_weight",
                "require_voice_for_tool_requests",
                "small_talk_chance",
                "same_request_mode_limit",
                "autonomous_phase_progression_enabled",
                "phase_auto_advance_grace_sec",
                "phase_auto_advance_max_sec",
                "phase_auto_advance_tool_fraction",
                "interrupt_events_enabled",
                "interrupt_event_phase_probability",
                "interrupt_event_min_phase_elapsed_sec",
                "interrupt_event_max_phase_elapsed_sec",
                "interrupt_event_min_gap_sec",
                "interrupt_event_min_active_sec",
                "interrupt_event_tool_fraction",
            }:
                reload_required = True
            elif parameter.name == "enabled":
                self._enabled = bool(parameter.value)
                if not self._enabled:
                    self._active = False
                    self._pending_action = ""
                    self._pending_tool = ""
                    self._publish_overlay()
                    self._publish_state("disabled", "", "", False, False, "llm_surgeon_actor disabled")
                elif self._control_running:
                    self._active = True
                    self._schedule_next_decision(0.2)
            elif parameter.name == "decision_period_sec":
                self._decision_period_sec = float(parameter.value)
        if reload_required:
            try:
                self._load_parameters(overrides)
                if spec_changed:
                    self._reset_runtime()
                else:
                    self._schedule_interrupt_for_phase(self._current_phase_id, "parameter_reload")
            except Exception as exc:
                return SetParametersResult(successful=False, reason=str(exc))
        return SetParametersResult(successful=True)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    def _models_url(self) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/models"
        return f"{self._base_url}/v1/models"

    def _on_list_models(self, _request, response):
        try:
            api_response = requests.get(
                self._models_url(),
                headers=self._headers(),
                timeout=self._request_timeout_sec,
            )
            api_response.raise_for_status()
            payload = api_response.json()
            rows = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else payload
            model_ids: list[str] = []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, str):
                        model_id = row.strip()
                    elif isinstance(row, dict):
                        model_id = str(row.get("id") or row.get("model") or row.get("name") or "").strip()
                    else:
                        model_id = ""
                    if model_id and model_id not in model_ids:
                        model_ids.append(model_id)
            if self._model_id and self._model_id not in model_ids:
                model_ids.insert(0, self._model_id)
            response.success = True
            response.model_ids = model_ids
            response.message = f"{len(model_ids)} model(s) from {self._base_url}"
        except Exception as exc:
            response.success = False
            response.model_ids = []
            response.message = f"Model catalog unavailable at {self._base_url}: {exc}"
        return response

    def _on_list_model_catalog(self, _request, response):
        probes = self._provider_registry.probe_all()
        online_count = sum(1 for probe in probes if probe.reachable)
        model_count = 0
        response.active_provider_id = self._provider_id
        response.active_model_id = self._model_id
        response.providers = []
        response.models = []

        for probe in probes:
            provider_status = ModelProviderStatus()
            provider_status.provider_id = probe.provider.provider_id
            provider_status.provider_name = probe.provider.display_name
            provider_status.endpoint = probe.provider.base_url
            provider_status.reachable = probe.reachable
            provider_status.status = probe.status
            provider_status.detail = probe.detail
            provider_status.latency_sec = float(probe.latency_sec)
            provider_status.model_count = len(probe.models)
            response.providers.append(provider_status)

            for model in probe.models:
                entry = ModelCatalogEntry()
                entry.provider_id = model.provider_id
                entry.provider_name = model.provider_name
                entry.model_id = model.model_id
                entry.display_name = model.display_name
                entry.capability = model.capability
                entry.load_state = model.load_state
                entry.selectable = model.selectable
                entry.detail = model.detail
                entry.runtime_managed = model.runtime_managed
                entry.available_actions = list(model.available_actions)
                response.models.append(entry)
                model_count += 1

        advertised = {
            (str(entry.provider_id), str(entry.model_id))
            for entry in response.models
        }
        reachable_by_provider = {
            probe.provider.provider_id: probe.reachable for probe in probes
        }
        for remembered_provider_id, remembered_model_id in self._provider_model_selections.items():
            if not remembered_model_id or (
                remembered_provider_id,
                remembered_model_id,
            ) in advertised:
                continue
            provider = self._provider_registry.get_provider(remembered_provider_id)
            configured = ModelCatalogEntry()
            configured.provider_id = remembered_provider_id
            configured.provider_name = (
                provider.display_name
                if provider is not None
                else remembered_provider_id
            )
            configured.model_id = remembered_model_id
            configured.display_name = remembered_model_id
            configured.capability = "unknown"
            matching_entry = next(
                (
                    entry
                    for entry in response.models
                    if str(entry.provider_id) == remembered_provider_id
                    and self._provider_registry.canonical_model_id(
                        remembered_provider_id,
                        str(entry.model_id),
                    )
                    == self._provider_registry.canonical_model_id(
                        remembered_provider_id,
                        remembered_model_id,
                    )
                ),
                None,
            )
            override = self._provider_registry.runtime_state(
                remembered_provider_id,
                remembered_model_id,
            )
            if override is not None:
                configured.load_state, configured.detail = override
            elif matching_entry is not None:
                configured.load_state = str(matching_entry.load_state)
                configured.detail = str(matching_entry.detail)
            else:
                configured.load_state = "configured"
                configured.detail = "Configured model was not returned by /v1/models"
            configured.selectable = reachable_by_provider.get(
                remembered_provider_id,
                False,
            )
            configured.runtime_managed = bool(
                provider is not None and provider.runtime_commands
            )
            configured.available_actions = list(
                self._provider_registry.available_actions(
                    remembered_provider_id,
                    configured.load_state,
                )
            )
            response.models.insert(0, configured)
            model_count += 1

        response.success = online_count > 0
        response.message = (
            f"{online_count}/{len(probes)} providers online; {model_count} model(s)"
        )
        return response

    def _on_select_model_provider(self, request, response):
        provider_id = str(request.provider_id).strip().lower()
        model_id = str(request.model_id).strip()
        response.provider_id = self._provider_id
        response.model_id = self._model_id
        if not provider_id or not model_id:
            response.success = False
            response.message = "provider_id and model_id are required"
            return response
        provider = self._provider_registry.get_provider(provider_id)
        if provider is None:
            response.success = False
            response.message = f"Unknown model provider: {provider_id}"
            return response
        probe = self._provider_registry.probe(provider_id)
        if not probe.reachable:
            response.success = False
            response.message = (
                f"{provider.display_name} is unavailable: {probe.status} ({probe.detail})"
            )
            return response
        available_models = {model.model_id: model for model in probe.models}
        remembered_model = self._provider_model_selections.get(provider_id, "")
        catalog_model = available_models.get(model_id)
        if catalog_model is None:
            catalog_model = self._provider_registry.matching_model(
                provider_id,
                model_id,
                probe.models,
            )
        if catalog_model is None and model_id != remembered_model:
            response.success = False
            response.message = (
                f"{model_id} is not advertised by {provider.display_name}"
            )
            return response
        runtime_note = ""
        if provider.managed and catalog_model is not None:
            runtime = self._provider_registry.ensure_runtime_ready(
                provider_id,
                catalog_model,
                requested_model_id=model_id,
            )
            if not runtime.success:
                response.success = False
                response.message = runtime.message
                return response
            runtime_note = f"; runtime {runtime.state}"

        result = self.set_parameters_atomically(
            [
                Parameter("provider_id", value=provider_id),
                Parameter("model_id", value=model_id),
            ]
        )
        if not result.successful:
            response.success = False
            response.message = result.reason or "Provider selection was rejected"
            return response
        response.success = True
        response.provider_id = self._provider_id
        response.model_id = self._model_id
        response.message = (
            f"LLM surgeon provider set to {provider.display_name}; model set to "
            f"{model_id}{runtime_note}"
        )
        return response

    def _on_control_model_runtime(self, request, response):
        result = self._provider_registry.control_runtime(
            str(request.provider_id),
            str(request.model_id),
            str(request.command),
        )
        response.success = result.success
        response.provider_id = result.provider_id
        response.model_id = result.model_id
        response.state = result.state
        response.message = result.message
        return response

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _now(self) -> float:
        stamp = self.get_clock().now().to_msg()
        return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0

    def _on_control(self, msg: String) -> None:
        raw_command = msg.data.strip()
        command, _, start_phase_id = raw_command.partition(":")
        command = command.strip().lower()
        start_phase_id = start_phase_id.strip()

        if command == "mute_actor":
            try:
                mute_sec = max(0.0, float(start_phase_id or 8.0))
            except ValueError:
                mute_sec = 8.0
            self._manual_override_mute_until_sec = max(
                self._manual_override_mute_until_sec,
                self._now() + mute_sec,
            )
            self._record_event("manual_override_actor_muted", "", {"mute_sec": mute_sec})
            self._schedule_next_decision(mute_sec + 0.2)
            return
        if command in {"start", "start_actors", "resume"}:
            if command in {"start", "start_actors"} and start_phase_id:
                self._set_initial_phase(start_phase_id, "control_start_phase")
            elif command in {"start", "start_actors"}:
                self._phase_entered_sec = self._now()
                self._phase_used_tools = set()
                self._clear_interrupt_state(clear_cooldown=True)
                self._schedule_interrupt_for_phase(self._current_phase_id, "control_start")
            self._control_running = True
            self._active = bool(self._enabled)
            if self._active:
                self._schedule_next_decision(0.2)
            else:
                self._publish_state("disabled", "", "", False, False, "llm_surgeon_actor disabled")
        elif command in {"pause", "stop"}:
            self._control_running = False
            self._active = False
        elif command == "reset":
            self._control_running = False
            self._manual_override_mute_until_sec = 0.0
            self._reset_runtime(start_phase_id)

    def _reset_bed_robot_arm_group_states(self) -> None:
        bed_group_spec = self._spec.get_bed_robot_arm_group_spec()
        self._bed_group_states = {
            group.id: {
                "connected": bool(group.enabled),
                "state": "standby",
                "operation": "",
                "direction": "",
                "distance_mm": 0.0,
                "distance_origin": "",
                "end_effector_profile": group.initial_end_effector_profile,
                "progress": 0.0,
                "error_code": "",
                "rejection_reason": "",
            }
            for group in (bed_group_spec.groups if bed_group_spec is not None else [])
        }
        self._bed_group_status_stamp_ns = {}

    def _reset_runtime(self, start_phase_id: str = "") -> None:
        self._active = False
        self._control_running = False
        self._held_tools = []
        self._mayo_tools = []
        self._pending_action = ""
        self._pending_tool = ""
        self._pending_group_requests.clear()
        self._reset_bed_robot_arm_group_states()
        self._current_phase_id = start_phase_id if start_phase_id in self._phase_ids else self._spec.default_phase_id
        self._phase_entered_sec = self._now()
        self._phase_used_tools = set()
        self._recent_events.clear()
        self._last_speech = ""
        self._last_hand_overlay = ""
        self._clear_interrupt_state(clear_cooldown=True)
        self._request_mode_history.clear()
        self._next_decision_time = 0.0
        self._schedule_interrupt_for_phase(self._current_phase_id, "reset")
        self._publish_overlay()
        self._publish_state("idle", "", "", False, False, "actor reset")

    def _set_initial_phase(self, phase_id: str, reason: str) -> None:
        if phase_id not in self._phase_ids:
            return
        if phase_id == self._current_phase_id:
            self._phase_entered_sec = self._now()
            self._phase_used_tools = set()
            self._clear_interrupt_state(clear_cooldown=True)
            self._schedule_interrupt_for_phase(self._current_phase_id, "control_start_phase")
            return
        previous_phase = self._current_phase_id
        self._current_phase_id = phase_id
        self._phase_entered_sec = self._now()
        self._phase_used_tools = set()
        self._clear_interrupt_state(clear_cooldown=True)
        self._schedule_interrupt_for_phase(self._current_phase_id, "control_start_phase")
        self._record_event("initial_phase_selected", "", {"from": previous_phase, "to": phase_id, "reason": reason})
        self._publish_overlay()

    def _on_skill_status(self, msg: SkillStatus) -> None:
        action = msg.action.strip()
        tool_id = self._resolve_tool(msg.instrument_id)
        if not tool_id:
            return
        state = msg.state.strip()
        if action in RETRIEVE_ACTIONS:
            if state in RECOVERY_FAILURE_STATES:
                return
            if state in RECOVERY_STARTED_STATES and (state != "completed" or bool(msg.success)):
                if tool_id in self._mayo_tools:
                    self._remove_from_mayo(tool_id)
                    self._record_event(
                        "skill_retrieve_started",
                        tool_id,
                        {"action": action, "state": state},
                    )
                    self._publish_overlay()
            if state != "completed" or not bool(msg.success):
                return
        elif state != "completed" or not bool(msg.success):
            return
        if action in HANDOVER_ACTIONS:
            overflow_tools = self._add_held_tool(tool_id)
            self._mark_phase_tool_used(tool_id)
            self._remove_from_mayo(tool_id)
            for overflow_tool in overflow_tools:
                self._add_to_mayo(overflow_tool)
                self._publish_actor_event("place_on_mayo", overflow_tool, "")
                self._record_event(
                    "auto_place_on_mayo_capacity",
                    overflow_tool,
                    {"reason": "held_capacity_exceeded_after_handover"},
                )
            self._clear_pending("handover_completed", tool_id)
            self._record_event("skill_handover_completed", tool_id, {"action": action})
            self._publish_overlay()
            self._publish_state("using_tool", tool_id, "", False, False, f"{tool_id} delivered")
            self._schedule_next_decision(
                self._rng.uniform(
                    self._post_handover_public_cue_min_sec,
                    self._post_handover_public_cue_max_sec,
                )
            )
        elif action in RETRIEVE_ACTIONS:
            self._remove_from_mayo(tool_id)
            self._remove_held_tool(tool_id)
            self._clear_pending("retrieve_completed", tool_id)
            self._record_event("skill_retrieve_completed", tool_id, {"action": action})
            self._publish_overlay()
            self._schedule_next_decision(self._rng.uniform(1.0, 3.0))
        elif action in {"predict_tool", "tool_predict"}:
            self._record_event("skill_predict_completed", tool_id, {"action": action})

    def _on_bed_robot_arm_group_status(self, msg: BedRobotArmGroupStatus) -> None:
        group_id = str(msg.group_id or "").strip()
        if group_id not in {"suction", "retraction"}:
            return
        status_ns = int(msg.stamp.sec) * 1_000_000_000 + int(msg.stamp.nanosec)
        current_ns = self._bed_group_status_stamp_ns.get(group_id, 0)
        if current_ns and status_ns < current_ns:
            return
        state = self._bed_group_states.setdefault(group_id, {})
        pending = self._pending_group_requests.get(group_id)
        is_health = not str(msg.operation or "").strip() and str(
            msg.request_id or ""
        ).startswith("health-")
        pending_request_id = str(pending.get("request_id", "")) if pending else ""
        mismatched_pending = bool(
            pending_request_id
            and msg.request_id
            and msg.request_id != pending_request_id
        )
        if mismatched_pending:
            return
        next_state = msg.state or state.get("state", "standby")
        connected = next_state not in {"offline", "fault"}
        if is_health:
            connected = bool(msg.success) and msg.error_code != "server_unavailable"
            health_state = (
                "offline"
                if not connected
                else (
                    (msg.state or "standby")
                    if state.get("state") == "offline"
                    else state.get("state", "standby")
                )
            )
            changed = bool(
                state.get("connected") != connected
                or state.get("state") != health_state
            )
            state.update(
                {
                    "connected": connected,
                    "state": health_state,
                }
            )
            if changed:
                self._bed_group_status_stamp_ns[group_id] = status_ns
            return
        self._bed_group_status_stamp_ns[group_id] = status_ns
        state.update(
            {
                "connected": connected,
                "state": next_state,
                "operation": msg.operation,
                "direction": msg.direction,
                "distance_mm": float(msg.distance_mm),
                "distance_origin": msg.distance_origin,
                "progress": float(msg.progress),
                "error_code": msg.error_code,
                "rejection_reason": (
                    "" if msg.outcome == "cancelled_by_runtime_control" else msg.rejection_reason
                ),
            }
        )
        if msg.end_effector_profile and bool(msg.terminal and msg.success):
            state["end_effector_profile"] = msg.end_effector_profile
        if (
            bool(msg.terminal)
            and pending is not None
            and (not pending_request_id or msg.request_id == pending_request_id)
        ):
            self._pending_group_requests.pop(group_id, None)
            self._record_event(
                "bed_robot_arm_group_completed" if msg.success else "bed_robot_arm_group_rejected",
                "",
                {
                    "request_id": msg.request_id,
                    "group_id": group_id,
                    "operation": msg.operation,
                    "outcome": msg.outcome,
                    "direction": msg.direction,
                    "distance_mm": float(msg.distance_mm),
                    "error_code": msg.error_code,
                    "rejection_reason": msg.rejection_reason,
                },
            )
            self._schedule_next_decision(0.5)

    def _on_bed_robot_arm_group_request(self, msg: BedRobotArmGroupRequest) -> None:
        """Bind an actor speech cue to the public deterministic voice router."""
        group_id = str(msg.group_id or "").strip()
        pending = self._pending_group_requests.get(group_id)
        if pending is None or pending.get("request_id"):
            return
        if str(msg.source or "") == "llm_surgeon_actor":
            return
        if str(msg.operation or "") != str(pending.get("operation", "")):
            return
        pending["request_id"] = str(msg.request_id or "")

    def _clear_pending(self, reason: str, tool_id: str) -> None:
        if self._pending_tool and self._pending_tool != tool_id:
            return
        self._record_event(reason, tool_id, {"pending_action": self._pending_action})
        self._pending_action = ""
        self._pending_tool = ""

    def _resolve_tool(self, raw_tool: str) -> str:
        resolved = self._spec.resolve_instrument_alias(raw_tool)
        if resolved:
            return resolved
        return raw_tool if raw_tool in self._tool_ids else ""

    def _load_phase_tool_sequences(self) -> dict[str, list[str]]:
        sequences: dict[str, list[str]] = {}
        prompt_sequences = self._procedure_prompt.get("seq", {}) if isinstance(self._procedure_prompt, dict) else {}
        if isinstance(prompt_sequences, dict):
            for phase_id, rows in prompt_sequences.items():
                ordered: list[str] = []
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, list) or len(row) < 2:
                        continue
                    for raw_tool in row[:2]:
                        raw_text = str(raw_tool or "").strip()
                        if not raw_text or raw_text.lower() == "any":
                            continue
                        tool_id = self._resolve_tool(raw_text)
                        if tool_id and tool_id in self._requestable_tool_ids and tool_id not in ordered:
                            ordered.append(tool_id)
                if ordered:
                    sequences[str(phase_id)] = ordered
        for phase_id in self._phase_ids:
            if phase_id in sequences:
                continue
            sequences[phase_id] = [
                tool_id
                for tool_id in self._spec.get_expected_instruments(phase_id)
                if tool_id in self._requestable_tool_ids
            ]
        return sequences

    def _active_sequence_phase_id(self) -> str:
        return self._active_interrupt_phase_id or self._current_phase_id

    def _current_sequence_tools(self) -> set[str]:
        return set(self._phase_tool_sequences.get(self._active_sequence_phase_id(), []))

    def _preferred_sequence_tool(self) -> str:
        phase_id = self._active_sequence_phase_id()
        used_tools = self._interrupt_used_tools if phase_id == self._active_interrupt_phase_id else self._phase_used_tools
        for tool_id in self._phase_tool_sequences.get(phase_id, []):
            if tool_id in used_tools:
                continue
            if tool_id in self._held_tools:
                return ""
            if self._pending_tool == tool_id:
                return ""
            return tool_id
        return ""

    def _remove_from_mayo(self, tool_id: str) -> None:
        self._mayo_tools = [tool for tool in self._mayo_tools if tool != tool_id]

    def _add_to_mayo(self, tool_id: str) -> None:
        if tool_id and tool_id not in self._mayo_tools:
            self._mayo_tools.append(tool_id)

    def _primary_held_tool(self) -> str:
        return self._held_tools[-1] if self._held_tools else ""

    def _held_capacity_remaining(self) -> int:
        return max(0, self._max_held_tools - len(self._held_tools))

    def _remove_held_tool(self, tool_id: str) -> None:
        self._held_tools = [tool for tool in self._held_tools if tool != tool_id]

    def _add_held_tool(self, tool_id: str) -> list[str]:
        if not tool_id:
            return []
        self._remove_held_tool(tool_id)
        self._held_tools.append(tool_id)
        overflow: list[str] = []
        while len(self._held_tools) > self._max_held_tools:
            overflow.append(self._held_tools.pop(0))
        return overflow

    def _mark_phase_tool_used(self, tool_id: str) -> None:
        if tool_id:
            if (
                self._active_interrupt_phase_id
                and tool_id in set(self._spec.get_expected_instruments(self._active_interrupt_phase_id))
            ):
                self._interrupt_used_tools.add(tool_id)
            else:
                self._phase_used_tools.add(tool_id)

    def _phase_elapsed_sec(self) -> float:
        return max(0.0, self._now() - self._phase_entered_sec)

    def _phase_min_duration_sec(self, phase_id: str) -> float:
        try:
            return max(0.0, float(self._spec.get_phase_min_duration(phase_id)))
        except Exception:
            return 0.0

    def _phase_tool_coverage(self, phase_id: str) -> tuple[bool, int, int]:
        expected_tools = list(self._spec.get_expected_instruments(phase_id))
        if not expected_tools:
            return (True, 0, 0)
        expected_set = set(expected_tools)
        used_tools = self._interrupt_used_tools if phase_id in self._interrupt_phase_ids else self._phase_used_tools
        used_count = len(expected_set.intersection(used_tools))
        required_count = max(
            1,
            int(len(expected_set) * self._phase_auto_advance_tool_fraction + 0.999),
        )
        return (used_count >= required_count, used_count, len(expected_set))

    def _phase_display_name(self, phase_id: str) -> str:
        return self._phase_display_by_id.get(phase_id, phase_id)

    def _public_interrupt_label(self, phase_id: str) -> str:
        label = self._phase_display_name(phase_id)
        for suffix in (" interrupt", " Interrupt", "인터럽트"):
            if label.endswith(suffix):
                label = label[: -len(suffix)].strip()
        return label or phase_id

    def _interrupt_candidates_for_phase(self, phase_id: str) -> list[str]:
        if not self._interrupt_events_enabled or phase_id not in self._normal_phase_ids:
            return []
        allowed = self._spec.get_allowed_next_phases(phase_id)
        return [phase for phase in allowed if phase in self._interrupt_phase_ids]

    def _clear_scheduled_interrupt(self) -> None:
        self._scheduled_interrupt_phase_id = ""
        self._scheduled_interrupt_from_phase_id = ""
        self._scheduled_interrupt_at_sec = 0.0

    def _clear_interrupt_state(self, *, clear_cooldown: bool = False) -> None:
        self._clear_scheduled_interrupt()
        self._active_interrupt_phase_id = ""
        self._interrupt_return_phase_id = ""
        self._interrupt_entered_sec = 0.0
        self._interrupt_used_tools = set()
        self._field_event_lines = []
        if clear_cooldown:
            self._last_interrupt_completed_sec = -1_000_000_000.0

    def _schedule_interrupt_for_phase(self, phase_id: str, reason: str) -> None:
        self._clear_scheduled_interrupt()
        if not self._interrupt_events_enabled:
            return
        if phase_id not in self._normal_phase_ids:
            return
        now = self._now()
        if now - self._last_interrupt_completed_sec < self._interrupt_event_min_gap_sec:
            return
        candidates = self._interrupt_candidates_for_phase(phase_id)
        if not candidates:
            return
        if self._rng.random() > self._interrupt_event_phase_probability:
            return
        self._scheduled_interrupt_phase_id = self._rng.choice(candidates)
        self._scheduled_interrupt_from_phase_id = phase_id
        delay = self._rng.uniform(
            self._interrupt_event_min_phase_elapsed_sec,
            self._interrupt_event_max_phase_elapsed_sec,
        )
        self._scheduled_interrupt_at_sec = self._phase_entered_sec + delay
        self._record_event(
            "interrupt_event_scheduled",
            "",
            {
                "phase": self._scheduled_interrupt_phase_id,
                "from": phase_id,
                "at_elapsed_sec": round(delay, 1),
                "reason": reason,
            },
        )

    def _interrupt_public_lines(self, phase_id: str) -> list[str]:
        label = self._public_interrupt_label(phase_id)
        return [f"Field event: {label}"]

    def _actor_transition_allowed(self, current_phase: str, target_phase: str) -> bool:
        if target_phase == current_phase:
            return True
        if target_phase in self._interrupt_phase_ids and current_phase in self._normal_phase_ids:
            return True
        if self._spec.is_transition_allowed(current_phase, target_phase):
            return True
        if current_phase in self._interrupt_phase_ids and target_phase == self._interrupt_return_phase_id:
            return True
        return False

    def _start_interrupt_event(self, phase_id: str, speech: str, reason: str) -> None:
        if phase_id not in self._interrupt_phase_ids:
            return
        self._active_interrupt_phase_id = phase_id
        self._interrupt_return_phase_id = self._current_phase_id
        self._interrupt_entered_sec = self._now()
        self._interrupt_used_tools = set()
        self._field_event_lines = self._interrupt_public_lines(phase_id)
        self._last_speech = speech
        self._last_hand_overlay = ""
        self._publish_actor_event("field_event", "", speech, phase_id=phase_id)
        if speech:
            self._publish_voice(speech)
            self._publish_actor_event("small_talk", "", speech)
        self._record_event(
            "field_event_started",
            "",
            {"phase": phase_id, "return_phase": self._interrupt_return_phase_id, "reason": reason},
        )

    def _resolve_interrupt_event(self, speech: str, reason: str) -> None:
        phase_id = self._active_interrupt_phase_id
        if not phase_id:
            return
        self._last_speech = speech
        self._last_hand_overlay = ""
        self._publish_actor_event("field_event_resolved", "", speech, phase_id=phase_id)
        if speech:
            self._publish_voice(speech)
            self._publish_actor_event("small_talk", "", speech)
        self._record_event(
            "field_event_resolved",
            "",
            {"phase": phase_id, "return_phase": self._interrupt_return_phase_id, "reason": reason},
        )
        self._active_interrupt_phase_id = ""
        self._interrupt_return_phase_id = ""
        self._interrupt_entered_sec = 0.0
        self._interrupt_used_tools = set()
        self._field_event_lines = []
        self._last_interrupt_completed_sec = self._now()
        self._schedule_interrupt_for_phase(self._current_phase_id, "interrupt_resolved")

    def _interrupt_trigger_decision(self) -> dict[str, Any] | None:
        if not self._scheduled_interrupt_phase_id:
            return None
        if self._current_phase_id != self._scheduled_interrupt_from_phase_id:
            self._clear_scheduled_interrupt()
            return None
        if self._now() < self._scheduled_interrupt_at_sec:
            return None
        target_phase = self._scheduled_interrupt_phase_id
        label = self._public_interrupt_label(target_phase)
        self._clear_scheduled_interrupt()
        return {
            "v": "1",
            "action": "advance_phase",
            "tool": "",
            "request_mode": "none",
            "speech": self._rng.choice(
                [
                    "잠시 멈추고 수술 시야를 정리하겠습니다.",
                    "잠시 멈춰 주세요. 수술 시야를 먼저 안정시키겠습니다.",
                    "수술 시야를 안정시키겠습니다.",
                ]
            ),
            "phase": target_phase,
            "next_dwell_sec": self._min_dwell_sec,
            "reason_code": f"interrupt_event_triggered:{label}",
        }

    def _interrupt_tool_target_count(self, phase_id: str) -> int:
        expected_count = len(self._spec.get_expected_instruments(phase_id))
        if expected_count <= 0:
            return 0
        return max(1, int(expected_count * self._interrupt_event_tool_fraction + 0.999))

    def _interrupt_management_decision(self) -> dict[str, Any] | None:
        phase_id = self._active_interrupt_phase_id
        if not phase_id or phase_id not in self._interrupt_phase_ids:
            return None
        expected_tools = [
            tool_id
            for tool_id in self._spec.get_expected_instruments(phase_id)
            if tool_id in self._requestable_tool_ids
        ]
        target_count = self._interrupt_tool_target_count(phase_id)
        elapsed = max(0.0, self._now() - self._interrupt_entered_sec)
        if target_count > 0 and len(set(expected_tools).intersection(self._interrupt_used_tools)) < target_count:
            for tool_id in expected_tools:
                if tool_id in self._interrupt_used_tools:
                    continue
                if tool_id in self._held_tools:
                    self._mark_phase_tool_used(tool_id)
                    return {
                        "v": "1",
                        "action": "place_on_mayo",
                        "tool": tool_id,
                        "request_mode": "none",
                        "speech": "",
                        "phase": phase_id,
                        "next_dwell_sec": self._min_dwell_sec,
                        "reason_code": "interrupt_tool_used_place_on_mayo",
                    }
                if tool_id in self._mayo_tools:
                    if self._held_capacity_remaining() <= 0:
                        return self._place_held_before_completion_decision("interrupt_free_hand_for_mayo_tool")
                    return {
                        "v": "1",
                        "action": "pickup_from_mayo",
                        "tool": tool_id,
                        "request_mode": "none",
                        "speech": "",
                        "phase": phase_id,
                        "next_dwell_sec": self._min_dwell_sec,
                        "reason_code": "interrupt_reuse_mayo_tool",
                    }
                if self._held_capacity_remaining() <= 0:
                    return self._place_held_before_completion_decision("interrupt_free_hand_for_tool_request")
                request_mode = self._weighted_request_mode("voice")
                return {
                    "v": "1",
                    "action": "request_tool",
                    "tool": tool_id,
                    "request_mode": request_mode,
                    "speech": self._request_speech(tool_id, "", request_mode),
                    "phase": phase_id,
                    "next_dwell_sec": self._min_dwell_sec,
                    "reason_code": "interrupt_tool_request",
                }
        interrupt_held = [tool_id for tool_id in self._held_tools if tool_id in set(expected_tools)]
        if interrupt_held:
            return {
                "v": "1",
                "action": "place_on_mayo",
                "tool": interrupt_held[0],
                "request_mode": "none",
                "speech": "",
                "phase": phase_id,
                "next_dwell_sec": self._min_dwell_sec,
                "reason_code": "interrupt_resolved_place_tool_on_mayo",
            }
        if elapsed < self._interrupt_event_min_active_sec:
            return None
        return_phase = self._interrupt_return_phase_id or self._current_phase_id
        return {
            "v": "1",
            "action": "advance_phase",
            "tool": "",
            "request_mode": "none",
            "speech": self._rng.choice(
                [
                    "수술 시야가 안정됐습니다. 계속하겠습니다.",
                    "상황이 조절됐습니다. 수술을 계속하겠습니다.",
                    "좋습니다. 그대로 수술을 이어가겠습니다.",
                ]
            ),
            "phase": return_phase,
            "next_dwell_sec": self._min_dwell_sec,
            "reason_code": "interrupt_event_resolved",
        }

    def _next_normal_phase(self, phase_id: str) -> str:
        if phase_id in self._interrupt_phase_ids:
            return ""
        allowed = [
            phase
            for phase in self._spec.get_allowed_next_phases(phase_id)
            if phase != phase_id and phase not in self._interrupt_phase_ids
        ]
        if not allowed:
            return ""
        try:
            current_index = self._normal_phase_ids.index(phase_id)
        except ValueError:
            current_index = -1
        ordered_future = [
            phase
            for phase in self._normal_phase_ids[current_index + 1 :]
            if phase in allowed
        ]
        return ordered_future[0] if ordered_future else allowed[0]

    def _terminal_normal_phase(self, phase_id: str) -> bool:
        if phase_id not in self._normal_phase_ids:
            return False
        return self._normal_phase_ids.index(phase_id) == len(self._normal_phase_ids) - 1

    def _set_current_phase(self, phase_id: str, reason: str) -> None:
        if phase_id == self._current_phase_id:
            return
        previous_phase = self._current_phase_id
        now = self._now()
        self._current_phase_id = phase_id
        self._phase_entered_sec = now
        self._phase_used_tools = set()
        if phase_id in self._interrupt_phase_ids:
            self._active_interrupt_phase_id = phase_id
            self._interrupt_return_phase_id = previous_phase if previous_phase in self._normal_phase_ids else ""
            self._interrupt_entered_sec = now
            self._field_event_lines = self._interrupt_public_lines(phase_id)
        elif previous_phase in self._interrupt_phase_ids:
            self._active_interrupt_phase_id = ""
            self._interrupt_return_phase_id = ""
            self._interrupt_entered_sec = 0.0
            self._field_event_lines = []
            self._last_interrupt_completed_sec = now
            self._schedule_interrupt_for_phase(phase_id, "interrupt_resolved")
        else:
            self._field_event_lines = []
            self._schedule_interrupt_for_phase(phase_id, "phase_entered")
        self._record_event(
            "phase_entered",
            "",
            {"from": previous_phase, "to": phase_id, "reason": reason},
        )

    def _auto_phase_progression_decision(self) -> dict[str, Any] | None:
        if not self._autonomous_phase_progression_enabled:
            return None
        if self._pending_action or self._pending_group_requests:
            return None
        if self._current_phase_id in self._interrupt_phase_ids:
            return None
        elapsed = self._phase_elapsed_sec()
        min_duration = self._phase_min_duration_sec(self._current_phase_id)
        tools_ready, used_count, expected_count = self._phase_tool_coverage(self._current_phase_id)
        readiness_due = elapsed >= min_duration + self._phase_auto_advance_grace_sec and tools_ready
        force_due = elapsed >= max(min_duration + self._phase_auto_advance_grace_sec, self._phase_auto_advance_max_sec)
        if not readiness_due and not force_due:
            return None
        next_phase = self._next_normal_phase(self._current_phase_id)
        if next_phase:
            reason = "phase_watchdog_elapsed" if readiness_due else "phase_watchdog_max_dwell"
            return {
                "v": "1",
                "action": "advance_phase",
                "tool": "",
                "request_mode": "none",
                "speech": self._phase_advance_speech(next_phase),
                "phase": next_phase,
                "next_dwell_sec": self._min_dwell_sec,
                "reason_code": f"{reason}:{used_count}/{expected_count}:elapsed={elapsed:.1f}",
            }
        if self._terminal_normal_phase(self._current_phase_id) and (readiness_due or force_due):
            if self._held_tools:
                return self._place_held_before_completion_decision(
                    f"phase_watchdog_terminal_place_held:{used_count}/{expected_count}:elapsed={elapsed:.1f}"
                )
            return {
                "v": "1",
                "action": "complete_procedure",
                "tool": "",
                "request_mode": "none",
                "speech": "수술을 마쳤습니다.",
                "phase": self._current_phase_id,
                "next_dwell_sec": self._min_dwell_sec,
                "reason_code": f"phase_watchdog_terminal:{used_count}/{expected_count}:elapsed={elapsed:.1f}",
            }
        return None

    def _phase_advance_speech(self, next_phase: str) -> str:
        return ""

    def _place_held_before_completion_decision(self, reason_code: str) -> dict[str, Any]:
        tool = self._held_tools[0] if self._held_tools else ""
        return {
            "v": "1",
            "action": "place_on_mayo",
            "tool": tool,
            "request_mode": "none",
            "speech": "",
            "phase": self._current_phase_id,
            "next_dwell_sec": self._min_dwell_sec,
            "reason_code": reason_code,
        }

    def _place_held_to_free_hand(
        self,
        *,
        reason_code: str,
        source_action: str,
        wanted_tool: str = "",
    ) -> tuple[str, str]:
        placed_tool = self._held_tools[0] if self._held_tools else ""
        if not placed_tool:
            return ("", "place_on_mayo_requires_held_tool")
        self._remove_held_tool(placed_tool)
        self._add_to_mayo(placed_tool)
        self._last_speech = ""
        self._last_hand_overlay = ""
        self._publish_actor_event("place_on_mayo", placed_tool, "")
        self._record_event(
            "place_on_mayo",
            placed_tool,
            {
                "source_action": source_action,
                "wanted_tool": wanted_tool,
                "reason": reason_code,
            },
        )
        self._publish_state("place_on_mayo", placed_tool, "", False, False, reason_code)
        return (placed_tool, "")

    def _schedule_next_decision(self, delay_sec: float | None = None) -> None:
        if delay_sec is None:
            delay_sec = self._rng.uniform(self._min_dwell_sec, self._max_dwell_sec)
        self._next_decision_time = self._now() + max(0.0, float(delay_sec))

    def _decision_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "v": {"type": "string", "enum": ["1"]},
                "action": {
                    "type": "string",
                    "enum": [
                        "wait",
                        "small_talk",
                        "request_tool",
                        "request_bed_robot_arm_group",
                        "place_on_mayo",
                        "advance_phase",
                        "complete_procedure",
                    ],
                },
                "tool": {"type": "string", "enum": ["", *self._tool_ids]},
                "request_mode": {"type": "string", "enum": ["none", "voice", "hand", "voice_hand"]},
                "group_id": {"type": "string", "enum": ["", "suction", "retraction"]},
                "group_operation": {
                    "type": "string",
                    "enum": [
                        "",
                        "suction_start",
                        "suction_stop",
                        "retraction",
                        "release_retraction",
                        "change_end_effector",
                    ],
                },
                "end_effector_profile": {"type": "string"},
                "speech": {"type": "string"},
                "phase": {"type": "string", "enum": self._phase_ids},
                "next_dwell_sec": {"type": "number", "minimum": 0.5, "maximum": 10.0},
                "reason_code": {"type": "string"},
            },
            "required": [
                "v",
                "action",
                "tool",
                "request_mode",
                "group_id",
                "group_operation",
                "end_effector_profile",
                "speech",
                "phase",
                "next_dwell_sec",
                "reason_code",
            ],
            "additionalProperties": False,
        }

    def _build_system_prompt(self) -> str:
        phases = [
            {
                "id": phase.id,
                "next": [
                    next_phase
                    for next_phase in phase.possible_next
                    if next_phase not in self._interrupt_phase_ids
                ],
                "interrupt_events": [
                    next_phase
                    for next_phase in phase.possible_next
                    if next_phase in self._interrupt_phase_ids
                ],
                "tools": list(phase.expected_instruments),
                "min_sec": float(phase.min_duration_sec),
            }
            for phase in self._spec.bundle.phases
        ]
        instruments = [
            {
                "id": instrument.id,
                "name": instrument.display_name,
                "role": instrument.role,
                "requestable": bool(getattr(instrument, "requestable", True)),
            }
            for instrument in self._spec.bundle.instruments
        ]
        return (
            "You are the surgeon actor in a surgical task-planning simulator. "
            "You are the source of surgical progress, speech, and handover cues. "
            "Do not assume any external digital twin truth. You only know the JSON context supplied by this actor. "
            "Humanoid actions always succeed, but you must wait for completed skill feedback before assuming a delivered or recovered tool. "
            "After using a tool, place it on the Mayo stand instead of handing it directly back. "
            "Use small_talk sometimes, but keep it realistic and short. "
            "Never reveal phase labels, Pxx ids, current stage names, next stage names, or phase-transition intent in spoken speech. "
            "Phase changes are silent internal simulation state updates; never speak a line whose purpose is to announce, hint, or confirm a phase transition. "
            "For action=advance_phase, speech must be an empty string. "
            "Do not say phrases like 'next step', 'move on', 'proceed to', 'advance to', or any procedure phase name. "
            "Only tool-request speech may mention the requested tool; small_talk must not mention phase or progress state. "
            "Every non-empty spoken sentence must be concise, natural Korean. "
            "Tool display names may remain as supplied, but the surrounding request and all other speech must be Korean. "
            "When requesting a tool, choose voice, hand, or voice_hand. "
            "Use action=request_bed_robot_arm_group only for a phase-appropriate entry in context.bed_robot_arm_group.available_cues. "
            "For that action, copy one cue utterance exactly into speech, set group_id/group_operation/end_effector_profile from the cue, and set tool='' and request_mode='none'. "
            "Never mention or choose a physical robot-arm number, ID, member count, or mounting position; address only the suction or retraction logical group. "
            "Retraction speech may use an explicit distance or a qualitative expression such as 조금/많이; the downstream VLM determines one of six directions and mm. "
            "Do not repeat a group cue while that group has a pending request. Suction, retraction, and humanoid work are independent, so another lane may continue while one group is active. "
            "The surgeon can hold at most two tools. If context.held_capacity_remaining is positive, you may request a different needed tool while still holding one tool. "
            "If two tools are already held, place one held tool on Mayo before requesting another tool. "
            "If a needed tool is already in context.mayo_stand, request it normally; the humanoid will pick it up from Mayo and hand it over. "
            "Never request a tool that is already in context.held_tools. "
            "Never use place_on_mayo unless the chosen tool is listed in context.held_tools. "
            "Do not self-pick tools from Mayo; pickup_from_mayo is normalized to a humanoid request. "
            "Interrupt phases are procedure-defined intermittent field events, not a fixed final step. "
            "When context.interrupt_event.active is true, prioritize the active interrupt phase's expected tools, then advance back to context.interrupt_event.return_phase once stable. "
            "Follow the doctor procedure prompt flow when choosing phase progress and next tool timing. "
            "Use context.phase_elapsed_sec and context.phase_tool_coverage to avoid staying in one phase after its expected actions are complete. "
            "The doctor prompt may use Pxx/Txx ids; use runtime_phase and tools[*].rt crosswalk, and emit only runtime phase/tool ids. "
            "Never request a doctor tool id with an empty runtime id. "
            "Return exactly one JSON object matching the schema. "
            f"Runtime phases/tools: {json.dumps({'phases': phases, 'instruments': instruments}, separators=(',', ':'))}. "
            f"Doctor procedure prompt: {json.dumps(self._procedure_prompt, separators=(',', ':'))}"
        )

    def _available_bed_robot_arm_group_cues(self) -> list[dict[str, Any]]:
        phase_id = self._current_phase_id
        rows: list[dict[str, Any]] = []
        for cue in self._spec.get_bed_robot_arm_group_cues(phase_id):
            if cue.group_id in self._pending_group_requests:
                continue
            state = self._bed_group_states.get(cue.group_id, {})
            if not bool(state.get("connected", False)):
                continue
            execution_state = str(state.get("state", "standby"))
            if cue.operation == "suction_start" and execution_state != "standby":
                continue
            if cue.operation == "suction_stop" and execution_state != "suctioning":
                continue
            if cue.operation == "release_retraction" and execution_state != "holding":
                continue
            if cue.operation == "retraction" and execution_state not in {"standby", "holding"}:
                continue
            if (
                cue.end_effector_profile
                and state.get("end_effector_profile", "") != cue.end_effector_profile
            ):
                continue
            utterances = list(cue.utterances)
            if not utterances:
                continue
            rows.append(
                {
                    "cue_id": cue.id,
                    "group_id": cue.group_id,
                    "operation": cue.operation,
                    "utterances": utterances,
                    "directions": list(cue.directions),
                    "default_distance_mm": float(cue.default_distance_mm),
                    "end_effector_profile": cue.end_effector_profile,
                    "feedback_text": cue.feedback_text,
                }
            )
        for transition in self._spec.get_bed_robot_arm_end_effector_transitions(phase_id):
            if transition.group_id in self._pending_group_requests:
                continue
            state = self._bed_group_states.get(transition.group_id, {})
            if not bool(state.get("connected", False)):
                continue
            if str(state.get("state", "standby")) != "holding":
                continue
            if state.get("end_effector_profile", "") != transition.from_profile:
                continue
            utterances = list(transition.utterances)
            if not utterances:
                continue
            rows.append(
                {
                    "cue_id": transition.id,
                    "group_id": transition.group_id,
                    "operation": "change_end_effector",
                    "utterances": utterances,
                    "directions": [],
                    "default_distance_mm": 0.0,
                    "end_effector_profile": transition.to_profile,
                    "from_end_effector_profile": transition.from_profile,
                    "feedback_text": transition.feedback_text,
                }
            )
        return rows

    def _actor_context(self) -> dict[str, Any]:
        expected_tools = self._spec.get_expected_instruments(self._current_phase_id)
        elapsed = self._phase_elapsed_sec()
        tools_ready, used_count, expected_count = self._phase_tool_coverage(self._current_phase_id)
        return {
            "procedure": self._spec.procedure_id,
            "procedure_prompt_id": str(self._procedure_prompt.get("id", "")) if isinstance(self._procedure_prompt, dict) else "",
            "phase": self._current_phase_id,
            "phase_next": [
                phase
                for phase in self._spec.get_allowed_next_phases(self._current_phase_id)
                if phase not in self._interrupt_phase_ids
            ],
            "phase_elapsed_sec": round(elapsed, 1),
            "phase_min_sec": self._phase_min_duration_sec(self._current_phase_id),
            "phase_used_tools": sorted(self._phase_used_tools),
            "phase_tool_coverage": {
                "ready": tools_ready,
                "used": used_count,
                "expected": expected_count,
            },
            "phase_auto_advance_max_sec": self._phase_auto_advance_max_sec,
            "expected_tools": expected_tools,
            "requestable_tools": self._requestable_tool_ids,
            "held_tool": self._primary_held_tool(),
            "held_tools": list(self._held_tools),
            "held_capacity": self._max_held_tools,
            "held_capacity_remaining": self._held_capacity_remaining(),
            "mayo_stand": list(self._mayo_tools),
            "pending": {"action": self._pending_action, "tool": self._pending_tool},
            "bed_robot_arm_group": {
                "states": self._bed_group_states,
                "pending": self._pending_group_requests,
                "available_cues": self._available_bed_robot_arm_group_cues(),
                "directions": ["UP", "DOWN", "LEFT", "RIGHT", "LEFT_RIGHT", "UP_DOWN"],
                "distance_rule": (
                    "explicit cm/mm and unitless numeric values are not clamped; "
                    "qualitative values are 1..30 mm; missing distance defaults to 10 mm"
                ),
            },
            "interrupt_event": {
                "enabled": self._interrupt_events_enabled,
                "scheduled": bool(self._scheduled_interrupt_phase_id),
                "active": bool(self._active_interrupt_phase_id),
                "phase": self._active_interrupt_phase_id or self._scheduled_interrupt_phase_id,
                "return_phase": self._interrupt_return_phase_id,
                "candidate_interrupts": self._interrupt_candidates_for_phase(self._current_phase_id),
                "public_cues": list(self._field_event_lines),
            },
            "episode_style": self._episode_style,
            "random_hint": round(self._rng.random(), 3),
            "recent_events": list(self._recent_events),
            "rules": [
                "Normal return path is surgeon hand -> Mayo stand -> robot left hand -> cleaner -> rack.",
                "Use place_on_mayo for a used tool when the surgeon is finished with it for now.",
                "place_on_mayo is valid only for a tool in held_tools.",
                "If a needed tool is in mayo_stand, use request_tool; the humanoid retrieves it from Mayo and hands it over.",
                "request_tool is invalid only for tools already in held_tools.",
                "Use complete_procedure only when the current scenario segment is done.",
                (
                    "If pending.action is not empty, do not start another tool or phase action; "
                    "a bed robot-arm group cue for a non-pending group remains allowed."
                ),
                "A bed robot-arm group request is allowed only from bed_robot_arm_group.available_cues and must use one exact utterance.",
                "Never include a physical arm ID or arm count in speech.",
                "Interrupt events are procedure-defined intermittent phases. If interrupt_event.active is true, stabilize it with that phase's expected tools, then return to interrupt_event.return_phase.",
            ],
        }

    def _request_model(self, context: dict[str, Any]) -> tuple[dict[str, Any], str, float]:
        body: dict[str, Any] = {
            "model": self._model_id,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": json.dumps(context, separators=(",", ":"), sort_keys=True)},
            ],
            "temperature": self._temperature,
            "top_p": self._top_p,
            "max_tokens": self._max_output_tokens,
            "seed": self._seed + len(self._recent_events),
        }
        if self._reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        if self._response_format == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "surgeon_actor_decision",
                    "strict": True,
                    "schema": self._schema,
                },
            }
        elif self._response_format == "json_object":
            body["response_format"] = {"type": "json_object"}
        force_disable_thinking(body, provider_id=self._provider_id)
        start = time.perf_counter()
        response = requests.post(
            f"{self._base_url}/v1/chat/completions",
            json=body,
            headers=self._headers(),
            timeout=self._request_timeout_sec,
        )
        latency = time.perf_counter() - start
        response.raise_for_status()
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        raw = str(message.get("content") or message.get("reasoning_content") or "")
        payload = self._parse_json(raw)
        return payload, raw, latency

    def _parse_json(self, raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start < 0 or end <= start:
                raise
            payload = json.loads(raw[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("actor payload must be an object")
        return payload

    def _validate_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("v", "")) != "1":
            raise ValueError("unsupported actor schema version")
        action = str(payload.get("action", "wait"))
        allowed_actions = {
            "wait",
            "small_talk",
            "request_tool",
            "request_bed_robot_arm_group",
            "place_on_mayo",
            "pickup_from_mayo",
            "advance_phase",
            "complete_procedure",
        }
        if action not in allowed_actions:
            raise ValueError(f"unsupported actor action: {action}")
        tool = str(payload.get("tool", ""))
        phase = str(payload.get("phase", self._current_phase_id)) or self._current_phase_id
        request_mode = str(payload.get("request_mode", "none"))
        group_id = str(payload.get("group_id", ""))
        group_operation = str(payload.get("group_operation", ""))
        end_effector_profile = str(payload.get("end_effector_profile", ""))
        if tool:
            resolved = self._resolve_tool(tool)
            if not resolved:
                raise ValueError(f"unknown tool: {tool}")
            tool = resolved
        if phase not in self._phase_ids:
            raise ValueError(f"unknown phase: {phase}")
        if action == "request_bed_robot_arm_group":
            operations = {
                "suction": {"suction_start", "suction_stop"},
                "retraction": {
                    "retraction",
                    "release_retraction",
                    "change_end_effector",
                },
            }
            if group_id not in operations or group_operation not in operations[group_id]:
                raise ValueError("invalid logical bed robot-arm group operation")
        else:
            group_id = ""
            group_operation = ""
            end_effector_profile = ""
        return {
            "v": "1",
            "action": action,
            "tool": tool,
            "request_mode": request_mode,
            "group_id": group_id,
            "group_operation": group_operation,
            "end_effector_profile": end_effector_profile,
            "speech": str(payload.get("speech", ""))[:240],
            "phase": phase,
            "next_dwell_sec": float(payload.get("next_dwell_sec", self._min_dwell_sec)),
            "reason_code": str(payload.get("reason_code", ""))[:120],
        }

    def _tick(self) -> None:
        if not self._enabled or not self._active:
            return
        if self._now() < self._manual_override_mute_until_sec:
            return
        if self._now() < self._next_decision_time:
            return
        if not self._pending_action and not self._pending_group_requests:
            interrupt_decision = self._interrupt_trigger_decision()
            if interrupt_decision is not None:
                raw = json.dumps({"source": "interrupt_scheduler", **interrupt_decision}, separators=(",", ":"))
                self._apply_decision(interrupt_decision, raw, 0.0)
                return
            interrupt_decision = self._interrupt_management_decision()
            if interrupt_decision is not None:
                raw = json.dumps({"source": "interrupt_scheduler", **interrupt_decision}, separators=(",", ":"))
                self._apply_decision(interrupt_decision, raw, 0.0)
                return
            auto_decision = self._auto_phase_progression_decision()
            if auto_decision is not None:
                raw = json.dumps({"source": "phase_watchdog", **auto_decision}, separators=(",", ":"))
                self._apply_decision(auto_decision, raw, 0.0)
                return
        context = self._actor_context()
        try:
            payload, raw, latency = self._request_model(context)
            decision = self._validate_decision(payload)
            decision = self._diversify_decision(decision)
            self._last_decision_raw = raw
            self._apply_decision(decision, raw, latency)
        except Exception as exc:
            self._publish_llm_decision(
                raw_json=self._last_decision_raw,
                accepted=False,
                reject_reason=str(exc),
                action="",
                tool="",
                request_mode="",
                speech="",
                hidden_phase=self._current_phase_id,
                latency_sec=0.0,
            )
            self._record_event("actor_decision_rejected", "", {"reason": str(exc)})
            self._schedule_next_decision(2.0)

    def _match_bed_robot_arm_group_cue(
        self,
        *,
        group_id: str,
        operation: str,
        speech: str,
        end_effector_profile: str,
    ) -> dict[str, Any] | None:
        if re.search(
            r"(?:bed[_ -]?arm|robot[_ -]?arm)\s*\d|"
            r"로봇\s*팔\s*(?:\d+|[일이삼]번)|"
            r"(?:\d+|[일이삼])\s*번\s*(?:로봇\s*)?팔",
            speech,
            flags=re.IGNORECASE,
        ):
            return None
        for cue in self._available_bed_robot_arm_group_cues():
            if cue["group_id"] != group_id or cue["operation"] != operation:
                continue
            if speech not in cue["utterances"]:
                continue
            expected_profile = str(cue.get("end_effector_profile", ""))
            if expected_profile != end_effector_profile:
                continue
            return cue
        return None

    def _mask_decision_for_pending_lanes(
        self, decision: dict[str, Any]
    ) -> dict[str, Any]:
        """Keep independent lanes live without overlapping one lane's work."""

        action = str(decision.get("action", "wait"))
        blocked_by_humanoid = bool(self._pending_action) and action not in {
            "request_bed_robot_arm_group",
            "wait",
            "small_talk",
        }
        blocked_phase_change = bool(self._pending_group_requests) and action in {
            "advance_phase",
            "complete_procedure",
        }
        if not blocked_by_humanoid and not blocked_phase_change:
            return decision
        masked = dict(decision)
        masked.update(
            {
                "action": "wait",
                "tool": "",
                "request_mode": "none",
                "group_id": "",
                "group_operation": "",
                "end_effector_profile": "",
                "speech": "",
                "phase": self._current_phase_id,
                "reason_code": (
                    "pending_humanoid_lane"
                    if blocked_by_humanoid
                    else "pending_bed_robot_arm_group_lane"
                ),
            }
        )
        return masked

    def _apply_decision(self, decision: dict[str, Any], raw: str, latency: float) -> None:
        decision = self._mask_decision_for_pending_lanes(decision)
        action = str(decision["action"])
        tool = str(decision["tool"])
        request_mode = str(decision["request_mode"])
        group_id = str(decision.get("group_id", ""))
        group_operation = str(decision.get("group_operation", ""))
        end_effector_profile = str(decision.get("end_effector_profile", ""))
        speech = str(decision["speech"])
        next_dwell = max(self._min_dwell_sec, min(self._max_dwell_sec, float(decision["next_dwell_sec"])))
        phase = str(decision["phase"])

        if action == "pickup_from_mayo":
            action = "request_tool"
            request_mode = self._weighted_request_mode(
                request_mode if request_mode in {"voice", "hand", "voice_hand"} else "voice"
            )
            speech = self._request_speech(tool, speech, request_mode)

        accepted = True
        reject_reason = ""
        if action == "request_tool":
            sequence_tools = self._current_sequence_tools()
            preferred_tool = self._preferred_sequence_tool()
            if preferred_tool and tool and tool != preferred_tool:
                self._record_event(
                    "sequence_request_corrected",
                    tool,
                    {"preferred": preferred_tool, "phase": self._active_sequence_phase_id()},
                )
                tool = preferred_tool
            elif (
                sequence_tools
                and tool
                and tool not in sequence_tools
                and tool not in self._held_tools
                and tool not in self._mayo_tools
                and tool != self._pending_tool
            ):
                accepted = False
                reject_reason = "tool_outside_current_phase_sequence"
            elif (
                sequence_tools
                and not preferred_tool
                and tool
                and tool not in self._held_tools
                and tool not in self._mayo_tools
                and tool != self._pending_tool
            ):
                accepted = False
                reject_reason = "phase_sequence_complete"
            if not accepted:
                pass
            elif not tool:
                accepted = False
                reject_reason = "request_tool requires tool"
            elif tool in self._held_tools:
                action = "continue_using"
                speech = ""
                self._last_speech = speech
                self._last_hand_overlay = ""
                self._publish_actor_event("continue_using", tool, speech)
                self._record_event("continue_using", tool, {"source_action": "request_tool", "speech": speech})
                self._publish_state("continue_using", tool, speech, False, False, "requested_tool_already_held")
            elif self._held_capacity_remaining() <= 0:
                action = "place_on_mayo"
                tool, reject_reason = self._place_held_to_free_hand(
                    reason_code="free_hand_before_new_request",
                    source_action="request_tool",
                    wanted_tool=tool,
                )
                accepted = not bool(reject_reason)
                request_mode = "none"
                speech = ""
            elif tool not in self._requestable_tool_ids:
                accepted = False
                reject_reason = "tool_not_requestable"
            else:
                event_type = "voice_request" if request_mode == "voice" else "request_tool"
                hand_overlay = "Surgeon hand extending" if request_mode in {"hand", "voice_hand"} else ""
                self._publish_actor_event(event_type, tool, speech, ready_for_handover=True)
                self._pending_action = "handover"
                self._pending_tool = tool
                self._last_speech = speech
                self._last_hand_overlay = hand_overlay
                self._record_event("request_tool", tool, {"mode": request_mode, "speech": speech})
                if speech:
                    self._publish_voice(speech)
                self._publish_state("request_tool", tool, speech, True, False, decision["reason_code"])
        elif action == "request_bed_robot_arm_group":
            cue = self._match_bed_robot_arm_group_cue(
                group_id=group_id,
                operation=group_operation,
                speech=speech,
                end_effector_profile=end_effector_profile,
            )
            if tool or request_mode != "none":
                accepted = False
                reject_reason = "group_request_must_not_use_tool_or_hand_request_mode"
            elif group_id in self._pending_group_requests:
                accepted = False
                reject_reason = "bed_robot_arm_group_request_already_pending"
            elif cue is None:
                accepted = False
                reject_reason = "group_request_not_grounded_in_current_phase_cue"
            elif not speech:
                accepted = False
                reject_reason = "bed_robot_arm_group_request_requires_spoken_cue"
            else:
                self._pending_group_requests[group_id] = {
                    "request_id": "",
                    "cue_id": str(cue.get("cue_id", "")),
                    "operation": group_operation,
                    "speech": speech,
                }
                self._publish_voice(speech)
                self._publish_actor_event("bed_robot_arm_group_request", "", speech)
                self._last_speech = speech
                self._last_hand_overlay = ""
                self._record_event(
                    "bed_robot_arm_group_request",
                    "",
                    {
                        "request_id": "",
                        "cue_id": cue.get("cue_id", ""),
                        "group_id": group_id,
                        "operation": group_operation,
                        "end_effector_profile": end_effector_profile,
                        "speech": speech,
                    },
                )
                self._publish_state(
                    "request_bed_robot_arm_group",
                    "",
                    speech,
                    False,
                    False,
                    str(cue.get("feedback_text", decision["reason_code"])),
                )
        elif action == "place_on_mayo":
            placed_tool = tool if tool in self._held_tools else self._primary_held_tool()
            if not placed_tool or placed_tool not in self._held_tools:
                accepted = False
                reject_reason = "place_on_mayo_requires_held_tool"
            else:
                speech = self._sanitize_non_request_speech(speech, allowed_tool=placed_tool)
                self._remove_held_tool(placed_tool)
                self._add_to_mayo(placed_tool)
                self._last_speech = speech
                self._last_hand_overlay = ""
                self._publish_actor_event("place_on_mayo", placed_tool, speech)
                if speech:
                    self._publish_voice(speech)
                self._record_event("place_on_mayo", placed_tool, {"speech": speech})
                self._publish_state("place_on_mayo", placed_tool, speech, False, False, decision["reason_code"])
        elif action == "pickup_from_mayo":
            sequence_tools = self._current_sequence_tools()
            preferred_tool = self._preferred_sequence_tool()
            if preferred_tool and preferred_tool in self._mayo_tools and tool != preferred_tool:
                self._record_event(
                    "sequence_mayo_pickup_corrected",
                    tool,
                    {"preferred": preferred_tool, "phase": self._active_sequence_phase_id()},
                )
                tool = preferred_tool
            elif sequence_tools and tool and tool not in sequence_tools:
                accepted = False
                reject_reason = "mayo_tool_outside_current_phase_sequence"
            if not accepted:
                pass
            elif not tool or tool not in self._mayo_tools:
                accepted = False
                reject_reason = "pickup_from_mayo_requires_mayo_tool"
            elif self._held_capacity_remaining() <= 0:
                action = "place_on_mayo"
                tool, reject_reason = self._place_held_to_free_hand(
                    reason_code="free_hand_before_mayo_pickup",
                    source_action="pickup_from_mayo",
                    wanted_tool=tool,
                )
                accepted = not bool(reject_reason)
                request_mode = "none"
                speech = ""
            else:
                speech = ""
                self._remove_from_mayo(tool)
                self._add_held_tool(tool)
                self._mark_phase_tool_used(tool)
                self._last_speech = speech
                self._last_hand_overlay = ""
                self._publish_actor_event("continue_using", tool, speech)
                self._record_event("pickup_from_mayo", tool, {"speech": speech})
                self._publish_state("continue_using", tool, speech, False, False, decision["reason_code"])
        elif action == "advance_phase":
            speech = ""
            if phase != self._current_phase_id and not self._actor_transition_allowed(self._current_phase_id, phase):
                accepted = False
                reject_reason = "illegal_phase_transition"
            elif phase in self._interrupt_phase_ids and self._current_phase_id in self._normal_phase_ids:
                action = "field_event"
                self._start_interrupt_event(phase, speech, decision["reason_code"])
                self._publish_state("field_event", "", speech, False, False, decision["reason_code"])
            elif self._active_interrupt_phase_id and phase == self._current_phase_id:
                action = "resolve_interrupt"
                self._resolve_interrupt_event(speech, decision["reason_code"])
                self._publish_state("resolve_interrupt", "", speech, False, False, decision["reason_code"])
            else:
                target_phase = phase
                self._last_speech = speech
                self._last_hand_overlay = ""
                if target_phase != self._current_phase_id:
                    self._publish_actor_event("advance_phase_cue", "", speech, phase_id=target_phase)
                    self._set_current_phase(target_phase, decision["reason_code"])
                if speech:
                    self._publish_voice(speech)
                self._record_event("advance_phase", "", {"phase": target_phase, "speech": speech})
                self._publish_state("advance_phase", "", speech, False, False, decision["reason_code"])
        elif action == "complete_procedure":
            if self._held_tools:
                action = "place_on_mayo"
                placed_tool = self._held_tools[0]
                tool = placed_tool
                speech = ""
                self._remove_held_tool(placed_tool)
                self._add_to_mayo(placed_tool)
                self._last_speech = speech
                self._last_hand_overlay = ""
                self._publish_actor_event("place_on_mayo", placed_tool, speech)
                self._record_event(
                    "place_on_mayo",
                    placed_tool,
                    {"source_action": "complete_procedure", "reason": "place_held_before_completion"},
                )
                self._publish_state(
                    "place_on_mayo",
                    placed_tool,
                    speech,
                    False,
                    False,
                    f"place_held_before_completion:{decision['reason_code']}",
                )
            else:
                speech = self._sanitize_non_request_speech(speech)
                self._last_speech = speech or "수술을 마쳤습니다."
                self._last_hand_overlay = ""
                self._publish_actor_event("request_procedure_completion", "", self._last_speech)
                self._publish_voice(self._last_speech)
                self._record_event("complete_procedure", "", {"speech": self._last_speech})
                self._publish_state("complete_procedure", "", self._last_speech, False, False, decision["reason_code"])
                self._active = False
        elif action == "small_talk":
            speech = self._sanitize_small_talk_speech(speech)
            if speech:
                self._publish_voice(speech)
                self._publish_actor_event("small_talk", "", speech)
            self._last_speech = speech
            self._last_hand_overlay = ""
            self._record_event("small_talk", "", {"speech": speech})
            self._publish_state("small_talk", "", speech, False, False, decision["reason_code"])
        else:
            self._last_speech = ""
            self._last_hand_overlay = ""
            self._publish_state("idle", "", "", False, False, decision["reason_code"])

        self._publish_overlay()
        self._publish_llm_decision(
            raw_json=raw,
            accepted=accepted,
            reject_reason=reject_reason,
            action=action,
            tool=tool,
            request_mode=request_mode,
            speech=speech,
            hidden_phase=self._current_phase_id,
            latency_sec=latency,
        )
        if accepted:
            if action == "request_tool" and request_mode in {"voice", "hand", "voice_hand"}:
                self._request_mode_history.append(request_mode)
            if action == "place_on_mayo":
                self._schedule_next_decision(
                    self._rng.uniform(
                        self._post_mayo_public_cue_min_sec,
                        self._post_mayo_public_cue_max_sec,
                    )
                )
            else:
                self._schedule_next_decision(next_dwell + self._rng.uniform(0.0, 1.0))
        else:
            self._record_event("actor_decision_rejected", tool, {"reason": reject_reason, "action": action})
            self._schedule_next_decision(1.5)

    def _weighted_request_mode(self, model_mode: str) -> str:
        valid_modes = {"voice", "hand", "voice_hand"}
        if self._require_voice_for_tool_requests:
            valid_modes = {"voice", "voice_hand"}
            if model_mode == "hand":
                model_mode = "voice_hand"
        mode = model_mode if model_mode in valid_modes and self._rng.random() < 0.45 else ""
        if not mode:
            modes = [
                item
                for item, weight in self._request_mode_weights.items()
                if item in valid_modes and weight > 0.0
            ]
            weights = [self._request_mode_weights[item] for item in modes]
            fallback_modes = ["voice", "voice_hand"] if self._require_voice_for_tool_requests else ["voice", "hand", "voice_hand"]
            mode = self._rng.choices(modes or fallback_modes, weights=weights or None, k=1)[0]
        if len(self._request_mode_history) >= self._same_request_mode_limit:
            recent = list(self._request_mode_history)[-self._same_request_mode_limit :]
            if recent and all(item == mode for item in recent):
                alternatives = [item for item in valid_modes if item != mode]
                mode = self._rng.choice(alternatives)
        return mode

    def _tool_is_spoken(self, tool: str, speech: str) -> bool:
        lowered = speech.lower()
        speech_tokens = {
            token
            for token in "".join(char.lower() if char.isalnum() else " " for char in speech).split()
            if token
        }
        generic_tokens = {
            "tool",
            "instrument",
            "forceps",
            "clamp",
            "retractor",
            "cautery",
            "scissors",
            "pencil",
            "tip",
            "blade",
            "handle",
            "please",
            "next",
        }
        for alias in self._tool_aliases_by_id.get(tool, [tool]):
            alias_text = str(alias or "").strip().lower()
            if not alias_text:
                continue
            if alias_text in lowered:
                return True
            alias_tokens = {
                token
                for token in "".join(char.lower() if char.isalnum() else " " for char in alias_text).split()
                if token
            }
            if not alias_tokens:
                continue
            distinctive = alias_tokens.difference(generic_tokens)
            if distinctive and distinctive.issubset(speech_tokens):
                return True
            if len(alias_tokens) >= 2 and len(alias_tokens.intersection(speech_tokens)) >= 2:
                return True
        return False

    def _sanitize_non_request_speech(self, speech: str, *, allowed_tool: str = "") -> str:
        if not speech.strip():
            return ""
        if not self._contains_hangul(speech):
            return ""
        if self._speech_has_phase_cue(speech):
            return ""
        mentioned_other_tool = any(
            tool_id != allowed_tool and self._tool_is_spoken(tool_id, speech)
            for tool_id in self._requestable_tool_ids
        )
        if mentioned_other_tool:
            return ""
        return speech[:240]

    def _speech_has_phase_cue(self, speech: str) -> bool:
        lowered = speech.lower()
        cue_phrases = {
            "next step",
            "move on",
            "move to",
            "proceed to",
            "advance to",
            "next phase",
            "next part",
        }
        if any(phrase in lowered for phrase in cue_phrases):
            return True
        speech_tokens = {
            token
            for token in "".join(char.lower() if char.isalnum() else " " for char in speech).split()
            if token
        }
        for phase_id, display_name in self._phase_display_by_id.items():
            for label in (phase_id, display_name):
                label_text = str(label or "").strip().lower()
                if not label_text:
                    continue
                if label_text in lowered and len(label_text) > 1:
                    return True
                label_tokens = {
                    token
                    for token in "".join(char.lower() if char.isalnum() else " " for char in label_text).split()
                    if token
                }
                if len(label_tokens) >= 2 and label_tokens.issubset(speech_tokens):
                    return True
        return False

    def _sanitize_small_talk_speech(self, speech: str) -> str:
        sanitized = str(speech or "").strip()[:240]
        if not sanitized:
            return self._small_talk_line()
        if not self._contains_hangul(sanitized):
            return self._small_talk_line()
        if self._speech_has_phase_cue(sanitized):
            return self._small_talk_line()
        if self._last_speech and sanitized == self._last_speech:
            return self._small_talk_line()
        if any(self._tool_is_spoken(tool_id, sanitized) for tool_id in self._requestable_tool_ids):
            return self._small_talk_line()
        return sanitized

    def _request_speech(self, tool: str, speech: str, request_mode: str) -> str:
        if request_mode == "hand":
            return ""
        display = self._tool_display_by_id.get(tool, tool)
        templates = [
            "{tool} 주세요.",
            "{tool} 부탁합니다.",
            "{tool} 준비해 주세요.",
            "{tool} 전달해 주세요.",
        ]
        return self._rng.choice(templates).format(tool=display)[:240]

    def _small_talk_line(self) -> str:
        lines = [
            "수술 시야가 안정적입니다.",
            "시야를 안정적으로 유지해 주세요.",
            "메이요 스탠드를 정리해 주세요.",
            "수술 시야를 정돈해 주세요.",
            "감사합니다.",
            "그대로 유지해 주세요.",
        ]
        return self._rng.choice(lines)

    @staticmethod
    def _contains_hangul(text: str) -> bool:
        return bool(re.search(r"[가-힣]", str(text or "")))

    def _diversify_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        next_decision = dict(decision)
        action = str(next_decision.get("action", "wait"))
        if action == "request_tool":
            preferred_tool = self._preferred_sequence_tool()
            if preferred_tool and str(next_decision.get("tool", "")) != preferred_tool:
                next_decision["tool"] = preferred_tool
                reason = str(next_decision.get("reason_code", ""))
                next_decision["reason_code"] = f"sequence_preferred:{reason}"[:120]
            elif not preferred_tool and self._current_sequence_tools():
                next_decision["action"] = "small_talk"
                next_decision["tool"] = ""
                next_decision["request_mode"] = "none"
                next_decision["speech"] = self._small_talk_line()
                reason = str(next_decision.get("reason_code", ""))
                next_decision["reason_code"] = f"sequence_complete_wait_for_phase:{reason}"[:120]
                return next_decision
            mode = self._weighted_request_mode(str(next_decision.get("request_mode", "none")))
            tool = str(next_decision.get("tool", ""))
            next_decision["request_mode"] = mode
            next_decision["speech"] = self._request_speech(tool, str(next_decision.get("speech", "")), mode)
            return next_decision
        if action == "pickup_from_mayo":
            preferred_tool = self._preferred_sequence_tool()
            sequence_tools = self._current_sequence_tools()
            if preferred_tool and preferred_tool in self._mayo_tools and str(next_decision.get("tool", "")) != preferred_tool:
                next_decision["tool"] = preferred_tool
                reason = str(next_decision.get("reason_code", ""))
                next_decision["reason_code"] = f"sequence_preferred_mayo:{reason}"[:120]
            requested_tool = str(next_decision.get("tool", ""))
            if requested_tool and sequence_tools and requested_tool not in sequence_tools:
                if preferred_tool and preferred_tool in self._requestable_tool_ids:
                    if preferred_tool in self._held_tools or preferred_tool == self._pending_tool:
                        next_decision["action"] = "small_talk"
                        next_decision["tool"] = ""
                        next_decision["request_mode"] = "none"
                        next_decision["speech"] = self._small_talk_line()
                        next_decision["reason_code"] = "mayo_pickup_outside_phase_wait_for_preferred"
                        return next_decision
                    if self._held_capacity_remaining() <= 0:
                        next_decision["action"] = "place_on_mayo"
                        next_decision["tool"] = self._held_tools[0] if self._held_tools else ""
                        next_decision["request_mode"] = "none"
                        next_decision["speech"] = ""
                        next_decision["reason_code"] = "free_hand_before_phase_preferred_request"
                        return next_decision
                    mode = self._weighted_request_mode(str(next_decision.get("request_mode", "voice")))
                    next_decision["action"] = "request_tool"
                    next_decision["tool"] = preferred_tool
                    next_decision["request_mode"] = mode
                    next_decision["speech"] = self._request_speech(
                        preferred_tool,
                        str(next_decision.get("speech", "")),
                        mode,
                    )
                    next_decision["reason_code"] = "mayo_pickup_outside_phase_converted_to_preferred_request"
                    return next_decision
                next_decision["action"] = "small_talk"
                next_decision["tool"] = ""
                next_decision["request_mode"] = "none"
                next_decision["speech"] = self._small_talk_line()
                next_decision["reason_code"] = "mayo_pickup_outside_phase_suppressed"
                return next_decision
            if requested_tool and requested_tool not in self._mayo_tools:
                candidate_tool = preferred_tool or requested_tool
                if candidate_tool in self._held_tools:
                    next_decision["action"] = "small_talk"
                    next_decision["tool"] = ""
                    next_decision["request_mode"] = "none"
                    next_decision["speech"] = self._small_talk_line()
                    next_decision["reason_code"] = "mayo_pickup_tool_already_held"
                elif candidate_tool in self._requestable_tool_ids:
                    if self._held_capacity_remaining() <= 0:
                        next_decision["action"] = "place_on_mayo"
                        next_decision["tool"] = self._held_tools[0] if self._held_tools else ""
                        next_decision["request_mode"] = "none"
                        next_decision["speech"] = ""
                        next_decision["reason_code"] = "free_hand_before_invalid_mayo_pickup"
                    else:
                        mode = self._weighted_request_mode(str(next_decision.get("request_mode", "voice")))
                        next_decision["action"] = "request_tool"
                        next_decision["tool"] = candidate_tool
                        next_decision["request_mode"] = mode
                        next_decision["speech"] = self._request_speech(
                            candidate_tool,
                            str(next_decision.get("speech", "")),
                            mode,
                        )
                        next_decision["reason_code"] = "mayo_pickup_missing_converted_to_request"
            return next_decision
        if action in {"small_talk", "wait"}:
            tool = self._resolve_tool(str(next_decision.get("tool", "")))
            if not tool:
                mentioned_tools = [
                    tool_id
                    for tool_id in self._requestable_tool_ids
                    if self._tool_is_spoken(tool_id, str(next_decision.get("speech", "")))
                ]
                tool = mentioned_tools[0] if mentioned_tools else ""
            preferred_tool = self._preferred_sequence_tool()
            candidate_tool = preferred_tool if preferred_tool and (not tool or tool not in self._current_sequence_tools()) else tool
            if (
                candidate_tool
                and candidate_tool in self._requestable_tool_ids
                and candidate_tool not in self._held_tools
                and candidate_tool not in self._mayo_tools
                and candidate_tool != self._pending_tool
            ):
                if self._held_capacity_remaining() <= 0:
                    next_decision["action"] = "place_on_mayo"
                    next_decision["tool"] = self._held_tools[0] if self._held_tools else ""
                    next_decision["request_mode"] = "none"
                    next_decision["speech"] = ""
                    next_decision["reason_code"] = "free_hand_before_tool_mentioned_in_talk"
                    return next_decision
                mode = self._weighted_request_mode(str(next_decision.get("request_mode", "voice")))
                next_decision["action"] = "request_tool"
                next_decision["tool"] = candidate_tool
                next_decision["request_mode"] = mode
                next_decision["speech"] = self._request_speech(
                    candidate_tool,
                    str(next_decision.get("speech", "")),
                    mode,
                )
                next_decision["reason_code"] = "tool_mention_converted_to_request"
                return next_decision
            next_decision["tool"] = ""
            next_decision["request_mode"] = "none"
            if action == "wait":
                if self._rng.random() < self._small_talk_chance:
                    next_decision["action"] = "small_talk"
                    next_decision["speech"] = self._small_talk_line()
                    next_decision["reason_code"] = "stochastic_small_talk"
                else:
                    next_decision["speech"] = ""
                return next_decision
            if not str(next_decision.get("speech", "")).strip() or tool:
                next_decision["speech"] = self._small_talk_line()
            return next_decision
        if action == "wait" and self._rng.random() < self._small_talk_chance:
            next_decision["action"] = "small_talk"
            next_decision["tool"] = ""
            next_decision["request_mode"] = "none"
            next_decision["speech"] = self._small_talk_line()
            next_decision["reason_code"] = "stochastic_small_talk"
        return next_decision

    def _publish_actor_event(
        self,
        event_type: str,
        tool: str,
        speech: str,
        *,
        ready_for_handover: bool = False,
        ready_for_retrieval: bool = False,
        phase_id: str | None = None,
    ) -> None:
        msg = SurgeonActorEvent()
        msg.stamp = self._stamp()
        msg.event_type = event_type
        msg.tool_id = tool
        msg.phase_id = phase_id or self._current_phase_id
        msg.voice_text = speech
        msg.note = "llm_surgeon_actor"
        msg.ready_for_handover = bool(ready_for_handover)
        msg.ready_for_retrieval = bool(ready_for_retrieval)
        msg.override = False
        self._actor_event_pub.publish(msg)

    def _publish_voice(self, speech: str) -> None:
        text = str(speech or "").strip()
        if not text:
            return
        stamp = self._stamp()
        msg = SpeechUtterance()
        msg.stamp = stamp
        msg.start_stamp = stamp
        msg.end_stamp = stamp
        msg.utterance_id = f"actor-{uuid.uuid4().hex}"
        msg.text = text
        msg.is_final = True
        msg.has_confidence = True
        msg.confidence = 1.0
        msg.speaker_role = "surgeon"
        msg.language = "und"
        msg.source = "llm_surgeon_actor"
        self._speech_pub.publish(msg)

    def _publish_state(
        self,
        intent: str,
        requested_tool: str,
        speech: str,
        ready_for_handover: bool,
        ready_for_retrieval: bool,
        scene_note: str,
    ) -> None:
        msg = SurgeonState()
        msg.stamp = self._stamp()
        msg.procedure_id = self._spec.procedure_id
        msg.phase_id = self._current_phase_id
        msg.intent = intent
        msg.requested_tool = requested_tool
        msg.ready_for_handover = bool(ready_for_handover)
        msg.ready_for_retrieval = bool(ready_for_retrieval)
        msg.scripted = False
        msg.voice_text = speech
        msg.scene_note = scene_note
        self._state_pub.publish(msg)

    def _publish_overlay(self) -> None:
        overlay = {
            "hand": self._last_hand_overlay,
            "mayo": list(self._mayo_tools),
            "speech": self._last_speech,
            "field_event": list(self._field_event_lines),
            "held_tool": self._primary_held_tool(),
            "held_tools": list(self._held_tools),
        }
        msg = String()
        msg.data = json.dumps(overlay, separators=(",", ":"), sort_keys=True)
        self._overlay_pub.publish(msg)

    def _publish_llm_decision(
        self,
        *,
        raw_json: str,
        accepted: bool,
        reject_reason: str,
        action: str,
        tool: str,
        request_mode: str,
        speech: str,
        hidden_phase: str,
        latency_sec: float,
    ) -> None:
        msg = SurgeonLLMDecision()
        msg.stamp = self._stamp()
        msg.model_id = self._model_id
        msg.raw_json = raw_json
        msg.accepted = bool(accepted)
        msg.reject_reason = reject_reason
        msg.action = action
        msg.tool = tool
        msg.request_mode = request_mode
        msg.speech = speech
        msg.hidden_phase = hidden_phase
        msg.latency_sec = float(latency_sec)
        msg.seed = int(self._seed)
        msg.overlay_json = json.dumps(
            {
                "hand": self._last_hand_overlay,
                "mayo": list(self._mayo_tools),
                "field_event": list(self._field_event_lines),
                "held_tool": self._primary_held_tool(),
                "held_tools": list(self._held_tools),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        self._decision_pub.publish(msg)

    def _record_event(self, event_type: str, tool_id: str, detail: dict[str, Any]) -> None:
        self._recent_events.append(
            {
                "t": event_type,
                "tool": tool_id,
                "phase": self._current_phase_id,
                "detail": detail,
                "at": round(self._now(), 2),
            }
        )


def main() -> None:
    rclpy.init()
    node = LLMSurgeonActorNode()
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
