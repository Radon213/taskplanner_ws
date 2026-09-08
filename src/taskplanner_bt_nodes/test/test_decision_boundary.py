from __future__ import annotations

from pathlib import Path
import re
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


def test_world_state_wait_yields_once_at_the_executor_cadence() -> None:
    root = ET.parse(TREE_PATH).getroot()
    tick = _named_sequence(root, "TaskplannerAssistTick")
    retry = next(tick.iter("RetryUntilSuccessful"))
    delay = next(iter(retry))
    assert retry.attrib["num_attempts"] == "-1"
    assert delay.tag == "Delay"
    # This is exactly one 40 Hz executor period, not the historical 100 ms
    # extra gate in front of every direct handover decision.
    assert int(delay.attrib["delay_msec"]) == 25
    assert [child.tag for child in delay] == ["LoadWorldState"]

    source = CPP_PATH.read_text(encoding="utf-8")
    load_world = _section(source, "class LoadWorldState", "class IsProcedureActive")
    assert "world_state_stale" in load_world
    assert "return BT::NodeStatus::FAILURE;" in load_world


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
    assert '"request.implicit_confidence"' not in direct_hand
    assert '"request.implicit_stability_sec"' not in direct_hand
    assert '"open_receive"' in direct_hand
    assert "hasActiveRobotTask(*this)" in implicit
    assert "directHandSignalActive(*this)" in implicit
    assert "rightHandEmpty" not in implicit
    assert '"prediction.tool"' not in implicit
    assert '"prepositioned_right"' in exact_preposition
    assert "kHandHandoverMinConfidence" not in source
    assert "kHandHandoverMinStabilitySec" not in source
    assert "kImplicitPredictionMinConfidence" not in source
    assert "kImplicitPredictionMinStabilitySec" not in source
    assert "kPreparationMinConfidence" not in source
    assert "kPreparationMinStabilitySec" not in source
    assert '"prediction.autonomous_ready"' in source


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


def test_recovery_waits_for_right_hand_preposition_to_be_returned() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    recovery = _section(
        source, "class SelectRecoveryTool", "class ShouldDispatchDecision"
    )

    assert 'readBlackboard(*this, "robot.right_hand_tool", right_hand_tool);' in recovery
    assert 'readBlackboard(*this, "robot.right_hand_instance", right_hand_instance);' in recovery
    assert "retrieve_blocked_right_hand_preposition" in recovery
    assert 'return BT::NodeStatus::FAILURE;' in recovery
    # The finishing cleanup branch must remain ahead of the gate so a held
    # preposition can be returned before Mayo recovery is retried.
    cleanup_at = recovery.index('if (execution_state == "finishing")')
    gate_at = recovery.index("retrieve_blocked_right_hand_preposition")
    assert cleanup_at < gate_at


def test_dt_handover_admission_is_enforced_by_bt_without_source_reselection() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    load_world = _section(source, "class LoadWorldState", "class IsProcedureActive")
    action_guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )
    can_handover = _section(source, "class CanHandover", "class CanPreposition")
    explicit_selection = _section(
        source, "class SelectExplicitTool", "class SelectImplicitTool"
    )

    assert '"state.handover_hint"' in load_world
    assert 'readBlackboard(*this, "state.handover_hint", handover_hint)' in action_guard
    assert "const bool source_admitted = !explicit_request_selected || handover_hint;" in (
        action_guard
    )
    assert "requested_tool_not_robot_reachable" in action_guard
    assert 'lifecycle == "surgeon_owned"' not in can_handover
    assert "findActiveInstanceForType(*this, tool_id" not in explicit_selection
    assert "Digital Twin is the sole owner of supply selection" in explicit_selection
    assert "stale_surgeon_owned_voice_retry_allowed" not in source


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
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )

    # The BT executes only reducer-issued recovery state; it never rebuilds
    # Mayo policy from phase-role, VLM-confidence, or capacity heuristics.
    for removed in (
        '"future_use_expected"',
        '"mayo_recovery_confidence"',
        '"mayo_recovery_stability_sec"',
        '"completion_cleanup"',
        "mayo_tools.size() > 2",
        "selectRecoveryPolicyCandidate",
    ):
        assert removed not in source
    assert '"active_recovery_instances.csv"' in recovery
    assert '"authoritative_recovery_transaction"' in recovery


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

    assert "kPreparationMinConfidence" not in source
    assert "kPreparationMinStabilitySec" not in source
    assert "hasBlockingSafetyFlag(*this, true)" in preparation
    assert '"robot.right_hand_tool"' in preparation
    assert '"prediction.autonomous_ready"' in selection
    assert '"prediction.confidence"' not in selection
    assert '"prediction.stability_sec"' not in selection
    assert '"dt_authorized_prediction_replacement"' in recovery
    replacement = _section(
        source,
        "bool systemTopPredictionReplacesPreposition",
        "bool hasRecoveryContext",
    )
    assert '"reserved_for"' in replacement
    assert 'preposition_reservation != "voice_prepared"' in replacement

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


