"""Bounded, hardware-free stress tests for the durable control boundary."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import random
import sqlite3
import threading
import time

from retraction_control.command_models import CommandRequest, RetractionControlError
from retraction_control.runtime import (
    CommandLedger,
    CommandWorker,
    ExecutionReport,
    LedgerDecision,
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


def _wait_for_terminal(ledger: CommandLedger, command_id: str) -> None:
    deadline = time.monotonic() + 2.0
    while True:
        record = ledger.get(command_id)
        if record is not None and record.stage in {
            "completed",
            "failed",
            "canceled",
        }:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"command did not become terminal: {command_id}")
        time.sleep(0.001)


def test_many_sqlite_connections_still_reserve_one_command_exactly_once(tmp_path):
    path = (tmp_path / "shared-ledger.sqlite3").resolve()
    ledgers = [CommandLedger(path) for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            decisions = tuple(
                pool.map(
                    lambda index: ledgers[index % len(ledgers)]
                    .reserve("same-command", _fingerprint())
                    .decision,
                    range(256),
                )
            )
        assert decisions.count(LedgerDecision.NEW) == 1
        assert decisions.count(LedgerDecision.DUPLICATE) == 255
    finally:
        for ledger in ledgers:
            ledger.close()


def test_repeated_cold_open_worker_shutdown_leaves_no_worker_threads(tmp_path):
    path = (tmp_path / "restart-ledger.sqlite3").resolve()
    thread_prefix = "retraction-offline-soak-"

    for cycle in range(64):
        command_id = f"cycle-{cycle:03d}"
        with CommandLedger(path) as ledger:
            assert (
                ledger.reserve(command_id, _fingerprint()).decision
                is LedgerDecision.NEW
            )
            worker = CommandWorker(
                lambda _command, _stop: ExecutionReport(True, "synthetic_ok"),
                ledger,
                thread_name=f"{thread_prefix}{cycle:03d}",
            )
            worker.start()
            assert (
                worker.submit(RuntimeCommand(command_id, 1, object()))
                is SubmitDecision.QUEUED
            )
            _wait_for_terminal(ledger, command_id)
            assert worker.shutdown()
            assert not worker.fatal_error
            ledger.verify_integrity()

    with sqlite3.connect(path) as connection:
        count, nonterminal = connection.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN stage IN ('admitted', 'queued', 'running', 'stopping')
                            THEN 1 ELSE 0 END)
              FROM commands
            """
        ).fetchone()
    assert count == 64
    assert nonterminal == 0
    assert not any(
        thread.name.startswith(thread_prefix) for thread in threading.enumerate()
    )


def test_seeded_request_boundary_fuzz_is_deterministic_and_closed():
    rng = random.Random(0xAF7200)
    values = (
        None,
        False,
        True,
        -1,
        0,
        1,
        2,
        6,
        7,
        255,
        -0.0,
        0.0,
        0.001,
        float("inf"),
        float("-inf"),
        float("nan"),
        "",
        " taskplanner",
        "taskplanner",
        "cmd-1",
        "../escape",
        "bad space",
        object(),
    )
    cases = [tuple(rng.choice(values) for _ in range(6)) for _ in range(4096)]

    def classify(case):
        try:
            request = CommandRequest.from_wire(
                protocol_version=case[0],
                source_id=case[1],
                command_id=case[2],
                command=case[3],
                target_side=case[4],
                distance_m=case[5],
            )
        except RetractionControlError as exc:
            return ("rejected", exc.code.value, exc.field)
        return ("accepted", request.command.name, request.target_side.name)

    first = tuple(classify(case) for case in cases)
    second = tuple(classify(case) for case in cases)
    assert first == second
    assert any(result[0] == "rejected" for result in first)
