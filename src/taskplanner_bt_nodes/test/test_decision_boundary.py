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
NODE_CONFIG_PATH = PACKAGE_DIR / "config" / "taskplanner_bt_nodes.yaml"
CMAKE_PATH = PACKAGE_DIR / "CMakeLists.txt"
DT_NODE_PATH = SRC_DIR / "or_digital_twin" / "or_digital_twin" / "node.py"


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


def _named_sequence(root: ET.Element, name: str) -> ET.Element:
    return next(
        element
        for element in root.iter("Sequence")
        if element.attrib.get("name") == name
    )


def test_bt_rechecks_direct_hand_signal_policy_and_runtime_gate() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    runtime = _section(source, "class IsProcedureActive", "class HasExplicitRequest")
    implicit = _section(source, "class HasImplicitRequest", "class NeedsRecovery")
    direct_hand = _section(
        source, "bool directHandSignalActive", "bool isExactRightHandPreposition"
    )
    exact_preposition = _section(
        source, "bool isExactRightHandPreposition", "std::string findActiveInstanceForType"
    )

    assert 'execution_state == "running"' in runtime
    assert 'execution_state == "finishing"' in runtime
    assert 'execution_state == "running"' in direct_hand
    assert "implicit_tool.empty()" in direct_hand
    assert "confidence >= kHandHandoverMinConfidence" in direct_hand
    assert "stability_sec >= kHandHandoverMinStabilitySec" in direct_hand
    assert '"open_receive"' in direct_hand
    assert "directHandSignalActive(*this)" in implicit
    assert "kImplicitPredictionMinConfidence" in implicit
    assert "kImplicitPredictionMinStabilitySec" in implicit
    assert '"prepositioned_right"' in exact_preposition
    assert "kHandHandoverMinConfidence = 0.50" in source
    assert "kHandHandoverMinStabilitySec = 0.30" in source
    assert "kImplicitPredictionMinConfidence = 0.55" in source
    assert "kImplicitPredictionMinStabilitySec = 0.30" in source


def test_exact_right_hand_preposition_predicate_checks_the_full_identity() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    exact = _section(
        source, "bool isExactRightHandPreposition", "std::string findActiveInstanceForType"
    )
    implicit_condition = _section(
        source, "class HasImplicitRequest", "class NeedsRecovery"
    )
    implicit_selection = _section(
        source, "class SelectImplicitTool", "class SelectExpectedTool"
    )
    can_handover = _section(source, "class CanHandover", "class CanPreposition")
    action_guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    assert "prepositioned_instance != right_hand_instance" in exact
    assert "selected_instance != prepositioned_instance" in exact
    assert "!toolIsActive(node, prepositioned_instance)" in exact
    assert "!toolMatchesType(node, prepositioned_instance, prepositioned_tool)" in exact
    assert 'toolLifecycle(node, prepositioned_instance) != "prepositioned_right"' in exact
    assert 'owner == "robot_right_hand"' in exact
    assert "const bool canonical_right_hand_location" in exact
    assert 'location_id == "robot_right_hand"' in exact
    assert 'location_type == "robot_right_hand"' in exact
    assert '(location_id == "robot" && location_type == "robot")' in exact
    assert "return owner == \"robot_right_hand\" && canonical_right_hand_location;" in exact

    assert "isExactRightHandPreposition(*this)" in implicit_condition
    assert "isExactRightHandPreposition(*this)" in implicit_selection
    assert "isExactRightHandPreposition(*this, selected_tool)" in can_handover
    assert "isExactRightHandPreposition(*this, selected_tool)" in action_guard
    assert "isExactRightHandPreposition(*this, selected_tool)" in command


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


