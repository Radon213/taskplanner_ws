from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "taskplanner_owner_registry.py"
SPEC = importlib.util.spec_from_file_location("taskplanner_owner_registry_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
registry_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = registry_module
SPEC.loader.exec_module(registry_module)


def _registry():
    return registry_module.load_registry(
        ROOT / "config" / "taskplanner_runtime_owners.toml"
    )


def _anchors(registry):
    return registry_module.load_runtime_mode_anchors(
        ROOT / "config" / "taskplanner_runtime_owners.toml", registry
    )


def test_registry_has_canonical_owner_names_and_temporary_cli_aliases() -> None:
    registry = _registry()
    assert tuple(registry) == (
        "state-core",
        "command",
        "tool-state",
        "perception",
        "cam4-mayo",
        "projection",
        "execution",
        "operator-bridge",
        "scenario",
        "simulation-input",
        "asr",
        "tts",
        "surgery-record",
        "rosbag-recorder",
        "debug-observer",
        "debug-control",
        "debug-virtual",
        "surgimate",
    )
    assert registry["state-core"].service_for("live") == "taskplanner-state-core"
    assert registry["state-core"].service_for("replay") == "shadow-runner"
    assert registry_module.resolve_owner(registry, "core") is registry["state-core"]
    assert (
        registry_module.resolve_owner(registry, "bridge")
        is registry["operator-bridge"]
    )
    assert registry["scenario"].service == "taskplanner-scenario"
    assert registry["tts"].service == "taskplanner-tts"
    assert registry["tts"].restart_strategy == "sidecar"
    assert registry["rosbag-recorder"].service == "taskplanner-rosbag-recorder"
    assert registry["rosbag-recorder"].modes == ("live",)
    assert registry["rosbag-recorder"].restart_impact == "observer-only"
    assert registry["surgimate"].service == "taskplanner-surgimate"
    assert registry["surgimate"].modes == ("live", "debug", "replay")
    assert registry["surgimate"].compose_profile == "owners"
    assert registry["surgimate"].restart_strategy == "sidecar"


def test_mode_service_plan_is_canonical_and_excludes_independent_sidecars() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--root",
            str(ROOT),
            "services",
            "--mode",
            "live",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.splitlines() == [
        "taskplanner-state-core",
        "taskplanner-command",
        "taskplanner-tool-state",
        "taskplanner-perception",
        "taskplanner-cam4-mayo",
        "taskplanner-projection",
        "taskplanner-execution",
        "taskplanner-operator-bridge",
        "taskplanner-scenario",
        "taskplanner-surgery-record",
        "taskplanner-rosbag-recorder",
        "taskplanner-debug-observer",
    ]
    assert "taskplanner-surgimate" not in completed.stdout


def test_runtime_mode_anchors_are_toml_defined_services() -> None:
    anchors = _anchors(_registry())
    assert anchors["live"].name == "state-core"
    assert anchors["replay"].service_for("replay") == "shadow-runner"
    assert anchors["debug"].name == "debug-observer"
    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--root",
            str(ROOT),
            "anchor",
            "--mode",
            "debug",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == (
        "debug-observer|taskplanner-debug-observer|owners|dedicated"
    )


def test_optional_debug_virtual_owner_is_registry_selected_not_hardcoded(
    tmp_path: Path,
) -> None:
    enabled = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--root",
            str(ROOT),
            "services",
            "--mode",
            "debug",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT": "true"},
    )
    assert enabled.stdout.splitlines() == [
        "taskplanner-debug-observer",
        "taskplanner-debug-control",
        "taskplanner-debug-virtual",
    ]

    disabled_env = tmp_path / "debug-disabled.env"
    disabled_env.write_text("TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT=false\n", encoding="utf-8")
    disabled = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--root",
            str(ROOT),
            "--env-file",
            str(disabled_env),
            "services",
            "--mode",
            "debug",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT"},
    )
    assert disabled.stdout.splitlines() == [
        "taskplanner-debug-observer",
        "taskplanner-debug-control",
    ]


def test_state_reports_only_the_dedicated_owner_service() -> None:
    registry = _registry()

    def dedicated(_root: Path, service: str) -> tuple[str, str]:
        if service == "taskplanner-command":
            return "running", "Up 1 second"
        return "", ""

    state = registry_module.project_owner_state(
        ROOT, registry, registry["command"], "live", state_lookup=dedicated
    )
    assert (state.state, state.service) == ("running", "taskplanner-command")

    state = registry_module.project_owner_state(
        ROOT,
        registry,
        registry["command"],
        "live",
        state_lookup=lambda _root, _service: ("", ""),
    )
    assert (state.state, state.service) == ("not-deployed", "taskplanner-command")
    assert state.detail == "no owner container"


def test_surgimate_sidecar_state_is_projected_without_entering_core_plan() -> None:
    registry = _registry()
    running = registry_module.project_owner_state(
        ROOT,
        registry,
        registry["surgimate"],
        "debug",
        state_lookup=lambda _root, service: ("running", f"{service} Up"),
    )
    assert (running.state, running.service) == ("running", "taskplanner-surgimate")

    missing = registry_module.project_owner_state(
        ROOT,
        registry,
        registry["surgimate"],
        "debug",
        state_lookup=lambda _root, _service: ("", ""),
    )
    assert (missing.state, missing.service) == ("not-deployed", "taskplanner-surgimate")


def test_json_paths_work_with_slotted_dataclasses() -> None:
    listed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--root",
            str(ROOT),
            "list",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(listed.stdout)
    assert payload["scenario"]["service"] == "taskplanner-scenario"
    assert payload["debug-observer"]["restart_impact"] == "observer-only"
    assert payload["debug-virtual"]["enabled_when_env"] == (
        "TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT"
    )


def test_unsupported_owner_mode_is_explicit() -> None:
    registry = _registry()
    state = registry_module.project_owner_state(
        ROOT,
        registry,
        registry["simulation-input"],
        "live",
        state_lookup=lambda _root, _service: ("", ""),
    )
    assert state.state == "not-applicable"
    assert state.service == ""

    for mode in ("debug", "llm-surgeon", "replay"):
        record_state = registry_module.project_owner_state(
            ROOT,
            registry,
            registry["surgery-record"],
            mode,
            state_lookup=lambda _root, _service: ("", ""),
        )
        assert record_state.state == "not-applicable"
        assert record_state.service == ""


def test_disabled_optional_owner_is_not_projected_as_missing() -> None:
    registry = _registry()
    state = registry_module.project_owner_state(
        ROOT,
        registry,
        registry["debug-virtual"],
        "debug",
        environment={"TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT": "false"},
        state_lookup=lambda _root, _service: (_ for _ in ()).throw(
            AssertionError("disabled owner must not query Docker")
        ),
    )
    assert (state.state, state.service) == ("disabled", "taskplanner-debug-virtual")


def test_batch_service_projection_uses_one_docker_read_and_keeps_conflicts_visible() -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "taskplanner-command\trunning\tUp 2 seconds\n"
                "taskplanner-command\texited\tExited (1)\n"
                "taskplanner-scenario\trunning\tUp 1 second\n"
                "unrelated\trunning\tUp 1 hour\n"
            ),
            stderr="",
        )

    states = registry_module.docker_service_states(
        ROOT,
        ["taskplanner-command", "taskplanner-scenario", "missing"],
        run=run,
    )

    assert len(calls) == 1
    assert states["taskplanner-command"] == ("conflict", "2 containers")
    assert states["taskplanner-scenario"] == ("running", "Up 1 second")
    assert states["missing"] == ("", "")
