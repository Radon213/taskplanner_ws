from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET


PACKAGE_DIR = Path(__file__).parents[1]
SRC_DIR = PACKAGE_DIR.parents[0]
CPP_PATH = PACKAGE_DIR / "src" / "taskplanner_bt_nodes.cpp"
TREE_PATH = (
    SRC_DIR
    / "taskplanner_bt_trees"
    / "behavior"
    / "surgical_assist_v1.xml"
)
DT_NODE_PATH = SRC_DIR / "or_digital_twin" / "or_digital_twin" / "node.py"


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


def _named_sequence(root: ET.Element, name: str) -> ET.Element:
    return next(
        element
        for element in root.iter("Sequence")
        if element.attrib.get("name") == name
    )


def test_bt_owns_visual_request_policy_and_runtime_gate() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    runtime = _section(source, "class IsProcedureActive", "class IsPhaseCertain")
    implicit = _section(source, "class HasImplicitRequest", "class NeedsRecovery")

    assert 'execution_state == "running"' in runtime
    assert 'execution_state == "finishing"' in runtime
    assert "confidence < kImplicitGestureMinConfidence" in implicit
    assert "stability_sec < kImplicitGestureMinStabilitySec" in implicit
    assert '"open_receive"' in implicit
    assert "kPreparationMinConfidence" in implicit
    assert "kPreparationMinStabilitySec" in implicit
    assert '"prepositioned_right"' in implicit


def test_dt_handover_value_is_only_a_hint_to_bt() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    load_world = _section(source, "class LoadWorldState", "class IsProcedureActive")
    explicit_intents = _section(
        source, "bool isExplicitSurgeonIntent", "bool isAvailableStatus"
    )

    assert '"state.handover_hint"' in load_world
    assert (
        '"action.guard.handover_allowed", static_cast<bool>(msg.handover_allowed)'
        not in load_world
    )
    assert "extend_hand_for_handover" not in explicit_intents


def test_recovery_policy_uses_verified_facts_in_bt() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    recovery = _section(
        source, "RecoveryPolicyCandidate selectRecoveryPolicyCandidate", "bool hasRecoveryContext"
    )

    assert '"future_use_expected"' in recovery
    assert '"mayo_recovery_confidence"' in recovery
    assert '"mayo_recovery_stability_sec"' in recovery
    assert '"completion_cleanup"' in recovery
    assert "mayo_tools.size() > 2" in recovery


def test_tree_priority_keeps_visual_request_and_recovery_in_bt() -> None:
    root = ET.parse(TREE_PATH).getroot()
    xml = ET.tostring(root, encoding="unicode")

    explicit_at = xml.index('name="ExplicitRequest"')
    implicit_at = xml.index('name="VisualImplicitRequest"')
    recovery_at = xml.index('name="Recovery"')
    anticipatory_at = xml.index('name="AnticipatoryPreparation"')
    safety_at = xml.index('name="Safety"')
    assert explicit_at < implicit_at < recovery_at < anticipatory_at < safety_at


def test_preparation_is_reversible_and_separate_from_handover_policy() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    xml = ET.tostring(root, encoding="unicode")
    preparation = _section(
        source, "class CanPreposition", "class SelectExplicitTool"
    )
    selection = _section(
        source, "class SelectExpectedTool", "class SelectRecoveryTool"
    )
    recovery = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )

    assert "kPreparationMinConfidence = 0.65" in source
    assert "kPreparationMinStabilitySec = 0.3" in source
    assert "hasBlockingSafetyFlag(*this)" in preparation
    assert '"robot.right_hand_tool"' in preparation
    assert "kPreparationMinConfidence" in selection
    assert "kPreparationMinStabilitySec" in selection
    assert '"stable_prediction_replacement"' in recovery

    anticipatory = next(
        element
        for element in root.iter("Sequence")
        if element.attrib.get("name") == "AnticipatoryPreparation"
    )
    anticipatory_xml = ET.tostring(anticipatory, encoding="unicode")
    assert "<CanPreposition" in anticipatory_xml
    assert "<CanHandover" not in anticipatory_xml
    assert "<IsPhaseCertain" not in anticipatory_xml
    assert 'name="AnticipatoryPreparation"' in xml