def test_bt_does_not_infer_a_fixed_surgeon_hand_capacity() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    can_handover = _section(
        source, "class CanHandover", "class CanPreposition"
    )
    action_guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )

    assert "kMaxSurgeonHeldTools" not in source
    assert "surgeonHeldToolCount" not in source
    assert "toolOccupiesSurgeonHand" not in source

    # Removing the stale virtual hand-count veto must not weaken the remaining
    # execution, safety, availability, lifecycle, and request-evidence gates.
    assert "hasBlockingSafetyFlag" in can_handover
    assert "hasActiveRobotTask" in can_handover
    assert "toolIsActive" in can_handover
    assert '"action.guard.handover_allowed"' in can_handover
    assert "blocked_by_safety" in action_guard
    assert "active_task_id.empty()" in action_guard
    assert "usable_lifecycle" in action_guard
    assert "request_ready" in action_guard


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


def test_tree_priority_keeps_direct_hand_signal_and_recovery_in_bt() -> None:
    root = ET.parse(TREE_PATH).getroot()
    xml = ET.tostring(root, encoding="unicode")

    explicit_at = xml.index('name="ExplicitRequest"')
    implicit_at = xml.index('name="DirectHandHandoverSignal"')
    recovery_at = xml.index('name="Recovery"')
    anticipatory_at = xml.index('name="AnticipatoryPreparation"')
    idle_at = xml.index('name="IdleObserve"')
    assert explicit_at < implicit_at < recovery_at < anticipatory_at < idle_at


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
    assert '"system_top_replacement_stable_2s"' in recovery

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


def test_tool_agnostic_hand_signal_prefers_prepositioned_then_prediction() -> None:
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
    exact_preposition = _section(
        source, "bool isExactRightHandPreposition", "std::string findActiveInstanceForType"
    )

    assert "isExactRightHandPreposition(*this)" in implicit_condition
    assert 'toolLifecycle(node, prepositioned_instance) != "prepositioned_right"' in (
        exact_preposition
    )
    assert "prediction_confidence < kImplicitPredictionMinConfidence" in implicit_condition
    assert "prediction_stability_sec < kImplicitPredictionMinStabilitySec" in implicit_condition

    preposition_at = implicit_selection.index("robot.prepositioned_tool")
    prediction_at = implicit_selection.index("prediction.tool")
    assert preposition_at < prediction_at
    assert '"hand_signal_preposition_match"' in implicit_selection
    assert '"hand_signal_prediction_fallback"' in implicit_selection
    assert '"surgeon_owned"' not in implicit_selection
    assert '"request.implicit_tool"' not in implicit_selection

    assert "implicit_candidate_supported" in action_guard
    assert "directHandSignalActive(*this)" in action_guard
    assert "prediction_confidence >= kImplicitPredictionMinConfidence" in action_guard
    assert "prediction_stability_sec >= kImplicitPredictionMinStabilitySec" in action_guard
    assert "implicit_tool == predicted_tool" not in action_guard


