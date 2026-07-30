"""Release recorded transcript JSON causally into the production speech boundary."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import heapq
import json
import math
from typing import Any

from builtin_interfaces.msg import Time
import rclpy
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_msgs.msg import ShadowReplayState, SpeechUtterance


EXPLICIT_AVAILABILITY_POLICY = "explicit_available_sec"
CLAMPED_AVAILABILITY_POLICY = "explicit_available_sec_clamped_to_end"
LEGACY_AVAILABILITY_POLICY = "legacy_missing_available_sec_defer_to_end"
IMMEDIATE_AVAILABILITY_POLICY = "untimed_plain_text_publish_on_arrival"
RELEASE_TIMER_PERIOD_SEC = 0.01
HISTORY_TIMER_PERIOD_SEC = 0.5
MAX_HISTORY_ITEMS = 48


def replay_context_requires_transcript_reset(
    *,
    current_case_id: str,
    current_run_id: str,
    last_source_time_sec: float,
    next_case_id: str,
    next_run_id: str,
    next_source_time_sec: float,
) -> bool:
    """Detect case/run replacement or a rewind before releasing more speech."""

    case_changed = bool(
        next_case_id and next_case_id != current_case_id
    )
    run_changed = bool(next_run_id and next_run_id != current_run_id)
    rewound = bool(
        next_run_id
        and next_run_id == current_run_id
        and next_source_time_sec + 0.25 < last_source_time_sec
    )
    return case_changed or run_changed or rewound


def parse_transcript_payload(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {
            "text": "",
            "start_sec": None,
            "end_sec": None,
            "available_sec": None,
            "source_wav": "",
            "schema": "",
        }
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {
            "text": text,
            "start_sec": None,
            "end_sec": None,
            "available_sec": None,
            "source_wav": "",
            "schema": "",
        }
    if not isinstance(payload, dict):
        return {
            "text": text,
            "start_sec": None,
            "end_sec": None,
            "available_sec": None,
            "source_wav": "",
            "schema": "",
        }
    return {
        "text": str(payload.get("text", "")).strip(),
        "start_sec": payload.get("start_sec", payload.get("time_sec")),
        "end_sec": payload.get("end_sec"),
        "available_sec": payload.get("available_sec"),
        "source_wav": str(payload.get("source_wav", "")),
        "schema": str(payload.get("schema", "")),
    }


def _nonnegative_finite_seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0.0:
        return None
    return seconds


def resolve_available_sec(
    payload: dict[str, Any],
    *,
    arrival_sec: float,
) -> tuple[float, str]:
    """Return a causal complete-text release time and the applied policy.

    v2 records carry ``available_sec`` explicitly.  Legacy JSON records are
    intentionally not released at their source start timestamp; their
    utterance end is the safe compatibility floor.  Untimed plain text is
    already complete when it reaches this adapter and therefore remains an
    immediate-arrival compatibility case.
    """

    arrival = _nonnegative_finite_seconds(arrival_sec)
    if arrival is None:
        raise ValueError("arrival_sec must be a finite non-negative number")
    end = _nonnegative_finite_seconds(payload.get("end_sec"))
    explicit = _nonnegative_finite_seconds(payload.get("available_sec"))
    if explicit is not None:
        if end is not None and explicit < end:
            return end, CLAMPED_AVAILABILITY_POLICY
        return explicit, EXPLICIT_AVAILABILITY_POLICY
    if end is not None:
        return end, LEGACY_AVAILABILITY_POLICY
    return arrival, IMMEDIATE_AVAILABILITY_POLICY


def time_msg(value: Any, fallback: Time) -> Time:
    seconds = _nonnegative_finite_seconds(value)
    if seconds is None:
        return fallback
    msg = Time()
    msg.sec = int(seconds)
    msg.nanosec = int(round((seconds - msg.sec) * 1_000_000_000.0))
    if msg.nanosec >= 1_000_000_000:
        msg.sec += 1
        msg.nanosec -= 1_000_000_000
    return msg


def time_sec(value: Time) -> float:
    return float(value.sec) + float(value.nanosec) / 1_000_000_000


def transcript_history_json(
    case_id: str,
    rows: list[dict[str, Any]],
    *,
    run_id: str = "",
) -> str:
    return json.dumps(
        {
            "schema": "taskplanner.shadow_transcript_history.v1",
            "case_id": str(case_id),
            "run_id": str(run_id),
            "utterances": rows[:MAX_HISTORY_ITEMS],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class PendingTranscript:
    available_ns: int
    sequence: int
    utterance_id: str
    text: str
    start: Time
    end: Time
    availability_policy: str


class TranscriptReleaseBuffer:
    """Keep one timestamp-ordered, run-scoped queue of public transcripts."""

    def __init__(self) -> None:
        self._published_ids: set[str] = set()
        self._queued_ids: set[str] = set()
        self._pending: list[tuple[int, int, PendingTranscript]] = []
        self._sequence = 0

    def reset(self) -> None:
        self._published_ids.clear()
        self._queued_ids.clear()
        self._pending.clear()
        self._sequence = 0

    def add(
        self,
        *,
        available_ns: int,
        utterance_id: str,
        text: str,
        start: Time,
        end: Time,
        availability_policy: str,
    ) -> bool:
        if (
            utterance_id in self._published_ids
            or utterance_id in self._queued_ids
        ):
            return False
        self._sequence += 1
        pending = PendingTranscript(
            available_ns=int(available_ns),
            sequence=self._sequence,
            utterance_id=utterance_id,
            text=text,
            start=start,
            end=end,
            availability_policy=availability_policy,
        )
        self._queued_ids.add(utterance_id)
        heapq.heappush(
            self._pending,
            (pending.available_ns, pending.sequence, pending),
        )
        return True

    def pop_due(self, now_ns: int) -> list[PendingTranscript]:
        due: list[PendingTranscript] = []
        while self._pending and self._pending[0][0] <= int(now_ns):
            _, _, pending = heapq.heappop(self._pending)
            self._queued_ids.discard(pending.utterance_id)
            self._published_ids.add(pending.utterance_id)
            due.append(pending)
        return due


class RecordedTranscriptAdapterNode(Node):
    def __init__(self) -> None:
        super().__init__("recorded_transcript_adapter")
        self.declare_parameter("input_topic", "/surgery/transcript")
        self.declare_parameter("output_topic", "/shadow/speech/utterance")
        self.declare_parameter("history_topic", "/shadow/speech/history")
        self.declare_parameter("case_id", "unknown")
        self.declare_parameter("speaker_role", "surgeon")
        self.declare_parameter("language", "ko")

        self._case_id = str(self.get_parameter("case_id").value)
        self._speaker_role = str(self.get_parameter("speaker_role").value)
        self._language = str(self.get_parameter("language").value)
        self._run_id = ""
        self._last_replay_source_sec = 0.0
        self._buffer = TranscriptReleaseBuffer()
        self._legacy_policy_logged = False
        self._history: list[dict[str, Any]] = []
        self._publisher = self.create_publisher(
            SpeechUtterance,
            str(self.get_parameter("output_topic").value),
            20,
        )
        self._history_publisher = self.create_publisher(
            String,
            str(self.get_parameter("history_topic").value),
            1,
        )
        self.create_subscription(
            String,
            str(self.get_parameter("input_topic").value),
            self._on_transcript,
            20,
        )
        replay_state_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.create_subscription(
            ShadowReplayState,
            "/shadow/replay_state",
            self._on_replay_state,
            replay_state_qos,
        )
        self._release_timer = self.create_timer(
            RELEASE_TIMER_PERIOD_SEC,
            self._release_due,
        )
        self._history_timer = self.create_timer(
            HISTORY_TIMER_PERIOD_SEC,
            self._publish_history,
        )

    def _reset_for_run(self, run_id: str, source_time_sec: float) -> None:
        self._run_id = str(run_id)
        self._last_replay_source_sec = max(0.0, float(source_time_sec))
        self._buffer.reset()
        self._history = []
        self._legacy_policy_logged = False
        self._publish_history()

    def _on_replay_state(self, state: ShadowReplayState) -> None:
        case_id = str(state.case_id or "").strip()
        run_id = str(state.run_id or "")
        source_time_sec = max(0.0, float(state.source_time_sec))
        case_changed = bool(case_id and case_id != self._case_id)
        reset_required = replay_context_requires_transcript_reset(
            current_case_id=self._case_id,
            current_run_id=self._run_id,
            last_source_time_sec=self._last_replay_source_sec,
            next_case_id=case_id,
            next_run_id=run_id,
            next_source_time_sec=source_time_sec,
        )
        if case_changed:
            self._case_id = case_id
        if reset_required:
            self._reset_for_run(run_id, source_time_sec)
        else:
            self._last_replay_source_sec = source_time_sec

    def _on_transcript(self, source: String) -> None:
        payload = parse_transcript_payload(source.data)
        text = str(payload.get("text", "")).strip()
        if not text:
            self.get_logger().warning("ignored empty recorded transcript")
            return
        now = self.get_clock().now().to_msg()
        available_sec, availability_policy = resolve_available_sec(
            payload,
            arrival_sec=time_sec(now),
        )
        start = time_msg(payload.get("start_sec"), now)
        end = time_msg(payload.get("end_sec"), now)
        identity = (
            f"{self._case_id}|{start.sec}.{start.nanosec}|"
            f"{end.sec}.{end.nanosec}|{text}"
        )
        utterance_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        if (
            availability_policy == LEGACY_AVAILABILITY_POLICY
            and not self._legacy_policy_logged
        ):
            self.get_logger().info(
                "legacy transcript payload has no available_sec; "
                "complete text will be deferred to end_sec"
            )
            self._legacy_policy_logged = True
        elif availability_policy == CLAMPED_AVAILABILITY_POLICY:
            self.get_logger().warning(
                "transcript available_sec precedes end_sec; "
                "publication was clamped to end_sec"
            )

        added = self._buffer.add(
            available_ns=round(available_sec * 1_000_000_000),
            utterance_id=utterance_id,
            text=text,
            start=start,
            end=end,
            availability_policy=availability_policy,
        )
        if not added:
            return
        now_ns = int(now.sec) * 1_000_000_000 + int(now.nanosec)
        for pending in self._buffer.pop_due(now_ns):
            self._publish(pending, now)

    def _publish(self, pending: PendingTranscript, now: Time) -> None:
        utterance = SpeechUtterance()
        utterance.stamp = now
        utterance.start_stamp = pending.start
        utterance.end_stamp = pending.end
        utterance.utterance_id = pending.utterance_id
        utterance.text = pending.text
        utterance.is_final = True
        utterance.has_confidence = False
        utterance.confidence = 0.0
        utterance.speaker_role = self._speaker_role
        utterance.language = self._language
        utterance.source = (
            f"recorded_transcript:{self._case_id}:{self._run_id}"
            if self._run_id
            else f"recorded_transcript:{self._case_id}"
        )
        self._publisher.publish(utterance)
        self._history = [
            {
                "stamp": {
                    "sec": int(utterance.stamp.sec),
                    "nanosec": int(utterance.stamp.nanosec),
                },
                "start_stamp": {
                    "sec": int(utterance.start_stamp.sec),
                    "nanosec": int(utterance.start_stamp.nanosec),
                },
                "end_stamp": {
                    "sec": int(utterance.end_stamp.sec),
                    "nanosec": int(utterance.end_stamp.nanosec),
                },
                "utterance_id": utterance.utterance_id,
                "text": utterance.text,
                "is_final": bool(utterance.is_final),
                "has_confidence": bool(utterance.has_confidence),
                "confidence": float(utterance.confidence),
                "speaker_role": utterance.speaker_role,
                "language": utterance.language,
                "source": utterance.source,
            },
            *[
                row
                for row in self._history
                if str(row.get("utterance_id", "")) != utterance.utterance_id
            ],
        ][:MAX_HISTORY_ITEMS]
        self._publish_history()

    def _publish_history(self) -> None:
        message = String()
        message.data = transcript_history_json(
            self._case_id,
            self._history,
            run_id=self._run_id,
        )
        self._history_publisher.publish(message)

    def _release_due(self) -> None:
        now = self.get_clock().now().to_msg()
        now_ns = int(now.sec) * 1_000_000_000 + int(now.nanosec)
        for pending in self._buffer.pop_due(now_ns):
            self._publish(pending, now)


def main() -> None:
    rclpy.init()
    node = RecordedTranscriptAdapterNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
