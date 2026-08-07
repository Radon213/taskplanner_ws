from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_shadow_campaign",
    ROOT / "scripts" / "release_shadow_campaign.py",
)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


def test_case_command_is_strict_reproducible_and_isolated(tmp_path):
    args = argparse.Namespace(
        annotation_root=tmp_path / "annotations",
        dataset_root=tmp_path / "bags",
        output_dir=tmp_path / "output",
        bundle="thyroidectomy_demo",
        provider_id="ninfer",
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        api_mode="openai_compat",
        publish_period_sec=1.0,
        response_format="json_schema",
        reasoning_effort="none",
        vlm_task_profile="tool_forecast_only",
        max_output_tokens=320,
        seed=0,
        request_timeout_sec=60.0,
        retry_count=1,
        vlm_wait_timeout_sec=130.0,
        rate=1.0,
        replay_mode="elastic_demo",
        ros_domain_base=193,
        groot2_port_base=20193,
        rosbridge_port_base=9293,
        fault_scenario_path=None,
    )

    command = campaign.build_case_command(args, "0704_6", 2)

    assert command[command.index("--mode") + 1] == "strict"
    assert command[command.index("--ros-domain-id") + 1] == "195"
    assert command[command.index("--groot2-port") + 1] == "20195"
    assert command[command.index("--rosbridge-port") + 1] == "9295"
    assert command[command.index("--vlm-generation-seed") + 1] == "0"
    assert command[command.index("--vlm-task-profile") + 1] == "tool_forecast_only"
    assert command[command.index("--vlm-request-timeout-sec") + 1] == "60.0"
    assert command[command.index("--replay-vlm-health-timeout-sec") + 1] == "130.0"
    assert command[command.index("--replay-vlm-wait-timeout-sec") + 1] == "130.0"
    assert "--score-provisional-phase" in command
    assert "--counterfactual-feedback" in command
    assert "--type-instance-assumption" in command


def test_case_command_expands_wait_timeout_to_cover_retries(tmp_path):
    args = argparse.Namespace(
        annotation_root=tmp_path / "annotations",
        dataset_root=tmp_path / "bags",
        output_dir=tmp_path / "output",
        bundle="thyroidectomy_demo",
        provider_id="ninfer",
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        api_mode="openai_compat",
        publish_period_sec=1.0,
        response_format="json_schema",
        reasoning_effort="none",
        max_output_tokens=320,
        seed=0,
        request_timeout_sec=60.0,
        retry_count=2,
        vlm_wait_timeout_sec=20.0,
        rate=1.0,
        replay_mode="elastic_demo",
        ros_domain_base=193,
        groot2_port_base=20193,
        rosbridge_port_base=9293,
        fault_scenario_path=None,
    )

    command = campaign.build_case_command(args, "0704_6", 0)

    assert command[command.index("--replay-vlm-health-timeout-sec") + 1] == "60.0"
    assert command[command.index("--replay-vlm-wait-timeout-sec") + 1] == "185.0"
    assert command[command.index("--replay-drain-timeout-sec") + 1] == "185.0"


def test_case_command_forwards_fault_scenario_without_mutating_bag(tmp_path):
    scenario = tmp_path / "noise.yaml"
    scenario.write_text(
        "schema: taskplanner.fault_scenario.v1\n"
        "scenario_id: noise\nseed: 4\nevents: []\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        annotation_root=tmp_path / "annotations",
        dataset_root=tmp_path / "bags",
        output_dir=tmp_path / "output",
        bundle="thyroidectomy_demo",
        provider_id="ninfer",
        base_url="http://127.0.0.1:8080",
        model_id="qwen3.6-35b-a3b",
        api_mode="openai_compat",
        publish_period_sec=1.0,
        response_format="json_schema",
        reasoning_effort="none",
        max_output_tokens=320,
        seed=0,
        request_timeout_sec=60.0,
        retry_count=1,
        vlm_wait_timeout_sec=130.0,
        rate=1.0,
        replay_mode="realtime_1x",
        ros_domain_base=193,
        groot2_port_base=20193,
        rosbridge_port_base=9293,
        fault_scenario_path=scenario,
    )

    command = campaign.build_case_command(args, "0704_6", 0)

    assert command[command.index("--fault-scenario-path") + 1] == str(scenario)
    assert command[command.index("--source-bag") + 1] == str(
        args.dataset_root / "0704_6"
    )


def test_workspace_environment_is_loaded_without_caller_shell_state(tmp_path):
    setup = tmp_path / "setup.bash"
    setup.write_text(
        "export AMENT_PREFIX_PATH=/tmp/release-prefix\n"
        "export TASKPLANNER_RELEASE_PROBE=ready\n",
        encoding="utf-8",
    )

    environment = campaign.load_workspace_environment(
        setup,
        base_environment={"PATH": "/usr/bin:/bin"},
        required_package=None,
    )

    assert environment["AMENT_PREFIX_PATH"] == "/tmp/release-prefix"
    assert environment["TASKPLANNER_RELEASE_PROBE"] == "ready"
