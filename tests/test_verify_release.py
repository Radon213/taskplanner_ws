from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_release",
    ROOT / "scripts" / "verify_release.py",
)
assert SPEC is not None and SPEC.loader is not None
verify = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify
SPEC.loader.exec_module(verify)


def _config() -> dict:
    return json.loads(
        (ROOT / "config" / "release" / "verification.json").read_text(
            encoding="utf-8"
        )
    )


def test_rc_checks_include_shadow_campaign_and_metric_gate_when_requested(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    annotations = tmp_path / "annotations"
    baseline = tmp_path / "baseline" / "report"
    dataset.mkdir()
    annotations.mkdir()
    baseline.mkdir(parents=True)
    options = {
        "dataset_root": dataset,
        "annotation_root": annotations,
        "baseline_report_dir": baseline,
        "provider_id": "ninfer",
        "base_url": "http://127.0.0.1:8080",
        "model_id": "qwen3.6-35b-a3b",
        "request_timeout_sec": 60.0,
        "retry_count": 1,
        "wait_timeout_sec": 130.0,
        "fault_scenario_path": None,
        "max_regression_pp": 2.0,
        "safety_only": False,
    }

    checks = verify.build_checks(
        config=_config(),
        run_id="test-rc",
        report_dir=tmp_path / "report",
        tier="rc",
        verify_dataset_payloads=False,
        restart_iterations=1,
        soak_hours=0.01,
        shadow_options=options,
    )
    names = [check.name for check in checks]

    assert names.index("shadow_12case_campaign") < names.index("shadow_metric_gate")
    assert "--volume" in checks[names.index("shadow_12case_campaign")].command
    assert str(dataset) in checks[names.index("shadow_12case_campaign")].command
    assert str(baseline) in checks[names.index("shadow_metric_gate")].command
    assert "/release-shadow-output/shadow_campaign" in checks[
        names.index("shadow_12case_campaign")
    ].command
    assert not (tmp_path / "report" / "shadow_campaign").exists()
    assert "restart_and_soak_campaign" not in names


def test_shadow_fault_scenario_is_mounted_and_safety_only_is_forwarded(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    annotations = tmp_path / "annotations"
    baseline = tmp_path / "baseline" / "report"
    scenario = tmp_path / "fault.yaml"
    for path in (dataset, annotations, baseline):
        path.mkdir(parents=True)
    scenario.write_text("schema: taskplanner.fault_scenario.v1\n", encoding="utf-8")
    options = {
        "dataset_root": dataset,
        "annotation_root": annotations,
        "baseline_report_dir": baseline,
        "provider_id": "ninfer",
        "base_url": "http://127.0.0.1:8080",
        "model_id": "qwen3.6-35b-a3b",
        "request_timeout_sec": 60.0,
        "retry_count": 1,
        "wait_timeout_sec": 130.0,
        "fault_scenario_path": scenario,
        "max_regression_pp": 10.0,
        "safety_only": True,
    }

    checks = verify.build_checks(
        config=_config(),
        run_id="test-fault",
        report_dir=tmp_path / "report",
        tier="rc",
        verify_dataset_payloads=False,
        restart_iterations=1,
        soak_hours=0.01,
        shadow_options=options,
    )
    by_name = {check.name: check for check in checks}

    campaign_command = by_name["shadow_12case_campaign"].command
    gate_command = by_name["shadow_metric_gate"].command
    assert f"{scenario}:/release-fault-scenario.yaml:ro" in campaign_command
    assert "--fault-scenario-path /release-fault-scenario.yaml" in campaign_command
    assert "--max-regression-pp 10.0" in gate_command
    assert "--safety-only" in gate_command


def test_full_checks_include_durability_without_loading_a_model(tmp_path: Path) -> None:
    checks = verify.build_checks(
        config=_config(),
        run_id="test-full",
        report_dir=tmp_path / "report",
        tier="full",
        verify_dataset_payloads=False,
        restart_iterations=3,
        soak_hours=0.02,
        shadow_options=None,
    )
    names = [check.name for check in checks]

    assert "restart_and_soak_campaign" in names
    assert "shadow_12case_campaign" not in names
    assert all("manager/load" not in check.command for check in checks)


def test_web_build_installs_locked_dependencies(tmp_path: Path) -> None:
    checks = verify.build_checks(
        config=_config(),
        run_id="test-quick",
        report_dir=tmp_path / "report",
        tier="quick",
        verify_dataset_payloads=False,
        restart_iterations=1,
        soak_hours=0.01,
        shadow_options=None,
    )
    command = {check.name: check.command for check in checks}["web_build"]

    assert command.startswith("npm --prefix webapp ci && ")


def test_run_check_records_external_log_path_without_crashing(tmp_path: Path) -> None:
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    result = verify.run_check(
        verify.CheckSpec(
            name="external-log",
            classification="source",
            command="true",
        ),
        logs_dir,
    )

    assert result.status == "passed"
    assert Path(result.log_path) == logs_dir / "external-log.log"
