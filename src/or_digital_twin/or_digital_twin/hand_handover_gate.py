"""Fail-closed CAM4 right-hand handover-signal fusion.

The hand classifier supplies only a binary human signal.  It never chooses a
tool and it never publishes a robot command.  Gesture and facing observations
are joined only when their source headers are identical, then a source- and
receipt-time dwell gate turns the exact Right/Open_Palm/PALM_UP tuple into one
episode.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any


class FrameDisposition(str, Enum):
    POSITIVE = "positive"
    RELEASE = "release"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HandPerceptionPins:
    source_frame_id: str
    gesture_model_name: str
    gesture_model_version: str
    gesture_model_sha256: str
    facing_estimator_name: str
    facing_estimator_version: str
    facing_spec_sha256: str
    calibration_version: str
    handedness_mapping_version: str
    depth_registration_backend: str = "cuda_cabi_v1"


@dataclass(frozen=True)
class HandFrameEvidence:
    source_stamp_sec: float
    disposition: FrameDisposition
    reason: str
    confidence: float = 0.0
    hand_index: int = -1
    gesture_score: float = 0.0
    handedness_score: float = 0.0
    palm_up_score: float = 0.0


@dataclass(frozen=True)
class HandGateUpdate:
    accepted_sample: bool
    active: bool
    rising_edge: bool
    generation: int
    stability_sec: float
    confidence: float
    reason: str
    disposition: FrameDisposition


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _stamp_key(message: Any) -> tuple[int, int, str] | None:
    try:
        sec = int(message.header.stamp.sec)
        nanosec = int(message.header.stamp.nanosec)
        frame_id = str(message.header.frame_id)
    except (AttributeError, TypeError, ValueError):
        return None
    if sec < 0 or nanosec < 0 or nanosec >= 1_000_000_000 or not frame_id:
        return None
    return (sec, nanosec, frame_id)


def stamp_key(message: Any) -> tuple[int, int, str] | None:
    """Return the exact source-header key used for cross-topic fusion."""

    return _stamp_key(message)


def stamp_key_sec(key: tuple[int, int, str]) -> float:
    return float(key[0]) + float(key[1]) / 1_000_000_000.0


class ExactStampHandJoiner:
    """Bounded, order-independent exact-header join for two ROS arrays."""

    def __init__(self, *, max_pending: int = 32) -> None:
        self._max_pending = max(4, int(max_pending))
        self._gestures: dict[tuple[int, int, str], Any] = {}
        self._facings: dict[tuple[int, int, str], Any] = {}

    def clear(self) -> None:
        self._gestures.clear()
        self._facings.clear()

    def add_gesture(self, message: Any) -> tuple[Any, Any] | None:
        return self._add(message, own=self._gestures, peer=self._facings)

    def add_facing(self, message: Any) -> tuple[Any, Any] | None:
        pair = self._add(message, own=self._facings, peer=self._gestures)
        if pair is None:
            return None
        facing, gesture = pair
        return (gesture, facing)

    def _add(
        self,
        message: Any,
        *,
        own: dict[tuple[int, int, str], Any],
        peer: dict[tuple[int, int, str], Any],
    ) -> tuple[Any, Any] | None:
        key = _stamp_key(message)
        if key is None:
            return None
        if key in own:
            return None
        own[key] = message
        other = peer.pop(key, None)
        if other is not None:
            own.pop(key, None)
            return (message, other)
        self._trim(own)
        self._trim(peer)
        return None

    def _trim(self, cache: dict[tuple[int, int, str], Any]) -> None:
        while len(cache) > self._max_pending:
            cache.pop(min(cache), None)


def validate_hand_health(
    payload: Any,
    *,
    pins: HandPerceptionPins,
    operator_mapping_approved: bool,
) -> tuple[bool, str]:
    """Validate the latched CAM4 health document and frozen provenance."""

    if not isinstance(payload, dict):
        return (False, "health_not_object")
    if payload.get("schema") != "pnu.hand_keypoint_health.v1":
        return (False, "health_schema_mismatch")
    required_true = (
        "ready",
        "rgb_ready",
        "depth_ready",
        "depth_alignment_validated",
        "depth_registration_ready",
        "model_ready",
        "hand_inference_ready",
        "gesture_model_ready",
        "gesture_inference_ready",
        "palm_facing_observation_ready",
    )
    for field in required_true:
        if payload.get(field) is not True:
            return (False, f"health_{field}_false")
    if payload.get("depth_registration_degraded") is True:
        return (False, "health_depth_registration_degraded")
    if payload.get("depth_registration_backend_active") != pins.depth_registration_backend:
        return (False, "health_depth_backend_mismatch")
    if payload.get("handedness_policy") != "forced_camera_constraint":
        return (False, "health_handedness_policy_mismatch")
    if payload.get("forced_handedness_label") != "Right":
        return (False, "health_forced_handedness_mismatch")
    mapping_verified = payload.get("palm_facing_mapping_verified") is True
    if not mapping_verified and not operator_mapping_approved:
        return (False, "health_palm_mapping_unverified")

    if payload.get("gesture_model_version") != pins.gesture_model_version:
        return (False, "health_gesture_version_mismatch")
    if payload.get("gesture_model_asset_sha256") != pins.gesture_model_sha256:
        return (False, "health_gesture_sha_mismatch")
    estimator = payload.get("palm_facing_estimator")
    if not isinstance(estimator, dict):
        return (False, "health_facing_estimator_missing")
    expected_estimator = {
        "name": pins.facing_estimator_name,
        "version": pins.facing_estimator_version,
        "spec_sha256": pins.facing_spec_sha256,
        "calibration_version": pins.calibration_version,
        "handedness_mapping_version": pins.handedness_mapping_version,
    }
    for field, expected in expected_estimator.items():
        if estimator.get(field) != expected:
            return (False, f"health_facing_{field}_mismatch")
    return (True, "health_ready")


def classify_hand_frame(
    gesture_message: Any,
    facing_message: Any,
    *,
    pins: HandPerceptionPins,
    minimum_gesture_score: float = 0.5,
    minimum_handedness_score: float = 0.5,
    minimum_palm_up_score: float = 0.0,
) -> HandFrameEvidence:
    """Classify one exact-stamp pair without carrying frame-local IDs forward."""

    gesture_key = _stamp_key(gesture_message)
    facing_key = _stamp_key(facing_message)
    if gesture_key is None or facing_key is None or gesture_key != facing_key:
        return HandFrameEvidence(0.0, FrameDisposition.UNKNOWN, "header_mismatch")
    source_stamp_sec = stamp_key_sec(gesture_key)
    if gesture_key[2] != pins.source_frame_id:
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "source_frame_mismatch",
        )
    expected_gesture = (
        pins.gesture_model_name,
        pins.gesture_model_version,
        pins.gesture_model_sha256,
    )
    actual_gesture = (
        str(getattr(gesture_message, "model_name", "")),
        str(getattr(gesture_message, "model_version", "")),
        str(getattr(gesture_message, "model_asset_sha256", "")),
    )
    if actual_gesture != expected_gesture:
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "gesture_provenance_mismatch",
        )
    expected_facing = (
        pins.facing_estimator_name,
        pins.facing_estimator_version,
        pins.facing_spec_sha256,
        pins.calibration_version,
        pins.handedness_mapping_version,
    )
    actual_facing = (
        str(getattr(facing_message, "estimator_name", "")),
        str(getattr(facing_message, "estimator_version", "")),
        str(getattr(facing_message, "estimator_spec_sha256", "")),
        str(getattr(facing_message, "calibration_version", "")),
        str(getattr(facing_message, "handedness_mapping_version", "")),
    )
    if actual_facing != expected_facing:
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "facing_provenance_mismatch",
        )
    if "Open_Palm" not in tuple(getattr(gesture_message, "supported_gestures", ())):
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "open_palm_not_supported",
        )
    if "PALM_UP" not in tuple(getattr(facing_message, "supported_facings", ())):
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "palm_up_not_supported",
        )

    gesture_hands = list(getattr(gesture_message, "hands", ()))
    facing_hands = list(getattr(facing_message, "hands", ()))
    if not gesture_hands and not facing_hands:
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.RELEASE,
            "observed_no_hand",
        )

    def by_index(rows: list[Any]) -> dict[int, Any] | None:
        result: dict[int, Any] = {}
        for row in rows:
            try:
                index = int(row.hand_index)
            except (AttributeError, TypeError, ValueError):
                return None
            if index < 0 or index in result:
                return None
            result[index] = row
        return result

    gestures = by_index(gesture_hands)
    facings = by_index(facing_hands)
    if gestures is None or facings is None:
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "duplicate_or_invalid_hand_index",
        )
    if set(gestures) != set(facings):
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "hand_index_set_mismatch",
        )
    common_indices = sorted(set(gestures) & set(facings))
    eligible: list[tuple[int, Any, Any, float, float, float]] = []
    for index in common_indices:
        gesture = gestures[index]
        facing = facings[index]
        if not bool(getattr(gesture, "has_handedness", False)):
            continue
        if not bool(getattr(facing, "has_handedness", False)):
            continue
        if str(getattr(gesture, "handedness_label", "")) != "Right":
            continue
        if str(getattr(facing, "handedness_label", "")) != "Right":
            continue
        gesture_hand_score = _finite(getattr(gesture, "handedness_score", None))
        facing_hand_score = _finite(getattr(facing, "handedness_score", None))
        gesture_score = _finite(getattr(gesture, "score", None))
        palm_up_score = _finite(getattr(facing, "palm_up_score", None))
        if None in (
            gesture_hand_score,
            facing_hand_score,
            gesture_score,
            palm_up_score,
        ):
            continue
        handedness_score = min(gesture_hand_score, facing_hand_score)
        if (
            handedness_score < minimum_handedness_score
            or gesture_score < 0.0
            or gesture_score > 1.0
            or palm_up_score < -1.0
            or palm_up_score > 1.0
        ):
            continue
        eligible.append(
            (
                index,
                gesture,
                facing,
                gesture_score,
                handedness_score,
                palm_up_score,
            )
        )
    if len(eligible) != 1:
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.UNKNOWN,
            "right_hand_ambiguous" if eligible else "right_hand_unavailable",
        )

    index, gesture, facing, gesture_score, handedness_score, palm_up_score = eligible[0]
    gesture_valid = bool(getattr(gesture, "has_classification", False))
    facing_valid = bool(getattr(facing, "has_facing", False))
    gesture_label = str(getattr(gesture, "category_name", ""))
    facing_label = str(getattr(facing, "facing_label", ""))
    if (
        gesture_valid
        and facing_valid
        and gesture_label == "Open_Palm"
        and facing_label == "PALM_UP"
        and gesture_score >= minimum_gesture_score
        and palm_up_score >= minimum_palm_up_score
    ):
        confidence = min(
            gesture_score,
            handedness_score,
            max(0.0, min(1.0, (palm_up_score + 1.0) / 2.0)),
        )
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.POSITIVE,
            "right_open_palm_palm_up",
            confidence=confidence,
            hand_index=index,
            gesture_score=gesture_score,
            handedness_score=handedness_score,
            palm_up_score=palm_up_score,
        )
    if (
        (gesture_valid and gesture_label != "Open_Palm")
        or (facing_valid and facing_label != "PALM_UP")
    ):
        return HandFrameEvidence(
            source_stamp_sec,
            FrameDisposition.RELEASE,
            "observed_non_request_pose",
            hand_index=index,
            gesture_score=gesture_score,
            handedness_score=handedness_score,
            palm_up_score=palm_up_score,
        )
    return HandFrameEvidence(
        source_stamp_sec,
        FrameDisposition.UNKNOWN,
        "hand_classification_unknown",
        hand_index=index,
        gesture_score=gesture_score,
        handedness_score=handedness_score,
        palm_up_score=palm_up_score,
    )


class ContinuousHandHandoverGate:
    """Dual-clock dwell, one-shot episode latch, and fresh-release debounce."""

    def __init__(
        self,
        *,
        dwell_sec: float = 0.300,
        release_sec: float = 0.500,
        max_positive_gap_sec: float = 0.200,
        max_source_age_sec: float = 0.500,
        future_tolerance_sec: float = 0.500,
        max_receipt_silence_sec: float = 0.500,
        minimum_positive_samples: int = 4,
    ) -> None:
        self.dwell_sec = max(0.001, float(dwell_sec))
        self.release_sec = max(0.001, float(release_sec))
        self.max_positive_gap_sec = max(0.001, float(max_positive_gap_sec))
        self.max_source_age_sec = max(0.001, float(max_source_age_sec))
        self.future_tolerance_sec = max(0.0, float(future_tolerance_sec))
        self.max_receipt_silence_sec = max(0.001, float(max_receipt_silence_sec))
        self.minimum_positive_samples = max(2, int(minimum_positive_samples))
        self.reset_all()

    def reset_all(self) -> None:
        self.generation = 0
        self._episode_latched = False
        self._inhibited_until_release = False
        self._release_since: float | None = None
        self._release_since_receipt: float | None = None
        self._last_release_stamp: float | None = None
        self._last_release_receipt: float | None = None
        self._first_positive_stamp: float | None = None
        self._first_positive_receipt: float | None = None
        self._last_positive_stamp: float | None = None
        self._last_positive_receipt: float | None = None
        self._last_source_stamp: float | None = None
        self._last_receipt_monotonic: float | None = None
        self._positive_samples = 0
        self._confidence = 0.0
        self.active = False
        self.stability_sec = 0.0

    def withdraw(self, *, preserve_episode: bool = True) -> None:
        self._reset_positive_run()
        self._reset_release_run()
        if not preserve_episode:
            self._episode_latched = False
            self._inhibited_until_release = False

    def inhibit_until_release(self) -> None:
        """Withdraw now and require a fresh continuous release before rearming."""

        self._episode_latched = True
        self._inhibited_until_release = True
        self._reset_positive_run()
        self._reset_release_run()

    def _reset_positive_run(self) -> None:
        self._first_positive_stamp = None
        self._first_positive_receipt = None
        self._last_positive_stamp = None
        self._last_positive_receipt = None
        self._positive_samples = 0
        self._confidence = 0.0
        self.active = False
        self.stability_sec = 0.0

    def _reset_release_run(self) -> None:
        self._release_since = None
        self._release_since_receipt = None
        self._last_release_stamp = None
        self._last_release_receipt = None

    def _update(self, *, accepted: bool, rising: bool, reason: str, disposition: FrameDisposition) -> HandGateUpdate:
        return HandGateUpdate(
            accepted_sample=accepted,
            active=self.active,
            rising_edge=rising,
            generation=self.generation,
            stability_sec=self.stability_sec,
            confidence=self._confidence if self.active else 0.0,
            reason=reason,
            disposition=disposition,
        )

    def observe(
        self,
        evidence: HandFrameEvidence,
        *,
        source_now_sec: float,
        receipt_monotonic: float,
    ) -> HandGateUpdate:
        stamp = float(evidence.source_stamp_sec)
        source_now = float(source_now_sec)
        receipt = float(receipt_monotonic)
        if not all(math.isfinite(value) for value in (stamp, source_now, receipt)) or stamp <= 0.0:
            self.withdraw(preserve_episode=True)
            return self._update(
                accepted=False,
                rising=False,
                reason="invalid_source_time",
                disposition=FrameDisposition.UNKNOWN,
            )
        if source_now - stamp > self.max_source_age_sec:
            self.withdraw(preserve_episode=True)
            return self._update(
                accepted=False,
                rising=False,
                reason="stale_source_time",
                disposition=FrameDisposition.UNKNOWN,
            )
        if stamp - source_now > self.future_tolerance_sec:
            self.withdraw(preserve_episode=True)
            return self._update(
                accepted=False,
                rising=False,
                reason="future_source_time",
                disposition=FrameDisposition.UNKNOWN,
            )
        if self._last_source_stamp is not None:
            if stamp < self._last_source_stamp - 1e-9:
                self.withdraw(preserve_episode=True)
                return self._update(
                    accepted=False,
                    rising=False,
                    reason="source_time_regression",
                    disposition=FrameDisposition.UNKNOWN,
                )
            if abs(stamp - self._last_source_stamp) <= 1e-9:
                return self._update(
                    accepted=False,
                    rising=False,
                    reason="duplicate_source_time",
                    disposition=evidence.disposition,
                )
        self._last_source_stamp = stamp
        self._last_receipt_monotonic = receipt

        if evidence.disposition is FrameDisposition.UNKNOWN:
            self.withdraw(preserve_episode=True)
            return self._update(
                accepted=True,
                rising=False,
                reason=evidence.reason,
                disposition=evidence.disposition,
            )
        if evidence.disposition is FrameDisposition.RELEASE:
            self._reset_positive_run()
            if self._episode_latched or self._inhibited_until_release:
                release_continuous = bool(
                    self._last_release_stamp is not None
                    and self._last_release_receipt is not None
                    and stamp - self._last_release_stamp
                    <= self.max_positive_gap_sec
                    and receipt - self._last_release_receipt
                    <= self.max_positive_gap_sec
                    and receipt >= self._last_release_receipt
                )
                if not release_continuous:
                    self._release_since = stamp
                    self._release_since_receipt = receipt
                self._last_release_stamp = stamp
                self._last_release_receipt = receipt
                release_source_span = max(
                    0.0,
                    stamp
                    - (
                        self._release_since
                        if self._release_since is not None
                        else stamp
                    ),
                )
                release_receipt_span = max(
                    0.0,
                    receipt
                    - (
                        self._release_since_receipt
                        if self._release_since_receipt is not None
                        else receipt
                    ),
                )
                if (
                    release_source_span + 1e-9 >= self.release_sec
                    and release_receipt_span + 1e-9 >= self.release_sec
                ):
                    self._episode_latched = False
                    self._inhibited_until_release = False
                    self._reset_release_run()
            return self._update(
                accepted=True,
                rising=False,
                reason=evidence.reason,
                disposition=evidence.disposition,
            )

        self._reset_release_run()
        if self._inhibited_until_release:
            self._reset_positive_run()
            return self._update(
                accepted=True,
                rising=False,
                reason="fresh_release_required",
                disposition=evidence.disposition,
            )
        if (
            self._last_positive_stamp is None
            or self._last_positive_receipt is None
            or stamp - self._last_positive_stamp > self.max_positive_gap_sec
            or receipt - self._last_positive_receipt > self.max_positive_gap_sec
            or receipt < self._last_positive_receipt
        ):
            self._first_positive_stamp = stamp
            self._first_positive_receipt = receipt
            self._positive_samples = 1
            self._confidence = max(0.0, min(1.0, float(evidence.confidence)))
        else:
            self._positive_samples += 1
            self._confidence = min(
                self._confidence,
                max(0.0, min(1.0, float(evidence.confidence))),
            )
        self._last_positive_stamp = stamp
        self._last_positive_receipt = receipt
        first = self._first_positive_stamp if self._first_positive_stamp is not None else stamp
        first_receipt = (
            self._first_positive_receipt
            if self._first_positive_receipt is not None
            else receipt
        )
        self.stability_sec = min(
            max(0.0, stamp - first),
            max(0.0, receipt - first_receipt),
        )
        self.active = bool(
            self.stability_sec + 1e-9 >= self.dwell_sec
            and self._positive_samples >= self.minimum_positive_samples
        )
        rising = False
        if self.active and not self._episode_latched:
            self.generation += 1
            self._episode_latched = True
            rising = True
        return self._update(
            accepted=True,
            rising=rising,
            reason=evidence.reason,
            disposition=evidence.disposition,
        )

    def expire(self, *, receipt_monotonic: float) -> HandGateUpdate | None:
        if self._last_receipt_monotonic is None:
            return None
        now = float(receipt_monotonic)
        if not math.isfinite(now):
            return None
        if now - self._last_receipt_monotonic <= self.max_receipt_silence_sec:
            return None
        if (
            not self.active
            and self._first_positive_stamp is None
            and self._release_since is None
        ):
            return None
        self.withdraw(preserve_episode=True)
        return self._update(
            accepted=False,
            rising=False,
            reason="hand_stream_silent",
            disposition=FrameDisposition.UNKNOWN,
        )
