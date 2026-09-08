"""ROS adapter for the durable Taskplanner TTS dispatcher."""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading
import time

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import (
    DurabilityPolicy,
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
)
from std_msgs.msg import String
from surgical_interop_msgs.msg import GatewayInfo
from surgical_msgs.msg import (
    BedRobotArmGroupStatus,
    ExecutionTrace,
    HumanoidReply,
    SkillCommand,
    SkillStatus,
    SurgeonRequest,
    TTSPlaybackStatus,
    TwinEvent,
    VoiceCommandIntent,
)

from .announcements import (
    DEFAULT_TOOL_ALIASES,
    EXECUTION_ANNOUNCEMENT_TOPIC,
    ExecutionAnnouncementFact,
    ExecutionAnnouncementResolver,
    default_prewarm_texts,
    execution_announcement_request,
    lifecycle_announcement_request,
    parse_execution_announcement_fact,
    parse_procedure_lifecycle_event,
    parse_tool_aliases,
    parse_successful_surgery_record_status,
    PROCEDURE_LIFECYCLE_EVENT_TOPIC,
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
    direct_reply_timing,
    filter_waiting_for_active_run,
)


def _default_project_root() -> Path:
    configured = os.environ.get("TASKPLANNER_ARPAH_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "ARPA-H"


def _default_tool_aliases() -> list[str]:
    """Merge optional Live aliases into the reviewed concise Korean defaults."""

    configured = os.environ.get("TASKPLANNER_TTS_TOOL_ALIASES", "")
    extras = [entry.strip() for entry in configured.split(",") if entry.strip()]
    return [*DEFAULT_TOOL_ALIASES, *extras]


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

        # The direct voice lane keeps command admission outside TTS.  Receive
        # the VLM's already validated reply type at its source instead of a
        # gate-produced relay topic, while preserving the existing
        # ``HumanoidReply`` message contract.
        self.declare_parameter("input_topic", "/vlm/humanoid_reply")
        self.declare_parameter("enable_vlm_free_speech", False)
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
        # Fixed phrases are materialized once with the SDK's maximum quality
        # setting.  Runtime misses continue to use the normal low-latency
        # synthesizer configured by ``steps``.
        self.declare_parameter("prewarm_steps", 100)
        self.declare_parameter("speed", 1.05)
        self.declare_parameter("silence_duration", 0.3)
        self.declare_parameter("output_target", "")
        self.declare_parameter("synthesis_timeout_sec", 120.0)
        self.declare_parameter("waiting_timeout_sec", 120.0)
        self.declare_parameter("gateway_info_timeout_sec", 3.0)
        self.declare_parameter(
            "surgery_record_status_topic",
            "/surgery/record/post_status",
        )
        # Durable reply rows are needed for short restart/replay idempotency,
        # not as an unbounded transcript archive.  These owner-local knobs
        # can be tuned without coupling TTS retention to the planner.
        self.declare_parameter("terminal_history_max_rows", 2_048)
        self.declare_parameter("terminal_history_max_age_sec", 14.0 * 24.0 * 60.0 * 60.0)
        self.declare_parameter("intra_op_threads", 4)
        self.declare_parameter("inter_op_threads", 1)
        default_tool_aliases = _default_tool_aliases()
        self.declare_parameter("tool_aliases", default_tool_aliases)
        # Keep a scenario-local concise alias cache-ready too. This remains a
        # TTS-owner-only presentation setting; it does not touch the planner.
        self.declare_parameter(
            "prewarm_texts",
            default_prewarm_texts(default_tool_aliases),
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
        self._terminal_history_max_rows = int(value("terminal_history_max_rows"))
        if self._terminal_history_max_rows < 1:
            raise ValueError("terminal_history_max_rows must be positive")
        self._terminal_history_max_age_sec = float(value("terminal_history_max_age_sec"))
        if self._terminal_history_max_age_sec <= 0:
            raise ValueError("terminal_history_max_age_sec must be positive")
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
        self._prewarm_steps = int(value("prewarm_steps"))
        if self._prewarm_steps < 1 or self._prewarm_steps > 100:
            raise ValueError("prewarm_steps must be between 1 and 100")
        self._prewarm_config = replace(config, steps=self._prewarm_steps)
        self._tts_voice_id = config.voice_id
        self._tts_output_device = config.output_device
        self._suppressed_reply_sequence = 0
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
        self._prewarm_synthesizer = None
        if self._prewarm_config.steps != config.steps:
            self._prewarm_synthesizer = SupertonicSubprocessSynthesizer(
                python_executable=str(value("synth_python")),
                model_dir=model_dir,
                voice_id=self._prewarm_config.voice_id,
                language=self._prewarm_config.language,
                steps=self._prewarm_config.steps,
                speed=self._prewarm_config.speed,
                silence_duration=self._prewarm_config.silence_duration,
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
        self._prewarm_texts = tuple(
            str(text).strip()
            for text in value("prewarm_texts")
            if str(text).strip()
        )
        if not self._prewarm_texts:
            if self._prewarm_synthesizer is not None:
                self._prewarm_synthesizer.close()
            self._dispatcher.close()
            raise ValueError("prewarm_texts must contain at least one phrase")
        self._dispatcher.configure_precomputed_cache(
            config=self._prewarm_config,
            texts=self._prewarm_texts,
        )
        # Make the owner observable before best-effort warmup.  Model/cache
        # prewarming runs in a daemon below and must not put cold startup on
        # the scenario-start critical path.
        self._status_publisher = self.create_publisher(
            TTSPlaybackStatus,
            str(value("status_topic")),
            status_qos,
        )
        for interrupted in self._dispatcher.store.fail_interrupted_playing():
            self._publish_status(interrupted)
        pruned_terminal_history = self._prune_terminal_history()
        if pruned_terminal_history:
            self.get_logger().info(
                f"Pruned {pruned_terminal_history} retained terminal TTS rows"
            )
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
        self._announcement_aliases = parse_tool_aliases(value("tool_aliases"))
        self._announcements = ExecutionAnnouncementResolver(
            tool_aliases=value("tool_aliases")
        )
        # A direct execution fact may arrive just before retained GatewayInfo
        # during a focused TTS restart.  Hold only this small, run-scoped fact
        # until the authority scope is known; do not reconstruct it from a
        # mutable queue of BT/Twin/voice evidence.
        self._pending_execution_announcements: deque = deque(maxlen=64)
        self._pending_procedure_lifecycle_events: deque = deque(maxlen=16)
        # A short compatibility path for a rolling deployment in which the
        # execution owner has not yet been restarted with the dedicated
        # announcement publisher.  It listens only for an endpoint-accepted
        # autonomous preparation and reconstructs the *same* bounded fact the
        # current bridge would publish.  Consequently both paths derive the
        # identical durable reply id and cannot double-play once every owner
        # has the current publisher.
        self._preparation_fallback_commands: OrderedDict[
            tuple[str, str], ExecutionAnnouncementFact
        ] = OrderedDict()
        self._preparation_fallback_accepted: OrderedDict[
            tuple[str, str], None
        ] = OrderedDict()
        self._prewarm_complete = False
        self._prewarm_error = ""
        self._prewarm_thread: threading.Thread | None = None
        self._last_procedure_scope: tuple[str, str] | None = None
        self._last_record_announcement_request_id = ""
        self._waiting_expiry_timer = self.create_timer(
            min(1.0, max(0.1, self._waiting_timeout_sec / 4.0)),
            self._expire_waiting,
        )
        self._terminal_history_timer = self.create_timer(
            15.0 * 60.0,
            self._prune_terminal_history,
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
        # Deterministic execution audio has one fact owner: execution emits a
        # command/run/generation admission fact after an endpoint accepts it.
        # Do not reconstruct the same sentence from BT/Twin/voice/status/trace
        # delivery order; that was the source of duplicate pickup/return TTS.
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
                String,
                EXECUTION_ANNOUNCEMENT_TOPIC,
                self._on_execution_announcement,
                50,
            ),
            self.create_subscription(
                String,
                PROCEDURE_LIFECYCLE_EVENT_TOPIC,
                self._on_procedure_lifecycle_event,
                QoSProfile(
                    history=HistoryPolicy.KEEP_LAST,
                    depth=16,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                ),
            ),
            self.create_subscription(
                SkillCommand,
                "/bt/skill_command",
                self._on_preparation_fallback_skill_command,
                50,
            ),
            self.create_subscription(
                ExecutionTrace,
                "/surgery/execution_trace",
                self._on_preparation_fallback_execution_trace,
                50,
            ),
            self.create_subscription(
                String,
                str(value("surgery_record_status_topic")),
                self._on_surgery_record_status,
                10,
            ),
        ]
        # Legacy correlation remains an opt-in observer only for experimental
        # VLM free speech.  It is deliberately absent from the normal fixed
        # command/TTS path so those streams cannot race a deterministic fact.
        self._legacy_evidence_subscriptions = []
        if bool(value("enable_vlm_free_speech")):
            self._legacy_evidence_subscriptions = [
                self.create_subscription(TwinEvent, "/twin/events", self._on_twin_event, 50),
                self.create_subscription(
                    VoiceCommandIntent,
                    "/surgery/voice/proposal",
                    self._on_voice_intent,
                    50,
                ),
                self.create_subscription(
                    SurgeonRequest,
                    "/surgeon/request",
                    self._on_surgeon_request,
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
        self._prewarm_thread = threading.Thread(
            target=self._prewarm_background,
            name="tts-prewarm",
            daemon=True,
        )
        self._prewarm_thread.start()
        self.get_logger().info(
            "TTS endpoint ready; deterministic execution announcements await active "
            "GatewayInfo: VLM free speech=%s voice=%s lang=%s steps=%d "
            "speed=%.2f output=%s prewarm=background(%d,steps=%d)"
            % (
                "enabled"
                if bool(value("enable_vlm_free_speech"))
                else "disabled",
                config.voice_id,
                config.language,
                config.steps,
                config.speed,
                config.output_device,
                len(self._prewarm_texts),
                self._prewarm_config.steps,
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
        timing = direct_reply_timing(
            timing=message.timing,
            function_call_name=message.function_call_name,
            function_arguments_json=message.function_arguments_json,
        )
        if timing != str(message.timing or "").strip():
            self.get_logger().info(
                "TTS using direct immediate timing for uncorrelated VLM reply: "
                f"reply_id={message.reply_id} function={message.function_call_name or 'none'}"
            )
        request = ReplyRequest(
            reply_id=message.reply_id,
            turn_id=message.turn_id,
            utterance_id=message.utterance_id,
            gateway_instance_id=message.gateway_instance_id,
            procedure_run_id=message.procedure_run_id,
            text=message.text,
            timing=timing,
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
        if not bool(self.get_parameter("enable_vlm_free_speech").value):
            # Keep the producer outbox bounded even while conversational audio
            # is disabled.  ``duplicate_suppressed`` is the existing terminal
            # non-playback ACK understood by the VLM outbox; the message makes
            # the policy reason explicit for operators.
            self._ack_suppressed_reply(
                request,
                message="suppressed_vlm_free_speech_disabled",
            )
            self.get_logger().info(
                "Suppressed VLM free speech by TTS policy: "
                f"reply_id={request.reply_id}"
            )
            return
        if self._announcements.is_duplicate_voice_reply(
            utterance_id=message.utterance_id,
            text=message.text,
        ):
            # The deterministic execution observer owns the spoken outcome
            # for this already-admitted tool command.  Acknowledging the VLM
            # outbox as ``duplicate_suppressed`` stops retry delivery without
            # queuing a second, semantically identical WAV.
            self._ack_execution_duplicate_reply(request)
            self.get_logger().info(
                "Suppressed VLM acknowledgement duplicated by deterministic "
                f"tool announcement: reply_id={request.reply_id}"
            )
            return
        self._submit_request(request)

    def _ack_execution_duplicate_reply(self, request: ReplyRequest) -> None:
        """Publish a terminal VLM-outbox ACK for non-played duplicate wording."""

        self._ack_suppressed_reply(
            request,
            message="suppressed_duplicate_execution_announcement",
        )

    def _ack_suppressed_reply(
        self,
        request: ReplyRequest,
        *,
        message: str,
    ) -> None:
        """Acknowledge one policy-suppressed VLM reply without playing it."""

        self._suppressed_reply_sequence += 1
        status = TTSPlaybackStatus()
        status.stamp = self.get_clock().now().to_msg()
        status.sequence = self._suppressed_reply_sequence
        status.reply_id = request.reply_id
        status.turn_id = request.turn_id
        status.utterance_id = request.utterance_id
        status.procedure_run_id = request.procedure_run_id
        status.state = "duplicate_suppressed"
        status.timing = request.timing
        status.text = request.text
        status.text_sha256 = hashlib.sha256(
            request.text.encode("utf-8")
        ).hexdigest()
        status.voice_id = self._tts_voice_id
        status.output_device = self._tts_output_device
        status.cache_key = ""
        status.synth_latency_ms = 0.0
        status.audio_duration_sec = 0.0
        status.playback_latency_ms = 0.0
        status.terminal = True
        status.success = True
        status.error_code = ""
        status.message = message
        self._status_publisher.publish(status)

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

    def _prewarm_background(self) -> None:
        """Warm fixed cache entries without delaying TTS/node readiness."""

        prewarm_synthesizer = self._prewarm_synthesizer
        try:
            results = self._dispatcher.prewarm(
                self._prewarm_texts,
                config=self._prewarm_config,
                synthesizer=prewarm_synthesizer,
            )
            self._prewarm_complete = True
            self.get_logger().info(
                "TTS background prewarm finished: "
                f"{len(results)} phrases at steps={self._prewarm_config.steps}"
            )
        except Exception as exc:  # presentation cache only; playback can synthesize
            self._prewarm_error = type(exc).__name__
            self.get_logger().warning(
                f"TTS background prewarm stopped: {self._prewarm_error}"
            )
        finally:
            if prewarm_synthesizer is not None:
                prewarm_synthesizer.close()
                self._prewarm_synthesizer = None

    def _on_procedure_lifecycle_event(self, message: String) -> None:
        """Speak the early finish cue after manager admission.

        SimulationManager owns the finish transition.  This observer only
        accepts the manager's run-scoped event, and never infers it from a
        voice proposal, raw control state, or the eventual terminal heartbeat.
        """

        event = parse_procedure_lifecycle_event(message.data)
        if event is None:
            return
        scope = getattr(self, "_gateway_scope", None)
        procedure_active = bool(getattr(self, "_procedure_active", False))
        dispatcher_started = bool(getattr(self, "_dispatcher_started", False))
        if scope is None:
            pending = getattr(self, "_pending_procedure_lifecycle_events", None)
            if pending is None:
                pending = deque(maxlen=16)
                self._pending_procedure_lifecycle_events = pending
            pending.append(event)
            return
        if (
            not procedure_active
            or not scope[0]
            or not scope[1]
            or event.procedure_run_id != scope[1]
        ):
            # A known inactive/different scope proves that this event is stale;
            # do not replay it into a later procedure run.
            return
        if not dispatcher_started:
            pending = getattr(self, "_pending_procedure_lifecycle_events", None)
            if pending is None:
                pending = deque(maxlen=16)
                self._pending_procedure_lifecycle_events = pending
            pending.append(event)
            return
        self._submit_procedure_lifecycle_event(event)

    def _flush_pending_procedure_lifecycle_events(self) -> None:
        """Release only early lifecycle events for the active run."""

        pending = getattr(self, "_pending_procedure_lifecycle_events", None)
        if pending is None:
            return
        events = list(pending)
        pending.clear()
        scope = getattr(self, "_gateway_scope", None)
        if (
            not getattr(self, "_dispatcher_started", False)
            or not getattr(self, "_procedure_active", False)
            or scope is None
            or not scope[0]
            or not scope[1]
        ):
            return
        for event in events:
            if event.procedure_run_id == scope[1]:
                self._submit_procedure_lifecycle_event(event)

    def _submit_procedure_lifecycle_event(self, event) -> None:
        """Submit one manager-owned lifecycle event through durable TTS."""

        scope = getattr(self, "_gateway_scope", None)
        if (
            scope is None
            or not getattr(self, "_dispatcher_started", False)
            or not getattr(self, "_procedure_active", False)
            or not scope[0]
            or not scope[1]
            or event.procedure_run_id != scope[1]
        ):
            return
        if event.event == "procedure_finishing":
            # The manager-owned edge is the single admission point for the
            # run-scoped TTS finish barrier.  The dispatcher drops pending
            # non-lifecycle work and rejects later tool announcements before
            # this lifecycle cue is submitted.
            begin_finishing = getattr(
                self._dispatcher,
                "begin_procedure_finishing",
                None,
            )
            if callable(begin_finishing):
                begin_finishing(scope[0], scope[1])
        request = lifecycle_announcement_request(
            event=event.event,
            gateway_instance_id=scope[0],
            procedure_run_id=scope[1],
        )
        if request is not None:
            self._submit_request(request)

    def _on_execution_announcement(self, message: String) -> None:
        """Speak exactly one execution-owned accepted-command fact."""

        fact = parse_execution_announcement_fact(message.data)
        if fact is None:
            return
        scope = getattr(self, "_gateway_scope", None)
        if (
            scope is None
            or not getattr(self, "_dispatcher_started", False)
            or not getattr(self, "_procedure_active", False)
            or not scope[0]
            or not scope[1]
        ):
            # GatewayInfo is retained, but DDS cross-topic delivery order is
            # not defined. This also covers the stopped-to-started transition:
            # an inactive retained GatewayInfo can already have installed
            # ``(gateway, \"\")`` when the first endpoint acceptance arrives.
            # Preserve only the bounded fact until an exact active scope is
            # known; the flush still requires the same procedure run.
            pending = getattr(self, "_pending_execution_announcements", None)
            if pending is None:
                pending = deque(maxlen=64)
                self._pending_execution_announcements = pending
            pending.append(fact)
            return
        if fact.procedure_run_id != scope[1]:
            return
        self._submit_execution_announcement_fact(fact)

    def _flush_pending_execution_announcements(self) -> None:
        """Release only facts that match the just-confirmed active run."""

        queue = getattr(self, "_pending_execution_announcements", None)
        if queue is None:
            return
        pending = list(queue)
        queue.clear()
        scope = getattr(self, "_gateway_scope", None)
        if (
            not getattr(self, "_dispatcher_started", False)
            or not getattr(self, "_procedure_active", False)
            or scope is None
            or not scope[0]
            or not scope[1]
        ):
            return
        for fact in pending:
            if fact.procedure_run_id != scope[1]:
                continue
            self._submit_execution_announcement_fact(fact)

    def _submit_execution_announcement_fact(
        self,
        fact: ExecutionAnnouncementFact,
    ) -> None:
        """Submit one run-scoped bridge fact through the durable TTS ledger."""

        scope = getattr(self, "_gateway_scope", None)
        if (
            scope is None
            or not getattr(self, "_dispatcher_started", False)
            or not getattr(self, "_procedure_active", False)
            or not scope[0]
            or not scope[1]
            or fact.procedure_run_id != scope[1]
        ):
            return
        if bool(
            getattr(self._dispatcher, "is_procedure_finishing", lambda *_args: False)(
                scope[0], scope[1]
            )
        ) and not fact.completion_cleanup:
            # Facts can still be in flight on DDS after the manager crossed
            # the finish edge.  Admit only execution-owned completion cleanup;
            # ordinary late tool cues remain fenced out.
            return
        request = execution_announcement_request(
            fact,
            gateway_instance_id=scope[0],
            procedure_run_id=scope[1],
            aliases=getattr(self, "_announcement_aliases", {}),
        )
        if request is not None:
            self._submit_request(request)

    @staticmethod
    def _remember_bounded(
        entries: OrderedDict,
        key: tuple[str, str],
        value: object,
    ) -> None:
        entries.pop(key, None)
        entries[key] = value
        while len(entries) > 64:
            entries.popitem(last=False)

    def _on_preparation_fallback_skill_command(self, message: SkillCommand) -> None:
        """Cache one autonomous preparation for an accepted-trace fallback.

        This is intentionally narrower than the former general correlator:
        an explicit voice command, handover, return, or recovery is never
        reconstructed here.  The execution-owned announcement remains their
        only speech source.
        """

        if not self._run_scoped_evidence_is_current(message):
            return
        if (
            str(message.action or "").strip() != "predict_tool"
            or bool(message.voice_backed)
        ):
            return
        command_id = str(message.command_id or "").strip()
        procedure_run_id = str(message.procedure_run_id or "").strip()
        if not command_id or not procedure_run_id:
            return
        key = (procedure_run_id, command_id)
        fact = ExecutionAnnouncementFact(
            command_id=command_id,
            procedure_run_id=procedure_run_id,
            route="tool_transfer",
            action="predict_tool",
            instrument_id=str(message.instrument_id or "").strip(),
            request_generation=max(0, int(message.request_generation)),
            voice_backed=False,
        )
        commands = getattr(self, "_preparation_fallback_commands", None)
        if commands is None:
            commands = OrderedDict()
            self._preparation_fallback_commands = commands
        self._remember_bounded(commands, key, fact)
        accepted = getattr(self, "_preparation_fallback_accepted", {})
        if key in accepted:
            self._submit_execution_announcement_fact(fact)

    def _on_preparation_fallback_execution_trace(
        self,
        message: ExecutionTrace,
    ) -> None:
        """Release a cached preparation only after endpoint acknowledgement."""

        if not self._run_scoped_evidence_is_current(message):
            return
        if (
            str(message.route or "").strip() != "tool_transfer"
            or str(message.transport or "").strip().lower() != "action"
            or str(message.stage or "").strip().lower() != "accepted"
            or str(message.evidence or "").strip().lower() != "goal_response"
            or not bool(message.dispatch_submitted)
        ):
            return
        command_id = str(message.command_id or "").strip()
        procedure_run_id = str(message.procedure_run_id or "").strip()
        if not command_id or not procedure_run_id:
            return
        key = (procedure_run_id, command_id)
        accepted = getattr(self, "_preparation_fallback_accepted", None)
        if accepted is None:
            accepted = OrderedDict()
            self._preparation_fallback_accepted = accepted
        self._remember_bounded(accepted, key, None)
        fact = getattr(self, "_preparation_fallback_commands", {}).get(key)
        if fact is not None:
            self._submit_execution_announcement_fact(fact)

    def _on_twin_event(self, message: TwinEvent) -> None:
        if not self._run_scoped_evidence_is_current(message):
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
        for request in self._announcements.observe_voice_intent(
            utterance_id=str(detail.get("utterance_id", "")),
            request_generation=generation,
            accepted=detail.get("accepted") is True,
        ):
            self._submit_request(request)
        self._correlator.observe_voice_intent(
            utterance_id=str(detail.get("utterance_id", "")),
            request_generation=generation,
            accepted=detail.get("accepted") is True,
        )

    def _on_voice_intent(self, message: VoiceCommandIntent) -> None:
        """Observe the resolver-owned typed voice proposal for TTS dedupe."""

        if not self._evidence_is_current(message):
            return
        for request in self._announcements.observe_typed_voice_tool_intent(
            utterance_id=message.utterance_id,
            intent=message.intent,
            tool_id=message.tool_id,
            accepted=(
                message.disposition == "propose"
                and not bool(message.requires_confirmation)
            ),
        ):
            self._submit_request(request)

    def _on_surgeon_request(self, message: SurgeonRequest) -> None:
        """Keep a manual recovery silent without altering its Action path."""

        if not self._evidence_is_current(message):
            return
        self._announcements.observe_explicit_retrieval_request(
            tool_id=message.requested_tool,
            accepted=(
                message.event_type == "return_tool"
                and bool(message.override)
                and bool(message.ready_for_retrieval)
            ),
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
        previous_scope = self._gateway_scope
        previous_active = self._procedure_active
        previous_last_scope = getattr(self, "_last_procedure_scope", None)
        had_gateway_info = previous_scope is not None
        procedure_active = bool(
            message.procedure_active and gateway_instance_id and procedure_run_id
        )
        scope = (
            gateway_instance_id,
            procedure_run_id if procedure_active else "",
        )
        scope_changed = (
            scope != previous_scope
            or procedure_active != previous_active
        )
        if procedure_active:
            self._last_procedure_scope = scope
        elif (
            previous_last_scope is not None
            and gateway_instance_id
            and gateway_instance_id != previous_last_scope[0]
        ):
            # A new gateway epoch cannot authenticate a late record outcome
            # from the retired process.
            self._last_procedure_scope = None
        self._gateway_scope = scope
        self._procedure_active = procedure_active
        self._announcements.observe_gateway_info(
            gateway_instance_id=scope[0],
            procedure_run_id=scope[1],
            procedure_active=procedure_active,
        )
        if scope_changed:
            self._scope_stamp_floor_ns = self._stamp_ns(message)
            self._correlator.reset()
            playback_scope = (
                scope
                if self._procedure_active
                else self._last_procedure_scope or ("", "")
            )
            if self._dispatcher.active_scope != playback_scope:
                self._dispatcher.set_active_procedure_scope(*playback_scope)
        if self._procedure_active and not self._dispatcher_started:
            # Cleanup must precede recovery, otherwise an old queued reply can
            # be picked up before the authoritative procedure scope is known.
            self._dispatcher.start()
            self._dispatcher_started = True
            self.get_logger().info(
                f"TTS playback enabled for procedure_run_id={scope[1]}"
            )
        if (
            had_gateway_info
            and not previous_active
            and self._procedure_active
            and scope != previous_last_scope
        ):
            request = lifecycle_announcement_request(
                event="procedure_start",
                gateway_instance_id=scope[0],
                procedure_run_id=scope[1],
            )
            if request is not None:
                self._submit_request(request)
        elif (
            previous_active
            and not self._procedure_active
            and previous_scope not in {None, ("", "")}
            and gateway_instance_id == previous_scope[0]
        ):
            request = lifecycle_announcement_request(
                event="procedure_stop",
                gateway_instance_id=previous_scope[0],
                procedure_run_id=previous_scope[1],
            )
            if request is not None:
                self._submit_request(request)
        self._flush_pending_procedure_lifecycle_events()
        # The first controller admission can precede retained active
        # GatewayInfo.  Keep that fact, but queue it only after the lifecycle
        # start announcement for this scope: presentation must not say that a
        # tool is being prepared before it says the procedure has begun.
        # Scope filtering in the flush remains unchanged, so a stale fact can
        # never cross into this new procedure.
        self._flush_pending_execution_announcements()
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
        pending = getattr(self, "_pending_execution_announcements", None)
        if pending is not None:
            pending.clear()
        pending_lifecycle = getattr(
            self, "_pending_procedure_lifecycle_events", None
        )
        if pending_lifecycle is not None:
            pending_lifecycle.clear()
        self._correlator.reset()
        self._announcements.observe_gateway_info(
            gateway_instance_id="",
            procedure_run_id="",
            procedure_active=False,
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
        if stamp is None:
            stamp = getattr(getattr(message, "header", None), "stamp", None)
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

    def _run_scoped_evidence_is_current(self, message: object) -> bool:
        return bool(
            self._evidence_is_current(message)
            and self._gateway_scope is not None
            and str(getattr(message, "procedure_run_id", "")).strip()
            == self._gateway_scope[1]
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

    def _prune_terminal_history(self) -> int:
        """Bound old terminal TTS rows without touching pending playback."""

        return self._dispatcher.store.prune_terminal_history(
            keep_recent=self._terminal_history_max_rows,
            max_age_sec=self._terminal_history_max_age_sec,
        )

    def _on_skill_command(self, message: SkillCommand) -> None:
        if not self._run_scoped_evidence_is_current(message):
            return
        self._correlator.observe_skill_command(
            command_id=message.command_id,
            request_generation=message.request_generation,
            voice_backed=message.voice_backed,
            action=message.action,
            instrument_id=message.instrument_id,
        )
        for request in self._announcements.observe_skill_command(
            command_id=message.command_id,
            action=message.action,
            instrument_id=message.instrument_id,
            request_generation=message.request_generation,
            voice_backed=message.voice_backed,
        ):
            self._submit_request(request)

    def _on_skill_status(self, message: SkillStatus) -> None:
        if not self._run_scoped_evidence_is_current(message):
            return
        self._correlator.observe_skill_status(
            command_id=message.command_id,
            state=message.state,
            success=message.success,
        )

    def _on_execution_trace(self, message: ExecutionTrace) -> None:
        if not self._run_scoped_evidence_is_current(message):
            return
        self._correlator.observe_execution_trace(
            command_id=message.command_id,
            stage=message.stage,
            terminal=message.terminal,
            evidence=message.evidence,
        )
        for request in self._announcements.observe_execution_trace(
            command_id=message.command_id,
            route=message.route,
            transport=message.transport,
            stage=message.stage,
            evidence=message.evidence,
            dispatch_submitted=message.dispatch_submitted,
            retraction_command=getattr(message, "retraction_command", 0),
            retraction_target_side=getattr(message, "retraction_target_side", 0),
            retraction_distance_m=getattr(message, "retraction_distance_m", 0.0),
        ):
            self._submit_request(request)

    def _on_retraction_status(self, message: BedRobotArmGroupStatus) -> None:
        if not self._run_scoped_evidence_is_current(message):
            return
        self._correlator.observe_retraction_status(
            request_id=message.request_id,
            state=message.state,
            outcome=message.outcome,
            terminal=message.terminal,
            success=message.success,
        )

    def _on_surgery_record_status(self, message: String) -> None:
        """Speak only a confirmed POST success for the just-finished run."""

        completion = parse_successful_surgery_record_status(message.data)
        scope = self._last_procedure_scope
        if (
            completion is None
            or self._procedure_active
            or not self._dispatcher_started
            or scope is None
            or completion.procedure_run_id != scope[1]
            or self._dispatcher.active_scope != scope
            or completion.request_id == self._last_record_announcement_request_id
        ):
            return
        request = lifecycle_announcement_request(
            event="surgery_record_completed",
            gateway_instance_id=scope[0],
            procedure_run_id=scope[1],
            correlation_id=completion.request_id,
        )
        if request is None:
            return
        self._last_record_announcement_request_id = completion.request_id
        self._submit_request(request)

    def release_waiting(self, reply_id: str) -> bool:
        """Pure admission callback surface for an owning integration adapter."""

        return self._dispatcher.release_waiting(reply_id) is not None

    def destroy_node(self) -> bool:
        prewarm_thread = getattr(self, "_prewarm_thread", None)
        if prewarm_thread is not None and prewarm_thread.is_alive():
            # The worker is daemonized and checks dispatcher startup between
            # phrases.  Never turn shutdown/restart into a model-warmup wait.
            prewarm_thread.join(timeout=0.05)
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
