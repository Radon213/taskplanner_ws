"""Pure, fail-closed playback-evidence joins for validated humanoid replies.

This module consumes facts that have already been published by Taskplanner. It
never calls an Action or Service and therefore cannot cause robot motion.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import threading
from typing import Callable, Iterable, Mapping

from .core import PlaybackEvent


HANDOVER_ACTIONS = frozenset({"pick_up_and_handover", "direct_handover"})
DIRECT_HANDOVER_FUNCTION = "request_tool_handover"
DIRECT_RETRACTION_FUNCTION = "adjust_retraction"


@dataclass(frozen=True)
class PresentationEvidence:
    """Non-executable reply timing rule for one already-known evidence type.

    This intentionally is not a command registry.  It has no endpoint, schema,
    admission, or dispatch information: it only says which published evidence
    a pending spoken reply may wait for.
    """

    timings: frozenset[str]
    matcher_name: str


_PRESENTATION_EVIDENCE: Mapping[str, PresentationEvidence] = {
    DIRECT_HANDOVER_FUNCTION: PresentationEvidence(
        timings=frozenset({"on_function_accepted", "on_function_completed"}),
        matcher_name="handover",
    ),
    DIRECT_RETRACTION_FUNCTION: PresentationEvidence(
        timings=frozenset({"on_function_accepted"}),
        matcher_name="retraction",
    ),
}


def presentation_evidence_for(
    function_call_name: str,
) -> PresentationEvidence | None:
    """Return a local display rule, never a command admission decision."""

    return _PRESENTATION_EVIDENCE.get(str(function_call_name or "").strip())


ReceiptMatcher = Callable[[PlaybackEvent, dict[str, object]], bool]


@dataclass(frozen=True)
class SkillCommandFact:
    command_id: str
    request_generation: int
    action: str
    instrument_id: str


@dataclass(frozen=True)
class SkillStatusFact:
    command_id: str
    state: str
    success: bool


@dataclass(frozen=True)
class ExecutionTraceFact:
    command_id: str
    stage: str
    terminal: bool
    evidence: str


@dataclass(frozen=True)
class RetractionStatusFact:
    request_id: str
    state: str
    outcome: str
    terminal: bool
    success: bool


def parse_function_arguments(value: str) -> dict[str, object] | None:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def direct_reply_timing(
    *,
    timing: str,
    function_call_name: str,
    function_arguments_json: str,
) -> str:
    """Choose a playable timing for a raw VLM reply on the direct voice lane.

    The retired function-admission gate used to create one request ID shared by
    a VLM function and the command it admitted.  The deterministic voice lane
    deliberately has no such join.  Tool handover still has a useful
    independent join through the admitted ASR utterance, typed intent, and
    action evidence.  Other function-timed replies therefore become immediate
    playback instead of waiting for a request ID that no direct command owns.

    Invalid timing values are intentionally returned unchanged: the playback
    boundary remains responsible for rejecting malformed message data rather
    than silently treating it as an immediate reply.
    """

    normalized_timing = str(timing or "").strip()
    if normalized_timing not in {
        "on_function_accepted",
        "on_function_completed",
    }:
        return normalized_timing
    evidence = presentation_evidence_for(function_call_name)
    if evidence is None or normalized_timing not in evidence.timings:
        return "immediate"
    arguments = parse_function_arguments(function_arguments_json)
    if not isinstance(arguments, dict):
        return "immediate"
    if str(function_call_name or "").strip() == DIRECT_HANDOVER_FUNCTION:
        if not str(arguments.get("tool_id", "")).strip():
            return "immediate"
        return normalized_timing
    # Direct typed retraction derives its id from the accepted ASR utterance,
    # not from a VLM presentation hint. Do not leave a raw reply waiting for a
    # request it cannot prove belongs to it.
    return "immediate"


def _base_tool_id(value: str) -> str:
    return value.strip().split("#", 1)[0]


def filter_waiting_for_active_run(
    jobs: Iterable[PlaybackEvent],
    *,
    procedure_active: bool,
    procedure_run_id: str,
    gateway_instance_id: str = "",
) -> list[PlaybackEvent]:
    """Return waiting work in the requested run and optional gateway epoch."""

    active_run = procedure_run_id.strip()
    if not procedure_active or not active_run:
        return []
    active_gateway = gateway_instance_id.strip()
    return [
        job
        for job in jobs
        if job.procedure_run_id == active_run
        and (
            not active_gateway
            or job.gateway_instance_id == active_gateway
        )
    ]


class AuthoritativeTimingCorrelator:
    """Release waiting replies only after independent execution evidence joins."""

    def __init__(
        self,
        *,
        waiting_provider: Callable[[], Iterable[PlaybackEvent]],
        release_waiting: Callable[[str], object],
        max_facts: int = 1024,
    ) -> None:
        self._waiting_provider = waiting_provider
        self._release_waiting = release_waiting
        self._max_facts = max(32, int(max_facts))
        self._lock = threading.RLock()
        self._generation_by_utterance: OrderedDict[str, int] = OrderedDict()
        self._ambiguous_generations: set[int] = set()
        self._commands_by_id: OrderedDict[str, SkillCommandFact] = OrderedDict()
        self._command_ids_by_generation: dict[int, list[str]] = {}
        self._statuses: OrderedDict[tuple[str, str], SkillStatusFact] = OrderedDict()
        self._traces: OrderedDict[tuple[str, str], ExecutionTraceFact] = OrderedDict()
        self._retraction_statuses: OrderedDict[str, RetractionStatusFact] = OrderedDict()
        # A deliberately tiny, local presentation map selects receipt
        # evidence. It is separate from command routing and cannot make a
        # VLM-proposed name executable.
        self._receipt_matchers: Mapping[str, ReceiptMatcher] = {
            "handover": self._handover_ready,
            "retraction": self._retraction_ready,
        }

    def reset(self) -> None:
        with self._lock:
            self._generation_by_utterance.clear()
            self._ambiguous_generations.clear()
            self._commands_by_id.clear()
            self._command_ids_by_generation.clear()
            self._statuses.clear()
            self._traces.clear()
            self._retraction_statuses.clear()

    def _put_bounded(self, mapping: OrderedDict, key: object, value: object) -> None:
        mapping.pop(key, None)
        mapping[key] = value
        while len(mapping) > self._max_facts:
            mapping.popitem(last=False)

    def observe_voice_intent(
        self,
        *,
        utterance_id: str,
        request_generation: int,
        accepted: bool,
    ) -> list[str]:
        utterance_id = utterance_id.strip()
        if isinstance(request_generation, bool):
            return []
        generation = int(request_generation)
        if not accepted or not utterance_id or generation <= 0:
            return []
        with self._lock:
            if self._generation_by_utterance.get(utterance_id) == generation:
                return self._evaluate_locked()
            owners = [
                existing_utterance
                for existing_utterance, existing_generation in self._generation_by_utterance.items()
                if existing_generation == generation
            ]
            if owners:
                # A generation must identify exactly one spoken turn in a run.
                # Preserve neither candidate when the producer violates that
                # invariant; the gateway-run reset handles legitimate reuse.
                for owner in owners:
                    self._generation_by_utterance.pop(owner, None)
                self._ambiguous_generations.add(generation)
                return []
            if generation in self._ambiguous_generations:
                return []
            self._put_bounded(
                self._generation_by_utterance, utterance_id, generation
            )
            return self._evaluate_locked()

    def observe_skill_command(
        self,
        *,
        command_id: str,
        request_generation: int,
        voice_backed: bool,
        action: str,
        instrument_id: str,
    ) -> list[str]:
        command_id = command_id.strip()
        action = action.strip()
        generation = int(request_generation)
        # Preparation, unused-preposition return and retrieval are deliberately
        # excluded. Only the actual handover leg can admit handover wording.
        if (
            not command_id
            or not voice_backed
            or generation <= 0
            or action not in HANDOVER_ACTIONS
        ):
            return []
        fact = SkillCommandFact(
            command_id=command_id,
            request_generation=generation,
            action=action,
            instrument_id=instrument_id.strip(),
        )
        with self._lock:
            self._put_bounded(self._commands_by_id, command_id, fact)
            command_ids = self._command_ids_by_generation.setdefault(generation, [])
            if command_id not in command_ids:
                command_ids.append(command_id)
            return self._evaluate_locked()

    def observe_skill_status(
        self, *, command_id: str, state: str, success: bool
    ) -> list[str]:
        command_id = command_id.strip()
        if not command_id:
            return []
        with self._lock:
            normalized_state = state.strip()
            self._put_bounded(
                self._statuses,
                (command_id, normalized_state),
                SkillStatusFact(command_id, normalized_state, bool(success)),
            )
            return self._evaluate_locked()

    def observe_execution_trace(
        self,
        *,
        command_id: str,
        stage: str,
        terminal: bool,
        evidence: str,
    ) -> list[str]:
        command_id = command_id.strip()
        if not command_id:
            return []
        with self._lock:
            normalized_stage = stage.strip()
            self._put_bounded(
                self._traces,
                (command_id, normalized_stage),
                ExecutionTraceFact(
                    command_id,
                    normalized_stage,
                    bool(terminal),
                    evidence.strip(),
                ),
            )
            return self._evaluate_locked()

    def observe_retraction_status(
        self,
        *,
        request_id: str,
        state: str,
        outcome: str,
        terminal: bool,
        success: bool,
    ) -> list[str]:
        request_id = request_id.strip()
        if not request_id:
            return []
        with self._lock:
            self._put_bounded(
                self._retraction_statuses,
                request_id,
                RetractionStatusFact(
                    request_id=request_id,
                    state=state.strip(),
                    outcome=outcome.strip(),
                    terminal=bool(terminal),
                    success=bool(success),
                ),
            )
            return self._evaluate_locked()

    def evaluate(self) -> list[str]:
        with self._lock:
            return self._evaluate_locked()

    def _handover_ready(self, job: PlaybackEvent, arguments: dict[str, object]) -> bool:
        generation = self._generation_by_utterance.get(job.utterance_id)
        if generation is None:
            return False
        requested_tool = _base_tool_id(str(arguments.get("tool_id", "")))
        for command_id in self._command_ids_by_generation.get(generation, ()):
            command = self._commands_by_id.get(command_id)
            if command is None or command.action not in HANDOVER_ACTIONS:
                continue
            if requested_tool and _base_tool_id(command.instrument_id) != requested_tool:
                continue
            desired_stage = (
                "accepted"
                if job.timing == "on_function_accepted"
                else "completed"
            )
            status = self._statuses.get((command_id, desired_stage))
            trace = self._traces.get((command_id, desired_stage))
            if status is None or trace is None:
                continue
            if job.timing == "on_function_accepted":
                if (
                    status.state == "accepted"
                    and status.success
                    and trace.stage == "accepted"
                    and not trace.terminal
                    and trace.evidence == "goal_response"
                ):
                    return True
            elif job.timing == "on_function_completed":
                if (
                    status.state == "completed"
                    and status.success
                    and trace.stage == "completed"
                    and trace.terminal
                    and trace.evidence == "controller_result"
                ):
                    return True
        return False

    def _retraction_ready(
        self, job: PlaybackEvent, arguments: dict[str, object]
    ) -> bool:
        # The Service contract proves admission only; no current controller
        # contract proves physical completion for this request.
        if job.timing != "on_function_accepted":
            return False
        request_id = str(job.function_request_id or "").strip()
        if not request_id:
            return False
        status = self._retraction_statuses.get(request_id)
        return bool(
            status is not None
            and status.request_id == request_id
            and status.state == "accepted"
            and status.outcome == "accepted"
            and status.terminal
            and status.success
        )

    def _evaluate_locked(self) -> list[str]:
        released: list[str] = []
        for job in list(self._waiting_provider()):
            arguments = parse_function_arguments(job.function_arguments_json)
            if arguments is None:
                continue
            evidence = presentation_evidence_for(job.function_call_name)
            if (
                evidence is None
                or job.timing not in evidence.timings
            ):
                continue
            matcher = self._receipt_matchers.get(evidence.matcher_name)
            ready = bool(matcher is not None and matcher(job, arguments))
            if ready and self._release_waiting(job.reply_id) is not None:
                released.append(job.reply_id)
        return released
