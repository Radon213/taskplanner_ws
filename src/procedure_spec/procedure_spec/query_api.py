"""Convenience query API for procedure specs."""

from __future__ import annotations

from collections.abc import Iterable
import re

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
        declared_interrupts = [phase_id for phase_id in bundle.interrupt_phase_ids if phase_id in self._phases]
        if declared_interrupts:
            self._interrupt_phase_ids = declared_interrupts
        else:
            self._interrupt_phase_ids = [
                phase.id
                for phase in bundle.phases
                if "interrupt" in phase.id.lower() or "interrupt" in phase.display_name.lower()
            ]
        declared_normals = [phase_id for phase_id in bundle.normal_phase_ids if phase_id in self._phases]
        self._normal_phase_ids = declared_normals or [
            phase.id for phase in bundle.phases if phase.id not in set(self._interrupt_phase_ids)
        ]

    @property
    def procedure_id(self) -> str:
        return self.bundle.procedure_id

    @property
    def phase_ids(self) -> list[str]:
        return [phase.id for phase in self.bundle.phases]

    @property
    def normal_phase_ids(self) -> list[str]:
        return list(self._normal_phase_ids)

    @property
    def interrupt_phase_ids(self) -> list[str]:
        return list(self._interrupt_phase_ids)

    @property
    def default_phase_id(self) -> str:
        return self.bundle.phases[0].id

    def get_phase(self, phase_id: str):
        return self._phases[phase_id]

    def get_allowed_next_phases(self, phase_id: str) -> list[str]:
        return list(self.get_phase(phase_id).possible_next)

    def is_interrupt_phase(self, phase_id: str) -> bool:
        return phase_id in set(self._interrupt_phase_ids)

    def is_normal_phase(self, phase_id: str) -> bool:
        return phase_id in set(self._normal_phase_ids)

    def get_next_normal_phase(self, phase_id: str) -> str:
        if phase_id not in self._normal_phase_ids:
            return ""
        index = self._normal_phase_ids.index(phase_id)
        if index + 1 >= len(self._normal_phase_ids):
            return ""
        return self._normal_phase_ids[index + 1]

    def is_terminal_normal_phase(self, phase_id: str) -> bool:
        return bool(self._normal_phase_ids and phase_id == self._normal_phase_ids[-1])

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

    def get_bed_robot_arm_group_spec(self):
        return self.bundle.bed_robot_arm_groups

    def get_bed_robot_arm_group_cues(self, phase_id: str = ""):
        spec = self.bundle.bed_robot_arm_groups
        if not spec:
            return []
        if not phase_id:
            return list(spec.cues)
        return [cue for cue in spec.cues if cue.phase_id == phase_id]

    def get_bed_robot_arm_end_effector_transitions(self, phase_id: str = ""):
        spec = self.bundle.bed_robot_arm_groups
        if not spec:
            return []
        if not phase_id:
            return list(spec.end_effector_transitions)
        return [transition for transition in spec.end_effector_transitions if transition.phase_id == phase_id]

    def resolve_instrument_alias(self, raw_name: str) -> str | None:
        normalized = raw_name.strip().lower()
        direct = self._alias_index.get(normalized)
        if direct:
            return direct
        numeric_id = re.fullmatch(r"([a-z]+)[\s_-]*0*(\d+)", normalized)
        if not numeric_id:
            return None
        prefix, number = numeric_id.group(1), int(numeric_id.group(2))
        for instrument_id in self._instruments:
            canonical = re.fullmatch(r"([a-z]+)0*(\d+)", instrument_id.lower())
            if canonical and canonical.group(1) == prefix and int(canonical.group(2)) == number:
                return instrument_id
        return None

    def resolve_phase_id(self, raw_phase: str) -> str | None:
        normalized = raw_phase.strip().lower()
        for phase_id in self._phases:
            if phase_id.lower() == normalized:
                return phase_id
        numeric_id = re.fullmatch(r"([a-z]+)[\s_-]*0*(\d+)", normalized)
        if not numeric_id:
            return None
        prefix, number = numeric_id.group(1), int(numeric_id.group(2))
        for phase_id in self._phases:
            canonical = re.fullmatch(r"([a-z]+)0*(\d+)", phase_id.lower())
            if canonical and canonical.group(1) == prefix and int(canonical.group(2)) == number:
                return phase_id
        return None

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