def test_deterministic_ngram_preparation_does_not_wait_for_vlm_health() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    preparation = _section(
        source, "class CanPreposition", "class SelectExplicitTool"
    )

    # The n-gram policy is Digital-Twin-owned. A VLM response may arrive
    # later in a new run, but it is not an input to this preparation decision.
    assert "hasBlockingSafetyFlag(*this, true)" in preparation


def test_hand_signal_requires_an_exact_prepositioned_right_hand_tool() -> None:
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
    assert "rightHandEmpty" not in implicit_condition
    assert '"prediction.tool"' not in implicit_condition
    assert "kImplicitPredictionMinConfidence" not in implicit_condition
    assert "kImplicitPredictionMinStabilitySec" not in implicit_condition

    assert '"hand_signal_preposition_match"' in implicit_selection
    assert '"hand_signal_prediction_fallback"' not in implicit_selection
    assert '"prediction.tool"' not in implicit_selection
    assert "findActiveInstanceForType" not in implicit_selection
    assert '"surgeon_owned"' not in implicit_selection
    assert '"request.implicit_tool"' not in implicit_selection

    assert "directHandSignalActive(*this)" in action_guard
    assert "implicit_prediction_fallback" not in action_guard
    assert "direct_hand_prediction_selected" not in action_guard
    assert "implicit_request_requires_prepositioned_right_tool" in source


def test_cam4_hand_presence_blocks_autonomous_mayo_prediction_and_recovery() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    load_world = _section(source, "class LoadWorldState", "class IsProcedureActive")
    active_selector = _section(
        source, "std::string findActiveInstanceForType", "bool hasBlockingSafetyFlag"
    )
    anticipatory_selector = _section(
        source,
        "std::string findAnticipatoryInstanceForType",
        "bool explicitRequestReplacesPreposition",
    )
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )

    assert "msg.cam4_mayo_hand_present" in load_world
    assert '"perception.cam4_mayo_hand_present"' in load_world
    # The candidate finder is shared by implicit request selection and must
    # therefore default to preserving the observation-only Mayo exclusion.
    # Only the direct hand-request path opts into the explicit override below.
    assert "bool allow_occupied_mayo = false" in active_selector
    assert "mayoWorkspaceOccupied(node)" in active_selector
    assert "mayo_occupied && on_mayo && !allow_occupied_mayo" in active_selector
    assert "mayoWorkspaceOccupied(node)" in anticipatory_selector
    assert "if (mayo_occupied)" in anticipatory_selector
    assert "mayoWorkspaceOccupied(*this)" in recovery_selection
    assert 'std::string("cam4_mayo_hand_present")' in recovery_selection
    assert "kWorldStateMaxReceiptAgeNs" in source
    assert "last_world_state_receipt_ns_" in load_world
    assert "steadyNowNs()" in load_world
    assert 'std::string("world_state_stale")' in load_world
    assert '"perception.cam4_mayo_hand_present", true' in load_world


