from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
WORKSPACE_ENTRYPOINT = "/workspaces/taskplanner_ws/docker/entrypoint.sh"

ROS_WORKSPACE_SERVICES = {
    "taskplanner-dev",
    "shadow-runner",
    "taskplanner-asr",
    "taskplanner-tts",
    "taskplanner-runtime",
    "public-rosbridge",
    "multicam-observer",
    "integration-debug",
    "monitor-media-gateway",
}

NON_ROS_SERVICES = {
    "webapp",
    "webapp-lan-proxy",
    "integration-debug-lan-proxy",
    "public-rosbridge-lan-proxy",
    "integration-debug-tailscale-proxy",
}


def _resolved_services() -> dict[str, dict[str, object]]:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "*",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    config = json.loads(completed.stdout)
    return config["services"]


def test_ros_workspace_services_use_bind_mounted_entrypoint() -> None:
    services = _resolved_services()

    for service_name in sorted(ROS_WORKSPACE_SERVICES):
        assert services[service_name]["entrypoint"] == [WORKSPACE_ENTRYPOINT]


def test_non_ros_services_do_not_inherit_image_entrypoint() -> None:
    services = _resolved_services()

    for service_name in sorted(NON_ROS_SERVICES):
        assert services[service_name]["entrypoint"] == []


def test_workspace_entrypoint_uses_isolated_container_install() -> None:
    entrypoint_path = ROOT / "docker" / "entrypoint.sh"
    source = entrypoint_path.read_text(encoding="utf-8")
    collapsed_source = " ".join(source.replace("\\\n", " ").split())
    container_setup = "/workspaces/taskplanner_ws/install/docker/setup.bash"
    host_setup = "/workspaces/taskplanner_ws/install/setup.bash"
    guarded_setup = (
        'if [[ "${TASKPLANNER_SKIP_WORKSPACE_SETUP:-false}" != "true" '
        f"&& -f {container_setup} ]]; then source {container_setup} fi"
    )

    assert COMPOSE_PATH.is_file()
    assert entrypoint_path.stat().st_mode & stat.S_IXUSR
    assert os.access(entrypoint_path, os.X_OK)
    assert guarded_setup in collapsed_source
    assert host_setup not in source
