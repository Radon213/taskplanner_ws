from __future__ import annotations

from vlm_node.real_vlm import DialogueTurn, DialogueTurnGate


def _enqueue(
    gate: DialogueTurnGate,
    *,
    utterance_id: str,
    text: str = "바이폴라 전달해 주세요",
) -> DialogueTurn:
    turn = gate.enqueue(utterance_id=utterance_id, text=text)
    assert isinstance(turn, DialogueTurn)
    assert turn.utterance_id == utterance_id
    assert turn.text == text
    return turn


def test_dialogue_turns_are_claimed_and_committed_in_fifo_order() -> None:
    gate = DialogueTurnGate(epoch=7)
    first = _enqueue(gate, utterance_id="utterance-1")
    second = _enqueue(
        gate,
        utterance_id="utterance-2",
        text="도구를 회수해 주세요",
    )

    assert first.turn_id != second.turn_id
    assert gate.next_pending() == first
    assert (
        gate.claim(turn_id=second.turn_id, correlation_id="correlation-2")
        is None
    )

    assert (
        gate.claim(turn_id=first.turn_id, correlation_id="correlation-1")
        == first
    )
    assert gate.commit(
        turn_id=first.turn_id,
        correlation_id="correlation-1",
        reply="바이폴라 전달을 준비하겠습니다.",
        valid=True,
    )
    assert gate.next_pending() == second

    assert (
        gate.claim(turn_id=second.turn_id, correlation_id="correlation-2")
        == second
    )
    assert gate.commit(
        turn_id=second.turn_id,
        correlation_id="correlation-2",
        reply="도구를 회수하겠습니다.",
        valid=True,
    )
    assert gate.next_pending() is None


def test_same_text_with_a_new_utterance_id_creates_a_new_turn() -> None:
    gate = DialogueTurnGate(epoch=3)
    first = _enqueue(gate, utterance_id="utterance-a")
    second = _enqueue(gate, utterance_id="utterance-b")

    assert first.text == second.text
    assert first.utterance_id != second.utterance_id
    assert first.turn_id != second.turn_id

    assert gate.claim(
        turn_id=first.turn_id,
        correlation_id="correlation-a",
    ) == first
    assert gate.commit(
        turn_id=first.turn_id,
        correlation_id="correlation-a",
        reply="첫 번째 응답입니다.",
        valid=True,
    )
    assert gate.next_pending() == second


def test_result_claim_requires_matching_turn_and_correlation_ids() -> None:
    gate = DialogueTurnGate(epoch=5)
    turn = _enqueue(gate, utterance_id="utterance-1")

    assert gate.claim(turn_id="", correlation_id="correlation-1") is None
    assert gate.claim(turn_id=turn.turn_id, correlation_id="") is None
    assert gate.claim(
        turn_id="not-the-pending-turn",
        correlation_id="correlation-1",
    ) is None

    assert gate.claim(
        turn_id=turn.turn_id,
        correlation_id="correlation-1",
    ) == turn
    assert gate.claim(
        turn_id=turn.turn_id,
        correlation_id="correlation-other",
    ) is None
    assert not gate.commit(
        turn_id=turn.turn_id,
        correlation_id="correlation-other",
        reply="상관관계가 다른 결과입니다.",
        valid=True,
    )
    assert gate.next_pending() == turn


def test_duplicate_commit_is_rejected() -> None:
    gate = DialogueTurnGate(epoch=11)
    turn = _enqueue(gate, utterance_id="utterance-1")

    assert gate.claim(
        turn_id=turn.turn_id,
        correlation_id="correlation-1",
    ) == turn
    assert gate.commit(
        turn_id=turn.turn_id,
        correlation_id="correlation-1",
        reply="응답을 한 번만 전달합니다.",
        valid=True,
    )
    assert not gate.commit(
        turn_id=turn.turn_id,
        correlation_id="correlation-1",
        reply="중복 응답입니다.",
        valid=True,
    )
    assert gate.claim(
        turn_id=turn.turn_id,
        correlation_id="correlation-1",
    ) is None


def test_invalid_or_missing_reply_keeps_turn_pending_and_retryable() -> None:
    gate = DialogueTurnGate(epoch=13)
    turn = _enqueue(gate, utterance_id="utterance-1")

    assert gate.claim(
        turn_id=turn.turn_id,
        correlation_id="correlation-invalid",
    ) == turn
    assert not gate.commit(
        turn_id=turn.turn_id,
        correlation_id="correlation-invalid",
        reply="스키마 검증에 실패한 응답",
        valid=False,
    )
    assert gate.next_pending() == turn

    assert gate.claim(
        turn_id=turn.turn_id,
        correlation_id="correlation-empty",
    ) == turn
    assert not gate.commit(
        turn_id=turn.turn_id,
        correlation_id="correlation-empty",
        reply=None,
        valid=True,
    )
    assert gate.next_pending() == turn

    assert gate.claim(
        turn_id=turn.turn_id,
        correlation_id="correlation-retry",
    ) == turn
    assert gate.commit(
        turn_id=turn.turn_id,
        correlation_id="correlation-retry",
        reply="재시도에서 생성된 유효한 응답입니다.",
        valid=True,
    )
    assert gate.next_pending() is None


def test_reset_advances_epoch_and_rejects_stale_claims() -> None:
    gate = DialogueTurnGate(epoch=19)
    stale = _enqueue(gate, utterance_id="utterance-old")
    assert stale.epoch == 19
    assert gate.claim(
        turn_id=stale.turn_id,
        correlation_id="correlation-old",
    ) == stale

    gate.reset(epoch=20)

    assert gate.epoch == 20
    assert gate.next_pending() is None
    assert gate.claim(
        turn_id=stale.turn_id,
        correlation_id="correlation-old",
    ) is None
    assert not gate.commit(
        turn_id=stale.turn_id,
        correlation_id="correlation-old",
        reply="리셋 이전의 지연된 응답입니다.",
        valid=True,
    )

    current = _enqueue(gate, utterance_id="utterance-new")
    assert current.epoch == 20
    assert current.turn_id != stale.turn_id
    assert gate.next_pending() == current