def test_tool_unspecified_visual_request_prefers_prepositioned_prediction() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    implicit_condition = _section(
        source, "class HasImplicitRequest", "class NeedsRecovery"
    )
    implicit_selection = _section(
        source, "class SelectImplicitTool", "class SelectExpectedTool"
    )
    action_guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )

    assert "prepared_instance" in implicit_condition
    assert (
        'toolLifecycle(*this, prepared_instance) == "prepositioned_right"'
        in implicit_condition
    )
    assert "prediction_confidence < kPreparationMinConfidence" in implicit_condition
    assert "prediction_stability_sec < kPreparationMinStabilitySec" in implicit_condition

    preposition_at = implicit_selection.index("robot.prepositioned_tool")
    prediction_at = implicit_selection.index("prediction.tool")
    assert preposition_at < prediction_at
    assert '"implicit_visual_preposition_match"' in implicit_selection
    assert '"implicit_visual_prediction_fallback"' in implicit_selection
    assert '"surgeon_owned"' not in implicit_selection

    assert "implicit_candidate_supported" in action_guard
    assert "prediction_confidence >= kPreparationMinConfidence" in action_guard
    assert "prediction_stability_sec >= kPreparationMinStabilitySec" in action_guard


def test_reducer_records_evidence_without_creating_action_obligations() -> None:
    source = DT_NODE_PATH.read_text(encoding="utf-8")
    implicit = _section(
        source, "def _handle_vlm_implicit_request", "def _vlm_tool_rows"
    )
    prediction = _section(
        source, "def _fused_tool_prediction", "def _handle_vlm_tool_prediction"
    )
    prediction_handler = _section(
        source, "def _handle_vlm_tool_prediction", "def _on_vlm_result"
    )
    vlm_result = _section(source, "def _on_vlm_result", "def _on_observation")

    assert "SurgeonRequest()" not in implicit
    assert "update_surgeon_request" not in implicit
    assert "_tool_available_for_prediction" not in prediction
    assert "predicted_tool_not_available_for_preposition" not in prediction_handler
    assert "interrupt_visible" not in vlm_result
    assert "record_mayo_policy_evidence" in vlm_result
    assert "promote_mayo_recovery_from_vlm" not in vlm_result


def test_prediction_replacement_returns_old_preposition_before_preparing_new() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    replacement = _section(
        source,
        "bool stablePredictionReplacesPreposition",
        "struct RecoveryPolicyCandidate",
    )
    recovery_context = _section(
        source, "bool hasRecoveryContext", "bool hasActiveRobotTask"
    )
    expected_selection = _section(
        source, "class SelectExpectedTool", "class SelectRecoveryTool"
    )
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    assert "predicted_tool != prepositioned_tool" in replacement
    assert "replacement_instance = findAnticipatoryInstanceForType" in replacement
    assert "replacement_available" in replacement
    assert "replacement_available = !replacement_instance.empty()" in replacement
    assert "confidence >= kPreparationMinConfidence" in replacement
    assert "stability_sec >= kPreparationMinStabilitySec" in replacement
    assert "stablePredictionReplacesPreposition(node)" in recovery_context

    replacement_at = recovery_selection.index(
        "if (stablePredictionReplacesPreposition(*this))"
    )
    generic_recovery_at = recovery_selection.index(
        "const auto policy_candidate = selectRecoveryPolicyCandidate(*this)"
    )
    assert replacement_at < generic_recovery_at
    assert '"prepositioned_right"' in recovery_selection[replacement_at:]
    assert '"return_unused_preposition"' in recovery_selection[replacement_at:]
    assert '"stable_prediction_replacement"' in recovery_selection[replacement_at:]

    assert "hasRecoveryContext(*this)" in expected_selection
    assert (
        expected_selection.index("hasRecoveryContext(*this)")
        < expected_selection.index("kPreparationMinConfidence")
    )
    assert 'next_required_transition == "return_unused_preposition"' in command
    assert 'writeBlackboard(*this, "bt.arm", std::string("right"))' in command
    assert (
        'writeBlackboard(*this, "bt.action", '
        'std::string("return_unused_preposition"))'
    ) in command

    decision_root = next(root.iter("Fallback"))
    branch_names = [
        child.attrib.get("name")
        for child in decision_root
        if child.tag == "Sequence"
    ]
    assert branch_names.index("Recovery") < branch_names.index(
        "AnticipatoryPreparation"
    )


