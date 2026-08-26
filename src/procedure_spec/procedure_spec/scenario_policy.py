"""Single query boundary for authored scenario choices.

This module deliberately does not contain physical safety logic.  A positive
scenario decision only says that the active procedure wants a capability; the
runtime still has to pass controller, Service admission, execution-state,
freshness, idempotency, and preflight interlocks.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Iterable

from .models import (
    ProcedureBundle,
    ScenarioPolicySpec,
    ScenarioRuntimeRequirements,
)


PRESERVED_RUNTIME_INTERLOCKS = frozenset(
    {
        "controller_admission",
        "service_admission",
        "execution_state",
        "stopped_state_route_change",
        "preflight_ack",
        "freshness",
        "idempotency",
    }
)

SCENARIO_RUNTIME_REQUIREMENT_KEYS = frozenset(
    field.name for field in fields(ScenarioRuntimeRequirements)
)

# Compatibility defaults live here, not in launch/preflight/orchestrator code.
# Prompt bundles can override the complete record under
# ``scenario_policy.runtime_requirements`` without changing Python consumers.
DEFAULT_SCENARIO_RUNTIME_REQUIREMENTS = ScenarioRuntimeRequirements()
_COMPATIBILITY_RUNTIME_REQUIREMENTS = {
    "thyroidectomy": ScenarioRuntimeRequirements(
        procedure_type="thyroidectomy",
        tool_handover_action_required=True,
        retraction_service_required=True,
        bed_robot_status_required=False,
        image_vlm_enabled=True,
        dialogue_vlm_enabled=True,
        perception_enabled=True,
        rfdetr_tool_observations_required=False,
        voice_intent_resolver_enabled=True,
        surgeon_actor_enabled=True,
        phase_inference_enabled=True,
        retraction_workflow_state_enforced=True,
        legacy_raw_retractor_voice_enabled=False,
    ),
    "thyroidectomy_demo": ScenarioRuntimeRequirements(
        procedure_type="thyroidectomy",
        tool_handover_action_required=True,
        retraction_service_required=True,
        bed_robot_status_required=False,
        image_vlm_enabled=True,
        dialogue_vlm_enabled=True,
        perception_enabled=True,
        rfdetr_tool_observations_required=True,
        voice_intent_resolver_enabled=True,
        surgeon_actor_enabled=True,
        phase_inference_enabled=True,
        retraction_workflow_state_enforced=False,
        legacy_raw_retractor_voice_enabled=False,
    ),
    "nephrectomy": ScenarioRuntimeRequirements(
        procedure_type="nephrectomy",
        tool_handover_action_required=True,
        retraction_service_required=True,
        bed_robot_status_required=False,
        image_vlm_enabled=True,
        dialogue_vlm_enabled=True,
        perception_enabled=True,
        rfdetr_tool_observations_required=False,
        voice_intent_resolver_enabled=True,
        surgeon_actor_enabled=True,
        phase_inference_enabled=True,
        retraction_workflow_state_enforced=True,
        legacy_raw_retractor_voice_enabled=False,
    ),
    "inguinal_hernia_repair_demo": ScenarioRuntimeRequirements(
        procedure_type="inguinal_hernia_repair",
        tool_handover_action_required=False,
        retraction_service_required=True,
        bed_robot_status_required=False,
        image_vlm_enabled=False,
        dialogue_vlm_enabled=True,
        perception_enabled=False,
        rfdetr_tool_observations_required=False,
        voice_intent_resolver_enabled=True,
        surgeon_actor_enabled=False,
        phase_inference_enabled=False,
        retraction_workflow_state_enforced=False,
        legacy_raw_retractor_voice_enabled=False,
    ),
}


def resolve_scenario_runtime_requirements(
    bundle: ProcedureBundle,
) -> ScenarioRuntimeRequirements:
    """Resolve authored runtime choices with one legacy-compatible fallback."""

    authored = (
        bundle.scenario_policy.runtime_requirements
        if bundle.scenario_policy is not None
        else None
    )
    if authored is not None:
        return authored
    return _COMPATIBILITY_RUNTIME_REQUIREMENTS.get(
        str(bundle.procedure_id).strip().casefold(),
        DEFAULT_SCENARIO_RUNTIME_REQUIREMENTS,
    )


@dataclass(frozen=True, slots=True)
class ScenarioPolicyDecision:
    allowed: bool
    reason: str = ""


class ScenarioPolicy:
    """Read-only policy facade assembled from one procedure bundle."""

    def __init__(self, bundle: ProcedureBundle):
        self._bundle = bundle
        self._requestable_instrument_ids = frozenset(
            instrument.id for instrument in bundle.instruments if instrument.requestable
        )
        self._groups = {
            group.id: group
            for group in (
                bundle.bed_robot_arm_groups.groups
                if bundle.bed_robot_arm_groups is not None
                else []
            )
        }
        self._spec = bundle.scenario_policy or self._legacy_policy(bundle)
        self._runtime_requirements = resolve_scenario_runtime_requirements(bundle)

    @staticmethod
    def _legacy_policy(bundle: ProcedureBundle) -> ScenarioPolicySpec:
        action = bundle.action_guard
        humanoid = bundle.humanoid_policy
        return ScenarioPolicySpec(
            handover_arm=humanoid.handover_arm if humanoid is not None else "right",
            recovery_arm=humanoid.recovery_arm if humanoid is not None else "left",
            require_cleaning_after_surgeon_use=(
                humanoid.require_cleaning_after_surgeon_use
                if humanoid is not None
                else True
            ),
            allow_anticipatory_hold=(
                humanoid.allow_anticipatory_hold if humanoid is not None else True
            ),
            voice_override_preempts_preposition=(
                humanoid.voice_override_preempts_preposition
                if humanoid is not None
                else True
            ),
            allow_prepositioning_when_uncertain=(
                bool(getattr(action, "allow_prepositioning_when_uncertain", False))
            ),
            explicit_request_priority=(
                bool(getattr(action, "explicit_request_priority", True))
            ),
            unused_preposition_destination=(
                "mayo"
                if humanoid is None or humanoid.return_unused_preposition_to_mayo
                else "retain"
            ),
        )

    @property
    def spec(self) -> ScenarioPolicySpec:
        return self._spec

    @property
    def runtime_requirements(self) -> ScenarioRuntimeRequirements:
        return self._runtime_requirements

    @property
    def requestable_instrument_ids(self) -> frozenset[str]:
        return self._requestable_instrument_ids

    def check_instrument_request(self, instrument_id: str) -> ScenarioPolicyDecision:
        normalized = str(instrument_id or "").strip()
        if normalized in self._requestable_instrument_ids:
            return ScenarioPolicyDecision(True)
        return ScenarioPolicyDecision(
            False,
            f"instrument '{normalized}' is not requestable in this scenario",
        )

    def check_group_enabled(self, group_id: str) -> ScenarioPolicyDecision:
        normalized = str(group_id or "").strip()
        group = self._groups.get(normalized)
        if group is None:
            return ScenarioPolicyDecision(
                False,
                f"group '{normalized}' is not configured for this scenario",
            )
        if not group.enabled:
            return ScenarioPolicyDecision(
                False,
                f"group '{normalized}' is disabled for this scenario",
            )
        return ScenarioPolicyDecision(True)

    def check_group_operation(
        self,
        group_id: str,
        operation_candidates: str | Iterable[str],
    ) -> ScenarioPolicyDecision:
        enabled = self.check_group_enabled(group_id)
        if not enabled.allowed:
            return enabled
        candidates = (
            {operation_candidates}
            if isinstance(operation_candidates, str)
            else {str(item) for item in operation_candidates}
        )
        group = self._groups[str(group_id).strip()]
        if candidates.intersection(group.allowed_operations):
            return ScenarioPolicyDecision(True)
        requested = ", ".join(sorted(candidates)) or "<empty>"
        return ScenarioPolicyDecision(
            False,
            f"operation '{requested}' is not allowed for group '{group_id}' by scenario policy",
        )

    def check_group_voice_command(
        self,
        group_id: str,
        command: str,
    ) -> ScenarioPolicyDecision:
        enabled = self.check_group_enabled(group_id)
        if not enabled.allowed:
            return enabled
        group = self._groups[str(group_id).strip()]
        # Legacy bundles did not author a voice-command fence. Preserve that
        # compatibility, while an explicitly configured empty list means deny
        # all and can be changed without touching Python/C++ code.
        if not group.voice_command_policy_configured:
            return ScenarioPolicyDecision(True)
        normalized = str(command or "").strip()
        if normalized in set(group.allowed_voice_commands):
            return ScenarioPolicyDecision(True)
        return ScenarioPolicyDecision(
            False,
            f"voice command '{normalized}' is disabled for group '{group_id}' by scenario policy",
        )

    def allows_prepositioning(self, *, phase_uncertain: bool) -> bool:
        return bool(
            not phase_uncertain or self._spec.allow_prepositioning_when_uncertain
        )

    @property
    def unused_preposition_destination(self) -> str:
        return self._spec.unused_preposition_destination
