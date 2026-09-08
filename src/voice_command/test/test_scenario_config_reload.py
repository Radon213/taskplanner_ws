from __future__ import annotations

from pathlib import Path

import pytest

from procedure_spec import ScenarioConfigSnapshot
from voice_command.scenario_reload import (
    scenario_config_apply_is_safe,
    scenario_config_bundle_path,
    scenario_config_reload_is_authorized,
)


@pytest.mark.parametrize(
    ("running", "execution_state", "expected"),
    [
        (True, "paused", True),
        (False, "idle", True),
        (False, "halted", True),
        (False, "completed", True),
        (False, "terminated", True),
        (True, "running", False),
        (False, "starting", False),
        (True, "idle", False),
        (False, "", False),
    ],
)
def test_voice_scenario_reload_accepts_only_paused_or_stopped_runtime(
    running: bool,
    execution_state: str,
    expected: bool,
) -> None:
    assert (
        scenario_config_apply_is_safe(
            running=running,
            execution_state=execution_state,
        )
        is expected
    )


def test_scenario_config_bundle_path_rejects_name_path_mismatch(
    tmp_path: Path,
) -> None:
    snapshot = ScenarioConfigSnapshot(
        bundle_name="other_bundle",
        spec_dir=str(tmp_path / "expected_bundle"),
        revision="sha256:test",
    )

    with pytest.raises(ValueError, match="bundle_name"):
        scenario_config_bundle_path(snapshot)


def test_manual_bundle_reload_is_rejected_before_first_authoritative_state() -> None:
    assert not scenario_config_reload_is_authorized(
        state_received=False,
        running=False,
        execution_state="idle",
    )
    assert scenario_config_reload_is_authorized(
        state_received=True,
        running=False,
        execution_state="idle",
    )
