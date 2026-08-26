from __future__ import annotations

from collections import deque
import copy
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from builtin_interfaces.msg import Time
import or_digital_twin.node as node_module
from or_digital_twin.node import ORDigitalTwinNode
from or_digital_twin.twin import ORDigitalTwin
from or_digital_twin.voice_intent_receipt import (
    GatewayLeaseAuthority,
    VoiceIntentReceiptDraft,
    VoiceIntentReceiptKey,
    VoiceIntentReceiptLedger,
    voice_intent_fingerprint,
)
from procedure_spec import load_bundle, load_voice_command_catalog
from surgical_interop_msgs.msg import GatewayInfo
from surgical_msgs.msg import VoiceCommandIntent


def _bundle_dir() -> Path:
    return (
        Path(__file__).parents[2]
        / "procedure_spec"
        / "procedure_spec"
        / "specs"
        / "thyroidectomy"
    )


def _private_database(tmp_path: Path, name: str = "receipts.sqlite3") -> Path:
    directory = tmp_path / name.removesuffix(".sqlite3")
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory / name


def _activate_gateway(
    node,
    gateway_instance_id: str = "gateway-a",
    *,
    procedure_run_id: str = "run-1",
    source_stamp_ns: int = 101_000_000_000,
    revision: int = 1,
) -> str:
    return node._voice_gateway_authority.observe(
        gateway_instance_id=gateway_instance_id,
        procedure_run_id=procedure_run_id,
        procedure_type="thyroidectomy",
        catalog_version="catalog-v1",
        schema_version="surgery-state/v1",
        interface_version="0.4.0",
        procedure_active=True,
        revision=revision,
        source_stamp_ns=source_stamp_ns,
        now_ns=source_stamp_ns,
        received_monotonic=time.monotonic(),
        expected_procedure_type="thyroidectomy",
    )


def _node(database: Path, *, run_id: str = "run-1", now_sec: int = 101):
    node = ORDigitalTwinNode.__new__(ORDigitalTwinNode)
    node._twin = ORDigitalTwin(load_bundle(_bundle_dir()))
    node._twin.state.procedure_run_id = run_id
    node._voice_command_catalog = load_voice_command_catalog(_bundle_dir())
    node._tool_predict_stability = {}
    node._tool_prediction_last_sample_by_source = {}
    node._validated_tool_request_history = deque(maxlen=12)
    node._recent_voice_intent_ids = {}
    node._require_voice_intent_source_metadata = True
    node._voice_intent_max_age_sec = 3.0
    node._voice_intent_future_tolerance_sec = 1.0
    node._voice_intent_dedupe_retention_sec = 120.0
    node._voice_intent_receipt_ledger = VoiceIntentReceiptLedger(database)
    node._voice_gateway_authority = GatewayLeaseAuthority(timeout_sec=3.0)
    node._voice_gated_admission_lock = threading.RLock()
    node._voice_gated_mutation_dirty = False
    node._voice_gated_mutation_fatal = False
    events: list[tuple[str, dict]] = []
    world_updates: list[bool] = []
    node._publish_event = lambda event_type, **kwargs: events.append(
        (event_type, kwargs)
    )
    node._publish_world_state = lambda: world_updates.append(True)
    node._stamp = lambda: Time(sec=now_sec)
    if run_id == "run-1":
        assert _activate_gateway(node) == "gateway_scope_active"
    return node, events, world_updates


def _intent(**overrides) -> VoiceCommandIntent:
    catalog = load_voice_command_catalog(_bundle_dir())
    payload = {
        "utterance_id": "utterance-1",
        "gateway_instance_id": "gateway-a",
        "procedure_run_id": "run-1",
        "function_request_id": "function-1",
        "source": "taskplanner_asr:cloud",
        "source_is_final": True,
        "source_speaker_role": "surgeon",
        "source_has_confidence": False,
        "source_confidence": 0.0,
        "procedure_id": catalog.procedure_id,
        "catalog_id": catalog.catalog_id,
        "intent": "tool_handover",
        "tool_id": "T04",
        "disposition": "propose",
        "requires_confirmation": False,
        "raw_text": "보비 전달해 줘",
        "normalized_text": "보비 전달해 줘",
        "urgency": "routine",
        "provenance": "vlm_function_admission_gate",
        "reason": "exact_tool_and_handover_verb",
    }
    payload.update(overrides)
    message = VoiceCommandIntent()
    message.header.stamp = Time(sec=100)
    for field_name, value in payload.items():
        setattr(message, field_name, value)
    return message