def test_cam4_hand_presence_does_not_enable_empty_hand_prediction_handover() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    operator_request = _section(
        source, "bool operatorRequestedMayoPickup", "bool hasBlockingSafetyFlag"
    )
    implicit_selection = _section(
        source, "class SelectImplicitTool", "class SelectExpectedTool"
    )
    action_guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )

    # An Open_Receive episode only releases the already prepared right-hand
    # instance. It cannot select a Mayo/rack tool from rank-1 prediction,
    # including while the Mayo workspace is occupied.
    assert '"prediction.tool"' not in implicit_selection
    assert "findActiveInstanceForType" not in implicit_selection
    assert "directHandSignalActive(node)" not in operator_request
    assert "rightHandEmpty(node)" not in operator_request
    assert "implicit_generation" not in operator_request
    assert "predicted_tool" not in operator_request
    assert "operatorRequestedMayoPickup(*this, selected_tool)" in action_guard
    mayo_guard = _section(
        action_guard, "const bool mayo_handover_allowed", "const bool allowed"
    )
    assert "!mayoWorkspaceOccupied(*this) || operator_requested_mayo_handover" in mayo_guard

    # Occupied-Mayo bypass remains limited to the explicit voice request path;
    # an implicit signal cannot re-enable it downstream.
    command_gate = command[: command.index('writeBlackboard(*this, "bt.decision"')]
    dispatch_gate = dispatch[: dispatch.index("if (hasActiveRobotTask(*this))")]
    assert 'mode == "explicit_request"' in command_gate
    assert 'decision == "explicit_request"' in dispatch_gate
    assert "implicit_request_requires_prepositioned_right_tool" in command


def test_cam4_hand_presence_allows_only_voice_backed_explicit_mayo_request() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    operator_request = _section(
        source, "bool operatorRequestedMayoPickup", "bool hasBlockingSafetyFlag"
    )
    action_guard = _section(
        source, "class ApplyActionGuard", "class ConfigureHumanoidCommand"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )
    dispatch = _section(
        source, "class ShouldDispatchDecision", "class EmitBTDecision"
    )

    assert 'surgeon_intent == "voice_request"' in operator_request
    assert "voice_backed && ready_for_handover && handover_hint" in operator_request
    assert "surgeon_instance == selected_tool" in operator_request
    assert "operatorRequestedMayoPickup(*this, selected_tool)" in action_guard

    # The override is deliberately narrow: a typed/voice explicit request can
    # pass the occupied-Mayo guard, but an arbitrary explicit BT branch cannot.
    command_gate = command[: command.index('writeBlackboard(*this, "bt.decision"')]
    dispatch_gate = dispatch[: dispatch.index("if (hasActiveRobotTask(*this))")]
    for gate, branch_name in ((command_gate, "mode"), (dispatch_gate, "decision")):
        assert f'{branch_name} == "explicit_request"' in gate
        assert "operatorRequestedMayoPickup(*this, selected_tool)" in gate
        assert "!operator_requested_mayo_handover" in gate


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
    assert "findActiveInstanceForType" not in implicit_selection
    assert '"prediction.tool"' not in implicit_selection

    # If that matching instance is already in the humanoid right hand, the
    # configured operation is a direct robot-to-surgeon handover.
    assert "isExactRightHandPreposition(*this, selected_tool)" in command
    assert 'std::string("direct_handover")' in command
    assert 'std::string("robot_right_hand")' in command
    assert 'std::string("surgeon_receive_zone")' in command


def test_reducer_uses_ngram_policy_without_creating_hand_action_obligations() -> None:
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
    assert "return self._ngram_tool_prediction()" in prediction
    assert "self._refresh_ngram_tool_policy" in prediction_handler
    assert "del payload, msg, received_sec" in prediction_handler
    assert "record_mayo_policy_evidence" not in prediction_handler
    assert "interrupt_visible" not in vlm_result
    assert "self._publish_world_state()" in vlm_result
    assert "record_mayo_policy_evidence" not in vlm_result
    assert "mayo_rows:" not in vlm_result
    assert "promote_mayo_recovery_from_vlm" not in vlm_result
    # Retired VLM fusion/stability code must be absent, not merely hidden
    # behind an early return where a future edit could reactivate it.
    for helper in (
        "_normalize_ranked_tool_distribution",
        "_update_stability",
        "_tool_prediction_sample_status",
        "_vlm_tool_rows",
        "_is_canonical_mayo_policy_state",
    ):
        assert f"def {helper}(" not in source
    assert "vlm_scores" not in prediction
    assert "_tool_predict_stability" not in prediction_handler