def test_direct_hand_signal_can_handover_the_robot_held_tool() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    implicit_condition = _section(
        source, "class HasImplicitRequest", "class NeedsRecovery"
    )
    implicit_selection = _section(
        source, "class SelectImplicitTool", "class SelectExpectedTool"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    # The legacy field remains in WorldState for ABI compatibility, but the
    # direct hand reducer leaves it empty and the BT never reads it as a tool.
    assert "implicit_tool != predicted_tool" not in implicit_condition
    assert 'readBlackboard(*this, "request.implicit_tool", tool_type)' not in (
        implicit_selection
    )
    assert "isExactRightHandPreposition(*this)" in implicit_selection
    assert '{"home_rack", "returned_home", "mayo_reuse"}' in implicit_selection

    # If that matching instance is already in the humanoid right hand, the
    # configured operation is a direct robot-to-surgeon handover.
    assert "isExactRightHandPreposition(*this, selected_tool)" in command
    assert 'std::string("direct_handover")' in command
    assert 'std::string("robot_right_hand")' in command
    assert 'std::string("surgeon_receive_zone")' in command


def test_reducer_records_evidence_without_creating_action_obligations() -> None:
    source = DT_NODE_PATH.read_text(encoding="utf-8")
    direct_hand = _section(
        source, "def _apply_hand_handover_update", "def _withdraw_hand_handover_evidence"
    )
    prediction = _section(
        source, "def _fused_tool_prediction", "def _handle_vlm_tool_prediction"
    )
    prediction_handler = _section(
        source, "def _handle_vlm_tool_prediction", "def _on_vlm_result"
    )
    vlm_result = _section(source, "def _on_vlm_result", "def _on_observation")

    assert "SurgeonRequest()" not in direct_hand
    assert "update_surgeon_request" not in direct_hand
    assert 'state.implicit_request_tool = ""' in direct_hand
    assert 'input_type="hand_handover_signal"' in direct_hand
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
        "bool systemTopPredictionReplacesPreposition",
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
    assert 'readBlackboard(node, "prediction.confidence"' not in replacement
    assert "kSystemTopReplacementMinStabilitySec = 2.0" in source
    assert "stability_sec >= kSystemTopReplacementMinStabilitySec" in replacement
    assert "systemTopPredictionReplacesPreposition(node)" in recovery_context

    replacement_at = recovery_selection.index(
        "if (systemTopPredictionReplacesPreposition(*this))"
    )
    generic_recovery_at = recovery_selection.index(
        "const auto policy_candidate = selectRecoveryPolicyCandidate(*this)"
    )
    assert replacement_at < generic_recovery_at
    assert '"prepositioned_right"' in recovery_selection[replacement_at:]
    assert '"return_unused_preposition"' in recovery_selection[replacement_at:]
    assert '"system_top_replacement_stable_2s"' in recovery_selection[replacement_at:]

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


def test_mayo_reuse_preparation_is_future_scoped_and_returns_to_mayo() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    candidate_guard = _section(
        source,
        "bool toolIsAnticipatoryCandidate",
        "bool systemTopPredictionReplacesPreposition",
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
    assert 'std::string("mayo_stand")' in command
    assert 'std::string("mayo_reuse_zone")' not in command
    assert 'std::string("mayo_recovery_zone")' not in command
    assert '"bt.source_location_id", prepare_source_location' in command
    assert '"bt.source_location_type", prepare_source_type' in command
    assert "stable next-tool prediction selected a Mayo reuse tool" in command
    assert 'home_location_id = "mayo_stand"' in recovery_selection
    assert 'home_location_type = "mayo_stand"' in recovery_selection
    assert '"bt.target_location_id", std::string("mayo_stand")' in command
    assert '"bt.target_location_type", std::string("mayo_stand")' in command
    assert "parked on Mayo to free the right hand" in command


def test_recovery_selection_consumes_instance_fifo_before_generic_scan() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )

    queue_at = recovery_selection.index('"active_recovery_instances.csv"')
    generic_at = recovery_selection.index(
        "for (const auto & tool_id : allTools(*this))", queue_at
    )
    assert queue_at < generic_at
    assert 'toolLifecycle(*this, instance_id) == "mayo_recovery"' in recovery_selection
    assert 'hasActiveRobotTask(*this)' in recovery_selection
    assert '"robot.left_hand_tool"' in recovery_selection
    assert '"cleaner.busy"' in recovery_selection
    assert "hasBlockingSafetyFlag(*this)" in recovery_selection


def test_bt_control_reads_only_rank_one_scalar_prediction_fields() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    load_world = _section(source, "class LoadWorldState", "class IsProcedureActive")

    assert "msg.predicted_tool" in load_world
    assert "msg.predicted_tool_confidence" in load_world
    assert "msg.predicted_tool_stability_sec" in load_world
    assert "ranked_tool_predictions" not in load_world


def test_return_unused_preposition_has_no_legacy_time_triggers() -> None:
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

    assert "kSystemTopReplacementMinStabilitySec = 2.0" in source
    assert "return hasRecoveryContext(*this)" in needs_recovery
    assert '"return_unused_preposition"' in recovery_selection
    assert '"system_top_replacement_stable_2s"' in recovery_selection
    assert "system top prediction changed for 2 s" in command
    for removed in (
        "kPreparationUnsupportedGraceSec",
        "kPreparationMaxDwellSec",
        "kPreparationStrongConfidence",
        "kPreparationStrongMaxDwellSec",
        "policy.expired_preposition_instance",
        "policy.expired_preposition_reason",
        "preposition_dwell_expired",
        "prediction_evidence_expired",
    ):
        assert removed not in source


def test_returned_preposition_has_no_time_based_rearm_cooldown() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    expected_selection = _section(
        source, "class SelectExpectedTool", "class SelectRecoveryTool"
    )
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )
    assert "kPreparationRetryCooldownSec" not in source
    assert "preposition_cooldown" not in expected_selection
    assert "preposition_cooldown" not in recovery_selection
    assert "steadyNowSec" not in source


