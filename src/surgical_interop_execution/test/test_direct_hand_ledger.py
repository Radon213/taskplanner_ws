from pathlib import Path

from surgical_interop_execution.direct_hand_ledger import (
    DurableDirectHandLedger,
    valid_procedure_run_id,
)


RUN_A = "a" * 32
RUN_B = "b" * 32


def _reserve(
    ledger: DurableDirectHandLedger,
    *,
    run_id: str = RUN_A,
    generation: int = 1,
    action: str = "direct_handover",
    tool: str = "T02",
):
    return ledger.reserve(
        procedure_run_id=run_id,
        episode_generation=generation,
        command_id=f"skill-hand-{run_id}-{generation}-{action}",
        action=action,
        instrument_id=tool,
        instrument_instance_id=f"{tool}#1",
        source_location=(
            "robot" if action == "direct_handover" else "tray"
        ),
        target_location=(
            "surgeon" if "handover" in action else "robot"
        ),
    )


def test_run_id_contract_is_exact_lowercase_uuid_hex() -> None:
    assert valid_procedure_run_id(RUN_A)
    assert not valid_procedure_run_id("A" * 32)
    assert not valid_procedure_run_id("a" * 31)
    assert not valid_procedure_run_id("")


def test_same_episode_handover_is_at_most_once() -> None:
    ledger = DurableDirectHandLedger(":memory:")
    first = _reserve(ledger)
    duplicate = _reserve(ledger)

    assert first.accepted is True
    assert duplicate.accepted is False
    assert duplicate.reason == "duplicate_command"


def test_same_episode_cannot_switch_handover_payload() -> None:
    ledger = DurableDirectHandLedger(":memory:")
    assert _reserve(ledger, action="direct_handover").accepted

    conflict = _reserve(
        ledger,
        action="pick_up_and_handover",
        tool="T03",
    )
    assert conflict.accepted is False
    assert conflict.reason == "direct_episode_payload_conflict"


def test_prepare_and_one_handover_are_distinct_episode_legs() -> None:
    ledger = DurableDirectHandLedger(":memory:")
    assert _reserve(ledger, action="prepare_tool").accepted
    assert _reserve(ledger, action="direct_handover").accepted
    assert not _reserve(ledger, action="direct_handover").accepted


def test_release_generation_and_new_run_rearm_independently() -> None:
    ledger = DurableDirectHandLedger(":memory:")
    assert _reserve(ledger, run_id=RUN_A, generation=1).accepted
    assert _reserve(ledger, run_id=RUN_A, generation=2).accepted
    assert _reserve(ledger, run_id=RUN_B, generation=1).accepted


def test_restart_preserves_reservation(tmp_path: Path) -> None:
    path = tmp_path / "direct.sqlite3"
    first = DurableDirectHandLedger(str(path))
    assert _reserve(first).accepted
    first.close()

    restarted = DurableDirectHandLedger(str(path))
    duplicate = _reserve(restarted)
    assert duplicate.accepted is False
    assert duplicate.reason == "duplicate_command"
