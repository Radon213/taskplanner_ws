"""Pure scenario-bounded semantic location belief tracker.

The tracker intentionally has no ROS dependency and no control output. Detector
frames, robot-action telemetry, and procedure events only update an advisory
categorical distribution over coarse semantic locations. The scenario-authored
inventory is immutable for the lifetime of a tracker instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Iterable, Mapping, Sequence


FIXED_LOCATIONS = (
    "tray",
    "mayo",
    "field",
    "surgeon",
    "robot",
    "cleaner",
    "unknown",
)
MAX_IGNORED_CLASS_NAMES = 32
MAX_COMMAND_EVIDENCE = 256

ALLOWED_SEMANTIC_EVENT_TYPES = frozenset(
    {
        "robotgraspedtool",
        "robottaskcompleted",
        "shadowadditionaltoolhandovercompleted",
        "toolhandovercompleted",
        "toolreceivedfromsurgeon",
        "toolsenttocleaner",
        "toolcleaningprogress",
        "toolcleaningcompleted",
        "toolprepared",
        "toolretrievedfrommayo",
        "toolreturnedtotray",
        "predictedtoolreturnedtotherack",
        "unusedprepositionreturned",
    }
)

# Best-effort source priors for legacy/type-only events.  A correlated command
# binding always wins; these priors are used only when the event omitted a
# concrete logical slot.
SEMANTIC_EVENT_SOURCE_LOCATIONS = {
    "robotgraspedtool": ("tray", "mayo"),
    "shadowadditionaltoolhandovercompleted": ("robot", "tray", "mayo"),
    "toolhandovercompleted": ("robot", "tray", "mayo"),
    "toolreceivedfromsurgeon": ("surgeon", "mayo", "field"),
    "toolsenttocleaner": ("robot",),
    "toolcleaningprogress": ("cleaner",),
    "toolcleaningcompleted": ("cleaner",),
    "toolprepared": ("tray", "mayo"),
    "toolretrievedfrommayo": ("mayo",),
    "toolreturnedtotray": ("robot", "cleaner", "mayo", "surgeon", "field"),
    "predictedtoolreturnedtotherack": ("robot",),
    "unusedprepositionreturned": ("robot",),
}


def semantic_location(location_id: str, location_type: str = "") -> str:
    """Collapse authored/action location detail to the tracker ontology."""

    text = f"{location_id} {location_type}".strip().casefold()
    if not text:
        return "unknown"
    # cleaner_slot contains the generic token "slot", so ownership-specific
    # locations must win before tray/rack/home fallback matching.
    if any(token in text for token in ("clean", "wash", "steril")):
        return "cleaner"
    if any(token in text for token in ("mayo", "reuse_zone", "recovery_zone")):
        return "mayo"
    if any(token in text for token in ("tray", "rack", "home", "slot")):
        return "tray"
    if any(token in text for token in ("surgeon", "receive", "operator")):
        return "surgeon"
    if any(token in text for token in ("robot", "humanoid", "gripper", "hand_tool")):
        return "robot"
    if any(token in text for token in ("field", "procedure", "patient")):
        return "field"
    return "unknown"


@dataclass(frozen=True, slots=True)
class InventoryItem:
    instrument_id: str
    instance_id: str
    display_name: str
    initial_location: str
    initial_confidence: float = 1.0
    # Probability that this scenario-bounded capacity slot is materialized at
    # bootstrap.  This is deliberately independent of location confidence:
    # an inactive slot starts at unknown with activity 0, while an active slot
    # may still have a low-confidence authored location.
    initial_activity_probability: float = 1.0
    # Procedure-authored identity policy. The runtime-wide mode is only an
    # opt-in capability; legacy fixed populations remain strict when false.
    exchangeable_population: bool = False


@dataclass(frozen=True, slots=True)
class TrackerConfig:
    # Camera likelihood is integrated in time, not once per accepted frame.
    # A 5 Hz camera and a 15 Hz camera therefore converge at nearly the same
    # rate, and one delayed frame cannot act like several seconds of evidence.
    evidence_window_sec: float = 0.05
    max_camera_evidence_dt_sec: float = 0.50
    miss_half_life_sec: float = 3.0
    # A detector-born exchangeable capacity slot can briefly survive a false
    # duplicate or cross-camera association wobble.  Once that slot remains at
    # unknown for this long, return it to dormant capacity so it no longer
    # appears active.
    unknown_slot_retire_sec: float = 10.0
    # All of these values are evidence scales, never admission/commit gates.
    # The same commit hysteresis remains the only location confirmation rule.
    # A detector hit while the robot is crossing the source ROI is often a
    # short class echo from the grasp attempt itself.  Keep it as evidence,
    # but make one such frame too weak to overturn a completed-Action claim.
    robot_motion_positive_scale: float = 0.10
    robot_motion_negative_scale: float = 0.1
    robot_motion_grace_sec: float = 0.75
    mayo_hand_positive_scale: float = 0.25
    mayo_hand_negative_scale: float = 0.05
    mayo_hand_grace_sec: float = 0.50
    # Per-second camera likelihood gain.  It is multiplied by frame elapsed
    # time, detector confidence, and the context scale below.
    positive_gain: float = 3.0
    confirm_threshold: float = 0.85
    # Every location, regardless of whether its evidence came from camera,
    # Action, or Service telemetry, must remain above the same global
    # confirmation threshold before the committed location changes.
    commit_dwell_sec: float = 0.50
    probable_threshold: float = 0.55
    commit_release_threshold: float = 0.45
    commit_switch_margin: float = 0.10
    identity_assignment_margin: float = 0.15
    # Camera-local UV continuity is deliberately short-lived. It may repair a
    # temporary class flip only when one recent instrument anchor is uniquely
    # closer than every competitor; otherwise it merely slows camera evidence.
    uv_memory_sec: float = 1.0
    uv_match_radius_px: float = 90.0
    uv_ambiguity_margin_px: float = 25.0
    uv_relabel_scale: float = 0.40
    uv_ambiguous_evidence_scale: float = 0.15
    # CAM4 is a fixed Mayo view.  In the exchangeable logical-slot model, a
    # new same-type appearance there is normally a surgeon return when that
    # type is presently surgeon-owned and no robot command holds it.  This is
    # an observation policy only; it never creates a control command.
    mayo_appearance_assumes_surgeon_return: bool = True
    # Library callers retain the conservative physical-identity behavior by
    # default.  The ROS node's production config opts into exchangeable logical
    # slots for identical, unmarked tools.
    exchangeable_instances: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.exchangeable_instances, bool):
            raise ValueError("exchangeable_instances must be boolean")
        if not isinstance(self.mayo_appearance_assumes_surgeon_return, bool):
            raise ValueError("mayo_appearance_assumes_surgeon_return must be boolean")
        positive_fields = (
            self.evidence_window_sec,
            self.max_camera_evidence_dt_sec,
            self.miss_half_life_sec,
            self.unknown_slot_retire_sec,
            self.robot_motion_grace_sec,
            self.mayo_hand_grace_sec,
            self.positive_gain,
            self.commit_dwell_sec,
            self.uv_memory_sec,
            self.uv_match_radius_px,
            self.uv_ambiguity_margin_px,
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in positive_fields):
            raise ValueError("tracker timing and gain parameters must be finite and positive")
        bounded_fields = (
            self.robot_motion_positive_scale,
            self.robot_motion_negative_scale,
            self.mayo_hand_positive_scale,
            self.mayo_hand_negative_scale,
            self.confirm_threshold,
            self.probable_threshold,
            self.commit_release_threshold,
            self.commit_switch_margin,
            self.identity_assignment_margin,
            self.uv_relabel_scale,
            self.uv_ambiguous_evidence_scale,
        )
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in bounded_fields):
            raise ValueError("tracker probability parameters must be in [0, 1]")
        if self.probable_threshold > self.confirm_threshold:
            raise ValueError("probable_threshold must not exceed confirm_threshold")
        if self.max_camera_evidence_dt_sec < self.evidence_window_sec:
            raise ValueError(
                "max_camera_evidence_dt_sec must not be below evidence_window_sec"
            )
        if self.uv_ambiguity_margin_px > self.uv_match_radius_px:
            raise ValueError(
                "uv_ambiguity_margin_px must not exceed uv_match_radius_px"
            )


@dataclass(slots=True)
class CommandEvidence:
    command_id: str
    instrument_id: str
    instance_id: str
    source_location: str
    target_location: str
    registered_sec: float
    execution_started: bool = False
    secured_robot_since_sec: float | None = None


@dataclass(slots=True)
class _Track:
    item: InventoryItem
    probabilities: dict[str, float]
    committed_location: str
    last_positive_sec: float | None = None
    last_positive_location: str = ""
    evidence_sources: list[str] = field(default_factory=list)
    active_command_id: str = ""
    model_version: str = ""
    ontology_version: str = ""
    calibration_version: str = ""
    ambiguous_identity: bool = False
    activity_probability: float = 1.0
    exchangeable_assignment: bool = False
    exchangeable_rebound: bool = False
    commit_candidate_location: str = ""
    commit_candidate_since_sec: float | None = None
    unknown_since_sec: float | None = None


@dataclass(frozen=True, slots=True)
class _CameraDetection:
    instrument_id: str
    confidence: float
    uv: tuple[float, float] | None = None
    provider_instrument_id: str = ""
    uv_recovered: bool = False


def _finite_probability(value: float, default: float = 0.0) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(converted):
        return default
    return min(max(converted, 0.0), 1.0)


def _normalized(values: Mapping[str, float]) -> dict[str, float]:
    bounded = {
        location: max(float(values.get(location, 0.0)), 0.0)
        for location in FIXED_LOCATIONS
    }
    total = sum(bounded.values())
    if not math.isfinite(total) or total <= 0.0:
        return {
            location: 1.0 if location == "unknown" else 0.0
            for location in FIXED_LOCATIONS
        }
    return {location: value / total for location, value in bounded.items()}


def _initial_distribution(location: str, confidence: float) -> dict[str, float]:
    semantic = location if location in FIXED_LOCATIONS else "unknown"
    confidence = min(max(_finite_probability(confidence, 1.0), 0.55), 0.98)
    epsilon = 1e-4
    result = {key: epsilon for key in FIXED_LOCATIONS}
    if semantic == "unknown":
        result["unknown"] = 1.0
    else:
        result[semantic] = confidence
        result["unknown"] = max(1.0 - confidence, epsilon)
    return _normalized(result)


class BeliefTracker:
    """Tracks exactly the supplied scenario capacity and never creates a track."""

    def __init__(
        self,
        inventory: Sequence[InventoryItem],
        *,
        config: TrackerConfig | None = None,
    ) -> None:
        if not inventory:
            raise ValueError("scenario capacity must contain at least one item")
        instance_ids = [item.instance_id for item in inventory]
        if any(not instance_id.strip() for instance_id in instance_ids):
            raise ValueError("every inventory item needs an instance_id")
        if len(set(instance_ids)) != len(instance_ids):
            raise ValueError("scenario capacity instance_id values must be unique")

        self.config = config or TrackerConfig()
        self._tracks: dict[str, _Track] = {}
        self._instances_by_instrument: dict[str, list[str]] = {}
        exchangeability_by_instrument: dict[str, bool] = {}
        for item in inventory:
            exchangeable = bool(item.exchangeable_population)
            previous_exchangeable = exchangeability_by_instrument.setdefault(
                item.instrument_id,
                exchangeable,
            )
            if previous_exchangeable != exchangeable:
                raise ValueError(
                    "all capacity slots for one instrument type must share "
                    "exchangeable_population"
                )
            initial = (
                item.initial_location
                if item.initial_location in FIXED_LOCATIONS
                else "unknown"
            )
            probabilities = _initial_distribution(initial, item.initial_confidence)
            committed = initial if probabilities[initial] >= self.config.confirm_threshold else ""
            self._tracks[item.instance_id] = _Track(
                item=item,
                probabilities=probabilities,
                committed_location=committed,
                activity_probability=_finite_probability(
                    item.initial_activity_probability,
                    1.0,
                ),
            )
            self._instances_by_instrument.setdefault(item.instrument_id, []).append(
                item.instance_id
            )
        for instance_ids_for_type in self._instances_by_instrument.values():
            instance_ids_for_type.sort()

        self._last_view_frame_sec: dict[str, float] = {}
        # One short-lived anchor per (camera view, instrument type). Types with
        # multiple active logical slots intentionally have no UV identity.
        self._visual_anchors: dict[
            tuple[str, str], tuple[float, float, float]
        ] = {}
        self._commands: dict[str, CommandEvidence] = {}
        # Terminal receipts are removed from event association immediately but
        # retained as bounded tombstones so stale SimulationState repetitions
        # cannot resurrect a completed command.
        self._retired_commands: dict[str, CommandEvidence] = {}
        self._robot_motion_last_active_sec: float | None = None
        self._robot_motion_reported_active = False
        self._active_task_id = ""
        self._mayo_hand_reported_present = False
        self._mayo_hand_last_present_sec: float | None = None
        # A run prior is an operator-approved snapshot of the currently shown
        # bounded population.  It fixes count/existence, while all subsequent
        # locations remain ordinary probability distributions.
        self._run_prior_frozen = False
        self._ignored_count = 0
        self._ignored_class_names: set[str] = set()
        self._ignored_ambiguous_command_count = 0

    @property
    def instance_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._tracks))

    @property
    def instrument_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._instances_by_instrument))

    @property
    def ignored_out_of_inventory_count(self) -> int:
        return self._ignored_count

    @property
    def ignored_class_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._ignored_class_names))

    @property
    def ignored_ambiguous_command_count(self) -> int:
        return self._ignored_ambiguous_command_count

    def note_ignored_class(self, class_name: str) -> None:
        bounded = str(class_name or "").strip()[:80]
        self._ignored_count += 1
        if bounded and (
            bounded in self._ignored_class_names
            or len(self._ignored_class_names) < MAX_IGNORED_CLASS_NAMES
        ):
            self._ignored_class_names.add(bounded)

    def _note_capacity_overflow(self, instrument_id: str, count: int) -> None:
        """Audit known-class detections beyond the authored slot capacity."""

        overflow = max(int(count), 0)
        if overflow <= 0:
            return
        self._ignored_count += overflow
        marker = f"{str(instrument_id or '').strip()[:60]}:capacity_overflow"
        if marker and (
            marker in self._ignored_class_names
            or len(self._ignored_class_names) < MAX_IGNORED_CLASS_NAMES
        ):
            self._ignored_class_names.add(marker)

    def update_config(self, config: TrackerConfig) -> None:
        """Atomically replace validated inference/timing parameters."""

        if not isinstance(config, TrackerConfig):
            raise TypeError("config must be a TrackerConfig")
        self.config = config
        self._update_commits()

    def set_robot_motion(self, active: bool, timestamp_sec: float, task_id: str = "") -> None:
        now = float(timestamp_sec)
        self._robot_motion_reported_active = bool(active)
        self._active_task_id = str(task_id or "")[:128] if active else ""
        if active:
            self._robot_motion_last_active_sec = now

    def set_mayo_hand_present(self, present: bool, timestamp_sec: float) -> None:
        """Record Mayo hand occupancy as soft occlusion context.

        This deliberately does not create a surgeon-location transition.  A
        hand in the Mayo ROI can hide a tool or return one; absent a correlated
        human/robot event, the camera may only change the probability it sees.
        """

        now = float(timestamp_sec)
        self._mayo_hand_reported_present = bool(present)
        if present:
            self._mayo_hand_last_present_sec = now

    def mayo_hand_present(self, now_sec: float) -> bool:
        if self._mayo_hand_reported_present:
            return True
        if self._mayo_hand_last_present_sec is None:
            return False
        return (
            float(now_sec) - self._mayo_hand_last_present_sec
            <= self.config.mayo_hand_grace_sec
        )

    def robot_motion_active(self, now_sec: float) -> bool:
        if self._robot_motion_reported_active:
            return True
        if self._robot_motion_last_active_sec is None:
            return False
        return (
            float(now_sec) - self._robot_motion_last_active_sec
            <= self.config.robot_motion_grace_sec
        )

    def motion_mode(self, now_sec: float) -> str:
        if self._robot_motion_reported_active:
            return "active"
        if self.robot_motion_active(now_sec):
            return "grace"
        return "idle"

    def _camera_context_scales(self, zone: str, now_sec: float) -> tuple[float, float]:
        """Return soft camera likelihood scales for the current scene context."""

        positive_scale = 1.0
        negative_scale = 1.0
        if self.robot_motion_active(now_sec):
            positive_scale *= self.config.robot_motion_positive_scale
            negative_scale *= self.config.robot_motion_negative_scale
        if zone == "mayo" and self.mayo_hand_present(now_sec):
            positive_scale *= self.config.mayo_hand_positive_scale
            negative_scale *= self.config.mayo_hand_negative_scale
        return positive_scale, negative_scale

    def freeze_run_prior(self) -> "BeliefTracker":
        """Return a fixed-population tracker seeded from the shown live belief.

        The operator starts only after reviewing the pre-run belief display.
        At that click the active logical slots and their distributions become
        the run prior.  Later camera evidence may move *location* probability,
        but cannot materialize another capacity slot or alter total counts.
        """

        selected: list[_Track] = []
        for track in self._tracks.values():
            if not self._exchangeable_type(track.item.instrument_id):
                selected.append(track)
                continue
            if track.activity_probability >= self.config.probable_threshold:
                selected.append(track)
        if not selected:
            raise ValueError("operator-verified run prior has no active tool slots")

        selected.sort(key=lambda track: track.item.instance_id)
        inventory = [
            InventoryItem(
                instrument_id=track.item.instrument_id,
                instance_id=track.item.instance_id,
                display_name=track.item.display_name,
                initial_location=max(
                    track.probabilities,
                    key=track.probabilities.get,
                ),
                initial_confidence=max(track.probabilities.values()),
                initial_activity_probability=1.0,
                exchangeable_population=track.item.exchangeable_population,
            )
            for track in selected
        ]
        frozen = BeliefTracker(inventory, config=self.config)
        for prior_track in selected:
            target = frozen._tracks[prior_track.item.instance_id]
            target.probabilities = dict(prior_track.probabilities)
            target.committed_location = prior_track.committed_location
            target.last_positive_sec = prior_track.last_positive_sec
            target.last_positive_location = prior_track.last_positive_location
            target.evidence_sources = list(prior_track.evidence_sources)
            target.model_version = prior_track.model_version
            target.ontology_version = prior_track.ontology_version
            target.calibration_version = prior_track.calibration_version
            target.ambiguous_identity = prior_track.ambiguous_identity
            target.activity_probability = 1.0
            target.exchangeable_assignment = prior_track.exchangeable_assignment
            target.exchangeable_rebound = prior_track.exchangeable_rebound
            target.commit_candidate_location = ""
            target.commit_candidate_since_sec = None
            target.unknown_since_sec = prior_track.unknown_since_sec
            frozen._append_source(target, "run_prior:operator_verified")
        frozen._mayo_hand_reported_present = self._mayo_hand_reported_present
        frozen._mayo_hand_last_present_sec = self._mayo_hand_last_present_sec
        frozen._robot_motion_reported_active = self._robot_motion_reported_active
        frozen._robot_motion_last_active_sec = self._robot_motion_last_active_sec
        frozen._active_task_id = self._active_task_id
        frozen._visual_anchors = {
            key: value
            for key, value in self._visual_anchors.items()
            if key[1] in frozen._instances_by_instrument
        }
        frozen._run_prior_frozen = True
        return frozen

    def _tracks_for_instrument(self, instrument_id: str) -> list[_Track]:
        return [
            self._tracks[instance_id]
            for instance_id in self._instances_by_instrument.get(
                str(instrument_id or ""),
                (),
            )
        ]

    def _exchangeable_type(self, instrument_id: str) -> bool:
        instance_ids = self._instances_by_instrument.get(
            str(instrument_id or ""),
            (),
        )
        return bool(
            self.config.exchangeable_instances
            and instance_ids
            and self._tracks[instance_ids[0]].item.exchangeable_population
        )

    @staticmethod
    def _relabel_locked(track: _Track) -> bool:
        """Return whether a logical label must remain bound to strong evidence.

        An executing Action is an absolute lock.  Robot, surgeon, and cleaner
        locations are also kept stable because they represent custody/recovery
        evidence that must not be relabelled merely to fit a camera count.
        """

        return bool(
            track.active_command_id
            or track.committed_location in {"robot", "surgeon", "cleaner"}
        )

    def _may_rebind_surgeon_owned_to_mayo(
        self,
        track: _Track,
        zone: str,
    ) -> bool:
        """Allow an uncontrolled Mayo appearance to represent a human return.

        A running robot Action remains a hard lock.  By contrast, after a
        completed handover the tracker has no independent actor identity for a
        newly visible same-type tool on the Mayo stand.  With exchangeable
        logical slots, treating that appearance as the surgeon-owned slot's
        return preserves the useful location/count fact instead of inventing a
        dormant duplicate.  The behavior is runtime-tunable for experiments.
        """

        return bool(
            self.config.mayo_appearance_assumes_surgeon_return
            and zone == "mayo"
            and not track.active_command_id
            and track.committed_location == "surgeon"
        )

    def _select_exchangeable_slot(
        self,
        instrument_id: str,
        *,
        source_location: str = "unknown",
        target_location: str = "unknown",
        exclude_active_commands: bool = True,
    ) -> _Track | None:
        """Select one deterministic logical slot without exceeding capacity."""

        candidates = self._tracks_for_instrument(instrument_id)
        if exclude_active_commands:
            candidates = [track for track in candidates if not track.active_command_id]
        if not candidates:
            return None
        source = (
            source_location
            if source_location in FIXED_LOCATIONS
            else semantic_location(source_location)
        )
        target = (
            target_location
            if target_location in FIXED_LOCATIONS
            else semantic_location(target_location)
        )

        def rank(track: _Track) -> tuple[float | int | str, ...]:
            source_known = source != "unknown"
            target_known = target != "unknown"
            return (
                0 if source_known and track.committed_location == source else 1,
                -track.probabilities.get(source, 0.0) if source_known else 0.0,
                track.probabilities.get(target, 0.0) if target_known else 0.0,
                -track.activity_probability,
                track.item.instance_id,
            )

        return min(candidates, key=rank)

    def _swap_exchangeable_beliefs(self, left: _Track, right: _Track, source: str) -> None:
        """Permute logical labels while preserving count and Action locks."""

        if left is right or self._relabel_locked(left) or self._relabel_locked(right):
            return
        fields_to_swap = (
            "probabilities",
            "committed_location",
            "last_positive_sec",
            "last_positive_location",
            "evidence_sources",
            "model_version",
            "ontology_version",
            "calibration_version",
            "ambiguous_identity",
            "activity_probability",
            "exchangeable_assignment",
            "exchangeable_rebound",
            "unknown_since_sec",
        )
        for name in fields_to_swap:
            left_value = getattr(left, name)
            setattr(left, name, getattr(right, name))
            setattr(right, name, left_value)
        left.ambiguous_identity = False
        right.ambiguous_identity = False
        left.exchangeable_assignment = True
        right.exchangeable_assignment = True
        left.exchangeable_rebound = True
        right.exchangeable_rebound = True
        self._append_source(left, f"exchangeable_rebind:{source}")
        self._append_source(right, f"exchangeable_rebind:{source}")

    def _rebind_requested_instance_to_source(
        self,
        requested: _Track,
        source_location: str,
    ) -> None:
        """Make a public Action ID denote the best free source-located slot.

        Identical physical tools have no stable visual identity.  Rebinding is
        therefore a permutation of logical labels, never creation or deletion
        of an inventory slot.  Strongly held/active labels are not exchanged.
        """

        if not self._exchangeable_type(requested.item.instrument_id):
            return
        source = (
            source_location
            if source_location in FIXED_LOCATIONS
            else semantic_location(source_location)
        )
        if source == "unknown" or self._relabel_locked(requested):
            return
        best = self._select_exchangeable_slot(
            requested.item.instrument_id,
            source_location=source,
            exclude_active_commands=True,
        )
        if best is None or best is requested or self._relabel_locked(best):
            return
        requested_score = requested.probabilities.get(source, 0.0)
        best_score = best.probabilities.get(source, 0.0)
        if (
            best.committed_location != source
            and best_score - requested_score < self.config.identity_assignment_margin
        ):
            return
        if best_score <= requested_score:
            return
        self._swap_exchangeable_beliefs(requested, best, source)

    def register_command(
        self,
        command_id: str,
        instrument_id: str,
        instance_id: str,
        source_location: str,
        target_location: str,
        timestamp_sec: float,
    ) -> bool:
        command_id = str(command_id or "").strip()
        if not command_id:
            return False
        instrument_id = str(instrument_id or "").strip()
        requested_instance = str(instance_id or "").strip()
        source = semantic_location(source_location)
        target = semantic_location(target_location)
        retired = self._retired_commands.get(command_id)
        if retired is not None:
            if (
                instrument_id
                and retired.instrument_id != instrument_id
            ) or (
                requested_instance
                and retired.instance_id != requested_instance
            ):
                return False
            return True
        existing = self._commands.get(command_id)
        if existing is not None:
            if (
                instrument_id
                and existing.instrument_id != instrument_id
            ) or (
                requested_instance
                and existing.instance_id != requested_instance
            ):
                return False
            # SimulationState repeats the active task on every frame. Preserve
            # the accepted/executing receipt instead of resetting it to a new
            # pre-dispatch command on each repetition.
            if existing.source_location == "unknown":
                existing.source_location = source
            if existing.target_location == "unknown":
                existing.target_location = target
            track = self._tracks.get(existing.instance_id)
            if track is None:
                return False
            track.active_command_id = command_id
            track.activity_probability = 1.0
            return True

        resolved_track: _Track | None = None
        if requested_instance:
            resolved_track = self._tracks.get(requested_instance)
            if resolved_track is None:
                return False
            if instrument_id and resolved_track.item.instrument_id != instrument_id:
                return False
            if (
                resolved_track.active_command_id
                and self._exchangeable_type(resolved_track.item.instrument_id)
            ):
                return False
            instrument_id = resolved_track.item.instrument_id
            self._rebind_requested_instance_to_source(resolved_track, source)
        else:
            candidates = self._tracks_for_instrument(instrument_id)
            if len(candidates) == 1 and not candidates[0].active_command_id:
                resolved_track = candidates[0]
            elif len(candidates) > 1 and self._exchangeable_type(instrument_id):
                resolved_track = self._select_exchangeable_slot(
                    instrument_id,
                    source_location=source,
                    target_location=target,
                )
                if resolved_track is not None:
                    resolved_track.exchangeable_assignment = True
                    self._append_source(
                        resolved_track,
                        f"exchangeable_command_bind:{source}",
                    )
            elif len(candidates) > 1:
                self._ignored_ambiguous_command_count += 1
                return False
        if resolved_track is None:
            return False
        resolved_instance = resolved_track.item.instance_id
        # Status streams occasionally omit a terminal frame. Keep that from
        # turning a long-running observer into an unbounded command ledger.
        while len(self._commands) >= MAX_COMMAND_EVIDENCE:
            stale_command_id = next(iter(self._commands))
            self._commands.pop(stale_command_id, None)
            for track in self._tracks.values():
                if track.active_command_id == stale_command_id:
                    track.active_command_id = ""
        self._commands[command_id] = CommandEvidence(
            command_id=command_id,
            instrument_id=instrument_id,
            instance_id=resolved_instance,
            source_location=source,
            target_location=target,
            registered_sec=float(timestamp_sec),
            execution_started=False,
        )
        resolved_track.active_command_id = command_id
        resolved_track.activity_probability = 1.0
        return True

    def _append_source(self, track: _Track, source: str) -> None:
        bounded = str(source or "").strip()[:80]
        if not bounded:
            return
        if bounded in track.evidence_sources:
            track.evidence_sources.remove(bounded)
        track.evidence_sources.append(bounded)
        del track.evidence_sources[:-6]

    def _boost(
        self,
        track: _Track,
        location: str,
        log_gain: float,
        *,
        source: str,
    ) -> None:
        if location not in FIXED_LOCATIONS:
            location = "unknown"
        multiplier = math.exp(min(max(float(log_gain), 0.0), 12.0))
        updated = dict(track.probabilities)
        updated[location] = max(updated.get(location, 0.0), 1e-8) * multiplier
        track.probabilities = _normalized(updated)
        self._append_source(track, source)

    def _mix_distribution(
        self,
        track: _Track,
        evidence: Mapping[str, float],
        weight: float,
        *,
        source: str,
    ) -> None:
        weight = _finite_probability(weight)
        likelihood = _normalized(evidence)
        track.probabilities = _normalized(
            {
                location: (1.0 - weight) * track.probabilities[location]
                + weight * likelihood[location]
                for location in FIXED_LOCATIONS
            }
        )
        self._append_source(track, source)

    def _mix_zone_occupancy(
        self,
        track: _Track,
        location: str,
        occupancy: float,
        confidence: float,
        dt_sec: float,
        positive_scale: float,
        negative_scale: float,
        *,
        source: str,
    ) -> None:
        """Move one slot marginal toward a count-bounded occupancy.

        Strict mode can share k indistinguishable detections across n physical
        identities. Exchangeable mode instead calls this with occupancy one for
        each of k selected logical slots and applies misses to the remaining
        capacity slots.
        """

        occupancy = _finite_probability(occupancy)
        non_location_mass = sum(
            probability
            for candidate, probability in track.probabilities.items()
            if candidate != location
        )
        target = {candidate: 0.0 for candidate in FIXED_LOCATIONS}
        target[location] = occupancy
        remainder = 1.0 - occupancy
        if non_location_mass > 0.0:
            for candidate in FIXED_LOCATIONS:
                if candidate != location:
                    target[candidate] = (
                        remainder
                        * track.probabilities[candidate]
                        / non_location_mass
                    )
        else:
            target["unknown"] += remainder
        weight = self._camera_evidence_weight(
            confidence,
            dt_sec,
            positive_scale,
        )
        if track.probabilities[location] > occupancy:
            # Reducing occupancy is count-negative evidence. Robot motion can
            # hide one identical instance, so attenuate only this downward
            # component while retaining full positive evidence.
            weight *= min(max(float(negative_scale), 0.0), 1.0)
        self._mix_distribution(track, target, weight, source=source)

    def _camera_evidence_weight(
        self,
        confidence: float,
        dt_sec: float,
        context_scale: float,
    ) -> float:
        """Convert a continuous-time camera likelihood to a bounded mix weight."""

        elapsed = min(
            max(float(dt_sec), 0.0),
            self.config.max_camera_evidence_dt_sec,
        )
        rate = (
            self.config.positive_gain
            * _finite_probability(confidence)
            * min(max(float(context_scale), 0.0), 1.0)
        )
        return 1.0 - math.exp(-rate * elapsed)

    def _activate_slot(
        self,
        track: _Track,
        confidence: float,
        dt_sec: float = 1.0,
        positive_scale: float = 1.0,
    ) -> None:
        if self._run_prior_frozen:
            # The operator-approved run population is immutable.  Camera
            # evidence can change only location posterior after start.
            track.activity_probability = 1.0
            return
        weight = self._camera_evidence_weight(
            confidence,
            dt_sec,
            positive_scale,
        )
        track.activity_probability = _finite_probability(
            track.activity_probability
            + (1.0 - track.activity_probability) * weight
        )

    def _apply_miss(self, track: _Track, zone: str, dt_sec: float, scale: float) -> None:
        if zone not in FIXED_LOCATIONS or zone == "unknown":
            return
        dt_sec = max(float(dt_sec), 0.0)
        effective = dt_sec * min(max(float(scale), 0.0), 1.0)
        retention = math.exp(-math.log(2.0) * effective / self.config.miss_half_life_sec)
        was_zone_slot = bool(
            track.committed_location == zone
            or max(track.probabilities, key=track.probabilities.get) == zone
        )
        updated = dict(track.probabilities)
        retained = updated[zone] * retention
        lost = updated[zone] - retained
        updated[zone] = retained
        updated["unknown"] += lost
        track.probabilities = _normalized(updated)
        if (
            not self._run_prior_frozen
            and
            self._exchangeable_type(track.item.instrument_id)
            and was_zone_slot
            and not self._relabel_locked(track)
        ):
            track.activity_probability = _finite_probability(
                track.activity_probability * retention
            )

    def _single_visual_slot(self, instrument_id: str) -> bool:
        """Return whether one camera anchor can name this type unambiguously."""

        tracks = self._tracks_for_instrument(instrument_id)
        if self._run_prior_frozen:
            return len(tracks) == 1
        active = [
            track
            for track in tracks
            if track.activity_probability >= self.config.probable_threshold
        ]
        return len(active) == 1

    def _retire_stale_unknown_capacity_slots(self, now_sec: float) -> None:
        """Return stale detector-born unknown slots to dormant capacity.

        Authored active inventory and command-held slots are deliberately not
        eligible.  This only reverses a prior camera activation of spare
        exchangeable capacity after it stays unresolved for the grace period.
        """

        if self._run_prior_frozen:
            return
        now = float(now_sec)
        for track in self._tracks.values():
            if (
                not self._exchangeable_type(track.item.instrument_id)
                or track.item.initial_activity_probability
                >= self.config.probable_threshold
                or track.activity_probability < self.config.probable_threshold
            ):
                track.unknown_since_sec = None
                continue
            top_location = max(
                track.probabilities,
                key=track.probabilities.get,
            )
            if (
                track.active_command_id
                or track.committed_location not in {"", "unknown"}
                or top_location != "unknown"
            ):
                track.unknown_since_sec = None
                continue
            if (
                track.unknown_since_sec is None
                or (
                    track.last_positive_sec is not None
                    and track.last_positive_sec > track.unknown_since_sec
                )
            ):
                track.unknown_since_sec = now
                continue
            if (
                now - track.unknown_since_sec
                < self.config.unknown_slot_retire_sec
            ):
                continue
            track.activity_probability = 0.0
            track.committed_location = ""
            track.probabilities = {
                location: 1.0 if location == "unknown" else 0.0
                for location in FIXED_LOCATIONS
            }
            track.ambiguous_identity = False
            track.exchangeable_assignment = False
            track.exchangeable_rebound = False
            self._clear_commit_candidate(track)
            self._append_source(track, "stale_unknown:capacity_retired")

    @staticmethod
    def _uv_distance(
        left: tuple[float, float],
        right: tuple[float, float],
    ) -> float:
        return math.hypot(left[0] - right[0], left[1] - right[1])

    @staticmethod
    def _camera_detection(raw: object) -> _CameraDetection | None:
        if isinstance(raw, _CameraDetection):
            return raw
        if not isinstance(raw, Sequence) or len(raw) < 2:
            return None
        instrument_id = str(raw[0] or "").strip()
        confidence = _finite_probability(raw[1])
        if not instrument_id or confidence <= 0.0:
            return None
        uv: tuple[float, float] | None = None
        if len(raw) >= 4:
            try:
                u_value = float(raw[2])
                v_value = float(raw[3])
            except (TypeError, ValueError):
                pass
            else:
                if math.isfinite(u_value) and math.isfinite(v_value):
                    uv = (u_value, v_value)
        return _CameraDetection(
            instrument_id=instrument_id,
            confidence=confidence,
            uv=uv,
            provider_instrument_id=instrument_id,
        )

    def _resolve_uv_continuity(
        self,
        *,
        view: str,
        detections: list[_CameraDetection],
        timestamp_sec: float,
    ) -> tuple[list[_CameraDetection], set[str]]:
        """Repair one unique class conflict and slow every ambiguous one.

        UV is camera-local, short-lived evidence. It never creates a tool or
        commits a location. A provider label may be re-associated only when
        its nearest recent instrument anchor is unique in both directions and
        a separate, at-least-as-confident detection already occupies the
        provider-labelled instrument. This is the simple fixed-count case of
        one real Bovie plus one reflected Adson labelled as a second Bovie.
        """

        now = float(timestamp_sec)
        for key, (_u_value, _v_value, seen_sec) in tuple(
            self._visual_anchors.items()
        ):
            if (
                key[0] == view
                and (
                    now < seen_sec
                    or now - seen_sec > self.config.uv_memory_sec
                    or not self._single_visual_slot(key[1])
                )
            ):
                self._visual_anchors.pop(key, None)

        anchors = {
            instrument_id: (u_value, v_value)
            for (anchor_view, instrument_id), (
                u_value,
                v_value,
                seen_sec,
            ) in self._visual_anchors.items()
            if anchor_view == view
            and 0.0 <= now - seen_sec <= self.config.uv_memory_sec
            and self._single_visual_slot(instrument_id)
        }
        uv_indices = [
            index
            for index, detection in enumerate(detections)
            if detection.uv is not None
        ]
        if not uv_indices:
            return detections, set()

        ambiguous_instruments: set[str] = set()
        # Very close detections are overlap evidence. Use their labels and all
        # nearby anchors only to reduce evidence strength, never to swap IDs.
        for offset, left_index in enumerate(uv_indices):
            left = detections[left_index]
            assert left.uv is not None
            for right_index in uv_indices[offset + 1 :]:
                right = detections[right_index]
                assert right.uv is not None
                if (
                    self._uv_distance(left.uv, right.uv)
                    > self.config.uv_ambiguity_margin_px
                ):
                    continue
                ambiguous_instruments.update(
                    (left.instrument_id, right.instrument_id)
                )
                for instrument_id, anchor_uv in anchors.items():
                    if min(
                        self._uv_distance(left.uv, anchor_uv),
                        self._uv_distance(right.uv, anchor_uv),
                    ) <= self.config.uv_match_radius_px:
                        ambiguous_instruments.add(instrument_id)

        detection_choice: dict[int, str] = {}
        distances_by_detection: dict[int, list[tuple[float, str]]] = {}
        distances_by_anchor: dict[str, list[tuple[float, int]]] = {
            instrument_id: [] for instrument_id in anchors
        }
        for index in uv_indices:
            detection = detections[index]
            assert detection.uv is not None
            candidates = sorted(
                (
                    self._uv_distance(detection.uv, anchor_uv),
                    instrument_id,
                )
                for instrument_id, anchor_uv in anchors.items()
                if self._uv_distance(detection.uv, anchor_uv)
                <= self.config.uv_match_radius_px
            )
            distances_by_detection[index] = candidates
            for distance, instrument_id in candidates:
                distances_by_anchor[instrument_id].append((distance, index))
            if not candidates:
                continue
            if (
                len(candidates) > 1
                and candidates[1][0] - candidates[0][0]
                < self.config.uv_ambiguity_margin_px
            ):
                ambiguous_instruments.update(
                    instrument_id for _distance, instrument_id in candidates
                )
                ambiguous_instruments.add(detection.instrument_id)
                continue
            detection_choice[index] = candidates[0][1]

        anchor_choice: dict[str, int] = {}
        for instrument_id, candidates in distances_by_anchor.items():
            candidates.sort()
            if not candidates:
                continue
            if (
                len(candidates) > 1
                and candidates[1][0] - candidates[0][0]
                < self.config.uv_ambiguity_margin_px
            ):
                ambiguous_instruments.add(instrument_id)
                ambiguous_instruments.update(
                    detections[index].instrument_id
                    for _distance, index in candidates[:2]
                )
                continue
            anchor_choice[instrument_id] = candidates[0][1]

        matched = {
            index: instrument_id
            for index, instrument_id in detection_choice.items()
            if (
                anchor_choice.get(instrument_id) == index
                and instrument_id not in ambiguous_instruments
                and detections[index].instrument_id
                not in ambiguous_instruments
            )
        }
        effective = list(detections)
        for index, anchor_instrument_id in matched.items():
            detection = detections[index]
            if detection.instrument_id == anchor_instrument_id:
                continue
            claimed_elsewhere = any(
                other_index != index
                and other_anchor == detection.instrument_id
                and detections[other_index].instrument_id
                == detection.instrument_id
                and detections[other_index].confidence >= detection.confidence
                for other_index, other_anchor in matched.items()
            )
            if not claimed_elsewhere:
                ambiguous_instruments.update(
                    (detection.instrument_id, anchor_instrument_id)
                )
                continue
            effective[index] = _CameraDetection(
                instrument_id=anchor_instrument_id,
                confidence=(
                    detection.confidence * self.config.uv_relabel_scale
                ),
                uv=detection.uv,
                provider_instrument_id=detection.instrument_id,
                uv_recovered=True,
            )

        by_instrument: dict[str, list[_CameraDetection]] = {}
        for detection in effective:
            by_instrument.setdefault(detection.instrument_id, []).append(
                detection
            )
        for instrument_id, rows in by_instrument.items():
            if (
                instrument_id in ambiguous_instruments
                or not self._single_visual_slot(instrument_id)
                or len(rows) != 1
                or rows[0].uv is None
            ):
                continue
            u_value, v_value = rows[0].uv
            self._visual_anchors[(view, instrument_id)] = (
                u_value,
                v_value,
                now,
            )
        return effective, ambiguous_instruments

    def apply_camera_frame(
        self,
        *,
        view: str,
        zone: str,
        detections: Iterable[
            tuple[str, float] | tuple[str, float, float, float]
        ],
        timestamp_sec: float,
        health_valid: bool,
        model_version: str = "",
        ontology_version: str = "",
        calibration_version: str = "",
    ) -> bool:
        """Apply one rate-limited per-view frame.

        Each detection is ``(instrument_id, class_confidence)`` or additionally
        carries camera-local ``(u_px, v_px)``. Unmapped or out-of-inventory
        provider classes are counted by :meth:`note_ignored_class` at the ROS
        boundary and never passed here.
        """

        now = float(timestamp_sec)
        view = str(view or "").strip()
        zone = zone if zone in FIXED_LOCATIONS else semantic_location(zone)
        if not health_valid or not view or zone == "unknown":
            return False
        previous = self._last_view_frame_sec.get(view)
        if previous is not None and now < previous:
            previous = None
        if previous is not None and now - previous < self.config.evidence_window_sec:
            return False
        # The first receipt gets one bounded nominal exposure.  Thereafter the
        # elapsed receipt time, rather than the number of frames, determines
        # camera influence.  This prevents a 0.90 CAM4 class wobble from
        # committing a location after a single frame and makes 5/15 Hz
        # sources comparable.
        elapsed_sec = (
            min(0.25, self.config.max_camera_evidence_dt_sec)
            if previous is None
            else max(now - previous, 0.0)
        )
        # A delayed positive frame is one bounded observation, while a camera
        # that continuously sees no tool should still decay that zone toward
        # unknown over its true elapsed absence.
        positive_dt_sec = min(
            elapsed_sec,
            self.config.max_camera_evidence_dt_sec,
        )
        self._last_view_frame_sec[view] = now

        parsed = [
            detection
            for raw in detections
            if (detection := self._camera_detection(raw)) is not None
            and detection.instrument_id in self._instances_by_instrument
        ]
        parsed, uv_ambiguous_instruments = self._resolve_uv_continuity(
            view=view,
            detections=parsed,
            timestamp_sec=now,
        )
        grouped: dict[str, list[_CameraDetection]] = {}
        for detection in parsed:
            if detection.instrument_id not in self._instances_by_instrument:
                continue
            grouped.setdefault(detection.instrument_id, []).append(detection)

        positive_scale, negative_scale = self._camera_context_scales(zone, now)
        for instrument_id, instance_ids in self._instances_by_instrument.items():
            instrument_detections = sorted(
                grouped.get(instrument_id, []),
                key=lambda detection: detection.confidence,
                reverse=True,
            )
            tracks = [self._tracks[instance_id] for instance_id in instance_ids]
            overflow = max(len(instrument_detections) - len(tracks), 0)
            if overflow:
                self._note_capacity_overflow(instrument_id, overflow)
                instrument_detections = instrument_detections[: len(tracks)]
            instrument_positive_scale = positive_scale
            instrument_negative_scale = negative_scale
            if instrument_id in uv_ambiguous_instruments:
                instrument_positive_scale *= (
                    self.config.uv_ambiguous_evidence_scale
                )
                instrument_negative_scale *= (
                    self.config.uv_ambiguous_evidence_scale
                )
                for track in tracks:
                    self._append_source(
                        track,
                        f"camera:{view}:uv_ambiguous_scaled",
                    )
            if not instrument_detections:
                for track in tracks:
                    self._apply_miss(
                        track,
                        zone,
                        elapsed_sec,
                        instrument_negative_scale,
                    )
                continue

            if self._exchangeable_type(instrument_id):
                # Logical slot labels are explicitly not physical identities.
                # Assign each of the bounded k detections to one concrete free
                # slot, retaining exact location counts instead of k/n symmetric
                # marginals. A stale/unobserved active slot at another zone is
                # relocation evidence and moves before dormant capacity is born.
                # Only a recent positive camera receipt protects it as a
                # simultaneous duplicate.
                simultaneous_window_sec = max(
                    2.0 * self.config.evidence_window_sec,
                    0.5,
                )

                def fresh_positive_elsewhere(track: _Track) -> bool:
                    if self._may_rebind_surgeon_owned_to_mayo(track, zone):
                        return False
                    if (
                        not track.last_positive_location
                        or track.last_positive_location == zone
                        or track.last_positive_sec is None
                    ):
                        return False
                    age = now - track.last_positive_sec
                    return 0.0 <= age <= simultaneous_window_sec

                eligible = [
                    track
                    for track in tracks
                    if not fresh_positive_elsewhere(track)
                    and (
                        not self._relabel_locked(track)
                        or track.committed_location == zone
                        or self._may_rebind_surgeon_owned_to_mayo(track, zone)
                    )
                ]

                def exchangeable_camera_rank(
                    track: _Track,
                ) -> tuple[float | int | str, ...]:
                    top_location = max(
                        track.probabilities,
                        key=track.probabilities.get,
                    )
                    if track.committed_location == zone or top_location == zone:
                        location_class = 0
                    elif self._may_rebind_surgeon_owned_to_mayo(track, zone):
                        # Prefer the returned human-held slot before a dormant
                        # capacity slot, while preserving any already-visible
                        # Mayo slot above it when the detector count did not
                        # actually increase.
                        location_class = 1
                    elif (
                        track.activity_probability >= self.config.probable_threshold
                        and (
                            bool(track.committed_location)
                            or top_location != "unknown"
                        )
                    ):
                        location_class = 2
                    elif (
                        track.activity_probability < self.config.probable_threshold
                        or not track.committed_location
                        or top_location == "unknown"
                    ):
                        location_class = 3
                    else:
                        location_class = 4
                    return (
                        location_class,
                        -track.probabilities[zone],
                        1 if track.active_command_id else 0,
                        track.item.instance_id,
                    )

                ranked_tracks = sorted(eligible, key=exchangeable_camera_rank)
                if len(instrument_detections) > len(ranked_tracks):
                    self._note_capacity_overflow(
                        instrument_id,
                        len(instrument_detections) - len(ranked_tracks),
                    )
                matched_ids: set[str] = set()
                for detection, track in zip(
                    instrument_detections[: len(tracks)],
                    ranked_tracks,
                ):
                    confidence = detection.confidence
                    assumed_surgeon_return = self._may_rebind_surgeon_owned_to_mayo(
                        track,
                        zone,
                    )
                    track.ambiguous_identity = False
                    track.exchangeable_assignment = True
                    self._mix_zone_occupancy(
                        track,
                        zone,
                        1.0,
                        confidence,
                        positive_dt_sec,
                        instrument_positive_scale,
                        instrument_negative_scale,
                        source=(
                            f"camera:{view}:uv_class_continuity"
                            if detection.uv_recovered
                            else f"camera:{view}:assumed_surgeon_return"
                            if assumed_surgeon_return
                            else f"camera:{view}:exchangeable_slot"
                        ),
                    )
                    self._activate_slot(
                        track,
                        confidence,
                        positive_dt_sec,
                        instrument_positive_scale,
                    )
                    track.last_positive_sec = now
                    track.last_positive_location = zone
                    track.model_version = str(model_version or "")[:120]
                    track.ontology_version = str(ontology_version or "")[:120]
                    track.calibration_version = str(calibration_version or "")[:120]
                    matched_ids.add(track.item.instance_id)
                for track in tracks:
                    if track.item.instance_id not in matched_ids:
                        self._apply_miss(
                            track,
                            zone,
                            elapsed_sec,
                            instrument_negative_scale,
                        )
                continue

            zone_scores = [track.probabilities[zone] for track in tracks]
            ambiguous = (
                len(tracks) > 1
                and (max(zone_scores) - min(zone_scores))
                <= self.config.identity_assignment_margin
            )
            if ambiguous:
                # A frame-local detector ID is not a physical identity. Share
                # count-bounded occupancy symmetrically across fixed instances.
                occupancy = min(len(instrument_detections) / len(tracks), 1.0)
                confidence = sum(
                    detection.confidence
                    for detection in instrument_detections[: len(tracks)]
                ) / min(len(instrument_detections), len(tracks))
                for track in tracks:
                    track.ambiguous_identity = True
                    self._mix_zone_occupancy(
                        track,
                        zone,
                        occupancy,
                        confidence,
                        positive_dt_sec,
                        instrument_positive_scale,
                        instrument_negative_scale,
                        source=f"camera:{view}",
                    )
                    if (
                        occupancy < 1.0
                        and max(track.probabilities.values())
                        < self.config.confirm_threshold
                    ):
                        track.committed_location = ""
                    track.last_positive_sec = now
                    track.last_positive_location = zone
                    track.model_version = str(model_version or "")[:120]
                    track.ontology_version = str(ontology_version or "")[:120]
                    track.calibration_version = str(calibration_version or "")[:120]
                continue

            ranked_tracks = sorted(tracks, key=lambda item: item.probabilities[zone], reverse=True)
            matched_ids: set[str] = set()
            for detection, track in zip(instrument_detections, ranked_tracks):
                confidence = detection.confidence
                self._mix_zone_occupancy(
                    track,
                    zone,
                    1.0,
                    confidence,
                    positive_dt_sec,
                    instrument_positive_scale,
                    instrument_negative_scale,
                    source=(
                        f"camera:{view}:uv_class_continuity"
                        if detection.uv_recovered
                        else f"camera:{view}"
                    ),
                )
                track.last_positive_sec = now
                track.last_positive_location = zone
                track.model_version = str(model_version or "")[:120]
                track.ontology_version = str(ontology_version or "")[:120]
                track.calibration_version = str(calibration_version or "")[:120]
                matched_ids.add(track.item.instance_id)
            for track in tracks:
                if track.item.instance_id not in matched_ids:
                    self._apply_miss(
                        track,
                        zone,
                        elapsed_sec,
                        instrument_negative_scale,
                    )

        self._update_commits(now)
        return True

    def _command_tracks(self, command: CommandEvidence) -> list[_Track]:
        if command.instance_id in self._tracks:
            return [self._tracks[command.instance_id]]
        # Commands are admitted only after resolving one concrete scenario
        # slot. Never recover a malformed command by applying strong action
        # evidence to every copy of an identical instrument.
        return []

    def _retire_command(self, command: CommandEvidence) -> None:
        """Remove a terminal command from association while tombstoning its ID."""

        self._commands.pop(command.command_id, None)
        if command.command_id in self._retired_commands:
            self._retired_commands.pop(command.command_id, None)
        self._retired_commands[command.command_id] = command
        while len(self._retired_commands) > MAX_COMMAND_EVIDENCE:
            self._retired_commands.pop(next(iter(self._retired_commands)), None)

    def apply_skill_status(
        self,
        command_id: str,
        state: str,
        success: bool,
        progress: float,
        timestamp_sec: float,
        reason_code: str = "",
    ) -> bool:
        command = self._commands.get(str(command_id or ""))
        if command is None:
            return False
        tracks = self._command_tracks(command)
        if not tracks:
            return False
        state_key = re.sub(r"[^a-z0-9]+", "_", str(state or "").casefold()).strip("_")
        terminal_state = state_key in {"completed", "succeeded", "success"}
        terminal_success = bool(success) and terminal_state
        canceled_state = state_key in {"canceled", "cancelled"}
        predispatch_source_unchanged = state_key in {
            "duplicate_suppressed",
            "rejected",
            "dispatch_failed",
            "server_unavailable",
            "offline",
        }
        terminal_failure = state_key in {
            "rejected",
            "dispatch_failed",
            "result_failed",
            "server_unavailable",
            "aborted",
            "failed",
        } or canceled_state or (terminal_state and not bool(success))
        reason_key = re.sub(
            r"[^a-z0-9]+",
            "_",
            str(reason_code or "").casefold(),
        ).strip("_")
        delivery_hand_command = bool(
            command.target_location == "robot"
            or (
                command.target_location == "surgeon"
                and command.source_location in {"tray", "mayo", "robot"}
            )
        )
        secured_delivery_state = bool(
            delivery_hand_command
            and state_key
            in {
                "moving_to_target",
                "holding",
                "ready_in_right_hand",
                "waiting_for_takeover",
            }
        )
        if secured_delivery_state:
            if command.secured_robot_since_sec is None:
                command.secured_robot_since_sec = float(timestamp_sec)
        elif state_key in {
            "placing",
            "recovering_to_tray",
            "stopping",
            "retreating",
        } or terminal_success or terminal_failure or predispatch_source_unchanged:
            command.secured_robot_since_sec = None
        no_dispatch_reason = any(
            marker in reason_key
            for marker in (
                "dispatch_failed",
                "goal_rejected",
                "server_unavailable",
                "action_server_unavailable",
                "source_unchanged",
                "before_dispatch",
            )
        )
        if state_key in {"accepted", "moving_to_source", "approaching_source"} or state_key in {
            "grasping",
            "picking",
            "picking_from_rack",
            "picking_from_mayo_for_handover",
            "moving_to_target",
            "holding",
            "ready_in_right_hand",
            "waiting_for_takeover",
            "placing",
            "handover_to_surgeon",
            "recovering_to_tray",
            "stopping",
            "retreating",
        }:
            command.execution_started = True
        if state_key == "busy" and not command.execution_started:
            predispatch_source_unchanged = True
        if state_key == "fault" and no_dispatch_reason:
            predispatch_source_unchanged = True
        if state_key in {"fault", "unknown"}:
            terminal_failure = True
        for track in tracks:
            track.active_command_id = command.command_id
            self._activate_slot(track, 1.0)
            if terminal_success:
                self._mix_distribution(
                    track,
                    {command.target_location: 0.98, "unknown": 0.02},
                    0.95,
                    source=f"skill:{state_key or 'completed'}",
                )
                track.active_command_id = ""
            elif canceled_state and reason_key == "canceled_source_unchanged":
                self._mix_distribution(
                    track,
                    {command.source_location: 0.98, "unknown": 0.02},
                    0.95,
                    source="skill:canceled_source_unchanged",
                )
                track.active_command_id = ""
            elif canceled_state and reason_key == "canceled_recovered_to_tray":
                self._mix_distribution(
                    track,
                    {"tray": 0.98, "unknown": 0.02},
                    0.95,
                    source="skill:canceled_recovered_to_tray",
                )
                track.active_command_id = ""
            elif predispatch_source_unchanged:
                self._mix_distribution(
                    track,
                    {command.source_location: 0.98, "unknown": 0.02},
                    0.95,
                    source=f"skill:{state_key or 'source_unchanged'}",
                )
                track.active_command_id = ""
            elif terminal_failure:
                self._mix_distribution(
                    track,
                    {
                        command.source_location: 0.55,
                        "robot": 0.20,
                        "unknown": 0.25,
                    },
                    # Even a previously confirmed delivery-hand candidate must
                    # become uncommitted when the controller cannot establish a
                    # terminal location.  0.70 guarantees that a robot belief
                    # of 1.0 falls below the common 0.45 release threshold while
                    # retaining source/robot/unknown as ordinary evidence.
                    0.70,
                    source=f"skill:{state_key}",
                )
                track.active_command_id = ""
            elif state_key in {"dispatching", "queued", "accepted"}:
                self._mix_distribution(
                    track,
                    {command.source_location: 0.95, "unknown": 0.05},
                    0.55,
                    source=f"skill:{state_key}",
                )
            elif state_key in {"moving_to_source", "approaching_source"}:
                self._mix_distribution(
                    track,
                    {command.source_location: 0.90, "unknown": 0.10},
                    0.70,
                    source=f"skill:{state_key}",
                )
            elif state_key in {
                "grasping",
                "picking",
                "picking_from_rack",
                "picking_from_mayo_for_handover",
            }:
                self._mix_distribution(
                    track,
                    {command.source_location: 0.58, "robot": 0.37, "unknown": 0.05},
                    0.72,
                    source=f"skill:{state_key}",
                )
            elif secured_delivery_state:
                # The public Action contract defines moving_to_target as moving
                # a secured tool, and waiting_for_takeover as holding it at the
                # handover pose.  Make that controller-correlated possession
                # strong enough to cross the normal confirmation threshold,
                # then let the existing dwell gate below decide when it commits.
                self._mix_distribution(
                    track,
                    {"robot": 0.96, "unknown": 0.04},
                    0.90,
                    source=f"skill:{state_key}",
                )
            elif state_key in {"moving_to_target", "holding", "ready_in_right_hand"}:
                self._mix_distribution(
                    track,
                    {"robot": 0.93, "unknown": 0.07},
                    0.78,
                    source=f"skill:{state_key}",
                )
            elif state_key in {"waiting_for_takeover", "placing", "handover_to_surgeon"}:
                self._mix_distribution(
                    track,
                    {command.target_location: 0.55, "robot": 0.40, "unknown": 0.05},
                    0.72,
                    source=f"skill:{state_key}",
                )
            elif state_key == "recovering_to_tray":
                self._mix_distribution(
                    track,
                    {"tray": 0.55, "robot": 0.40, "unknown": 0.05},
                    0.72,
                    source=f"skill:{state_key}",
                )
            elif state_key in {"stopping", "retreating", "cancel_requested"}:
                self._mix_distribution(
                    track,
                    {command.source_location: 0.45, "robot": 0.25, "unknown": 0.30},
                    0.65,
                    source=f"skill:{state_key}",
                )
            else:
                robot_weight = 0.65 + 0.20 * _finite_probability(progress)
                self._mix_distribution(
                    track,
                    {"robot": robot_weight, command.source_location: 0.25, "unknown": 0.10},
                    0.60,
                    source=f"skill:{state_key or 'executing'}",
                )
        motion_active = command.execution_started and not (
            terminal_success
            or terminal_failure
            or predispatch_source_unchanged
        )
        self.set_robot_motion(motion_active, timestamp_sec, command.command_id)
        self._update_commits(float(timestamp_sec))
        if terminal_success or terminal_failure or predispatch_source_unchanged:
            self._retire_command(command)
        return True

    def apply_semantic_event(
        self,
        *,
        instrument_id: str,
        instance_id: str,
        location: str,
        confidence: float,
        event_type: str,
        timestamp_sec: float,
    ) -> bool:
        event_key = re.sub(r"[^a-z0-9]+", "", str(event_type or "").casefold())
        if event_key not in ALLOWED_SEMANTIC_EVENT_TYPES:
            return False
        bounded_confidence = _finite_probability(confidence)
        if bounded_confidence <= 0.0:
            return False
        location = location if location in FIXED_LOCATIONS else semantic_location(location)
        instance_id = str(instance_id or "").strip()
        if instance_id:
            track = self._tracks.get(instance_id)
            tracks = [
                track
            ] if (
                track is not None
                and (
                    not instrument_id
                    or track.item.instrument_id == str(instrument_id or "")
                )
            ) else []
        else:
            instrument_id = str(instrument_id or "").strip()
            candidates = self._tracks_for_instrument(instrument_id)
            if len(candidates) > 1 and self._exchangeable_type(instrument_id):
                # Prefer the newest Action binding targeting this location. This
                # makes a type-only terminal event idempotently reinforce the
                # same logical slot selected at the Action boundary.
                matching_commands = sorted(
                    (
                        command
                        for command in self._commands.values()
                        if command.instrument_id == instrument_id
                        and command.target_location == location
                        and command.instance_id in self._tracks
                    ),
                    key=lambda command: (
                        self._tracks[command.instance_id].active_command_id
                        == command.command_id,
                        command.registered_sec,
                    ),
                    reverse=True,
                )
                if matching_commands:
                    selected = self._tracks[matching_commands[0].instance_id]
                else:
                    source_locations = SEMANTIC_EVENT_SOURCE_LOCATIONS.get(
                        event_key,
                        (),
                    )

                    def event_rank(
                        candidate: _Track,
                    ) -> tuple[float | int | str, ...]:
                        ordered_source_scores = tuple(
                            -candidate.probabilities[source]
                            for source in source_locations
                        )
                        return (
                            0 if candidate.active_command_id else 1,
                            *ordered_source_scores,
                            candidate.probabilities[location],
                            -candidate.activity_probability,
                            candidate.item.instance_id,
                        )

                    selected = min(candidates, key=event_rank)
                selected.exchangeable_assignment = True
                self._append_source(
                    selected,
                    f"exchangeable_event_bind:{event_key}",
                )
                tracks = [selected]
            elif len(candidates) > 1:
                self._ignored_ambiguous_command_count += 1
                return False
            else:
                tracks = candidates
        if not tracks or location == "unknown":
            return False
        weight = 0.90 * max(bounded_confidence, 0.35)
        for track in tracks:
            self._mix_distribution(
                track,
                {location: 0.98, "unknown": 0.02},
                weight,
                source=f"event:{str(event_type or 'location')[:60]}",
            )
            self._activate_slot(track, bounded_confidence)
        self._update_commits(float(timestamp_sec))
        return True

    def reconcile_fixed_states(
        self,
        states: Iterable[tuple[str, str, str, float]],
        *,
        source: str = "simulation_state",
    ) -> bool:
        """Atomically restore required slots from a read-only DT snapshot.

        Strict mode requires every slot. Exchangeable mode requires exactly the
        initially active slots; observer-only dormant capacity must be absent.
        """

        validated: dict[str, tuple[str, float]] = {}
        for instance_id, instrument_id, location, confidence in states:
            instance_id = str(instance_id or "").strip()
            track = self._tracks.get(instance_id)
            bounded_confidence = _finite_probability(confidence)
            if (
                track is None
                or track.item.instrument_id != str(instrument_id or "")
                or location not in FIXED_LOCATIONS
                or location == "unknown"
                or bounded_confidence <= 0.0
                or instance_id in validated
            ):
                return False
            validated[instance_id] = (location, bounded_confidence)
        validated_ids = set(validated)
        tracker_ids = set(self._tracks)
        if self.config.exchangeable_instances:
            # Future procedure population schemas may author dormant capacity
            # slots beyond the Digital Twin's active initial count. A freshly
            # built dormant slot is safe to omit from SimulationState; every
            # initially active slot remains mandatory so an incomplete frame
            # still cannot make publication ready.
            dormant_ids = {
                instance_id
                for instance_id, track in self._tracks.items()
                if _finite_probability(
                    track.item.initial_activity_probability,
                    1.0,
                ) == 0.0
            }
            required_ids = tracker_ids - dormant_ids
            # DT/controller inventory is authoritative only for initial_count.
            # A dormant capacity ID in this snapshot would improperly let the
            # control plane materialize an observation-only slot.
            if validated_ids != required_ids:
                return False
        elif validated_ids != tracker_ids:
            return False

        for instance_id, (location, confidence) in validated.items():
            track = self._tracks[instance_id]
            track.probabilities = _initial_distribution(
                location,
                max(confidence, 0.90),
            )
            track.committed_location = (
                location
                if track.probabilities[location] >= self.config.confirm_threshold
                else ""
            )
            track.last_positive_sec = None
            track.last_positive_location = ""
            track.active_command_id = ""
            track.activity_probability = max(confidence, 0.90)
            track.ambiguous_identity = False
            track.exchangeable_assignment = False
            track.exchangeable_rebound = False
            track.commit_candidate_location = ""
            track.commit_candidate_since_sec = None
            track.unknown_since_sec = None
            self._append_source(track, source)
        for instance_id in tracker_ids - validated_ids:
            track = self._tracks[instance_id]
            track.probabilities = _initial_distribution("unknown", 0.0)
            track.committed_location = ""
            track.last_positive_sec = None
            track.last_positive_location = ""
            track.active_command_id = ""
            track.activity_probability = 0.0
            track.ambiguous_identity = False
            track.exchangeable_assignment = False
            track.exchangeable_rebound = False
            track.commit_candidate_location = ""
            track.commit_candidate_since_sec = None
            track.unknown_since_sec = None
            self._append_source(track, f"{source}:dormant_capacity")
        return True

    @staticmethod
    def _clear_commit_candidate(track: _Track) -> None:
        track.commit_candidate_location = ""
        track.commit_candidate_since_sec = None

    def _candidate_has_dwelled(
        self,
        track: _Track,
        location: str,
        timestamp_sec: float | None,
    ) -> bool:
        """Advance one common commit candidate without inventing a new state.

        The candidate is merely hysteresis metadata for the probability
        distribution.  It is deliberately shared by camera, Action, and
        Service evidence so no producer gets a direct location-transition
        shortcut.
        """

        if timestamp_sec is None or not math.isfinite(float(timestamp_sec)):
            return False
        now = float(timestamp_sec)
        if (
            track.commit_candidate_location != location
            or track.commit_candidate_since_sec is None
            or now < track.commit_candidate_since_sec
        ):
            track.commit_candidate_location = location
            track.commit_candidate_since_sec = now
            return False
        return now - track.commit_candidate_since_sec >= self.config.commit_dwell_sec

    def _update_track_commit(
        self,
        track: _Track,
        timestamp_sec: float | None = None,
    ) -> None:
        top_location, top_probability = max(
            track.probabilities.items(), key=lambda item: item[1]
        )
        current = track.committed_location
        current_probability = (
            track.probabilities.get(current, 0.0) if current else 0.0
        )
        candidate_ready = False
        if (
            top_location != "unknown"
            and top_location != current
            and top_probability >= self.config.commit_release_threshold
        ):
            # Candidate stability begins when one location becomes the
            # clear competing leader.  Confirmation still requires the
            # global confirm threshold and switch margin below; this lets
            # several moderate camera frames count as continuous evidence
            # without letting a single strong frame commit immediately.
            candidate_ready = self._candidate_has_dwelled(
                track,
                top_location,
                timestamp_sec,
            )
        else:
            self._clear_commit_candidate(track)
        if not current:
            if (
                top_location != "unknown"
                and top_probability >= self.config.confirm_threshold
                and candidate_ready
            ):
                track.committed_location = top_location
                self._clear_commit_candidate(track)
            return
        if top_location == current:
            if current_probability < self.config.commit_release_threshold:
                track.committed_location = ""
            return
        if (
            top_location != "unknown"
            and top_probability >= self.config.confirm_threshold
            and top_probability - current_probability >= self.config.commit_switch_margin
        ):
            # Keep the previous committed location during the common dwell
            # period.  This is the only confirmation rule; a controller
            # result cannot bypass it, and persistent source detections can
            # cancel or reverse the candidate naturally.
            if candidate_ready:
                track.committed_location = top_location
                self._clear_commit_candidate(track)
        elif current_probability < self.config.commit_release_threshold:
            track.committed_location = ""

    def _update_commits(self, timestamp_sec: float | None = None) -> None:
        for track in self._tracks.values():
            self._update_track_commit(track, timestamp_sec)

    def _advance_secured_delivery_commits(self, timestamp_sec: float) -> None:
        """Advance only mature controller-confirmed delivery-hand candidates."""

        now = float(timestamp_sec)
        for command in self._commands.values():
            secured_since = command.secured_robot_since_sec
            if (
                secured_since is None
                or now - secured_since < self.config.commit_dwell_sec
            ):
                continue
            track = self._tracks.get(command.instance_id)
            if track is None or track.active_command_id != command.command_id:
                continue
            self._update_track_commit(track, now)

    def snapshot(self, now_sec: float) -> list[dict[str, object]]:
        now = float(now_sec)
        self._advance_secured_delivery_commits(now)
        self._retire_stale_unknown_capacity_slots(now)
        motion_mode = self.motion_mode(now)
        result: list[dict[str, object]] = []
        for instance_id in sorted(self._tracks):
            track = self._tracks[instance_id]
            top_location, top_probability = max(
                track.probabilities.items(), key=lambda item: item[1]
            )
            if track.last_positive_sec is None:
                age = -1.0
            else:
                age = max(now - track.last_positive_sec, 0.0)
            # Exact robot occlusion geometry is intentionally outside this
            # MVP. Motion only scales negative evidence and never invents an
            # occluded classification for every stale tool.
            if top_probability >= self.config.confirm_threshold:
                status = "confirmed"
            elif top_probability >= self.config.probable_threshold:
                status = "probable"
            else:
                status = "uncertain"
            committed = track.committed_location
            exchangeable = self._exchangeable_type(track.item.instrument_id)
            flags = [
                "scenario_bounded_capacity" if exchangeable else "fixed_inventory",
                "observation_only",
            ]
            existence_probability = (
                track.activity_probability if exchangeable else 1.0
            )
            if self._run_prior_frozen:
                # A scenario run starts from the operator-reviewed population;
                # neither false positives nor occlusion misses alter counts.
                existence_probability = 1.0
                flags.append("operator_verified_run_prior")
            if exchangeable:
                flags.extend(
                    [
                        "logical_instance_exchangeable",
                        "physical_identity_not_asserted",
                    ]
                )
                if existence_probability >= self.config.confirm_threshold:
                    flags.append("capacity_slot_active")
                elif existence_probability >= self.config.probable_threshold:
                    flags.append("capacity_slot_probable")
                else:
                    flags.append("capacity_slot_inactive")
                if track.exchangeable_assignment:
                    flags.append("exchangeable_slot_assigned")
                if track.exchangeable_rebound:
                    flags.append("exchangeable_slot_rebound")
            if track.ambiguous_identity:
                flags.append("identity_ambiguous_symmetric_evidence")
            if motion_mode != "idle":
                flags.append("robot_motion_negative_scaled")
            if self.mayo_hand_present(now):
                flags.append("mayo_hand_occlusion_scaled")
            result.append(
                {
                    "track_id": instance_id,
                    "instrument_id": track.item.instrument_id,
                    "instance_id": instance_id,
                    "display_name": track.item.display_name,
                    # In exchangeable mode this is the probability that the
                    # scenario-bounded logical capacity slot is active/present,
                    # not a claim of stable physical identity. Strict mode
                    # retains the fixed-inventory value 1.0.
                    "existence_probability": existence_probability,
                    "status": status,
                    "committed_location_id": committed,
                    "committed_location_probability": (
                        track.probabilities.get(committed, 0.0) if committed else 0.0
                    ),
                    "most_likely_location_id": top_location,
                    "most_likely_probability": top_probability,
                    "locations": dict(track.probabilities),
                    "last_positive_sec": track.last_positive_sec,
                    "last_positive_age_sec": age,
                    "evidence_sources": list(track.evidence_sources),
                    "motion_mode": motion_mode,
                    "active_command_id": track.active_command_id,
                    "model_version": track.model_version,
                    "ontology_version": track.ontology_version,
                    "calibration_version": track.calibration_version,
                    "status_flags": flags,
                }
            )
        return result
