from pathlib import Path
from types import SimpleNamespace

from procedure_spec import load_bundle
from tool_belief_tracker.inventory import (
    inventory_from_detected_tool_counts,
    inventory_from_procedure_spec,
)


def test_thyroidectomy_demo_inventory_and_authored_initial_placement_are_exact() -> None:
    source_root = Path(__file__).resolve().parents[2]
    spec_dir = (
        source_root
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy_demo"
    )
    spec = load_bundle(spec_dir)

    inventory = inventory_from_procedure_spec(spec)

    assert [item.instance_id for item in inventory] == [
        "T02#1",
        "T02#2",
        "T04#1",
        "T04#2",
        "T07#1",
        "T07#2",
        "T08#1",
        "T08#2",
    ]
    by_id = {item.instance_id: item for item in inventory}
    for instrument_id in ("T02", "T04", "T07", "T08"):
        active = by_id[f"{instrument_id}#1"]
        dormant = by_id[f"{instrument_id}#2"]
        assert active.initial_location == "tray"
        assert active.initial_activity_probability == 1.0
        assert active.exchangeable_population is True
        assert dormant.initial_location == "unknown"
        assert dormant.initial_confidence == 0.0
        assert dormant.initial_activity_probability == 0.0
        assert dormant.exchangeable_population is True


def test_exchangeable_capacity_builds_inactive_unknown_slots_after_initial_count() -> None:
    spec = SimpleNamespace(
        bundle=SimpleNamespace(
            instruments=[SimpleNamespace(id="T03", display_name="Adson")]
        ),
        get_initial_instrument_states=lambda: [
            SimpleNamespace(
                instrument_id="T03",
                instance_id="T03#1",
                location_id="mayo_a",
                confidence=0.72,
            )
        ],
        get_tool_inventory=lambda: {"T03": 1},
        get_tool_inventory_capacity=lambda: {"T03": 3},
        is_exchangeable_population=lambda _instrument_id: True,
        get_initial_location=lambda _instrument_id: "rack_a",
        get_location_type=lambda location_id: {
            "mayo_a": "mayo_stand",
            "rack_a": "instrument_rack",
        }[location_id],
    )

    inventory = inventory_from_procedure_spec(spec)

    assert [item.instance_id for item in inventory] == [
        "T03#1",
        "T03#2",
        "T03#3",
    ]
    active, dormant_one, dormant_two = inventory
    assert active.initial_location == "mayo"
    assert active.initial_confidence == 0.72
    assert active.initial_activity_probability == 1.0
    assert active.exchangeable_population is True
    for dormant in (dormant_one, dormant_two):
        assert dormant.initial_location == "unknown"
        assert dormant.initial_confidence == 0.0
        assert dormant.initial_activity_probability == 0.0
        assert dormant.exchangeable_population is True


def test_legacy_fixed_population_preserves_non_exchangeable_identity_policy() -> None:
    spec = SimpleNamespace(
        bundle=SimpleNamespace(
            instruments=[SimpleNamespace(id="T03", display_name="Adson")]
        ),
        get_initial_instrument_states=lambda: [],
        get_tool_inventory=lambda: {"T03": 2},
        get_tool_inventory_capacity=lambda: {"T03": 2},
        is_exchangeable_population=lambda _instrument_id: False,
        get_initial_location=lambda _instrument_id: "rack_a",
        get_location_type=lambda _location_id: "instrument_rack",
    )

    inventory = inventory_from_procedure_spec(spec)

    assert [item.instance_id for item in inventory] == ["T03#1", "T03#2"]
    assert all(item.initial_activity_probability == 1.0 for item in inventory)
    assert all(item.exchangeable_population is False for item in inventory)


def test_detected_start_inventory_uses_only_seen_types_and_clamps_capacity() -> None:
    spec = SimpleNamespace(
        bundle=SimpleNamespace(
            instruments=[
                SimpleNamespace(id="T03", display_name="Adson"),
                SimpleNamespace(id="T04", display_name="Bovie"),
            ]
        ),
        get_tool_inventory_capacity=lambda: {"T03": 1, "T04": 2},
        is_exchangeable_population=lambda instrument_id: instrument_id == "T04",
    )

    inventory = inventory_from_detected_tool_counts(
        spec,
        {"T03": 3, "T04": 1, "unknown": 9},
        initial_locations={"T03": "mayo_stand", "T04": "tray"},
        initial_confidences={"T03": 0.91, "T04": 0.74},
    )

    assert [item.instance_id for item in inventory] == ["T03#1", "T04#1"]
    assert [item.initial_location for item in inventory] == ["mayo", "tray"]
    assert [item.initial_confidence for item in inventory] == [0.91, 0.74]
    assert inventory[0].exchangeable_population is False
    assert inventory[1].exchangeable_population is True