def test_explicit_request_preempts_direct_hand_and_anticipatory_paths() -> None:
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
        "DirectHandHandoverSignal"
    )
    assert branch_names.index("ExplicitRequest") < branch_names.index(
        "AnticipatoryPreparation"
    )

    direct_hand = ET.tostring(
        _named_sequence(root, "DirectHandHandoverSignal"), encoding="unicode"
    )
    anticipatory = ET.tostring(
        _named_sequence(root, "AnticipatoryPreparation"), encoding="unicode"
    )
    assert "<Inverter" in direct_hand and "<HasExplicitRequest" in direct_hand
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


def test_implicit_dispatch_is_limited_to_one_handover_per_hand_episode() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )
    implicit_gate = _section(
        dispatch,
        'if (decision == "implicit_request" && implicit_handover_action)',
        "const auto signature = makeSignature",
    )

    assert "implicit_request_generation <= 0" in dispatch
    assert "procedure_run_id.empty()" in dispatch
    assert "implicit_handover_action" in dispatch
    assert 'action == "direct_handover"' in dispatch
    assert 'action == "pick_up_and_handover"' in dispatch
    assert (
        'decision == "implicit_request" && implicit_handover_action'
        in implicit_gate
    )
    assert "procedure_run_id" in implicit_gate
    assert "implicit_request_generation" in implicit_gate
    assert '"dispatch.last_implicit_episode"' in implicit_gate
    assert "selected_tool" not in implicit_gate
    assert "selected_tool_lifecycle" not in implicit_gate
    assert "right_hand_instance" not in implicit_gate
    assert "left_hand_instance" not in implicit_gate
    assert (
        'writeBlackboard(\n        *this, "dispatch.last_implicit_episode", '
        "implicit_episode_signature)"
    ) in dispatch


def test_direct_hand_identity_is_fail_closed_and_command_id_is_deterministic() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    load_world = _section(source, "class LoadWorldState", "class IsProcedureActive")
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )
    publish = _section(
        source, "class PublishSkillCommand", "}  // namespace taskplanner_bt_nodes"
    )
    implicit_publish = _section(
        publish, 'if (msg.mode == "implicit_request")', "} else {"
    )

    assert '"runtime.procedure_run_id", msg.procedure_run_id' in load_world
    assert (
        'decision == "implicit_request" &&\n'
        "      (procedure_run_id.empty() || implicit_request_generation <= 0)"
        in dispatch
    )
    assert "procedure_run_id + \"|\"" in dispatch
    assert "std::to_string(implicit_request_generation)" in dispatch
    assert "procedure_run_id" in dispatch[dispatch.index("static std::string makeSignature") :]

    assert 'msg.procedure_run_id = "";' in publish
    assert "msg.implicit_request_generation = 0;" in publish
    assert "if (procedure_run_id.empty() || implicit_request_generation <= 0)" in (
        implicit_publish
    )
    assert "return false;" in implicit_publish
    assert "msg.procedure_run_id = procedure_run_id;" in implicit_publish
    assert "msg.implicit_request_generation = static_cast<uint64_t>" in implicit_publish
    assert (
        '"skill-hand-" + procedure_run_id + "-" +\n'
        "        std::to_string(implicit_request_generation) + \"-\" + msg.action"
        in implicit_publish
    )
    assert "context_.getCurrentTime()" not in implicit_publish
    assert "skill_command_sequence" not in implicit_publish


