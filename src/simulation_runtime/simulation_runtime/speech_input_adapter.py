"""Admit public surgeon sentences from live or validation input boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from surgical_msgs.msg import InputSourceStatus, SpeechUtterance


def _stamp_sec(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) / 1_000_000_000.0


def observation_stamp_sec(msg: SpeechUtterance) -> float:
    # Freshness is about when the utterance was observed/published. The
    # transcript interval may legitimately end after a start-stamped bag
    # record, so use the envelope stamp before interval metadata.
    for stamp in (msg.stamp, msg.end_stamp, msg.start_stamp):
        value = _stamp_sec(stamp)
        if value > 0.0:
            return value
    return 0.0


@dataclass(frozen=True, slots=True)
class SpeechAdmission:
    accepted: bool
    reason: str
    text: str = ""


def normalize_sentence_text(text: str) -> str:
    return " ".join(str(text or "").strip().split())


class RecentSentences:
    def __init__(self, retention_sec: float) -> None:
        self._retention_sec = max(0.1, float(retention_sec))
        self._seen: dict[str, float] = {}

    def accept(self, text: str, now_monotonic: float) -> bool:
        cutoff = float(now_monotonic) - self._retention_sec
        self._seen = {
            key: seen_at
            for key, seen_at in self._seen.items()
            if seen_at >= cutoff
        }
        key = normalize_sentence_text(text).casefold()
        if not key or key in self._seen:
            return False
        self._seen[key] = float(now_monotonic)
        return True

    def clear(self) -> None:
        self._seen.clear()


def evaluate_utterance(
    msg: SpeechUtterance,
    *,
    now_sec: float,
    required_speaker_role: str,
    min_confidence: float,
    accept_missing_confidence: bool,
    require_timestamp: bool,
    max_age_sec: float,
    max_future_skew_sec: float,
) -> SpeechAdmission:
    text = str(msg.text or "").strip()
    if not text:
        return SpeechAdmission(False, "empty_text")
    if not bool(msg.is_final):
        return SpeechAdmission(False, "interim_transcript")
    required_role = str(required_speaker_role or "").strip().lower()
    speaker_role = str(msg.speaker_role or "").strip().lower()
    if required_role and speaker_role != required_role:
        return SpeechAdmission(False, f"unexpected_speaker_role:{speaker_role or 'missing'}")
    if bool(msg.has_confidence):
        confidence = float(msg.confidence)
        if confidence < float(min_confidence):
            return SpeechAdmission(False, f"low_confidence:{confidence:.3f}")
    elif not bool(accept_missing_confidence):
        return SpeechAdmission(False, "missing_confidence")

    observed_sec = observation_stamp_sec(msg)
    if observed_sec <= 0.0:
        if require_timestamp:
            return SpeechAdmission(False, "missing_timestamp")
    else:
        age_sec = float(now_sec) - observed_sec
        if age_sec > float(max_age_sec):
            return SpeechAdmission(False, f"stale:{age_sec:.3f}s")
        if age_sec < -float(max_future_skew_sec):
            return SpeechAdmission(False, f"future_timestamp:{-age_sec:.3f}s")
    return SpeechAdmission(True, "accepted", text)


class RecentUtteranceIds:
    def __init__(self, retention_sec: float) -> None:
        self._retention_sec = max(1.0, float(retention_sec))
        self._seen: dict[str, float] = {}

    def accept(self, utterance_id: str, now_monotonic: float) -> bool:
        cutoff = float(now_monotonic) - self._retention_sec
        self._seen = {
            key: seen_at
            for key, seen_at in self._seen.items()
            if seen_at >= cutoff
        }
        key = str(utterance_id or "").strip()
        if not key or key in self._seen:
            return False
        self._seen[key] = float(now_monotonic)
        return True

    def clear(self) -> None:
        self._seen.clear()


class SpeechInputAdapterNode(Node):
    def __init__(self) -> None:
        super().__init__("speech_input_adapter")
        self.declare_parameter("input_mode", "utterance")
        self.declare_parameter("input_topic", "/sensors/speech/utterance")
        self.declare_parameter("sentence_input_topic", "/sensors/surgeon/sentence")
        self.declare_parameter("output_topic", "/surgery/audio/request_text")
        self.declare_parameter("status_topic", "/input/speech/status")
        self.declare_parameter("sentence_source_id", "external_sentence_topic")
        self.declare_parameter("sentence_dedupe_sec", 1.0)
        self.declare_parameter("required_speaker_role", "surgeon")
        self.declare_parameter("min_confidence", 0.55)
        self.declare_parameter("accept_missing_confidence", True)
        self.declare_parameter("require_timestamp", True)
        self.declare_parameter("require_utterance_id", True)
        self.declare_parameter("max_age_sec", 3.0)
        self.declare_parameter("max_future_skew_sec", 1.0)
        self.declare_parameter("source_timeout_sec", 5.0)
        self.declare_parameter("dedupe_retention_sec", 120.0)

        self._input_mode = str(self.get_parameter("input_mode").value).strip().lower()
        if self._input_mode not in {"utterance", "sentence_text"}:
            raise ValueError(
                "input_mode must be either 'utterance' or 'sentence_text'"
            )
        self._sentence_source_id = str(
            self.get_parameter("sentence_source_id").value
        ).strip() or "external_sentence_topic"
        self._required_speaker_role = str(
            self.get_parameter("required_speaker_role").value
        )
        self._min_confidence = float(self.get_parameter("min_confidence").value)
        self._accept_missing_confidence = bool(
            self.get_parameter("accept_missing_confidence").value
        )
        self._require_timestamp = bool(
            self.get_parameter("require_timestamp").value
        )
        self._require_utterance_id = bool(
            self.get_parameter("require_utterance_id").value
        )
        self._max_age_sec = max(0.1, float(self.get_parameter("max_age_sec").value))
        self._max_future_skew_sec = max(
            0.0,
            float(self.get_parameter("max_future_skew_sec").value),
        )
        self._source_timeout_sec = max(
            0.5,
            float(self.get_parameter("source_timeout_sec").value),
        )
        self._recent_ids = RecentUtteranceIds(
            float(self.get_parameter("dedupe_retention_sec").value)
        )
        self._recent_sentences = RecentSentences(
            float(self.get_parameter("sentence_dedupe_sec").value)
        )

        self._received_count = 0
        self._accepted_count = 0
        self._rejected_count = 0
        self._epoch = 1
        self._last_source = ""
        self._last_detail = self._waiting_detail()
        self._last_observation_stamp = None
        self._last_accepted_monotonic = 0.0
        self._lifecycle_control_state = "stopped"
        self._last_lifecycle_control_signature: tuple[str, str] | None = None

        self._transcript_pub = self.create_publisher(
            String,
            str(self.get_parameter("output_topic").value),
            20,
        )
        self._status_pub = self.create_publisher(
            InputSourceStatus,
            str(self.get_parameter("status_topic").value),
            10,
        )
        if self._input_mode == "sentence_text":
            self.create_subscription(
                String,
                str(self.get_parameter("sentence_input_topic").value),
                self._on_sentence,
                20,
            )
        else:
            self.create_subscription(
                SpeechUtterance,
                str(self.get_parameter("input_topic").value),
                self._on_utterance,
                20,
            )
        self.create_subscription(
            String,
            "/simulation/control_state",
            self._on_control,
            10,
        )
        self.create_timer(1.0, self._publish_status)
        self._publish_status()

    def _now_sec(self) -> float:
        return self.get_clock().now().nanoseconds / 1_000_000_000.0

    def _waiting_detail(self) -> str:
        if self._input_mode == "sentence_text":
            return "waiting_for_sentence_text"
        return "waiting_for_final_surgeon_utterance"

    def _on_utterance(self, msg: SpeechUtterance) -> None:
        self._received_count += 1
        self._last_source = str(msg.source or "unknown")
        self._last_observation_stamp = (
            msg.end_stamp
            if _stamp_sec(msg.end_stamp) > 0.0
            else msg.stamp
        )
        utterance_id = str(msg.utterance_id or "").strip()
        if self._require_utterance_id and not utterance_id:
            self._reject("missing_utterance_id")
            return

        admission = evaluate_utterance(
            msg,
            now_sec=self._now_sec(),
            required_speaker_role=self._required_speaker_role,
            min_confidence=self._min_confidence,
            accept_missing_confidence=self._accept_missing_confidence,
            require_timestamp=self._require_timestamp,
            max_age_sec=self._max_age_sec,
            max_future_skew_sec=self._max_future_skew_sec,
        )
        if not admission.accepted:
            self._reject(admission.reason)
            return
        if utterance_id and not self._recent_ids.accept(
            utterance_id,
            time.monotonic(),
        ):
            self._reject("duplicate_utterance_id")
            return

        transcript = String()
        transcript.data = admission.text
        self._transcript_pub.publish(transcript)
        self._accepted_count += 1
        self._last_accepted_monotonic = time.monotonic()
        self._last_detail = "accepted_final_surgeon_utterance"
        self._publish_status()

    def _on_sentence(self, msg: String) -> None:
        self._received_count += 1
        self._last_source = self._sentence_source_id
        self._last_observation_stamp = self.get_clock().now().to_msg()
        sentence = normalize_sentence_text(msg.data)
        if not sentence:
            self._reject("empty_sentence")
            return
        if not self._recent_sentences.accept(sentence, time.monotonic()):
            self._reject("duplicate_sentence")
            return

        admitted = String()
        admitted.data = sentence
        self._transcript_pub.publish(admitted)
        self._accepted_count += 1
        self._last_accepted_monotonic = time.monotonic()
        self._last_detail = "accepted_sentence_text"
        self._publish_status()

    def _reject(self, reason: str) -> None:
        self._rejected_count += 1
        self._last_detail = str(reason)
        self.get_logger().warning(
            f"rejected speech input: {reason}",
            throttle_duration_sec=2.0,
        )
        self._publish_status()

    def _on_control(self, msg: String) -> None:
        command, _, detail = str(msg.data or "").strip().partition(":")
        command = command.lower()
        signature = (command, detail.strip())
        if command not in {
            "start",
            "start_runtime",
            "start_actors",
            "pause",
            "resume",
            "stop",
            "reset",
        }:
            return
        if command != "reset":
            if signature == getattr(
                self, "_last_lifecycle_control_signature", None
            ):
                return
            self._last_lifecycle_control_signature = signature
        if command == "start_runtime":
            self._lifecycle_control_state = "starting"
            return
        if command == "pause":
            self._lifecycle_control_state = "paused"
            return
        if command == "resume":
            self._lifecycle_control_state = "running"
            return
        if command == "stop":
            self._lifecycle_control_state = "stopped"
            return
        if command in {"start", "start_actors"}:
            if getattr(self, "_lifecycle_control_state", "stopped") == "running":
                return
            self._lifecycle_control_state = "running"
        self._recent_ids.clear()
        self._recent_sentences.clear()
        if command == "reset":
            self._last_lifecycle_control_signature = None
            self._lifecycle_control_state = "stopped"
            self._epoch += 1
            self._received_count = 0
            self._accepted_count = 0
            self._rejected_count = 0
            self._last_source = ""
            self._last_observation_stamp = None
            self._last_accepted_monotonic = 0.0
            self._last_detail = self._waiting_detail()
            self._publish_status()

    def _publish_status(self) -> None:
        now_monotonic = time.monotonic()
        age_sec = (
            now_monotonic - self._last_accepted_monotonic
            if self._last_accepted_monotonic > 0.0
            else -1.0
        )
        healthy = 0.0 <= age_sec <= self._source_timeout_sec
        if self._received_count == 0:
            state = "MISSING"
        elif healthy:
            state = "READY"
        elif self._accepted_count == 0:
            state = "ERROR"
        else:
            state = "STALE"

        status = InputSourceStatus()
        status.stamp = self.get_clock().now().to_msg()
        status.source_id = self._last_source
        status.modality = (
            "sentence_text" if self._input_mode == "sentence_text" else "speech"
        )
        status.state = state
        status.healthy = healthy
        if self._last_observation_stamp is not None:
            status.last_observation_stamp = self._last_observation_stamp
        status.age_sec = float(age_sec)
        status.received_count = int(self._received_count)
        status.accepted_count = int(self._accepted_count)
        status.rejected_count = int(self._rejected_count)
        status.epoch = int(self._epoch)
        status.dropped_count = 0
        status.error_code = (
            self._last_detail
            if state in {"ERROR", "STALE"}
            else ""
        )
        status.detail = self._last_detail
        self._status_pub.publish(status)


def main() -> None:
    rclpy.init()
    node = SpeechInputAdapterNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node.destroy_node()
        finally:
            if rclpy.ok():
                rclpy.shutdown()