def test_mayo_reuse_preparation_is_future_scoped_and_returns_to_origin() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    candidate_guard = _section(
        source,
        "bool toolIsAnticipatoryCandidate",
        "bool stablePredictionReplacesPreposition",
    )
    expected_selection = _section(
        source, "class SelectExpectedTool", "class SelectRecoveryTool"
    )
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    assert 'lifecycle == "mayo_reuse"' in candidate_guard
    assert '"future_use_expected"' in candidate_guard
    assert "return future_use_expected" in candidate_guard
    assert "findAnticipatoryInstanceForType" in candidate_guard
    assert "toolIsAnticipatoryCandidate(node, tool_id)" in candidate_guard
    assert "findAnticipatoryInstanceForType" in expected_selection
    assert 'const bool from_mayo_reuse = lifecycle == "mayo_reuse"' in command
    assert 'std::string("mayo_reuse_zone")' in command
    assert '"bt.source_location_id", prepare_source_location' in command
    assert '"bt.source_location_type", prepare_source_type' in command
    assert "stable next-tool prediction selected a Mayo reuse tool" in command
    assert '"preposition_origin_location"' in recovery_selection
    assert '"preposition_origin_type"' in recovery_selection
    assert '"preposition_origin_location"' in command
    assert '"preposition_origin_type"' in command
    assert "return to its source" in command


def test_stale_prediction_releases_reversible_preposition_in_bt() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    needs_recovery = _section(
        source, "class NeedsRecovery", "class IsToolAvailable"
    )
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    assert "kPreparationUnsupportedGraceSec = 0.8" in source
    assert "kPreparationMaxDwellSec = 6.0" in source
    assert "kPreparationStrongConfidence = 0.85" in source
    assert "kPreparationStrongMaxDwellSec = 30.0" in source
    assert '"robot.prepositioned_instance"' in needs_recovery
    assert '"prediction.tool"' in needs_recovery
    assert '"policy.expired_preposition_instance"' in needs_recovery
    assert '"policy.expired_preposition_reason"' in needs_recovery
    assert '"preposition_dwell_expired"' in needs_recovery
    assert "stablePredictionReplacesPreposition(*this)" in needs_recovery
    assert '"prediction_evidence_expired"' in recovery_selection
    assert '"policy.expired_preposition_reason"' in recovery_selection
    assert '"return_unused_preposition"' in recovery_selection
    assert "prediction evidence expired" in command
    assert "speculative preparation dwell expired" in command


def test_returned_preposition_rearms_only_after_continuous_absence() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    expected_selection = _section(
        source, "class SelectExpectedTool", "class SelectRecoveryTool"
    )
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )
    explicit_selection = _section(
        source, "class SelectExplicitTool", "class SelectImplicitTool"
    )

    assert "kPreparationRetryCooldownSec = 5.0" in source
    assert '"policy.preposition_cooldown_tool"' in expected_selection
    assert (
        '"policy.preposition_cooldown_clear_since_sec"'
        in expected_selection
    )
    assert "cooldown_tool == predicted_tool" in expected_selection
    assert "cooldown_clear_since_sec <= 0.0" in expected_selection
    assert (
        "now_sec - cooldown_clear_since_sec >="
        in expected_selection
    )
    assert 'policy_transition == "return_unused_preposition"' in recovery_selection
    assert '"policy.preposition_cooldown_tool"' in recovery_selection
    assert (
        '"policy.preposition_cooldown_clear_since_sec"'
        in recovery_selection
    )
    assert "preposition_cooldown" not in explicit_selection


