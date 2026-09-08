import pytest

from integration_debug.capabilities import DebugCapabilities, capability_for_operation


def test_observer_profile_starts_only_the_read_only_owner() -> None:
    capabilities = DebugCapabilities.parse("observer")

    assert capabilities.enabled("observer") is True
    assert capabilities.observer_only is True
    assert all(
        capabilities.enabled(name) is False
        for name in ("control", "asr", "record", "network")
    )
    rows = {row["name"]: row for row in capabilities.status_rows()}
    assert rows["observer"]["restart_scope"] == "debug-observer"
    assert rows["asr"]["state"] == "not_started"


def test_capability_csv_composes_small_independent_owners() -> None:
    capabilities = DebugCapabilities.parse("control,asr,record")

    assert capabilities.enabled_names == frozenset({"observer", "control", "asr", "record"})
    assert capability_for_operation("dispatch") == "control"
    assert capability_for_operation("asr_start") == "asr"
    assert capability_for_operation("record_submit") == "record"
    assert capability_for_operation("ping_host") == "network"


def test_full_profile_has_no_legacy_text_vlm_owner() -> None:
    capabilities = DebugCapabilities.parse("full")

    assert capabilities.enabled_names == frozenset(
        {"observer", "control", "asr", "record", "network"}
    )
    assert all(row["name"] != "vlm" for row in capabilities.status_rows())


@pytest.mark.parametrize("raw", ["", "observer,unknown", "vlm", 42])
def test_capability_parser_rejects_empty_or_unknown_values(raw: object) -> None:
    with pytest.raises(ValueError):
        DebugCapabilities.parse(raw)