def test_prediction_replacement_returns_old_preposition_before_preparing_new() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    root = ET.parse(TREE_PATH).getroot()
    replacement = _section(
        source,
        "bool systemTopPredictionReplacesPreposition",
        "bool hasRecoveryContext",
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
    assert "kSystemTopReplacementMinStabilitySec" not in source
    assert 'readBlackboard(node, "prediction.autonomous_ready"' in replacement
    assert "autonomous_ready" in replacement
    assert "systemTopPredictionReplacesPreposition(node)" in recovery_context
    assert "bool isVoicePreparedPreposition" in source
    assert "!isVoicePreparedPreposition(*this, tool_id)" in recovery_selection

    replacement_at = recovery_selection.index(
        "if (systemTopPredictionReplacesPreposition(*this))"
    )
    assert '"prepositioned_right"' in recovery_selection[replacement_at:]
    assert '"return_unused_preposition"' in recovery_selection[replacement_at:]
    assert '"dt_authorized_prediction_replacement"' in recovery_selection[replacement_at:]

    assert "hasRecoveryContext(*this)" in expected_selection
    assert '"prediction.autonomous_ready"' in expected_selection
    assert 'next_required_transition == "return_unused_preposition"' in command
    assert 'writeBlackboard(*this, "bt.arm", std::string("right"))' in command
    assert (
        'writeBlackboard(*this, "bt.action", '
        'std::string("return_unused_preposition"))'
    ) in command
    assert 'std::string("voice_prepared_waiting_for_hand")' in command

    dispatch = _section(source, "class ShouldDispatchDecision", "class EmitBTDecision")
    assert 'decision == "recovery"' in dispatch
    assert 'action == "return_unused_preposition"' in dispatch
    assert "isVoicePreparedPreposition(*this, selected_tool)" in dispatch

    decision_root = next(root.iter("Fallback"))
    branch_names = [
        child.attrib.get("name")
        for child in decision_root
        if child.tag == "Sequence"
    ]
    assert branch_names.index("Recovery") < branch_names.index(
        "AnticipatoryPreparation"
    )


def test_mayo_reuse_preparation_is_ngram_scoped_and_returns_to_mayo() -> None:
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
    assert '"future_use_expected"' not in candidate_guard
    assert "return true;" in candidate_guard
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


def test_completion_release_parks_an_excluded_preposition_before_mayo_recovery() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    recovery_selection = _section(
        source, "class SelectRecoveryTool", "class SetIdleDecision"
    )
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    completion_release_at = recovery_selection.index(
        '"completion_release_excluded_preposition"'
    )
    explicit_replacement_at = recovery_selection.index(
        "if (explicitRequestReplacesPreposition(*this))"
    )
    completion_frozen_at = recovery_selection.index(
        '"completion_frozen_mayo_target"'
    )
    assert completion_release_at < explicit_replacement_at
    assert completion_release_at < completion_frozen_at
    assert 'execution_state == "finishing"' in recovery_selection
    assert 'lifecycle == "prepositioned_right"' in recovery_selection
    assert 'toolNextRequiredTransition(*this, tool_id) == "return_unused_preposition"' in (
        recovery_selection
    )
    assert 'tool_id, "return_unused_preposition", "completion_release_excluded_preposition"' in (
        recovery_selection
    )
    assert 'tool_id, "return_preposition_to_tray", "completion_return_preposition_to_tray"' in (
        recovery_selection
    )
    assert '"completion_frozen_mayo_target"' in recovery_selection
    assert 'lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery"' in recovery_selection
    assert 'toolNextRequiredTransition(*this, tool_id) == "recover_left"' in recovery_selection
    assert 'next_required_transition == "return_preposition_to_tray"' in command
    assert 'std::string("return_preposition_to_tray")' in command
    assert "completion cleanup returns prepared recovery tool directly to tray" in command


def test_bt_control_reads_only_rank_one_scalar_prediction_fields() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    load_world = _section(source, "class LoadWorldState", "class IsProcedureActive")

    assert "msg.predicted_tool" in load_world
    assert "msg.autonomous_preparation_ready" in load_world
    assert "msg.predicted_tool_confidence" not in load_world
    assert "msg.predicted_tool_stability_sec" not in load_world
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

    assert "kSystemTopReplacementMinStabilitySec" not in source
    assert "return hasRecoveryContext(*this)" in needs_recovery
    assert '"return_unused_preposition"' in recovery_selection
    assert '"dt_authorized_prediction_replacement"' in recovery_selection
    assert "DT-authorized n-gram prediction changed" in command
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


def test_explicit_request_outranks_direct_hand_and_anticipatory_paths() -> None:
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
        "!surgeon_instance.empty()"
    )
    assert matching_preposition_at < requested_instance_at
    assert '"robot.right_hand_tool"' in explicit_selection
    assert '"robot.right_hand_instance"' in explicit_selection
    assert "toolMatchesType" in explicit_selection
    assert "toolMatchesType(*this, surgeon_instance, requested_tool_type)" in (
        explicit_selection
    )
    assert 'writeBlackboard(*this, "selected.tool", right_hand_instance)' in (
        explicit_selection
    )
    assert "right_hand_instance == selected_tool" in command
    assert "exact_direct_hand_preposition || explicit_right_hand_selection" in command