def _gateway(
    *,
    gateway_instance_id: str = "gateway-a",
    procedure_run_id: str = "run-1",
    active: bool = True,
    revision: int = 2,
    stamp_sec: int = 102,
    catalog_version: str = "catalog-v1",
) -> GatewayInfo:
    message = GatewayInfo()
    message.stamp = Time(sec=stamp_sec)
    message.revision = revision
    message.schema_version = "surgery-state/v1"
    message.interface_version = "0.4.0"
    message.catalog_version = catalog_version
    message.gateway_instance_id = gateway_instance_id
    message.procedure_run_id = procedure_run_id if active else ""
    message.procedure_type = "thyroidectomy"
    message.procedure_active = active
    return message


def test_fingerprint_is_canonical_and_transcript_free() -> None:
    base = {
        "utterance_id": "utterance-1",
        "intent": "tool_handover",
        "tool_id": "T04",
        "distance_m": 0.0,
        "raw_text": "원문 하나",
        "normalized_text": "정규화 하나",
        "evidence_spans": ["원문 하나"],
    }
    changed_transcript = {
        **base,
        "raw_text": "완전히 다른 원문",
        "normalized_text": "완전히 다른 정규화",
        "evidence_spans": ["완전히 다른 원문"],
    }
    assert voice_intent_fingerprint(base) == voice_intent_fingerprint(
        changed_transcript
    )
    assert voice_intent_fingerprint(base) != voice_intent_fingerprint(
        {**base, "tool_id": "T03"}
    )


def test_ledger_first_writer_duplicate_and_collision_never_rerun_builder(
    tmp_path: Path,
) -> None:
    database = _private_database(tmp_path)
    key = VoiceIntentReceiptKey("gateway-a", "run-1", "function-1")
    calls: list[str] = []

    def build() -> VoiceIntentReceiptDraft:
        calls.append("called")
        return VoiceIntentReceiptDraft(
            instrument_id="T04",
            mode="voice_command_intent",
            detail={"accepted": True, "tool_id": "T04"},
            accepted=True,
        )

    with VoiceIntentReceiptLedger(database) as ledger:
        first = ledger.first_writer(
            key=key,
            utterance_id="utterance-1",
            fingerprint_sha256="a" * 64,
            build_receipt=build,
        )
        duplicate = ledger.first_writer(
            key=key,
            utterance_id="utterance-1",
            fingerprint_sha256="a" * 64,
            build_receipt=build,
        )
        collision = ledger.first_writer(
            key=key,
            utterance_id="utterance-2",
            fingerprint_sha256="b" * 64,
            build_receipt=build,
        )

        assert first.is_new
        assert duplicate.is_duplicate
        assert collision.is_collision
        assert calls == ["called"]
        assert duplicate.receipt == first.receipt
        assert collision.receipt == first.receipt
        assert ledger.count() == 1


def test_ledger_rejects_transcript_fields_in_receipt_detail(
    tmp_path: Path,
) -> None:
    database = _private_database(tmp_path)
    with VoiceIntentReceiptLedger(database) as ledger:
        with pytest.raises(ValueError, match="unsupported fields"):
            ledger.first_writer(
                key=VoiceIntentReceiptKey(
                    "gateway-a", "run-1", "function-1"
                ),
                utterance_id="utterance-1",
                fingerprint_sha256="a" * 64,
                build_receipt=lambda: VoiceIntentReceiptDraft(
                    instrument_id="T04",
                    mode="voice_command_intent",
                    detail={
                        "accepted": True,
                        "raw_text": "절대 저장하면 안 되는 원문",
                    },
                    accepted=True,
                ),
            )
        assert ledger.count() == 0


