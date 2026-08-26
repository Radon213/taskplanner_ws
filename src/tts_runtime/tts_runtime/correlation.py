"""Pure, fail-closed joins for admission-gated humanoid replies.

This module consumes facts that have already been published by Taskplanner. It
never calls an Action or Service and therefore cannot cause robot motion.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import threading
from typing import Callable, Iterable

from .core import PlaybackEvent


HANDOVER_ACTIONS = frozenset({"pick_up_and_handover", "direct_handover"})


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
            ready = False
            if job.function_call_name == "request_tool_handover":
                ready = self._handover_ready(job, arguments)
            elif job.function_call_name == "adjust_retraction":
                ready = self._retraction_ready(job, arguments)
            elif job.function_call_name == "request_tool_retrieval":
                # No typed voice-to-execution path currently exists.
                ready = False
            if ready and self._release_waiting(job.reply_id) is not None:
                released.append(job.reply_id)
        return released
