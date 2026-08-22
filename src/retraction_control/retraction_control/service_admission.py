"""Pure admission controller for ExecuteRetractionCommand.

Validation and the durable idempotency fence run synchronously.  Physical work
is only placed on :class:`CommandWorker`; the returned reply never describes a
motion outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable, Iterable

from .command_models import (
    Command,
    CommandRequest,
    CommandValidationError,
    ResultCode,
    RetractionControlError,
)
from .runtime import (
    CommandLedger,
    CommandWorker,
    LedgerDecision,
    RuntimeCommand,
    SubmitDecision,
    canonical_request_fingerprint,
)


@dataclass(frozen=True, slots=True)
class AdmissionReply:
    request_accepted: bool
    result_code: ResultCode
    command_id: str
    message: str
    duplicate: bool = False


class AdmissionController:
    def __init__(
        self,
        ledger: CommandLedger,
        worker: CommandWorker,
        *,
        allowed_source_ids: Iterable[str],
        check_state: Callable[[CommandRequest], None],
    ) -> None:
        allowed = frozenset(str(item).strip() for item in allowed_source_ids)
        if not allowed or "" in allowed:
            raise ValueError("allowed_source_ids must contain non-empty IDs")
        self._ledger = ledger
        self._worker = worker
        self._allowed_source_ids = allowed
        self._check_state = check_state
        self._lock = threading.RLock()

    def admit(self, wire_request: object) -> AdmissionReply:
        # ROS may invoke Service callbacks concurrently.  Serialize the
        # reserve/state-check/queue sequence so duplicate retries always see a
        # fully recorded admission response.
        with self._lock:
            return self._admit_locked(wire_request)

    def _admit_locked(self, wire_request: object) -> AdmissionReply:
        try:
            request = CommandRequest.from_ros(wire_request)
        except CommandValidationError as exc:
            command_id = str(getattr(wire_request, "command_id", "")).strip()
            return AdmissionReply(
                False,
                exc.result_code,
                command_id,
                exc.code.value,
            )

        if request.source_id not in self._allowed_source_ids:
            return AdmissionReply(
                False,
                ResultCode.REJECTED,
                request.command_id,
                "source_id_not_allowed",
            )

        fingerprint_fields = request.as_dict()
        fingerprint_fields.pop("command_id")
        reservation = self._ledger.reserve(
            request.command_id,
            canonical_request_fingerprint(fingerprint_fields),
        )
        if reservation.decision is LedgerDecision.CONFLICT:
            return AdmissionReply(
                False,
                ResultCode.INVALID_PARAMETER,
                request.command_id,
                reservation.message,
            )
        if reservation.decision is LedgerDecision.DUPLICATE:
            record = self._ledger.get(request.command_id)
            if record is None:  # guarded by the same durable ledger
                return AdmissionReply(
                    False,
                    ResultCode.ERROR,
                    request.command_id,
                    "idempotence_record_unavailable",
                    duplicate=True,
                )
            return AdmissionReply(
                record.admission_accepted,
                ResultCode(record.admission_result_code),
                request.command_id,
                f"duplicate_no_reexecution:{record.admission_message or record.stage}",
                duplicate=True,
            )

        # Do not project future physical states into admission.  At most one
        # ordinary command may be outstanding; STOP alone has a priority path
        # alongside an active/queued command.
        if request.command is not Command.STOP_RETRACTION:
            worker_fault = str(getattr(self._worker, "fatal_error", "") or "")
            if worker_fault:
                self._ledger.record_admission(
                    request.command_id,
                    accepted=False,
                    result_code=int(ResultCode.ERROR),
                    message="worker_fail_closed",
                    stage="rejected",
                )
                return AdmissionReply(
                    False,
                    ResultCode.ERROR,
                    request.command_id,
                    "worker_fail_closed",
                )
            active_id = str(getattr(self._worker, "active_command_id", "") or "")
            pending_count = int(getattr(self._worker, "pending_count", 0) or 0)
            if active_id or pending_count:
                self._ledger.record_admission(
                    request.command_id,
                    accepted=False,
                    result_code=int(ResultCode.REJECTED),
                    message="controller_busy",
                    stage="rejected",
                )
                return AdmissionReply(
                    False,
                    ResultCode.REJECTED,
                    request.command_id,
                    "controller_busy",
                )

        try:
            self._check_state(request)
        except RetractionControlError as exc:
            self._ledger.record_admission(
                request.command_id,
                accepted=False,
                result_code=int(exc.result_code),
                message=exc.code.value,
                stage="rejected",
            )
            return AdmissionReply(
                False,
                exc.result_code,
                request.command_id,
                exc.code.value,
            )

        runtime_command = RuntimeCommand(
            command_id=request.command_id,
            command=int(request.command),
            payload=request,
            is_stop=request.command is Command.STOP_RETRACTION,
        )
        # Persist the stable Service reply before waking the worker.  Otherwise
        # a very fast fake executor could reach a terminal stage and then have
        # that physical result overwritten by a late admission write.
        self._ledger.record_admission(
            request.command_id,
            accepted=True,
            result_code=int(ResultCode.ACCEPTED),
            message="request_accepted_for_execution",
            stage="admitted",
        )
        submit = self._worker.submit(runtime_command)
        if submit is not SubmitDecision.QUEUED:
            message = {
                SubmitDecision.BUSY: "command_queue_busy",
                SubmitDecision.STOP_ALREADY_PENDING: "stop_already_pending",
                SubmitDecision.SHUTTING_DOWN: "controller_shutting_down",
                SubmitDecision.PERSISTENCE_ERROR: "command_queue_persistence_failed",
            }[submit]
            self._ledger.record_admission(
                request.command_id,
                accepted=False,
                result_code=int(
                    ResultCode.ERROR
                    if submit is SubmitDecision.PERSISTENCE_ERROR
                    else ResultCode.REJECTED
                ),
                message=message,
                stage="rejected",
            )
            return AdmissionReply(
                False,
                (
                    ResultCode.ERROR
                    if submit is SubmitDecision.PERSISTENCE_ERROR
                    else ResultCode.REJECTED
                ),
                request.command_id,
                message,
            )

        return AdmissionReply(
            True,
            ResultCode.ACCEPTED,
            request.command_id,
            "request_accepted_for_execution",
        )


__all__ = ["AdmissionController", "AdmissionReply"]
