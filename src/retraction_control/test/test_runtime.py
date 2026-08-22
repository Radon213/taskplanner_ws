from __future__ import annotations

import threading
import time
import sqlite3

import pytest

from retraction_control.runtime import (
    CommandLedger,
    CommandWorker,
    ExecutionReport,
    LedgerDecision,
    LedgerError,
    RuntimeCommand,
    SubmitDecision,
    canonical_request_fingerprint,
)


def _fingerprint(command: int = 1) -> str:
    return canonical_request_fingerprint(
        {
            "protocol_version": 1,
            "source_id": "taskplanner",
            "command": command,
            "target_side": 0,
            "distance_m": 0.0,
        }
    )


def test_ledger_is_durable_and_distinguishes_duplicate_from_conflict(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with CommandLedger(path.resolve()) as ledger:
        first = ledger.reserve("cmd-1", _fingerprint())
        assert first.decision is LedgerDecision.NEW
        ledger.record_admission(
            "cmd-1",
            accepted=True,
            result_code=0,
            message="request_accepted_for_execution",
            stage="queued",
        )

    with CommandLedger(path.resolve()) as ledger:
        duplicate = ledger.reserve("cmd-1", _fingerprint())
        conflict = ledger.reserve("cmd-1", _fingerprint(command=2))

    assert duplicate.decision is LedgerDecision.DUPLICATE
    assert duplicate.stage == "queued"
    assert conflict.decision is LedgerDecision.CONFLICT
    assert conflict.message == "command_id_reused_with_different_payload"


def test_restart_marks_uncertain_execution_interrupted_without_reexecution(tmp_path):
    path = (tmp_path / "ledger.sqlite3").resolve()
    with CommandLedger(path) as ledger:
        ledger.reserve("queued", _fingerprint())
        ledger.update("queued", "queued")
        ledger.reserve("running", _fingerprint(command=2))
        ledger.update("running", "running")

    with CommandLedger(path) as ledger:
        assert set(ledger.mark_interrupted()) == {"queued", "running"}
        assert ledger.get("queued").stage == "interrupted"
        assert ledger.reserve("queued", _fingerprint()).decision is LedgerDecision.DUPLICATE


def test_ledger_rejects_relative_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        CommandLedger("ledger.sqlite3")


def test_stop_is_queued_ahead_of_pending_work_and_signals_active_executor(tmp_path):
    ledger = CommandLedger((tmp_path / "ledger.sqlite3").resolve())
    started = threading.Event()
    release = threading.Event()
    traces: list[tuple[str, bool]] = []

    def execute(command, stop_requested):
        traces.append((command.command_id, stop_requested.is_set()))
        if command.command_id == "active":
            started.set()
            assert release.wait(2.0)
            return ExecutionReport(False, "stopped", canceled=stop_requested.is_set())
        return ExecutionReport(True, "ok")

    for command_id, command in (("active", 1), ("normal", 2), ("stop", 6)):
        ledger.reserve(command_id, _fingerprint(command))

    worker = CommandWorker(execute, ledger, max_pending=3)
    worker.start()
    assert worker.submit(RuntimeCommand("active", 1, object())) is SubmitDecision.QUEUED
    assert started.wait(2.0)
    assert worker.submit(RuntimeCommand("normal", 2, object())) is SubmitDecision.QUEUED
    assert (
        worker.submit(RuntimeCommand("stop", 6, object(), is_stop=True))
        is SubmitDecision.QUEUED
    )
    assert worker.stop_requested
    release.set()

    deadline = time.monotonic() + 2.0
    while ledger.get("normal").stage not in {"completed", "failed"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert worker.shutdown()
    ledger.close()

    assert [entry[0] for entry in traces] == ["active", "stop", "normal"]
    assert ledger.path.is_absolute()


def test_queue_reports_busy_without_running_a_second_physical_owner(tmp_path):
    ledger = CommandLedger((tmp_path / "ledger.sqlite3").resolve())
    blocker = threading.Event()

    def execute(_command, _stop_requested):
        blocker.wait(2.0)
        return ExecutionReport(True, "ok")

    for index in range(3):
        ledger.reserve(f"cmd-{index}", _fingerprint(index + 1))
    worker = CommandWorker(execute, ledger, max_pending=1)
    assert worker.submit(RuntimeCommand("cmd-0", 1, object())) is SubmitDecision.QUEUED
    assert worker.submit(RuntimeCommand("cmd-1", 2, object())) is SubmitDecision.BUSY
    blocker.set()
    assert worker.shutdown()
    ledger.close()


def test_shutdown_cancels_queued_work_instead_of_executing_it(tmp_path):
    ledger = CommandLedger((tmp_path / "ledger.sqlite3").resolve())
    executed = []
    ledger.reserve("queued", _fingerprint())
    worker = CommandWorker(
        lambda command, _stop: executed.append(command.command_id)
        or ExecutionReport(True, "ok"),
        ledger,
    )
    assert worker.submit(RuntimeCommand("queued", 1, object())) is SubmitDecision.QUEUED

    assert worker.shutdown()

    assert executed == []
    assert ledger.get("queued").stage == "canceled"
    assert ledger.get("queued").result_code == "controller_shutting_down"
    ledger.close()


def test_corrupt_or_wrong_schema_ledger_fails_before_admission(tmp_path):
    corrupt = (tmp_path / "corrupt.sqlite3").resolve()
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(LedgerError, match="corrupt"):
        CommandLedger(corrupt)

    wrong_schema = (tmp_path / "wrong.sqlite3").resolve()
    connection = sqlite3.connect(wrong_schema)
    connection.execute("CREATE TABLE commands (command_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(LedgerError, match="schema"):
        CommandLedger(wrong_schema)


def test_queue_persistence_failure_never_reaches_executor(tmp_path, monkeypatch):
    ledger = CommandLedger((tmp_path / "ledger.sqlite3").resolve())
    ledger.reserve("cmd", _fingerprint())
    executed = []
    worker = CommandWorker(
        lambda command, _stop: executed.append(command.command_id)
        or ExecutionReport(True, "ok"),
        ledger,
    )
    worker.start()

    def fail_update(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "update", fail_update)
    assert (
        worker.submit(RuntimeCommand("cmd", 1, object()))
        is SubmitDecision.PERSISTENCE_ERROR
    )
    assert worker.shutdown()
    assert executed == []
    assert worker.fatal_error.startswith("queue_persistence_failed")
    ledger.close()


def test_terminal_persistence_failure_is_interrupted_on_restart(tmp_path, monkeypatch):
    path = (tmp_path / "ledger.sqlite3").resolve()
    ledger = CommandLedger(path)
    ledger.reserve("cmd", _fingerprint())
    original_update = ledger.update

    def fail_terminal(command_id, stage, **kwargs):
        if stage == "completed":
            raise OSError("simulated fsync failure")
        return original_update(command_id, stage, **kwargs)

    monkeypatch.setattr(ledger, "update", fail_terminal)
    worker = CommandWorker(
        lambda _command, _stop: ExecutionReport(True, "ok"), ledger
    )
    worker.start()
    assert worker.submit(RuntimeCommand("cmd", 1, object())) is SubmitDecision.QUEUED
    deadline = time.monotonic() + 2.0
    while not worker.fatal_error:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert worker.shutdown()
    assert worker.fatal_error.startswith("terminal_persistence_failed")
    monkeypatch.undo()
    ledger.close()

    with CommandLedger(path) as reopened:
        assert reopened.get("cmd").stage == "running"
        assert reopened.mark_interrupted() == ("cmd",)
        assert reopened.get("cmd").stage == "interrupted"


def test_stage_observer_failure_cannot_kill_worker(tmp_path):
    ledger = CommandLedger((tmp_path / "ledger.sqlite3").resolve())
    ledger.reserve("cmd", _fingerprint())

    def broken_observer(*_args):
        raise RuntimeError("observer")

    worker = CommandWorker(
        lambda _command, _stop: ExecutionReport(True, "ok"),
        ledger,
        on_stage=broken_observer,
    )
    worker.start()
    assert worker.submit(RuntimeCommand("cmd", 1, object())) is SubmitDecision.QUEUED
    deadline = time.monotonic() + 2.0
    while ledger.get("cmd").stage != "completed":
        assert time.monotonic() < deadline
        time.sleep(0.005)
    assert worker.shutdown()
    assert worker.notification_errors
    assert not worker.fatal_error
    ledger.close()
