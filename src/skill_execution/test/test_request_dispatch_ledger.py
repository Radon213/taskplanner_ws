from skill_execution.bridge import RequestDispatchLedger


def test_explicit_request_generation_is_consumed_at_most_once():
    ledger = RequestDispatchLedger()

    assert ledger.consume(7)
    assert not ledger.consume(7)
    assert ledger.consume(8)


def test_non_request_commands_are_not_permanently_deduplicated():
    ledger = RequestDispatchLedger()

    assert ledger.consume(0)
    assert ledger.consume(0)


def test_reset_allows_reused_generation_after_runtime_reset():
    ledger = RequestDispatchLedger()

    assert ledger.consume(1)
    assert not ledger.consume(1)
    ledger.clear()
    assert ledger.consume(1)


def test_ledger_is_bounded():
    ledger = RequestDispatchLedger(max_entries=2)

    assert ledger.consume(1)
    assert ledger.consume(2)
    assert ledger.consume(3)
    assert ledger.consume(1)