def test_direct_hand_signal_remains_evidence_until_bt_policy_accepts_it() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    condition = _section(
        source, "class HasImplicitRequest", "class NeedsRecovery"
    )
    selection = _section(
        source, "class SelectImplicitTool", "class SelectExpectedTool"
    )
    guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )
    direct_hand = ET.tostring(
        _named_sequence(root, "DirectHandHandoverSignal"), encoding="unicode"
    )

    assert "writeBlackboard" not in condition
    assert "SurgeonRequest" not in condition
    assert '"request.explicit_tool"' not in condition
    assert '"request.surgeon_tool"' not in condition
    assert '"request.explicit_tool"' not in selection
    assert '"request.surgeon_tool"' not in selection
    assert '"selected.tool"' in selection
    assert '"hand_handover_signal"' in selection

    assert "<HasImplicitRequest" in direct_hand
    assert "<SelectImplicitTool" in direct_hand
    assert "<ApplyActionGuard" in direct_hand
    assert "<CanHandover" in direct_hand
    assert "<SelectExplicitTool" not in direct_hand
    assert "<IsPhaseCertain" not in direct_hand

    # Phase classification is observational context only. It must not veto
    # either explicit or implicit handover acceptance in the action guard.
    assert '"phase.uncertain"' not in guard
    assert "phase_uncertain_implicit_override" not in source
    assert "phase_uncertainty_permits_handover" not in source
    assert "kPhaseUncertainImplicitPrediction" not in source
    assert "voice_backed_explicit_request || direct_hand_preposition_selected" in guard