def test_voice_explicit_request_hands_over_an_already_prepared_tool() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    command = _section(
        source, "class ConfigureHumanoidCommand", "class ShouldDispatchDecision"
    )

    assert 'readBlackboard(*this, "request.voice_backed", voice_backed)' in command
    assert 'mode == "explicit_request" && voice_backed' in command
    assert 'mode == "implicit_request" &&' in command
    assert "exact_direct_hand_preposition || explicit_right_hand_selection" in command
    assert "voice_requested_tool_already_prepared" not in command
    assert 'std::string("direct_handover")' in command
    assert 'std::string("prepare_tool")' in command
    assert "voice-requested tool is prepared for a later hand signal" in command


def test_explicit_tool_selection_uses_the_twin_instance_while_other_modes_can_prefer_mayo() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    active_selector = _section(
        source, "std::string findActiveInstanceForType", "bool hasBlockingSafetyFlag"
    )
    anticipatory_selector = _section(
        source,
        "std::string findAnticipatoryInstanceForType",
        "bool explicitRequestReplacesPreposition",
    )
    explicit_selection = _section(
        source, "class SelectExplicitTool", "class SelectImplicitTool"
    )
    implicit_selection = _section(
        source, "class SelectImplicitTool", "class SelectExpectedTool"
    )
    expected_selection = _section(
        source, "class SelectExpectedTool", "class SelectRecoveryTool"
    )

    assert "bool prefer_mayo = false" in active_selector
    assert 'lifecycle == "mayo_reuse" || lifecycle == "mayo_recovery"' in (
        active_selector
    )
    assert "return first_eligible;" in active_selector
    assert "findActiveInstanceForType(*this, tool_id" not in explicit_selection
    assert "Digital Twin is the sole owner of supply selection" in explicit_selection
    assert "findActiveInstanceForType" not in implicit_selection
    assert '"prediction.tool"' not in implicit_selection
    assert 'toolLifecycle(node, tool_id) == "mayo_reuse"' in (
        anticipatory_selector
    )
    assert "findAnticipatoryInstanceForType" in expected_selection


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

    # All commands, including implicit handover, are bound to the accepted
    # procedure interval so a delayed command cannot be admitted by a later
    # run.  The older blank-field assertion predated this run fence.
    assert (
        'readBlackboard(*this, "runtime.procedure_run_id", msg.procedure_run_id);'
        in publish
    )
    assert "if (msg.procedure_run_id.empty())" in publish
    assert "msg.implicit_request_generation = 0;" in publish
    assert "if (implicit_request_generation <= 0)" in implicit_publish
    assert "return false;" in implicit_publish
    assert "msg.implicit_request_generation = static_cast<uint64_t>" in implicit_publish
    assert (
        '"skill-hand-" + msg.procedure_run_id + "-" +\n'
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
    assert "direct_hand_prediction_selected" not in guard
    assert "implicit_prediction_fallback" not in guard
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
    recovery = _section(source, "bool hasRecoveryContext", "bool hasActiveRobotTask")
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


def test_active_robot_action_blocks_every_bt_command_including_voice() -> None:
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

    assert "if (hasActiveRobotTask(*this))" in can_handover
    assert "const bool robot_task_slot_available = active_task_id.empty();" in (
        action_guard
    )
    assert "if (hasActiveRobotTask(*this))" in dispatch
    assert "voice_backed_selected" not in dispatch
    assert "validated_voice_request_present" not in dispatch
    assert "active_task_id.empty() || voice_backed_explicit_request" not in (
        action_guard
    )


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
    assert "if (hasActiveRobotTask(*this))" in dispatch
    assert "voice_backed_selected" not in dispatch


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
    # The configured command must preserve the Twin's confirmed source rather
    # than inventing a Mayo location for a stale or unknown instance.
    assert '"bt.source_location_id", tool_location' in mayo_branch
    assert '"bt.source_location_type", tool_location_type' in mayo_branch
    assert 'tool_location.empty() || tool_location_type.empty()' in mayo_branch
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