def test_explicit_request_preempts_visual_and_anticipatory_paths() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    decision_root = next(root.iter("Fallback"))
    branch_names = [
        child.attrib.get("name")
        for child in decision_root
        if child.tag == "Sequence"
    ]

    assert branch_names[0] == "ExplicitRequest"
    assert branch_names.index("ExplicitRequest") < branch_names.index(
        "VisualImplicitRequest"
    )
    assert branch_names.index("ExplicitRequest") < branch_names.index(
        "AnticipatoryPreparation"
    )

    visual = ET.tostring(
        _named_sequence(root, "VisualImplicitRequest"), encoding="unicode"
    )
    anticipatory = ET.tostring(
        _named_sequence(root, "AnticipatoryPreparation"), encoding="unicode"
    )
    assert "<Inverter" in visual and "<HasExplicitRequest" in visual
    assert "<Inverter" in anticipatory and "<HasExplicitRequest" in anticipatory

    expected_selection = _section(
        source, "class SelectExpectedTool", "class SelectRecoveryTool"
    )
    assert "!explicit_request.empty()" in expected_selection
    assert "!surgeon_request.empty()" in expected_selection


def test_explicit_request_uses_matching_preposition_before_another_instance() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    explicit_selection = _section(
        source, "class SelectExplicitTool", "class SelectImplicitTool"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    matching_preposition_at = explicit_selection.index(
        'std::string("explicit_request_preposition_match")'
    )
    requested_instance_at = explicit_selection.index(
        "if (!surgeon_instance.empty()"
    )
    assert matching_preposition_at < requested_instance_at
    assert '"robot.right_hand_tool"' in explicit_selection
    assert '"robot.right_hand_instance"' in explicit_selection
    assert "toolMatchesType" in explicit_selection
    assert 'writeBlackboard(*this, "selected.tool", right_hand_instance)' in (
        explicit_selection
    )
    assert "right_hand_instance == selected_tool" in command
    assert 'std::string("direct_handover")' in command


def test_dispatch_dedupe_does_not_rearm_on_phase_jitter() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )
    signature = _section(
        dispatch, "static std::string makeSignature", "return stream.str();"
    )

    assert '"phase.id"' not in dispatch
    assert "phase_id" not in signature
    assert "selected_tool_lifecycle" in signature
    assert "right_hand_instance" in signature
    assert "request_generation" in signature


def test_visual_implicit_request_remains_evidence_until_bt_policy_accepts_it() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    condition = _section(
        source, "class HasImplicitRequest", "class NeedsRecovery"
    )
    selection = _section(
        source, "class SelectImplicitTool", "class SelectExpectedTool"
    )
    visual = ET.tostring(
        _named_sequence(root, "VisualImplicitRequest"), encoding="unicode"
    )

    assert "writeBlackboard" not in condition
    assert "SurgeonRequest" not in condition
    assert '"request.explicit_tool"' not in condition
    assert '"request.surgeon_tool"' not in condition
    assert '"request.explicit_tool"' not in selection
    assert '"request.surgeon_tool"' not in selection
    assert '"selected.tool"' in selection
    assert '"implicit_visual_request"' in selection

    assert "<HasImplicitRequest" in visual
    assert "<SelectImplicitTool" in visual
    assert "<ApplyActionGuard" in visual
    assert "<CanHandover" in visual
    assert "<SelectExplicitTool" not in visual


def test_terminal_execution_states_cannot_reach_physical_commands() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    active_gate = _section(
        source, "class IsProcedureActive", "class IsPhaseCertain"
    )
    tick = _named_sequence(root, "TaskplannerAssistTick")
    tick_children = list(tick)

    assert 'execution_state == "running"' in active_gate
    assert 'execution_state == "finishing"' in active_gate
    assert '"stopped"' not in active_gate
    assert '"completed"' not in active_gate
    assert '"complete"' not in active_gate

    active_gate_at = next(
        index
        for index, child in enumerate(tick_children)
        if child.tag == "IsProcedureActive"
    )
    decision_root_at = next(
        index for index, child in enumerate(tick_children) if child.tag == "Fallback"
    )
    assert active_gate_at < decision_root_at

    decision_root = tick_children[decision_root_at]
    assert len(list(tick.iter("PublishSkillCommand"))) == len(
        list(decision_root.iter("PublishSkillCommand"))
    )
    assert not list(_named_sequence(root, "Safety").iter("PublishSkillCommand"))
    assert not list(_named_sequence(root, "IdleObserve").iter("PublishSkillCommand"))
