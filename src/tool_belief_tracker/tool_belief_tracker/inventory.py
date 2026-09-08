"""Build scenario-bounded tracker slots from the procedure-spec query surface."""

from __future__ import annotations

from collections.abc import Mapping

from .core import InventoryItem, semantic_location


def inventory_from_procedure_spec(spec) -> list[InventoryItem]:
    """Return one bounded slot per scenario-authored population capacity.

    ``get_tool_inventory()`` remains the controller/Digital Twin initial count;
    ``get_tool_inventory_capacity()`` is the larger observation-only logical
    slot budget. Explicit ``tool_placement.initial_states`` win for initially
    active instances, and missing active instances use the authored home
    placement. Capacity beyond the initial count starts inactive at unknown.
    No detector observation participates in this construction.
    """

    explicit = {
        state.instance_id: state
        for state in spec.get_initial_instrument_states()
    }
    instruments = {
        instrument.id: instrument
        for instrument in spec.bundle.instruments
    }
    result: list[InventoryItem] = []
    initial_counts = spec.get_tool_inventory()
    capacities = spec.get_tool_inventory_capacity()
    for instrument_id, capacity in capacities.items():
        instrument = instruments[instrument_id]
        initial_count = int(initial_counts.get(instrument_id, 0))
        exchangeable_population = bool(
            spec.is_exchangeable_population(instrument_id)
        )
        for index in range(1, int(capacity) + 1):
            instance_id = f"{instrument_id}#{index}"
            initially_active = index <= initial_count
            state = explicit.get(instance_id) if initially_active else None
            if not initially_active:
                location_id = "unknown"
                confidence = 0.0
                activity_probability = 0.0
            elif state is not None:
                location_id = state.location_id
                confidence = float(state.confidence)
                activity_probability = 1.0
            else:
                location_id = spec.get_initial_location(instrument_id) or "unknown"
                confidence = 1.0
                activity_probability = 1.0
            location_type = ""
            if location_id != "unknown":
                try:
                    location_type = spec.get_location_type(location_id)
                except (KeyError, ValueError):
                    location_type = ""
            result.append(
                InventoryItem(
                    instrument_id=instrument_id,
                    instance_id=instance_id,
                    display_name=str(instrument.display_name),
                    initial_location=semantic_location(location_id, location_type),
                    initial_confidence=confidence,
                    initial_activity_probability=activity_probability,
                    exchangeable_population=exchangeable_population,
                )
            )
    return result


def inventory_from_detected_tool_counts(
    spec,
    detected_counts: Mapping[str, int],
    *,
    initial_locations: Mapping[str, str] | None = None,
    initial_confidences: Mapping[str, float] | None = None,
) -> list[InventoryItem]:
    """Build a bounded run inventory from the tools seen at scenario start.

    Detector evidence selects which *known* instrument types and how many
    logical instances participate in this run.  It cannot introduce an
    un-authored type or exceed the procedure's authored per-type capacity.
    ``initial_locations`` and ``initial_confidences`` seed the first belief;
    later camera, action, and Twin evidence updates only these fixed slots.
    """

    instruments = {
        instrument.id: instrument
        for instrument in spec.bundle.instruments
    }
    capacities = spec.get_tool_inventory_capacity()
    locations = initial_locations or {}
    confidences = initial_confidences or {}
    result: list[InventoryItem] = []

    for instrument_id, capacity in capacities.items():
        try:
            detected = int(detected_counts.get(instrument_id, 0))
        except (TypeError, ValueError):
            detected = 0
        count = min(max(detected, 0), int(capacity))
        if count <= 0 or instrument_id not in instruments:
            continue
        instrument = instruments[instrument_id]
        location = semantic_location(locations.get(instrument_id, "unknown"))
        try:
            confidence = float(confidences.get(instrument_id, 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if not 0.0 <= confidence <= 1.0:
            confidence = 0.0
        for index in range(1, count + 1):
            result.append(
                InventoryItem(
                    instrument_id=instrument_id,
                    instance_id=f"{instrument_id}#{index}",
                    display_name=str(instrument.display_name),
                    initial_location=location,
                    initial_confidence=confidence,
                    initial_activity_probability=1.0,
                    exchangeable_population=bool(
                        spec.is_exchangeable_population(instrument_id)
                    ),
                )
            )
    return result
