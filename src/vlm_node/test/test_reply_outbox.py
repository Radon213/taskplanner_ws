from __future__ import annotations

from dataclasses import replace
import logging
import multiprocessing
import os
from pathlib import Path
import sqlite3
import stat
from threading import Thread

import pytest

from vlm_node.reply_outbox import (
    ALLOWED_ACK_STATES,
    ReplyCollisionError,
    ReplyEnvelope,
    ReplyOutbox,
)


def _envelope(
    suffix: str = "1",
    *,
    gateway: str = "gateway-1",
    run: str = "run-1",
    utterance: str | None = None,
) -> ReplyEnvelope:
    return ReplyEnvelope(
        stamp_sec=123,
        stamp_nanosec=456,
        source="real_vlm:thyroidectomy_demo:dialogue",
        source_epoch=7,
        source_sequence=11,
        correlation_id=f"correlation-{suffix}",
        schema_version="6",
        gateway_instance_id=gateway,
        procedure_run_id=run,
        utterance_id=utterance or f"utterance-{suffix}",
        turn_id=f"turn-{suffix}",
        reply_id=f"reply-{suffix}",
        text=f"수술 안내 문장 {suffix}",
        kind="acknowledgement",
        timing="on_function_accepted",
        speak=True,
        function_call_name="request_tool_handover",
        function_arguments_json=f'{{"tool_id":"T0{suffix}"}}',
        function_request_id=f"request-{suffix}",
        valid=True,
        validation_error="",
    )


