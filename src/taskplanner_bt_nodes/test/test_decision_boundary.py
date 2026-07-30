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


def test_bt_owns_visual_request_policy_and_runtime_gate() -> None:
    source = CPP_PATH.read_text(encoding="utf-8")
    runtime = _section(source, "class IsProcedureActive", "class IsPhaseCertain")
    implicit = _section(source, "class HasImplicitRequest", "class NeedsRecovery")

    assert 'execution_state == "running"' in runtime
    assert 'execution_state == "finishing"' in runtime
    assert "confidence < 0.8" in implicit
    assert "stability_sec < 0.7" in implicit
    assert '"open_receive"' in implicit


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
    safety_at = xml.index('name="Safety"')
    implicit_at = xml.index('name="VisualImplicitRequest"')
    recovery_at = xml.index('name="Recovery"')
    anticipatory_at = xml.index('name="AnticipatoryHandover"')
    assert explicit_at < safety_at < implicit_at < recovery_at < anticipatory_at


def test_reducer_records_evidence_without_creating_action_obligations() -> None:
    source = DT_NODE_PATH.read_text(encoding="utf-8")
    implicit = _section(
        source, "def _handle_vlm_implicit_request", "def _tool_available_for_prediction"
    )
    vlm_result = _section(source, "def _on_vlm_result", "def _on_observation")

    assert "SurgeonRequest()" not in implicit
    assert "update_surgeon_request" not in implicit
    assert "record_mayo_policy_evidence" in vlm_result
    assert "promote_mayo_recovery_from_vlm" not in vlm_result
