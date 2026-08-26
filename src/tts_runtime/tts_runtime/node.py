"""ROS adapter for the durable Taskplanner TTS dispatcher."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import time

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from surgical_interop_msgs.msg import GatewayInfo
from surgical_msgs.msg import (
    BedRobotArmGroupStatus,
    ExecutionTrace,
    HumanoidReply,
    SkillCommand,
    SkillStatus,
    TTSPlaybackStatus,
    TwinEvent,
)

from .backends import PipeWirePlayer, SupertonicSubprocessSynthesizer
from .core import (
    DeterministicWavCache,
    PlaybackDispatcher,
    PlaybackEvent,
    PlaybackStore,
    ReplyRequest,
    RuntimeConfig,
    supertonic_model_manifest_sha256,
)
from .correlation import (
    AuthoritativeTimingCorrelator,
    filter_waiting_for_active_run,
)
from .feedback import RetrievalFeedbackAnnouncer


def _default_project_root() -> Path:
    configured = os.environ.get("TASKPLANNER_ARPAH_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "ARPA-H"


def _create_steady_timer(
    node: Node,
    period_sec: float,
    callback,
):
    """Create a watchdog timer that advances independently of ROS time."""

    clock = Clock(clock_type=ClockType.STEADY_TIME)
    timer = node.create_timer(period_sec, callback, clock=clock)
    return clock, timer


class TTSRuntimeNode(Node):
    def __init__(self) -> None:
        super().__init__("tts_runtime")
        project_root = _default_project_root()
        state_root = Path.home() / ".local" / "state" / "taskplanner" / "tts_runtime"
        cache_root = Path.home() / ".cache" / "taskplanner" / "tts_runtime"
        benchmark_root = project_root / "supertonic_taskplanner_benchmark"

        self.declare_parameter("input_topic", "/tts/admitted_reply")
        self.declare_parameter("status_topic", "/tts/playback_status")
        self.declare_parameter("database_path", str(state_root / "playback.sqlite3"))
        self.declare_parameter("cache_dir", str(cache_root / "wav"))
        self.declare_parameter("model_dir", str(benchmark_root / "model-sdk-1.3.1"))
        self.declare_parameter(
            "model_identity",
            "supertonic-3:model-sdk-1.3.1:724fb5abbf5502583fb520898d45929e62f02c0b",
        )
        self.declare_parameter("synth_python", str(benchmark_root / ".venv" / "bin" / "python"))
        self.declare_parameter("voice_id", "F1")
        self.declare_parameter("language", "ko")
        self.declare_parameter("steps", 8)
        self.declare_parameter("speed", 1.05)
        self.declare_parameter("silence_duration", 0.3)
        self.declare_parameter("output_target", "")
        self.declare_parameter("synthesis_timeout_sec", 120.0)
        self.declare_parameter("waiting_timeout_sec", 120.0)
        self.declare_parameter("gateway_info_timeout_sec", 3.0)
        self.declare_parameter("intra_op_threads", 4)
        self.declare_parameter("inter_op_threads", 1)
        self.declare_parameter(
            "prewarm_texts",
            ["바이폴라 전달드리겠습니다", "도구 회수중입니다"],
        )

        value = lambda name: self.get_parameter(name).value
        model_dir = Path(str(value("model_dir"))).expanduser().resolve()
        output_target = str(value("output_target")).strip()
        self._waiting_timeout_sec = float(value("waiting_timeout_sec"))
        if self._waiting_timeout_sec <= 0:
            raise ValueError("waiting_timeout_sec must be positive")
        self._gateway_info_timeout_sec = float(value("gateway_info_timeout_sec"))
        if self._gateway_info_timeout_sec <= 0:
            raise ValueError("gateway_info_timeout_sec must be positive")
        base_model_identity = str(value("model_identity")).strip()
        if not base_model_identity:
            raise ValueError("model_identity must be non-empty")
        model_manifest = supertonic_model_manifest_sha256(
            model_dir,
            voice_id=str(value("voice_id")),
        )
        config = RuntimeConfig(
            # Stable across host/container mount paths so both resolve the same
            # content-addressed WAV cache namespace.
            model_identity=(
                f"{base_model_identity}:manifest-sha256:{model_manifest}"
            ),
            voice_id=str(value("voice_id")),
            language=str(value("language")),
            steps=int(value("steps")),
            speed=float(value("speed")),
            silence_duration=float(value("silence_duration")),
            output_device=output_target or "default",
        )
        status_qos = QoSProfile(
            depth=32,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        input_qos = QoSProfile(
            depth=64,
            reliability=ReliabilityPolicy.RELIABLE,
            # The admission gate owns replay through its durable SQLite
            # outbox.  Keeping both ends VOLATILE avoids a retained, already
            # acknowledged reply being delivered again to a restarted TTS
            # consumer.  The typed playback-status publisher above remains
            # TRANSIENT_LOCAL so producers can recover the latest ACK.
            durability=DurabilityPolicy.VOLATILE,
        )
        synthesizer = SupertonicSubprocessSynthesizer(
            python_executable=str(value("synth_python")),
            model_dir=model_dir,
            voice_id=config.voice_id,
            language=config.language,
            steps=config.steps,
            speed=config.speed,
            silence_duration=config.silence_duration,
            request_timeout_sec=float(value("synthesis_timeout_sec")),
            intra_op_threads=int(value("intra_op_threads")),
            inter_op_threads=int(value("inter_op_threads")),
        )
        self._dispatcher = PlaybackDispatcher(
            store=PlaybackStore(str(value("database_path"))),
            cache=DeterministicWavCache(str(value("cache_dir"))),
            synthesizer=synthesizer,
            player=PipeWirePlayer(target=output_target),
            config=config,
            on_event=self._publish_status,
        )
        prewarm_texts = [
            str(text).strip()
            for text in value("prewarm_texts")
            if str(text).strip()
        ]
        if not prewarm_texts:
            self._dispatcher.close()
            raise ValueError("prewarm_texts must contain at least one phrase")
        try:
            prewarm_results = self._dispatcher.prewarm(prewarm_texts)
        except BaseException:
            self._dispatcher.close()
            raise
        # The Compose health check discovers this publisher. Create it only
        # after model inference and cache publication succeeded, so graph
        # presence cannot race ahead of actual TTS readiness.
        self._status_publisher = self.create_publisher(
            TTSPlaybackStatus,
            str(value("status_topic")),
            status_qos,
        )
        for interrupted in self._dispatcher.store.fail_interrupted_playing():
            self._publish_status(interrupted)
        self._dispatcher_started = False
        self._gateway_scope: tuple[str, str] | None = None
        self._procedure_active = False
        self._scope_stamp_floor_ns: int | None = None
        self._gateway_info_last_seen_monotonic: float | None = None
        self._gateway_info_last_instance_id = ""
        self._gateway_info_last_revision = -1
        self._gateway_info_last_source_stamp_ns = 0
        self._pending_unscoped_replies: deque[ReplyRequest] = deque(maxlen=64)
        self._correlator = AuthoritativeTimingCorrelator(
            waiting_provider=self._current_run_waiting,
            release_waiting=self._dispatcher.release_waiting,
        )
        self._fixed_feedback = RetrievalFeedbackAnnouncer()
        self._waiting_expiry_timer = self.create_timer(
            min(1.0, max(0.1, self._waiting_timeout_sec / 4.0)),
            self._expire_waiting,
        )
        (
            self._gateway_info_watchdog_clock,
            self._gateway_info_watchdog_timer,
        ) = _create_steady_timer(
            self,
            min(0.5, max(0.1, self._gateway_info_timeout_sec / 4.0)),
            self._on_gateway_info_watchdog,
        )
        self._subscription = self.create_subscription(
            HumanoidReply,
            str(value("input_topic")),
            self._on_reply,
            input_qos,
        )
        # Read-only evidence subscriptions. None of these callbacks owns or
        # invokes an Action, Service, command topic, or motion interface.
        self._evidence_subscriptions = [
            self.create_subscription(
                GatewayInfo,
                "/surgery/gateway_info",
                self._on_gateway_info,
                QoSProfile(
                    depth=1,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                ),
            ),
            self.create_subscription(
                TwinEvent,
                "/twin/events",
                self._on_twin_event,
                50,
            ),
            self.create_subscription(
                SkillCommand,
                "/bt/skill_command",
                self._on_skill_command,
                50,
            ),
            self.create_subscription(
                SkillStatus,
                "/skill/status",
                self._on_skill_status,
                50,
            ),
            self.create_subscription(
                ExecutionTrace,
                "/surgery/execution_trace",
                self._on_execution_trace,
                50,
            ),
            self.create_subscription(
                BedRobotArmGroupStatus,
                "/bed_robot_arm_group/status",
                self._on_retraction_status,
                50,
            ),
        ]
        self.get_logger().info(
            "TTS endpoint ready; awaiting active GatewayInfo: "
            "voice=%s lang=%s steps=%d speed=%.2f output=%s prewarm=%d"
            % (
                config.voice_id,
                config.language,
                config.steps,
                config.speed,
                config.output_device,
                len(prewarm_results),
            )
        )

    def _publish_status(self, event: PlaybackEvent) -> None:
        message = TTSPlaybackStatus()
        message.stamp = self.get_clock().now().to_msg()
        message.sequence = event.sequence
        message.reply_id = event.reply_id
        message.turn_id = event.turn_id
        message.utterance_id = event.utterance_id
        message.procedure_run_id = event.procedure_run_id
        message.state = event.state
        message.timing = event.timing
        message.text = event.text
        message.text_sha256 = event.text_sha256
        message.voice_id = event.voice_id
        message.output_device = event.output_device
        message.cache_key = event.cache_key
        message.synth_latency_ms = event.synth_latency_ms
        message.audio_duration_sec = event.audio_duration_sec
        message.playback_latency_ms = event.playback_latency_ms
        message.terminal = event.terminal
        message.success = event.success
        message.error_code = event.error_code
        message.message = event.message
        self._status_publisher.publish(message)

    def _on_reply(self, message: HumanoidReply) -> None:
        if not message.valid or not message.speak:
            return
        request = ReplyRequest(
            reply_id=message.reply_id,
            turn_id=message.turn_id,
            utterance_id=message.utterance_id,
            gateway_instance_id=message.gateway_instance_id,
            procedure_run_id=message.procedure_run_id,
            text=message.text,
            timing=message.timing,
            function_call_name=message.function_call_name,
            function_arguments_json=message.function_arguments_json,
            function_request_id=message.function_request_id,
        )
        active_scope = (
            self._gateway_scope
            if self._gateway_scope is not None and self._procedure_active
            else ("", "")
        )
        if self._gateway_scope is None:
            # Cross-topic DDS delivery order is not defined. Buffer only in
            # memory until the authoritative retained gateway scope arrives;
            # never insert or play before then.
            self._pending_unscoped_replies.append(request)
            return
        if (
            not self._dispatcher_started
            or not active_scope[0]
            or not active_scope[1]
            or request.gateway_instance_id.strip() != active_scope[0]
            or request.procedure_run_id.strip() != active_scope[1]
        ):
            self.get_logger().warning(
                "Dropped humanoid reply outside authoritative active scope: "
                f"reply_id={request.reply_id} reply_run={request.procedure_run_id} "
                f"reply_gateway={request.gateway_instance_id or 'missing'} "
                f"active_gateway={active_scope[0] or 'unavailable'} "
                f"active_run={active_scope[1] or 'unavailable'}"
            )
            return
        self._submit_request(request)

    def _submit_request(self, request: ReplyRequest) -> None:
        try:
            result = self._dispatcher.submit(request)
        except ValueError as exc:
            self.get_logger().error(f"Rejected TTS reply: {exc}")
            detail = str(exc)
            if "reply_id collision" in detail:
                error_code = "reply_id_collision"
            elif "active playback scope" in detail:
                error_code = "procedure_scope_mismatch"
            else:
                error_code = "invalid_reply"
            self._dispatcher.reject(
                request,
                error_code=error_code,
                message="tts_reply_rejected_before_playback",
            )
            return
        if result.duplicate:
            self.get_logger().debug(
                f"Ignored duplicate TTS reply_id={request.reply_id} state={result.event.state}"
            )
        elif result.event.state.startswith("waiting_function_"):
            # Evidence can precede the VLM response due to independent DDS
            # delivery. Re-evaluate immediately after durable insertion.
            self._correlator.evaluate()

    def _on_twin_event(self, message: TwinEvent) -> None:
        if not self._evidence_is_current(message):
            return
        if message.event_type != "VoiceCommandIntentObserved":
            return
        try:
            detail = json.loads(message.detail_json)
            if not isinstance(detail, dict):
                return
            raw_generation = detail.get("request_generation", 0)
            if isinstance(raw_generation, bool):
                return
            generation = int(raw_generation)
        except (TypeError, ValueError, json.JSONDecodeError):
            return
        self._correlator.observe_voice_intent(
            utterance_id=str(detail.get("utterance_id", "")),
            request_generation=generation,
            accepted=detail.get("accepted") is True,
        )

    def _on_gateway_info(self, message: GatewayInfo) -> None:
        source_age_sec = self._gateway_info_source_age_sec(message)
        gateway_instance_id = str(message.gateway_instance_id or "").strip()
        procedure_run_id = str(message.procedure_run_id or "").strip()
        revision = int(message.revision)
        source_stamp_ns = self._stamp_ns(message)
        if source_age_sec is None:
            self._fence_gateway_info("stale_or_invalid_gateway_heartbeat")
            return
        if bool(message.procedure_active) and (
            not gateway_instance_id or not procedure_run_id
        ):
            self._fence_gateway_info("malformed_active_gateway_scope")
            return
        if (
            gateway_instance_id
            == getattr(self, "_gateway_info_last_instance_id", "")
            and (
                revision <= getattr(self, "_gateway_info_last_revision", -1)
                or source_stamp_ns
                <= getattr(self, "_gateway_info_last_source_stamp_ns", 0)
            )
        ):
            # A retained/replayed sample is not a heartbeat. Do not extend the
            # steady-clock lease; the watchdog will fence the current scope.
            return
        self._gateway_info_last_instance_id = gateway_instance_id
        self._gateway_info_last_revision = revision
        self._gateway_info_last_source_stamp_ns = source_stamp_ns
        self._gateway_info_last_seen_monotonic = (
            time.monotonic() - max(0.0, source_age_sec)
        )
        procedure_active = bool(
            message.procedure_active and gateway_instance_id and procedure_run_id
        )
        scope = (
            gateway_instance_id,
            procedure_run_id if procedure_active else "",
        )
        scope_changed = (
            scope != self._gateway_scope
            or procedure_active != self._procedure_active
        )
        self._gateway_scope = scope
        self._procedure_active = procedure_active
        self._fixed_feedback.observe_gateway_info(
            gateway_instance_id=scope[0],
            procedure_run_id=scope[1],
            procedure_type=message.procedure_type,
            procedure_active=procedure_active,
            stamp_ns=self._stamp_ns(message),
        )
        if scope_changed:
            self._scope_stamp_floor_ns = self._stamp_ns(message)
            self._correlator.reset()
            active_scope = scope if self._procedure_active else ("", "")
            self._dispatcher.set_active_procedure_scope(*active_scope)
        if self._procedure_active and not self._dispatcher_started:
            # Cleanup must precede recovery, otherwise an old queued reply can
            # be picked up before the authoritative procedure scope is known.
            self._dispatcher.start()
            self._dispatcher_started = True
            self.get_logger().info(
                f"TTS playback enabled for procedure_run_id={scope[1]}"
            )
        pending = list(self._pending_unscoped_replies)
        self._pending_unscoped_replies.clear()
        for request in pending:
            if (
                self._dispatcher_started
                and self._procedure_active
                and request.gateway_instance_id.strip() == scope[0]
                and request.procedure_run_id.strip() == scope[1]
            ):
                self._submit_request(request)
            else:
                self.get_logger().warning(
                    "Dropped buffered humanoid reply outside authoritative scope: "
                    f"reply_id={request.reply_id} "
                    f"reply_gateway={request.gateway_instance_id or 'missing'} "
                    f"reply_run={request.procedure_run_id}"
                )
        self._correlator.evaluate()

    def _on_gateway_info_watchdog(self) -> None:
        """Interrupt audio once the periodic gateway authority becomes stale."""

        last_seen = self._gateway_info_last_seen_monotonic
        if last_seen is None:
            return
        if time.monotonic() - float(last_seen) <= self._gateway_info_timeout_sec:
            return

        self._fence_gateway_info("gateway_heartbeat_timeout")

    def _gateway_info_source_age_sec(self, message: GatewayInfo) -> float | None:
        """Validate the source heartbeat before treating DDS receipt as live."""

        source_stamp_ns = self._stamp_ns(message)
        revision = int(message.revision)
        now_ns = int(self.get_clock().now().nanoseconds)
        if revision <= 0 or source_stamp_ns <= 0 or now_ns <= 0:
            return None
        age_sec = float(now_ns - source_stamp_ns) / 1_000_000_000.0
        if age_sec > self._gateway_info_timeout_sec or age_sec < -1.0:
            return None
        return age_sec

    def _fence_gateway_info(self, reason: str) -> None:
        """Apply the unavailable boundary once without trusting message text."""

        if (
            self._gateway_scope == ("", "")
            and not self._procedure_active
            and self._gateway_info_last_seen_monotonic is None
        ):
            return
        # Use an explicit unavailable sentinel instead of None. None is
        # reserved for startup, where transient-local delivery can legitimately
        # deliver HumanoidReply before GatewayInfo. After a timeout, replies are
        # dropped until a fresh heartbeat rather than buffered across the gap.
        previous_scope = self._gateway_scope
        self._gateway_info_last_seen_monotonic = None
        self._gateway_scope = ("", "")
        self._procedure_active = False
        self._scope_stamp_floor_ns = None
        self._pending_unscoped_replies.clear()
        self._correlator.reset()
        self._fixed_feedback.observe_gateway_info(
            gateway_instance_id="",
            procedure_run_id="",
            procedure_type="",
            procedure_active=False,
            stamp_ns=0,
        )
        self._dispatcher.set_active_procedure_scope("", "")
        if previous_scope not in {None, ("", "")}:
            self.get_logger().error(
                "GatewayInfo authority unavailable; TTS queue and playback fenced "
                f"({reason})"
            )

    @staticmethod
    def _stamp_ns(message: object) -> int:
        stamp = getattr(message, "stamp", None)
        return int(getattr(stamp, "sec", 0)) * 1_000_000_000 + int(
            getattr(stamp, "nanosec", 0)
        )

    def _evidence_is_current(self, message: object) -> bool:
        return bool(
            self._dispatcher_started
            and self._procedure_active
            and self._gateway_scope is not None
            and self._gateway_scope[0]
            and self._gateway_scope[1]
            and self._scope_stamp_floor_ns is not None
            and self._stamp_ns(message) > self._scope_stamp_floor_ns
        )

    def _current_run_waiting(self) -> list[PlaybackEvent]:
        if (
            self._gateway_scope is None
            or not self._procedure_active
            or not self._gateway_scope[1]
        ):
            return []
        return filter_waiting_for_active_run(
            self._dispatcher.store.list_waiting(),
            procedure_active=self._procedure_active,
            procedure_run_id=self._gateway_scope[1],
            gateway_instance_id=self._gateway_scope[0],
        )

    def _expire_waiting(self) -> None:
        for event in self._dispatcher.store.expire_waiting(
            timeout_sec=self._waiting_timeout_sec
        ):
            self._publish_status(event)

    def _on_skill_command(self, message: SkillCommand) -> None:
        if not self._evidence_is_current(message):
            return
        self._correlator.observe_skill_command(
            command_id=message.command_id,
            request_generation=message.request_generation,
            voice_backed=message.voice_backed,
            action=message.action,
            instrument_id=message.instrument_id,
        )

    def _on_skill_status(self, message: SkillStatus) -> None:
        if not self._evidence_is_current(message):
            return
        self._correlator.observe_skill_status(
            command_id=message.command_id,
            state=message.state,
            success=message.success,
        )
        fixed_request = self._fixed_feedback.observe_skill_status(
            command_id=message.command_id,
            action=message.action,
            state=message.state,
            success=message.success,
            message=message.message,
            stamp_ns=self._stamp_ns(message),
        )
        if fixed_request is not None:
            self._submit_request(fixed_request)

    def _on_execution_trace(self, message: ExecutionTrace) -> None:
        if not self._evidence_is_current(message):
            return
        self._correlator.observe_execution_trace(
            command_id=message.command_id,
            stage=message.stage,
            terminal=message.terminal,
            evidence=message.evidence,
        )

    def _on_retraction_status(self, message: BedRobotArmGroupStatus) -> None:
        if not self._evidence_is_current(message):
            return
        self._correlator.observe_retraction_status(
            request_id=message.request_id,
            state=message.state,
            outcome=message.outcome,
            terminal=message.terminal,
            success=message.success,
        )

    def release_waiting(self, reply_id: str) -> bool:
        """Pure admission callback surface for an owning integration adapter."""

        return self._dispatcher.release_waiting(reply_id) is not None

    def destroy_node(self) -> bool:
        self._dispatcher.close()
        return super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = TTSRuntimeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
