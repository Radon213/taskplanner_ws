from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from procedure_spec import load_bundle, load_frozen_tool_demand_prior
from vlm_node.real_vlm import RealVLMNode


def _demo_node() -> RealVLMNode:
    spec_dir = (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    node = RealVLMNode.__new__(RealVLMNode)
    node._spec = load_bundle(spec_dir)
    node._tool_demand_prior = load_frozen_tool_demand_prior(
        node._spec,
        spec_dir,
    )
    assert node._tool_demand_prior is not None
    node._world = SimpleNamespace(filtered_phase="P04")
    node._simulation = None
    node._phase_bootstrap_id = ""
    return node


def _context(
    *,
    observable_perception: dict[str, object],
    tool_location: str,
    inventory: dict[str, object],
) -> dict[str, object]:
    return {
        "candidates": {"evidence": {"current_phase": "P04"}},
        "digital_twin": {
            "completed_handovers": [{"tool": "T02", "at": 1.0}],
            # The lifecycle/owner facts are an allowed surgeon-use signal;
            # only the placement and availability facts vary in this test.
            "tools": [
                {
                    "id": "T04",
                    "lc": "surgeon_owned",
                    "own": "surgeon",
                    "loc": tool_location,
                    "lt": tool_location,
                }
            ],
            "forecast_inventory": inventory,
        },
        # This is intentionally not read by the demand helper.  CAM changes
        # affect observation/location elsewhere, never future-use demand.
        "observable_perception": observable_perception,
        "mayo_observation": [["T04", "reuse", 0.99]],
    }


def test_future_demand_sidecar_is_invariant_to_cam_and_mayo_placement() -> None:
    node = _demo_node()
    visible_on_mayo = _context(
        observable_perception={
            "schema": "taskplanner.rfdetr_multiview_tool_context.v1",
            "tool_detection_views": [
                {"view": "cam_4", "instances": [{"tool_id": "T04"}]}
            ],
        },
        tool_location="mayo_stand",
        inventory={"mayo_reuse": [["T04", 1]], "available": [["T04", 1]]},
    )
    not_visible_off_mayo = _context(
        observable_perception={"schema": "camera_offline", "tools": []},
        tool_location="unknown_external",
        inventory={"mayo_reuse": [], "available": [], "unavailable": [["T04", 1]]},
    )

    first = node._tool_demand_forecast(visible_on_mayo)
    second = node._tool_demand_forecast(not_visible_off_mayo)

    assert first == second
    assert [row[0] for row in first] == ["T02", "T04", "T07", "T08"]
    assert all(0.0 <= row[1] <= 1.0 for row in first)


def test_actor_log_stabilizer_attaches_the_display_only_demand_sidecar() -> None:
    node = _demo_node()
    context = _context(
        observable_perception={},
        tool_location="mayo_stand",
        inventory={},
    )
    context["evidence_window"] = {"speech": []}
    context["forecast_constraints"] = {}
    expected = node._tool_demand_forecast(context)

    # This test is about the new JSON sidecar, not the independently tested
    # CAM4/DT Mayo policy reducers.
    node._corroborate_mayo_with_cam4_semantics = lambda *_args: None
    node._suppress_non_mayo_recovery_candidates = lambda *_args, **_kwargs: None
    node._retain_mayo_policy_for_current_stand = lambda *_args: None
    payload = {
        "v": "4",
        "phase": [["P04", 0.8]],
        "tool": [],
        "intent": ["none", "", 0.0],
        "mayo": [],
        "mayo_retrieve": ["", 0.0],
        "u": 0.2,
        "sum": "field stable",
        "bed_robot_arm_group": None,
    }

    stabilized = node._stabilize_actor_log_payload(payload, context)

    assert stabilized["tool_demand_forecast"] == expected
    assert stabilized["mayo"] == []