def _crash_after_enqueue(path: str, envelope: ReplyEnvelope) -> None:
    outbox = ReplyOutbox(path)
    outbox.enqueue(envelope)
    os._exit(0)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_round_trip_preserves_every_scalar_and_uses_required_pragmas(
    tmp_path: Path,
) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    original = _envelope()

    with ReplyOutbox(database) as outbox:
        assert outbox.enqueue(original)
        assert outbox.pending_for_run("run-1") == [original]
        assert outbox.pending_count() == 1
        assert outbox.pending_count("run-1") == 1

        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("reply_id", ""),
        ("gateway_instance_id", ""),
        ("procedure_run_id", " "),
        ("utterance_id", ""),
        ("turn_id", ""),
        ("text", ""),
        ("valid", False),
        ("speak", False),
        ("timing", "eventually"),
    ],
)
def test_envelope_rejects_unpublishable_values(
    field_name: str,
    bad_value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(_envelope(), **{field_name: bad_value})


def test_first_writer_wins_for_reply_id_and_logical_turn(tmp_path: Path) -> None:
    database = tmp_path / "outbox" / "reply.sqlite3"
    first = _envelope()

    with ReplyOutbox(database) as outbox:
        assert outbox.enqueue(first)
        assert not outbox.enqueue(first)

        with pytest.raises(ReplyCollisionError):
            outbox.enqueue(replace(first, text="덮어쓰면 안 되는 다른 문장"))
        with pytest.raises(ReplyCollisionError):
            outbox.enqueue(replace(first, reply_id="different-reply"))

        assert outbox.pending_for_run("run-1") == [first]


def test_wal_commit_survives_process_crash_without_close(tmp_path: Path) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    original = _envelope()
    process = multiprocessing.get_context("fork").Process(
        target=_crash_after_enqueue,
        args=(str(database), original),
    )

    process.start()
    process.join(timeout=10)

    assert process.exitcode == 0
    with ReplyOutbox(database) as reopened:
        assert reopened.pending_for_run("run-1") == [original]


def test_ack_is_validated_monotonic_idempotent_and_durable(tmp_path: Path) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    original = _envelope()
    with ReplyOutbox(database) as outbox:
        outbox.enqueue(original)
        with pytest.raises(ValueError):
            outbox.mark_acked(original.reply_id, "not_an_ack")
        assert not outbox.mark_acked("unknown-reply", "queued")
        assert outbox.mark_acked(original.reply_id, "queued")
        assert not outbox.mark_acked(original.reply_id, "queued")
        assert outbox.mark_acked(original.reply_id, "playing")
        assert outbox.mark_acked(original.reply_id, "played")
        assert not outbox.mark_acked(original.reply_id, "failed")
        assert outbox.pending_count("run-1") == 0

    with ReplyOutbox(database) as reopened:
        assert reopened.pending_count("run-1") == 0

    assert ALLOWED_ACK_STATES == {
        "queued",
        "waiting_function_accepted",
        "waiting_function_completed",
        "duplicate_suppressed",
        "playing",
        "played",
        "failed",
    }


def test_exact_run_scope_limit_and_stale_transitions(tmp_path: Path) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    run_one = [_envelope(str(index), run="run-1") for index in range(1, 4)]
    prefix_collision = _envelope("10", run="run-10")

    with ReplyOutbox(database) as outbox:
        for envelope in (*run_one, prefix_collision):
            outbox.enqueue(envelope)

        assert outbox.pending_for_run("run-1", limit=2) == run_one[:2]
        assert outbox.pending_for_run("run-10") == [prefix_collision]
        with pytest.raises(ValueError):
            outbox.pending_for_run("run-1", limit=0)
        with pytest.raises(ValueError):
            outbox.pending_for_run("run-1", limit=1025)

        assert outbox.mark_stale_outside_run("run-1") == 1
        assert outbox.pending_for_run("run-10") == []
        assert outbox.pending_count() == 3

        assert outbox.mark_stale(run_one[0].reply_id)
        assert not outbox.mark_stale(run_one[0].reply_id)
        assert not outbox.mark_stale("unknown-reply")
        assert outbox.pending_for_run("run-1") == run_one[1:]


def test_exact_gateway_run_scope_fences_reused_run_id(tmp_path: Path) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    old_epoch = _envelope(
        "old",
        gateway="gateway-old",
        run="run-reused",
        utterance="same-utterance",
    )
    current_epoch = _envelope(
        "current",
        gateway="gateway-current",
        run="run-reused",
        utterance="same-utterance",
    )

    with ReplyOutbox(database) as outbox:
        outbox.enqueue(old_epoch)
        outbox.enqueue(current_epoch)
        assert outbox.pending_for_scope(
            "gateway-current", "run-reused"
        ) == [current_epoch]
        assert outbox.mark_stale_outside_scope(
            "gateway-current", "run-reused"
        ) == 1
        assert outbox.pending_for_scope(
            "gateway-old", "run-reused"
        ) == []
        assert outbox.pending_for_scope(
            "gateway-current", "run-reused"
        ) == [current_epoch]


def test_legacy_run_only_unique_constraint_is_migrated(tmp_path: Path) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE reply_outbox (
                reply_id TEXT PRIMARY KEY,
                stamp_sec INTEGER NOT NULL,
                stamp_nanosec INTEGER NOT NULL,
                source TEXT NOT NULL,
                source_epoch INTEGER NOT NULL,
                source_sequence INTEGER NOT NULL,
                correlation_id TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                procedure_run_id TEXT NOT NULL,
                utterance_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL,
                timing TEXT NOT NULL,
                speak INTEGER NOT NULL,
                function_call_name TEXT NOT NULL,
                function_arguments_json TEXT NOT NULL,
                function_request_id TEXT NOT NULL,
                valid INTEGER NOT NULL,
                validation_error TEXT NOT NULL,
                ack_state TEXT,
                acked_at_ns INTEGER,
                stale INTEGER NOT NULL DEFAULT 0,
                stale_reason TEXT NOT NULL DEFAULT '',
                enqueued_at_ns INTEGER NOT NULL,
                updated_at_ns INTEGER NOT NULL,
                UNIQUE (procedure_run_id, utterance_id)
            )
            """
        )
        legacy = _envelope(
            "legacy",
            gateway="gateway-legacy",
            run="run-reused",
            utterance="utterance-reused",
        )
        legacy_columns = [
            name
            for name in legacy.__dataclass_fields__
            if name != "gateway_instance_id"
        ]
        columns = ", ".join(legacy_columns)
        placeholders = ", ".join("?" for _ in legacy_columns)
        connection.execute(
            f"INSERT INTO reply_outbox ({columns}, enqueued_at_ns, "
            f"updated_at_ns) VALUES ({placeholders}, 1, 1)",
            tuple(getattr(legacy, name) for name in legacy_columns),
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()

    current = _envelope(
        "current",
        gateway="gateway-current",
        run="run-reused",
        utterance="utterance-reused",
    )
    with ReplyOutbox(database) as outbox:
        assert outbox.enqueue(current)
        assert outbox.pending_for_scope(
            "gateway-current", "run-reused"
        ) == [current]
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_inactive_scope_stales_every_unacknowledged_reply(tmp_path: Path) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    with ReplyOutbox(database) as outbox:
        first = _envelope("1", run="run-1")
        second = _envelope("2", run="run-2")
        outbox.enqueue(first)
        outbox.enqueue(second)
        assert outbox.mark_stale_outside_run("") == 2
        assert outbox.pending_count() == 0


def test_database_and_sidecars_are_private(tmp_path: Path) -> None:
    private = tmp_path / "private"
    database = private / "reply.sqlite3"
    private.mkdir(mode=0o777)
    private.chmod(0o777)

    with ReplyOutbox(database) as outbox:
        outbox.enqueue(_envelope())
        assert _mode(private) == 0o700
        assert _mode(database) == 0o600
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database}{suffix}")
            if sidecar.exists():
                assert _mode(sidecar) == 0o600


def test_collisions_do_not_log_or_expose_speech_plaintext(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    secret = "절대로 로그에 남기지 않을 평문 음성"
    original = replace(_envelope(), text=secret)

    caplog.set_level(logging.DEBUG)
    with ReplyOutbox(database) as outbox:
        outbox.enqueue(original)
        with pytest.raises(ReplyCollisionError) as raised:
            outbox.enqueue(replace(original, text=f"{secret} 변경"))

    assert secret not in str(raised.value)
    assert secret not in repr(original)
    assert secret not in caplog.text


def test_concurrent_identical_enqueues_are_thread_safe(tmp_path: Path) -> None:
    database = tmp_path / "private" / "reply.sqlite3"
    original = _envelope()
    results: list[bool] = []
    errors: list[BaseException] = []

    with ReplyOutbox(database) as outbox:
        def enqueue() -> None:
            try:
                results.append(outbox.enqueue(original))
            except BaseException as exc:  # pragma: no cover - diagnostic capture
                errors.append(exc)

        threads = [Thread(target=enqueue) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert not errors
        assert results.count(True) == 1
        assert results.count(False) == 11
        assert outbox.pending_for_run("run-1") == [original]
