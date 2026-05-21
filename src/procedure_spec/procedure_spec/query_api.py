"""Convenience query API for procedure specs."""

from __future__ import annotations

from collections.abc import Iterable

from .models import ProcedureBundle


class ProcedureSpec:
    """Read-only query surface for the loaded procedure bundle."""

    def __init__(self, bundle: ProcedureBundle):
        self.bundle = bundle
        self._phases = {phase.id: phase for phase in bundle.phases}
        self._instruments = {instrument.id: instrument for instrument in bundle.instruments}
        self._locations = {location.id: location for location in bundle.locations}
        self._alias_index = {
            alias.lower(): instrument.id
            for instrument in bundle.instruments
            for alias in {instrument.id, *instrument.aliases}
        }
        self._initial_placement = {
            placement.instrument_id: placement.location_id
            for placement in bundle.initial_placements
        }
        self._simulation_entities = {entity.id: entity for entity in bundle.simulation_entities}
        self._simulation_anchors = {anchor.id: anchor for anchor in bundle.simulation_anchors}

    @property
    def procedure_id(self) -> str:
        return self.bundle.procedure_id

    @property
    def phase_ids(self) -> list[str]:
        return [phase.id for phase in self.bundle.phases]

    @property
    def default_phase_id(self) -> str:
        return self.bundle.phases[0].id

    def get_phase(self, phase_id: str):
        return self._phases[phase_id]

    def get_allowed_next_phases(self, phase_id: str) -> list[str]:
        return list(self.get_phase(phase_id).possible_next)

    def get_expected_instruments(self, phase_id: str) -> list[str]:
        return list(self.get_phase(phase_id).expected_instruments)

    def get_phase_min_duration(self, phase_id: str) -> float:
        return float(self.get_phase(phase_id).min_duration_sec)

    def get_initial_location(self, instrument_id: str) -> str | None:
        return self._initial_placement.get(instrument_id)

    def get_initial_location_type(self, instrument_id: str) -> str | None:
        location_id = self.get_initial_location(instrument_id)
        if not location_id:
            return None
        return self.get_location_type(location_id)

    def get_location_type(self, location_id: str) -> str:
        return self._locations[location_id].type

    def list_instrument_ids(self) -> list[str]:
        return list(self._instruments.keys())

    def get_mock_perception_period_sec(self, default: float = 1.0) -> float:
        if not self.bundle.mock_perception:
            return float(default)
        return float(self.bundle.mock_perception.period_sec)

    def get_mock_perception_stages(self):
        if not self.bundle.mock_perception:
            return []
        return list(self.bundle.mock_perception.stages)

    def _is_home_reset_stage(self, stage) -> bool:
        if stage.surgeon_gesture is not None or stage.explicit_request.strip():
            return False
        visible_observations = [observation for observation in stage.observations if observation.visible]
        if not visible_observations:
            return False
        for observation in visible_observations:
            home_location_id = self.get_initial_location(observation.instrument_id)
            home_location_type = self.get_initial_location_type(observation.instrument_id)
            if observation.location_id != home_location_id or observation.location_type != home_location_type:
                return False
        return True

    def get_mock_perception_bootstrap_stage_index(self) -> int:
        stages = self.get_mock_perception_stages()
        for index, stage in enumerate(stages):
            if not self._is_home_reset_stage(stage):
                return index
        return 0

    def get_mock_perception_bootstrap_tick(self) -> int:
        stages = self.get_mock_perception_stages()
        stage_index = self.get_mock_perception_bootstrap_stage_index()
        return sum(stage.duration_ticks for stage in stages[:stage_index])

    def get_mock_surgeon_period_sec(self, default: float = 1.0) -> float:
        if not self.bundle.mock_surgeon:
            return float(default)
        return float(self.bundle.mock_surgeon.period_sec)

    def get_mock_surgeon_stages(self):
        if not self.bundle.mock_surgeon:
            return []
        return list(self.bundle.mock_surgeon.stages)

    def get_simulation_entities(self):
        return list(self._simulation_entities.values())

    def get_simulation_anchors(self):
        return list(self._simulation_anchors.values())

    def get_simulation_anchor(self, anchor_id: str):
        return self._simulation_anchors[anchor_id]

    def get_humanoid_policy(self):
        return self.bundle.humanoid_policy

    def resolve_instrument_alias(self, raw_name: str) -> str | None:
        return self._alias_index.get(raw_name.strip().lower())

    def is_transition_allowed(self, current_phase: str, next_phase: str) -> bool:
        if current_phase == next_phase:
            return True
        return next_phase in self.get_allowed_next_phases(current_phase)

    def rank_available_expected_instruments(
        self, phase_id: str, available_instruments: Iterable[str]
    ) -> list[str]:
        available = set(available_instruments)
        return [
            instrument_id
            for instrument_id in self.get_expected_instruments(phase_id)
            if instrument_id in available
        ]