def test_terminal_execution_states_cannot_reach_physical_commands() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    active_gate = _section(
        source, "class IsProcedureActive", "class HasExplicitRequest"
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
    assert not list(_named_sequence(root, "IdleObserve").iter("PublishSkillCommand"))


def test_phase_uncertainty_is_observational_only_not_a_bt_decision_gate() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    xml = TREE_PATH.read_text(encoding="utf-8")
    config = NODE_CONFIG_PATH.read_text(encoding="utf-8")
    cmake = CMAKE_PATH.read_text(encoding="utf-8")
    recovery = _section(
        source,
        "RecoveryPolicyCandidate selectRecoveryPolicyCandidate",
        "bool hasRecoveryContext",
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    # Preserve the WorldState mirror for observability while removing every
    # BT policy branch that interprets uncertainty as an execution veto.
    assert source.count('"phase.uncertain"') == 1
    assert 'writeBlackboard(*this, "phase.uncertain"' in source
    assert 'readBlackboard(node, "phase.uncertain"' not in recovery
    assert 'mode == "safety"' not in command
    assert "IsPhaseCertain" not in source
    assert "IsPhaseCertain" not in xml
    assert "IsPhaseCertain" not in config
    assert "IsPhaseCertain" not in cmake
    assert 'name="Safety"' not in xml
    assert "phase uncertain; observing without robot motion" not in xml


def test_skill_command_carries_voice_priority_provenance_only_for_explicit_mode() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    publish = _section(
        source, "class PublishSkillCommand", "}  // namespace taskplanner_bt_nodes"
    )

    assert 'readBlackboard(*this, "request.voice_backed", request_voice_backed)' in publish
    assert (
        'msg.voice_backed = request_voice_backed && msg.mode == "explicit_request";'
        in publish
    )


def test_voice_explicit_request_alone_can_bypass_the_bt_active_task_gate() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    can_handover = _section(
        source, "class CanHandover", "class CanPreposition"
    )
    action_guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )

    assert "hasActiveRobotTask(*this) && !voice_backed_selected" in can_handover
    assert "isExplicitSurgeonIntent(surgeon_intent)" in can_handover
    assert (
        "active_task_id.empty() || voice_backed_explicit_request"
        in action_guard
    )
    assert "hasActiveRobotTask(*this) && !voice_backed_selected" in dispatch
    assert 'decision == "explicit_request"' in dispatch
    assert "isExplicitSurgeonIntent(surgeon_intent)" in dispatch
    assert "validated_voice_request_present" in dispatch

    # Implicit requests never receive the bypass and still rely on the same
    # active-task checks before dispatch.
    assert "implicit_request_selected" in action_guard
    assert "voice_backed_explicit_request" in action_guard


def test_occupied_right_hand_uses_supported_sequential_return_before_handover() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )
    explicit_replacement = _section(
        source,
        "bool explicitRequestReplacesPreposition",
        "bool systemTopPredictionReplacesPreposition",
    )

    assert 'std::string("put_down_and_handover")' not in command
    assert 'held_lifecycle != "prepositioned_right"' in command
    assert 'writeBlackboard(*this, "selected.tool", right_hand_instance)' in command
    assert (
        'std::string("return_unused_preposition")'
        in command[command.index("} else if (!right_hand_instance.empty())") :]
    )
    replacement_branch = command[command.index("} else if (!right_hand_instance.empty())") :]
    assert 'mode != "explicit_request"' in replacement_branch
    assert "!explicitRequestReplacesPreposition(*this)" in replacement_branch
    assert "request_generation <= 0" in explicit_replacement
    assert "!isExplicitSurgeonIntent(surgeon_intent)" in explicit_replacement
    assert "requested_instance != prepositioned_instance" in explicit_replacement
    assert "requested_tool != prepositioned_tool" in explicit_replacement
    active_task_guard_at = replacement_branch.index("if (hasActiveRobotTask(*this))")
    configure_return_at = replacement_branch.index(
        'std::string("return_unused_preposition")'
    )
    assert active_task_guard_at < configure_return_at
    assert '"bt.source_location_id", std::string("robot_right_hand")' in replacement_branch
    assert '"bt.source_location_type", std::string("robot_right_hand")' in replacement_branch
    assert '"bt.target_location_id", std::string("mayo_stand")' in replacement_branch
    assert '"bt.target_location_type", std::string("mayo_stand")' in replacement_branch
    assert "right hand occupied; park held tool on Mayo" in replacement_branch
    assert 'action == "return_unused_preposition"' in dispatch
    assert dispatch.index('action == "return_unused_preposition"') < dispatch.index(
        "if (hasActiveRobotTask(*this) && !voice_backed_selected)"
    )


def test_mayo_request_uses_supported_prepare_then_direct_handover_sequence() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )
    mayo_branch = _section(command, "} else if (on_mayo)", "} else {")

    assert 'std::string("pick_up_from_mayo_and_handover")' not in command
    assert 'std::string("prepare_tool")' in mayo_branch
    assert 'std::string("mayo_stand")' in mayo_branch
    assert 'std::string("robot_right_hand")' in mayo_branch
    assert "requested tool is on Mayo; prepare it before handover" in mayo_branch

    # Preparation is a prerequisite, not the handover authorized by the
    # current open-palm episode. The episode is consumed only on the later
    # robot-to-surgeon direct_handover.
    implicit_gate = _section(
        dispatch,
        "const bool implicit_handover_action",
        "std::string implicit_episode_signature",
    )
    assert 'action == "direct_handover"' in implicit_gate
    assert 'action == "pick_up_and_handover"' in implicit_gate
    assert 'action == "prepare_tool"' not in implicit_gate
