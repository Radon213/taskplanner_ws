from copy import deepcopy

import pytest

from retraction_control.runtime_config import RuntimeConfigError, load_runtime_config


def _payload():
    return {
        "schema": "retraction_control.runtime.v1",
        "storage": {
            "data_directory": "/tmp/retraction-control-test",
            "ledger_filename": "ledger.sqlite3",
            "session_directory_name": "sessions",
            "shadow_trace_directory_name": "traces",
            "atomic_fsync": True,
        },
        "publish": {
            "status_period_sec": 0.5,
            "diagnostics_period_sec": 1.0,
        },
    }


def test_runtime_config_is_strict_and_checksum_deterministic():
    first = load_runtime_config(_payload())
    reordered = {
        "publish": deepcopy(_payload()["publish"]),
        "storage": deepcopy(_payload()["storage"]),
        "schema": "retraction_control.runtime.v1",
    }
    second = load_runtime_config(reordered)
    assert first == second
    assert first.checksum.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_directory", "/"),
        ("data_directory", "relative"),
        ("ledger_filename", "../ledger.sqlite3"),
        ("session_directory_name", "sessions/path"),
        ("shadow_trace_directory_name", ""),
        ("atomic_fsync", False),
    ],
)
def test_unsafe_storage_configuration_is_rejected(field, value):
    payload = _payload()
    payload["storage"][field] = value
    with pytest.raises(RuntimeConfigError):
        load_runtime_config(payload)


def test_unknown_runtime_key_is_rejected():
    payload = _payload()
    payload["storage"]["surprise"] = True
    with pytest.raises(RuntimeConfigError, match="unknown keys"):
        load_runtime_config(payload)


def test_data_directory_rejects_symlink_components(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    payload = _payload()
    payload["storage"]["data_directory"] = str(linked / "nested")

    with pytest.raises(RuntimeConfigError, match="symlink"):
        load_runtime_config(payload)