def test_gated_accepted_exact_duplicate_replays_receipt_without_mutation(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    message = _intent()

    node._on_voice_command_intent(message)
    first_generation = node._twin.state.surgeon_request_generation
    node._on_voice_command_intent(message)

    assert first_generation > 0
    assert node._twin.state.surgeon_request_generation == first_generation
    assert node._twin.request_queue_summary()["queued_tools"] == ["T04"]
    assert events[0] == events[1]
    assert events[0][1]["detail"]["accepted"] is True
    assert world_updates == [True]
    assert node._voice_intent_receipt_ledger.count() == 1
    node._voice_intent_receipt_ledger.close()


def test_rejected_first_writer_is_durable_and_duplicate_is_identical(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    message = _intent(requires_confirmation=True, reason="repair_required")

    node._on_voice_command_intent(message)
    node._on_voice_command_intent(message)

    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert world_updates == []
    assert events[0] == events[1]
    assert events[0][1]["detail"]["accepted"] is False
    assert events[0][1]["detail"]["reason"] == (
        "voice_intent_requires_confirmation"
    )
    node._voice_intent_receipt_ledger.close()


def test_restart_duplicate_replays_committed_receipt_even_when_source_is_stale(
    tmp_path: Path,
) -> None:
    database = _private_database(tmp_path)
    message = _intent()
    first, first_events, _first_world = _node(database)
    first._on_voice_command_intent(message)
    first._voice_intent_receipt_ledger.close()

    restarted, restarted_events, restarted_world = _node(
        database,
        now_sec=1000,
    )
    restarted._on_voice_command_intent(message)

    assert restarted_events == first_events
    assert restarted_world == []
    assert restarted._twin.state.surgeon_request_generation == 0
    assert restarted._recent_voice_intent_ids == {}
    restarted._voice_intent_receipt_ledger.close()


def test_collision_republishes_first_receipt_and_never_mutates_again(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    node._on_voice_command_intent(_intent())
    first_generation = node._twin.state.surgeon_request_generation

    node._on_voice_command_intent(
        _intent(tool_id="T03", urgency="urgent")
    )

    assert node._twin.state.surgeon_request_generation == first_generation
    assert world_updates == [True]
    assert events[1] == events[0]
    assert events[1][1]["instrument_id"] == "T04"
    assert events[1][1]["detail"]["tool_id"] == "T04"
    assert node._voice_intent_receipt_ledger.count() == 1
    node._voice_intent_receipt_ledger.close()


def test_run_mismatch_is_a_durable_rejection_and_cannot_later_mutate(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(
        _private_database(tmp_path),
        run_id="active-run",
    )
    assert _activate_gateway(
        node,
        procedure_run_id="other-run",
    ) == "gateway_scope_active"
    message = _intent(procedure_run_id="other-run")

    node._on_voice_command_intent(message)
    node._twin.state.procedure_run_id = "other-run"
    node._on_voice_command_intent(message)

    assert events[0] == events[1]
    assert events[0][1]["detail"]["accepted"] is False
    assert events[0][1]["detail"]["reason"] == (
        "gated_voice_intent_twin_run_id_mismatch"
    )
    assert node._twin.state.surgeon_request_generation == 0
    assert world_updates == []
    node._voice_intent_receipt_ledger.close()


def test_new_gateway_same_run_is_a_distinct_first_writer_key(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    node._on_voice_command_intent(_intent())
    assert _activate_gateway(
        node,
        "gateway-b",
        source_stamp_ns=102_000_000_000,
    ) == "gateway_scope_active"
    node._on_voice_command_intent(
        _intent(gateway_instance_id="gateway-b")
    )

    assert node._voice_intent_receipt_ledger.count() == 2
    assert len(events) == 2
    assert events[0][1]["detail"]["gateway_instance_id"] == "gateway-a"
    assert events[1][1]["detail"]["gateway_instance_id"] == "gateway-b"
    assert events[0][1]["detail"]["accepted"] is True
    assert events[1][1]["detail"]["accepted"] is True
    assert len(world_updates) == 2
    node._voice_intent_receipt_ledger.close()


def test_new_gateway_intent_without_heartbeat_is_durably_rejected(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    message = _intent(
        gateway_instance_id="gateway-b",
        function_request_id="function-b",
    )

    node._on_voice_command_intent(message)
    assert events[-1][1]["detail"]["reason"] == (
        "gated_voice_intent_gateway_instance_id_mismatch"
    )
    assert events[-1][1]["detail"]["accepted"] is False
    assert world_updates == []
    assert node._voice_intent_receipt_ledger.count() == 1

    # Even after a later authoritative heartbeat, this logical first writer
    # remains the original fail-closed rejection and cannot mutate the reducer.
    assert _activate_gateway(
        node,
        "gateway-b",
        source_stamp_ns=102_000_000_000,
    ) == "gateway_scope_active"
    node._on_voice_command_intent(message)
    assert events[-1] == events[-2]
    assert node._twin.state.surgeon_request_generation == 0
    node._voice_intent_receipt_ledger.close()


def test_gateway_timeout_rejects_gated_intent_without_reducer_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    current = time.monotonic()
    monkeypatch.setattr(node_module.time, "monotonic", lambda: current + 3.1)

    node._on_voice_gateway_watchdog()
    node._on_voice_command_intent(_intent())

    assert events[-1][1]["detail"]["reason"] == "gateway_heartbeat_timeout"
    assert events[-1][1]["detail"]["accepted"] is False
    assert node._twin.state.surgeon_request_generation == 0
    assert world_updates == []
    node._voice_intent_receipt_ledger.close()


def test_idle_gateway_heartbeat_fences_gated_intent(tmp_path: Path) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    node._stamp = lambda: Time(sec=102)

    node._on_voice_gateway_info(_gateway(active=False))
    node._on_voice_command_intent(_intent())

    assert events[-1][1]["detail"]["reason"] == "gateway_procedure_inactive"
    assert events[-1][1]["detail"]["accepted"] is False
    assert node._twin.state.surgeon_request_generation == 0
    assert world_updates == []
    node._voice_intent_receipt_ledger.close()


def test_gateway_metadata_mutation_poison_is_latched_for_same_scope(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    node._stamp = lambda: Time(sec=102)

    node._on_voice_gateway_info(
        _gateway(catalog_version="catalog-mutated")
    )
    assert node._voice_gateway_authority.scope is None
    assert node._voice_gateway_authority.unavailable_reason == (
        "gateway_scope_metadata_mutation"
    )

    # A corrected heartbeat from the poisoned epoch/run cannot reopen it.
    node._stamp = lambda: Time(sec=103)
    node._on_voice_gateway_info(_gateway(revision=3, stamp_sec=103))
    assert node._voice_gateway_authority.scope is None
    node._on_voice_command_intent(_intent())

    assert events[-1][1]["detail"]["reason"] == (
        "gateway_scope_metadata_mutation"
    )
    assert node._twin.state.surgeon_request_generation == 0
    assert world_updates == []
    node._voice_intent_receipt_ledger.close()


def test_future_gateway_source_stamp_never_opens_or_extends_lease(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    node._stamp = lambda: Time(sec=101)

    node._on_voice_gateway_info(_gateway(stamp_sec=102))
    node._on_voice_command_intent(_intent())

    assert node._voice_gateway_authority.scope is None
    assert events[-1][1]["detail"]["reason"] == "gateway_heartbeat_future"
    assert node._twin.state.surgeon_request_generation == 0
    assert world_updates == []
    node._voice_intent_receipt_ledger.close()


def test_receipt_is_committed_before_observed_event_is_published(
    tmp_path: Path,
) -> None:
    database = _private_database(tmp_path)
    node, _events, _world_updates = _node(database)
    observed_counts: list[int] = []
    world_counts: list[int] = []

    def committed_count() -> int:
        with sqlite3.connect(database) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM voice_intent_receipts"
                ).fetchone()[0]
            )

    def publish(_event_type: str, **_kwargs) -> None:
        observed_counts.append(committed_count())

    node._publish_event = publish
    node._publish_world_state = lambda: world_counts.append(committed_count())
    node._on_voice_command_intent(_intent())

    assert world_counts == [1]
    assert observed_counts == [1]
    node._voice_intent_receipt_ledger.close()


def test_storage_sync_failure_rolls_back_reducer_and_never_publishes_dirty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    node._tool_predict_stability = {"T03": {"since": 1.0}}
    node._tool_prediction_last_sample_by_source = {
        "real_vlm": (1.0, "T03")
    }
    node._twin.state.predicted_tool = "T03"
    node._twin.state.predicted_tool_confidence = 0.75
    node._twin.state.predicted_tool_stability_sec = 1.25
    before_state = copy.deepcopy(node._twin.state)
    before_instruments = copy.deepcopy(node._twin.instrument_states)
    before_event_history = copy.deepcopy(node._twin.event_history)
    before_generation_counter = node._twin._request_generation_counter
    before_tool_history = copy.deepcopy(
        node._validated_tool_request_history
    )
    before_stability = copy.deepcopy(node._tool_predict_stability)
    before_samples = copy.deepcopy(
        node._tool_prediction_last_sample_by_source
    )
    maintenance_calls: list[bool] = []
    node._run_time_based_maintenance = lambda: maintenance_calls.append(True)

    ledger = node._voice_intent_receipt_ledger
    original_fsync = ledger._fsync_storage
    fsync_calls = 0

    def fail_first_fsync() -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 1:
            assert node._voice_gated_mutation_dirty is True
            # Call the class method so the fixture's publication recorder does
            # not mask the production dirty-state guard.
            ORDigitalTwinNode._publish_world_state(node)
            assert maintenance_calls == []
            raise OSError("injected receipt fsync failure")
        original_fsync()

    monkeypatch.setattr(ledger, "_fsync_storage", fail_first_fsync)

    with pytest.raises(OSError, match="injected receipt fsync failure"):
        node._on_voice_command_intent(_intent())

    assert node._twin.state == before_state
    assert node._twin.instrument_states == before_instruments
    assert node._twin.event_history == before_event_history
    assert node._twin._request_generation_counter == before_generation_counter
    assert node._validated_tool_request_history == before_tool_history
    assert node._tool_predict_stability == before_stability
    assert node._tool_prediction_last_sample_by_source == before_samples
    assert node._recent_voice_intent_ids == {}
    assert ledger.count() == 0
    assert events == []
    assert world_updates == []
    assert node._voice_gated_mutation_dirty is False
    assert node._voice_gated_mutation_fatal is False

    # The source dedupe state was also rolled back, so the identical retry is
    # a valid new first writer rather than a transient duplicate rejection.
    node._on_voice_command_intent(_intent())
    assert ledger.count() == 1
    assert events[-1][1]["detail"]["accepted"] is True
    assert node._twin.state.surgeon_request_generation > 0
    ledger.close()


def test_ambiguous_commit_outcome_rolls_back_and_latches_world_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    ledger = node._voice_intent_receipt_ledger
    original_first_writer = ledger.first_writer

    def commit_then_report_failure(**kwargs):
        original_first_writer(**kwargs)
        raise OSError("injected ambiguous post-commit failure")

    monkeypatch.setattr(
        ledger,
        "first_writer",
        commit_then_report_failure,
    )

    with pytest.raises(
        RuntimeError,
        match="receipt commit outcome is ambiguous",
    ):
        node._on_voice_command_intent(_intent())

    assert ledger.count() == 1
    assert node._twin.state.surgeon_request_generation == 0
    assert node._twin.request_queue_summary()["queue_length"] == 0
    assert node._validated_tool_request_history == deque(maxlen=12)
    assert node._recent_voice_intent_ids == {}
    assert node._voice_gated_mutation_dirty is False
    assert node._voice_gated_mutation_fatal is True
    assert events == []
    assert world_updates == []

    maintenance_calls: list[bool] = []
    node._run_time_based_maintenance = lambda: maintenance_calls.append(True)
    ORDigitalTwinNode._publish_world_state(node)
    assert maintenance_calls == []

    # Further gated commands are explicit rejections and never consult the
    # ambiguous accepted row or mutate the reducer.
    node._on_voice_command_intent(_intent())
    assert events[-1][1]["detail"]["reason"] == (
        "gated_voice_intent_reducer_rollback_fatal"
    )
    assert node._twin.state.surgeon_request_generation == 0
    assert world_updates == []
    ledger.close()


def test_gateway_epoch_callback_cannot_race_scope_check_and_reducer_mutation(
    tmp_path: Path,
) -> None:
    node, events, world_updates = _node(_private_database(tmp_path))
    node._stamp = lambda: Time(sec=102)
    reducer_entered = threading.Event()
    allow_reducer = threading.Event()
    gateway_started = threading.Event()
    gateway_finished = threading.Event()
    thread_errors: list[BaseException] = []
    original_process = node._process_voice_command_intent

    def blocked_process(message, **kwargs):
        reducer_entered.set()
        if not allow_reducer.wait(timeout=2.0):
            raise TimeoutError("test did not release gated reducer")
        return original_process(message, **kwargs)

    node._process_voice_command_intent = blocked_process

    def run_intent() -> None:
        try:
            node._on_voice_command_intent(_intent())
        except BaseException as error:
            thread_errors.append(error)

    def switch_gateway() -> None:
        gateway_started.set()
        try:
            node._on_voice_gateway_info(
                _gateway(
                    gateway_instance_id="gateway-b",
                    revision=2,
                    stamp_sec=102,
                )
            )
        except BaseException as error:
            thread_errors.append(error)
        finally:
            gateway_finished.set()

    intent_thread = threading.Thread(target=run_intent)
    gateway_thread = threading.Thread(target=switch_gateway)
    intent_thread.start()
    assert reducer_entered.wait(timeout=2.0)
    gateway_thread.start()
    assert gateway_started.wait(timeout=2.0)
    # The callback has begun, but cannot mutate the active epoch while the
    # checked first writer still owns the admission lock.
    assert gateway_finished.wait(timeout=0.05) is False
    allow_reducer.set()
    intent_thread.join(timeout=2.0)
    gateway_thread.join(timeout=2.0)

    assert intent_thread.is_alive() is False
    assert gateway_thread.is_alive() is False
    assert thread_errors == []
    assert events[0][1]["detail"]["accepted"] is True
    assert events[0][1]["detail"]["gateway_instance_id"] == "gateway-a"
    assert world_updates == [True]
    assert node._voice_gateway_authority.scope.identity == (
        "gateway-b",
        "run-1",
    )
    node._voice_intent_receipt_ledger.close()


def test_database_and_receipt_detail_contain_no_transcript(tmp_path: Path) -> None:
    database = _private_database(tmp_path)
    node, events, _world_updates = _node(database)
    message = _intent(
        raw_text="비밀 원문 보비 전달",
        normalized_text="비밀 정규화 보비 전달",
        evidence_spans=["비밀 원문"],
    )
    node._on_voice_command_intent(message)
    node._voice_intent_receipt_ledger.close()

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT utterance_id, fingerprint_sha256, detail_json
              FROM voice_intent_receipts
            """
        ).fetchone()
    stored = "\n".join(str(value) for value in row)
    assert "비밀 원문" not in stored
    assert "비밀 정규화" not in stored
    detail = json.loads(row[2])
    assert "raw_text" not in detail
    assert "normalized_text" not in detail
    assert detail["raw_text_present"] is True
    assert detail["normalized_text_present"] is True
    assert events[0][1]["detail"] == detail


def test_ledger_enforces_private_directory_and_file_modes(tmp_path: Path) -> None:
    database = _private_database(tmp_path)
    ledger = VoiceIntentReceiptLedger(database)
    ledger.first_writer(
        key=VoiceIntentReceiptKey("gateway-a", "run-1", "function-1"),
        utterance_id="utterance-1",
        fingerprint_sha256="a" * 64,
        build_receipt=lambda: VoiceIntentReceiptDraft(
            instrument_id="T04",
            mode="voice_command_intent",
            detail={"accepted": True},
            accepted=True,
        ),
    )
    assert os.stat(database.parent).st_mode & 0o777 == 0o700
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if candidate.exists():
            assert os.stat(candidate).st_mode & 0o777 == 0o600
    ledger.close()

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    insecure.chmod(0o755)
    with pytest.raises(PermissionError, match="0700"):
        VoiceIntentReceiptLedger(insecure / "receipts.sqlite3")


def test_ledger_rejects_symlink_path(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    target = private / "target.sqlite3"
    target.touch(mode=0o600)
    link = private / "link.sqlite3"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        VoiceIntentReceiptLedger(link)
