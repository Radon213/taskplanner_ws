from __future__ import annotations

from pathlib import Path

from or_digital_twin.twin import ORDigitalTwin
from procedure_spec import InstrumentPopulationSpec, load_bundle


def _demo_spec():
    return load_bundle(
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "inguinal_hernia_repair_demo"
    )


def _set_exchangeable_population(
    spec,
    instrument_id: str,
    *,
    initial_count: int,
    capacity: int,
) -> None:
    instrument = next(
        item for item in spec.bundle.instruments if item.id == instrument_id
    )
    instrument.inventory_count = initial_count
    instrument.population = InstrumentPopulationSpec(
        initial_count=initial_count,
        capacity=capacity,
        exchangeable=True,
    )


def test_dt_materializes_initial_count_without_enumerating_capacity() -> None:
    spec = _demo_spec()
    _set_exchangeable_population(
        spec,
        "T01",
        initial_count=2,
        capacity=4,
    )

    twin = ORDigitalTwin(spec)

    assert [
        state.instance_id for state in twin._instances_for_type("T01")
    ] == ["T01#1", "T01#2"]
    assert "T01#3" not in twin.instrument_states
    assert "T01#4" not in twin.instrument_states
    assert "duplicate_tool_holder" not in twin.state.safety_flags


def test_dt_allows_zero_initial_exchangeable_population() -> None:
    spec = _demo_spec()
    _set_exchangeable_population(
        spec,
        "T01",
        initial_count=0,
        capacity=3,
    )

    twin = ORDigitalTwin(spec)

    assert twin._instances_for_type("T01") == []
    assert not any(
        state.instrument_id == "T01"
        for state in twin.instrument_states.values()
    )
    assert "duplicate_tool_holder" not in twin.state.safety_flags


def test_legacy_fixed_inventory_materialization_is_unchanged() -> None:
    twin = ORDigitalTwin(_demo_spec())

    assert [
        state.instance_id for state in twin._instances_for_type("T03")
    ] == ["T03#1", "T03#2"]
