from __future__ import annotations

import os
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"


def _services() -> dict[str, object]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))["services"]


def test_live_runtime_guard_rejects_stale_model_environment(tmp_path) -> None:
    command = _services()["taskplanner-runtime"]["command"]
    for provider_id, model_id in (
        ("vllm", "qwen3.6-35b-a3b"),
        ("ninfer", "unsloth/legacy-model"),
    ):
        environment = {
            **os.environ,
            "TASKPLANNER_RUNTIME_MODE": "live",
            "INPUT_PROFILE": "external",
            "EXECUTION_BACKEND": "action",
            "VLM_PROVIDER_ID": provider_id,
            "VLM_MODEL_ID": model_id,
        }

        # Run only the guard from a directory without install/docker. Even if
        # the model guard regresses, the install check prevents a ROS launch.
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=tmp_path,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 2
        assert (
            "Live model contract mismatch; require ninfer/qwen3.6-35b-a3b"
            in completed.stderr
        )


def test_replay_shadow_defaults_to_ninfer_without_lab_dependency() -> None:
    services = _services()
    shadow = services["shadow-runner"]
    replay_environment = (
        ROOT / "docker" / "orchestration" / "replay.env"
    ).read_text(encoding="utf-8")

    assert services["vllm-manager"]["profiles"] == ["lab"]
    assert set(shadow["depends_on"]) == {"ninfer-manager", "webapp"}
    assert shadow["environment"]["VLM_BASE_URL"].endswith(
        ":-http://127.0.0.1:8080}"
    )
    assert shadow["environment"]["VLM_PROVIDER_ID"].endswith(":-ninfer}")
    assert shadow["environment"]["VLM_MODEL_ID"].endswith(
        ":-qwen3.6-35b-a3b}"
    )
    assert shadow["environment"]["LMSTUDIO_PROVIDER_ENABLED"].endswith(
        ":-false}"
    )
    assert shadow["environment"]["UNSLOTH_PROVIDER_ENABLED"].endswith(
        ":-false}"
    )
    assert shadow["environment"]["VLLM_PROVIDER_ENABLED"].endswith(
        ":-false}"
    )
    assert shadow["environment"]["NINFER_PROVIDER_ENABLED"].endswith(
        ":-true}"
    )
    for required in (
        "VLM_BASE_URL=http://127.0.0.1:8080",
        "VLM_PROVIDER_ID=ninfer",
        "VLM_MODEL_ID=qwen3.6-35b-a3b",
        "TASKPLANNER_NINFER_AUTOLOAD_MODEL_ID=qwen3.6-35b-a3b",
        "LMSTUDIO_PROVIDER_ENABLED=false",
        "UNSLOTH_PROVIDER_ENABLED=false",
        "VLLM_PROVIDER_ENABLED=false",
        "NINFER_PROVIDER_ENABLED=true",
    ):
        assert required in replay_environment


def test_ninfer_service_has_no_legacy_catalog_inputs() -> None:
    environment = _services()["ninfer-manager"]["environment"]

    assert "NINFER_27B_ARTIFACT_REL" not in environment
    assert "NINFER_27B_MAX_CONTEXT" not in environment
    assert "NINFER_35B_ARTIFACT_REL" in environment
