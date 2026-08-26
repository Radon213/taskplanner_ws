"""Admit public surgeon sentences from live or validation input boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from surgical_msgs.msg import InputSourceStatus, SpeechUtterance, TTSPlaybackStatus


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


def admitted_utterance(
    msg: SpeechUtterance,
    *,
    text: str,
) -> SpeechUtterance:
    """Copy a validated ASR envelope without minting new source metadata.

    The live speech route must retain the original utterance identity and
    source timestamp all the way to the intent resolver.  In particular, do
    not turn a typed message into a newly-created ``String`` and then attach a
    local receipt time downstream: that would make replayed or stale speech
    indistinguishable from a live final transcript.
    """

    output = SpeechUtterance()
    output.stamp = msg.stamp
    output.start_stamp = msg.start_stamp
    output.end_stamp = msg.end_stamp
    output.utterance_id = str(msg.utterance_id or "")
    output.text = str(text or "")
    output.is_final = bool(msg.is_final)
    output.has_confidence = bool(msg.has_confidence)
    output.confidence = float(msg.confidence)
    output.speaker_role = str(msg.speaker_role or "")
    output.language = str(msg.language or "")
    output.source = str(msg.source or "")
    return output


@dataclass(frozen=True, slots=True)
class SpeechAdmission:
    accepted: bool
    reason: str
    text: str = ""


def normalize_sentence_text(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def normalize_tts_echo_text(text: str) -> str:
    """Return the spoken-content key used only for local speaker echo checks."""

    return "".join(
        character.casefold()
        for character in str(text or "")
        if character.isalnum()
    )


@dataclass(slots=True)
class _TTSEchoEntry:
    normalized_text: str
    updated_at: float
    expires_at: float


class TTSEchoGuard:
    """Track audible TTS replies and identify their ASR loopback.

    This helper deliberately has no ROS dependency.  Playback callbacks feed it
    plain values and the typed ASR boundary asks it for a matching ``reply_id``.
    """

    _TERMINAL_STATES = {
        "played",
        "failed",
        "interrupted",
        "interrupted_unknown",
    }

    def __init__(
        self,
        *,
        tail_sec: float = 0.8,
        similarity_threshold: float = 0.88,
        min_similarity_chars: int = 8,
        max_entries: int = 128,
        max_playing_sec: float = 30.0,
    ) -> None:
        self._tail_sec = max(0.0, float(tail_sec))
        self._similarity_threshold = min(
            1.0,
            max(0.0, float(similarity_threshold)),
        )
        self._min_similarity_chars = max(1, int(min_similarity_chars))
        self._max_entries = max(1, int(max_entries))
        self._max_playing_sec = max(1.0, float(max_playing_sec))
        self._entries: dict[str, _TTSEchoEntry] = {}

    def observe_playback(
        self,
        *,
        reply_id: str,
        text: str,
        state: str,
        now_monotonic: float,
        event_age_sec: float = 0.0,
        audio_duration_sec: float = 0.0,
        error_code: str = "",
    ) -> None:
        """Update one reply's bounded audible interval.

        Transient-local DDS can replay status history after an ASR restart.
        Event age prevents old retained ``playing`` rows from creating an
        immortal echo block and prevents old terminal rows from minting a new
        full tail interval.
        """

        now = float(now_monotonic)
        self._prune(now)
        reply_key = str(reply_id or "").strip()
        playback_state = str(state or "").strip().lower()
        event_age = max(0.0, float(event_age_sec))
        normalized = normalize_tts_echo_text(text)
        existing = self._entries.get(reply_key)

        if playback_state == "playing":
            if reply_key and normalized:
                expected_playing = float(audio_duration_sec)
                if expected_playing <= 0.0:
                    expected_playing = self._max_playing_sec
                expected_playing = min(
                    self._max_playing_sec,
                    max(0.1, expected_playing),
                )
                remaining = expected_playing + self._tail_sec - event_age
                if remaining <= 0.0:
                    self._entries.pop(reply_key, None)
                    return
                self._entries[reply_key] = _TTSEchoEntry(
                    normalized_text=normalized,
                    updated_at=now,
                    expires_at=now + remaining,
                )
                self._trim()
            return

        if playback_state not in self._TERMINAL_STATES or not reply_key:
            return
        # Synthesis/scope/waiting failures produced no speaker output. A
        # failed event extends the echo tail only when this process already
        # observed its playing interval, or when a retained playback failure
        # explicitly proves that playback may have begun.
        if (
            playback_state in {"failed", "interrupted", "interrupted_unknown"}
            and existing is None
            and str(error_code or "").strip()
            not in {"playback_failed", "interrupted_unknown"}
        ):
            return
        if not normalized and existing is not None:
            normalized = existing.normalized_text
        if not normalized:
            return
        remaining_tail = self._tail_sec - event_age
        if remaining_tail <= 0.0:
            self._entries.pop(reply_key, None)
            return
        self._entries[reply_key] = _TTSEchoEntry(
            normalized_text=normalized,
            updated_at=now,
            expires_at=now + remaining_tail,
        )
        self._trim()

    def matching_reply_id(
        self,
        text: str,
        *,
        now_monotonic: float,
    ) -> str | None:
        """Return the best audible reply match, if this looks like loopback."""

        now = float(now_monotonic)
        self._prune(now)
        candidate = normalize_tts_echo_text(text)
        if not candidate:
            return None

        for reply_id, entry in self._entries.items():
            if candidate == entry.normalized_text:
                return reply_id

        if len(candidate) < self._min_similarity_chars:
            return None
        best_reply_id: str | None = None
        best_similarity = self._similarity_threshold
        for reply_id, entry in self._entries.items():
            if len(entry.normalized_text) < self._min_similarity_chars:
                continue
            similarity = SequenceMatcher(
                None,
                candidate,
                entry.normalized_text,
                autojunk=False,
            ).ratio()
            if similarity >= best_similarity:
                best_reply_id = reply_id
                best_similarity = similarity
        return best_reply_id

    def clear(self) -> None:
        self._entries.clear()

    def _prune(self, now_monotonic: float) -> None:
        self._entries = {
            reply_id: entry
            for reply_id, entry in self._entries.items()
            if entry.expires_at >= float(now_monotonic)
        }

    def _trim(self) -> None:
        overflow = len(self._entries) - self._max_entries
        if overflow <= 0:
            return
        oldest = sorted(
            self._entries,
            key=lambda reply_id: self._entries[reply_id].updated_at,
        )
        for reply_id in oldest[:overflow]:
            self._entries.pop(reply_id, None)


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
    require_source: bool = False,
) -> SpeechAdmission:
    text = str(msg.text or "").strip()
    if not text:
        return SpeechAdmission(False, "empty_text")
    if not bool(msg.is_final):
        return SpeechAdmission(False, "interim_transcript")
    if require_source and not str(msg.source or "").strip():
        return SpeechAdmission(False, "missing_source")
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
        # ``sentence_text`` is retained only for Debug/replay compatibility.
        # A Live typed route must keep the ASR source envelope until the
        # resolver has checked its timestamp and one-shot identifier.
        self.declare_parameter("output_mode", "sentence_text")
        self.declare_parameter(
            "typed_output_topic",
            "/surgery/audio/admitted_utterance",
        )
        self.declare_parameter("status_topic", "/input/speech/status")
        self.declare_parameter("sentence_source_id", "external_sentence_topic")
        self.declare_parameter("sentence_dedupe_sec", 1.0)
        self.declare_parameter("required_speaker_role", "surgeon")
        self.declare_parameter("min_confidence", 0.55)
        self.declare_parameter("accept_missing_confidence", True)
        self.declare_parameter("require_timestamp", True)
        self.declare_parameter("require_utterance_id", True)
        self.declare_parameter("require_source", True)
        self.declare_parameter("max_age_sec", 3.0)
        self.declare_parameter("max_future_skew_sec", 1.0)
        self.declare_parameter("source_timeout_sec", 5.0)
        self.declare_parameter("dedupe_retention_sec", 120.0)
        self.declare_parameter("enable_tts_echo_guard", True)
        self.declare_parameter(
            "tts_playback_status_topic",
            "/tts/playback_status",
        )
        self.declare_parameter("tts_echo_tail_sec", 0.8)
        self.declare_parameter("tts_echo_similarity_threshold", 0.88)

        self._input_mode = str(self.get_parameter("input_mode").value).strip().lower()
        if self._input_mode not in {"utterance", "sentence_text"}:
            raise ValueError(
                "input_mode must be either 'utterance' or 'sentence_text'"
            )
        self._output_mode = str(
            self.get_parameter("output_mode").value
        ).strip().lower()
        if self._output_mode not in {"sentence_text", "typed_utterance"}:
            raise ValueError(
                "output_mode must be either 'sentence_text' or "
                "'typed_utterance'"
            )
        if (
            self._output_mode == "typed_utterance"
            and self._input_mode != "utterance"
        ):
            raise ValueError(
                "typed_utterance output requires input_mode='utterance'; "
                "String input has no ASR provenance to preserve"
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
        self._require_source = bool(self.get_parameter("require_source").value)
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
        self._enable_tts_echo_guard = bool(
            self.get_parameter("enable_tts_echo_guard").value
        )
        self._tts_echo_guard = TTSEchoGuard(
            tail_sec=float(self.get_parameter("tts_echo_tail_sec").value),
            similarity_threshold=float(
                self.get_parameter("tts_echo_similarity_threshold").value
            ),
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

        if self._output_mode == "typed_utterance":
            self._transcript_pub = self.create_publisher(
                SpeechUtterance,
                str(self.get_parameter("typed_output_topic").value),
                20,
            )
        else:
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
        if self._enable_tts_echo_guard and self._input_mode == "utterance":
            self.create_subscription(
                TTSPlaybackStatus,
                str(self.get_parameter("tts_playback_status_topic").value),
                self._on_tts_playback_status,
                QoSProfile(
                    depth=32,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                ),
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
            require_source=self._require_source,
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
        if self._enable_tts_echo_guard:
            echo_reply_id = self._tts_echo_guard.matching_reply_id(
                admission.text,
                now_monotonic=time.monotonic(),
            )
            if echo_reply_id:
                self._reject(self._tts_echo_detail(echo_reply_id))
                return

        if self._output_mode == "typed_utterance":
            self._transcript_pub.publish(
                admitted_utterance(msg, text=admission.text)
            )
        else:
            transcript = String()
            transcript.data = admission.text
            self._transcript_pub.publish(transcript)
        self._accepted_count += 1
        self._last_accepted_monotonic = time.monotonic()
        self._last_detail = "accepted_final_surgeon_utterance"
        self._publish_status()

    def _on_tts_playback_status(self, msg: TTSPlaybackStatus) -> None:
        stamp_sec = _stamp_sec(msg.stamp)
        event_age_sec = (
            max(0.0, self._now_sec() - stamp_sec)
            if stamp_sec > 0.0
            else 0.0
        )
        self._tts_echo_guard.observe_playback(
            reply_id=str(msg.reply_id or ""),
            text=str(msg.text or ""),
            state=str(msg.state or ""),
            now_monotonic=time.monotonic(),
            event_age_sec=event_age_sec,
            audio_duration_sec=float(msg.audio_duration_sec),
            error_code=str(msg.error_code or ""),
        )

    @staticmethod
    def _tts_echo_detail(reply_id: str) -> str:
        prefix = "tts_echo_suppressed:"
        safe_reply_id = " ".join(str(reply_id or "unknown").split())
        return (prefix + safe_reply_id)[:160]

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
