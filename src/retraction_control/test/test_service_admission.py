from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

from retraction_control.command_models import (
    ErrorCode,
    ResultCode,
    StateTransitionError,
)
from retraction_control.runtime import (
    CommandLedger,
    CommandWorker,
    ExecutionReport,
    SubmitDecision,
)
from retraction_control.service_admission import AdmissionController


class StubWorker:
    def __init__(self, decision=SubmitDecision.QUEUED):
        self.decision = decision
        self.commands = []
        self.active_command_id = ""
        self.pending_count = 0

    def submit(self, command):
        self.commands.append(command)
        return self.decision


def _wire(**overrides):
    values = {
        "protocol_version": 1,
        "source_id": "taskplanner",
        "command_id": "cmd-1",
        "command": 1,
        "target_side": 0,
        "distance_m": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _controller(tmp_path, worker, check_state=lambda _request: None):
    ledger = CommandLedger((tmp_path / "ledger.sqlite3").resolve())
    controller = AdmissionController(
        ledger,
        worker,
        allowed_source_ids=("taskplanner",),
        check_state=check_state,
    )
    return ledger, controller


def test_service_reply_is_admission_only_and_duplicate_does_not_requeue(tmp_path):
    worker = StubWorker()
    ledger, controller = _controller(tmp_path, worker)

    first = controller.admit(_wire())
    duplicate = controller.admit(_wire())

    assert first.request_accepted
    assert first.result_code is ResultCode.ACCEPTED
    assert duplicate.request_accepted
    assert duplicate.duplicate
    assert duplicate.message.startswith("duplicate_no_reexecution:")
    assert len(worker.commands) == 1
    ledger.close()


def test_same_id_with_changed_payload_is_rejected_as_conflict(tmp_path):
    worker = StubWorker()
    ledger, controller = _controller(tmp_path, worker)
    assert controller.admit(_wire()).request_accepted

    conflict = controller.admit(_wire(command=2))

    assert not conflict.request_accepted
    assert conflict.result_code is ResultCode.INVALID_PARAMETER
    assert conflict.message == "command_id_reused_with_different_payload"
    assert len(worker.commands) == 1
    ledger.close()


def test_state_rejection_is_cached_and_does_not_enter_worker(tmp_path):
    worker = StubWorker()

    def reject(_request):
        raise StateTransitionError(
            ErrorCode.COMMAND_NOT_ALLOWED,
            "not allowed in current state",
        )

    ledger, controller = _controller(tmp_path, worker, reject)
    first = controller.admit(_wire())
    duplicate = controller.admit(_wire())

    assert not first.request_accepted
    assert first.result_code is ResultCode.REJECTED
    assert not duplicate.request_accepted
    assert duplicate.duplicate
    assert worker.commands == []
    ledger.close()


def test_busy_and_unapproved_source_fail_closed(tmp_path):
    worker = StubWorker(SubmitDecision.BUSY)
    ledger, controller = _controller(tmp_path, worker)
    busy = controller.admit(_wire())
    wrong_source = controller.admit(
        _wire(command_id="cmd-2", source_id="unapproved")
    )

    assert not busy.request_accepted
    assert busy.message == "command_queue_busy"
    assert not wrong_source.request_accepted
    assert wrong_source.message == "source_id_not_allowed"
    ledger.close()


def test_wire_validation_maps_invalid_command_to_public_result(tmp_path):
    worker = StubWorker()
    ledger, controller = _controller(tmp_path, worker)
    result = controller.admit(_wire(command=99))

    assert not result.request_accepted
    assert result.result_code is ResultCode.INVALID_COMMAND
    assert worker.commands == []
    ledger.close()


def test_second_normal_command_is_rejected_while_worker_has_pending_work(tmp_path):
    worker = StubWorker()
    worker.pending_count = 1
    ledger, controller = _controller(tmp_path, worker)
    result = controller.admit(_wire())

    assert not result.request_accepted
    assert result.message == "controller_busy"
    assert worker.commands == []
    ledger.close()


def test_fast_physical_result_cannot_be_overwritten_by_admission_write(tmp_path):
    ledger = CommandLedger((tmp_path / "ledger.sqlite3").resolve())
    worker = CommandWorker(
        lambda _command, _stop: ExecutionReport(True, "fake_completed"),
        ledger,
    )
    controller = AdmissionController(
        ledger,
        worker,
        allowed_source_ids=("taskplanner",),
        check_state=lambda _request: None,
    )
    worker.start()
    assert controller.admit(_wire()).request_accepted

    deadline = time.monotonic() + 2.0
    while ledger.get("cmd-1").stage != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.005)
    record = ledger.get("cmd-1")
    assert record.admission_accepted
    assert record.admission_result_code == int(ResultCode.ACCEPTED)
    assert record.result_code == "fake_completed"
    assert controller.admit(_wire()).request_accepted
    assert worker.shutdown()
    ledger.close()


def test_concurrent_duplicate_admission_queues_exactly_once(tmp_path):
    worker = StubWorker()
    ledger, controller = _controller(tmp_path, worker)
    with ThreadPoolExecutor(max_workers=16) as pool:
        replies = tuple(pool.map(lambda _index: controller.admit(_wire()), range(64)))

    assert all(reply.request_accepted for reply in replies)
    assert sum(not reply.duplicate for reply in replies) == 1
    assert len(worker.commands) == 1
    ledger.close()


def test_worker_persistence_fault_is_reported_as_admission_error(tmp_path):
    class FaultedWorker(StubWorker):
        fatal_error = "terminal_persistence_failed"

    worker = FaultedWorker()
    ledger, controller = _controller(tmp_path, worker)
    reply = controller.admit(_wire())
    assert not reply.request_accepted
    assert reply.result_code is ResultCode.ERROR
    assert reply.message == "worker_fail_closed"
    assert worker.commands == []
    ledger.close()
