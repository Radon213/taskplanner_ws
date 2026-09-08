"""Operational-profile tests for direct owner launches.

The retained `taskplanner_live`/`taskplanner_mock` composite sources are not
installed runtime entrypoints.  Testing them as an operational graph made a
small owner restart depend on every historic node and procedure fixture.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

_SRC_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC_ROOT / "procedure_spec"))
sys.path.insert(0, str(_SRC_ROOT / "simulation_runtime"))
sys.path.insert(0, str(_SRC_ROOT / "bringup"))

from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch_ros.actions import Node

from bringup.runtime_owner_launch import generate_owner_launch_description, node_identity
from bringup.runtime_profile import resolve_runtime_profile


BRINGUP_ROOT = Path(__file__).resolve().parents[1]


def _load_launch(filename: str):
    path = BRINGUP_ROOT / "launch" / filename
    spec = importlib.util.spec_from_file_location(path.stem.replace(".", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_profile_projects_external_contract_into_each_direct_owner() -> None:
    profile = resolve_runtime_profile(
        "live",
        environment={
            "EXECUTION_BACKEND": "action",
            "TASKPLANNER_LIVE_DEFAULT_BUNDLE": "thyroidectomy_demo",
        },
    )
    assert profile.launch_arguments["input_profile"] == "external"
    assert profile.launch_arguments["execution_backend"] == "external"
    assert profile.launch_arguments["default_bundle"] == "thyroidectomy_demo"
    assert profile.arguments_for("command")["enable_rosbridge"] == "false"
    assert profile.arguments_for("operator-bridge")["enable_rosbridge"] == "true"
    assert profile.arguments_for("command")["speech_input_mode"] == (
        "tagged_sentence"
    )
    assert profile.arguments_for("command")["sentence_input_topic"] == (
        "/sensors/surgeon/sentence"
    )


def test_direct_live_owners_do_not_include_a_composite_graph() -> None:
    for owner in (
        "state-core",
        "command",
        "tool-state",
        "perception",
        "cam4-mayo",
        "projection",
        "execution",
        "operator-bridge",
        "scenario",
    ):
        description = generate_owner_launch_description(owner)
        assert not any(
            isinstance(action, IncludeLaunchDescription)
            for action in description.entities
        ), owner


def test_command_owner_preserves_single_router_and_resolver_nodes() -> None:
    description = generate_owner_launch_description("command")
    identities = {
        node_identity(action)
        for action in description.entities
        if type(action) is Node
    }
    assert identities == {
        ("simulation_runtime", "speech_input_adapter", "speech_input_adapter"),
        ("voice_command", "voice_intent_resolver", "voice_command_resolver"),
        ("voice_command", "command_router", "command_router"),
    }


def test_execution_owner_exposes_only_the_stable_proxy_endpoint_names() -> None:
    description = generate_owner_launch_description("execution")
    declarations = {
        action.name
        for action in description.entities
        if isinstance(action, DeclareLaunchArgument)
    }
    assert {
        "execution_route_state_topic",
        "retraction_proxy_service",
        "tool_handover_proxy_action",
        "route_selection_state_path",
        "route_selection_runtime_mode",
    } <= declarations


def test_legacy_composites_remain_source_only_and_are_not_installed() -> None:
    setup_source = (BRINGUP_ROOT / "setup.py").read_text(encoding="utf-8")
    assert "launch/taskplanner_live.launch.py" not in setup_source
    assert "launch/taskplanner_mock.launch.py" not in setup_source
    # Keep the historical files importable during the migration so users get
    # their explicit compatibility behaviour, but do not use them as probes.
    assert _load_launch("taskplanner_live.launch.py").generate_launch_description
    assert _load_launch("taskplanner_mock.launch.py").generate_launch_description
