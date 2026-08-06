from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "ninfer_runtime_manager",
    ROOT / "scripts" / "ninfer_runtime_manager.py",
)
assert SPEC is not None and SPEC.loader is not None
manager_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manager_module
SPEC.loader.exec_module(manager_module)


def wait_for_state(manager, model_id: str, expected: str) -> None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        state = next(
            row["state"]
            for row in manager.status_payload()["models"]
            if row["id"] == model_id
        )
        if state == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"{model_id} did not reach {expected}")


def test_catalog_starts_unloaded_and_does_not_spawn(tmp_path):
    artifact = tmp_path / "model.ninfer"
    artifact.write_bytes(b"ninfer")
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "qwen-vlm",
                        "display_name": "Qwen VLM",
                        "capability": "vision",
                        "artifact_path": str(artifact),
                        "start_command": [
                            "ninfer-serve",
                            "{artifact}",
                            "--port",
                            "{worker_port}",
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    models = manager_module._load_catalog(str(catalog))
    manager = manager_module.NInferRuntimeManager(
        models=models,
        worker_host="127.0.0.1",
        worker_port=8082,
        startup_timeout_sec=1.0,
        shutdown_timeout_sec=1.0,
        api_key="",
    )

    row = manager.model_catalog_payload()["data"][0]
    assert row["id"] == "qwen-vlm"
    assert row["load_state"] == "unloaded"
    assert row["loaded"] is False
    assert row["installed"] is True
    assert manager._processes == {}


def test_catalog_expands_environment_in_worker_settings(monkeypatch, tmp_path):
    artifact = tmp_path / "model.ninfer"
    artifact.write_bytes(b"ninfer")
    monkeypatch.setenv("NINFER_TEST_CONTEXT", "8192")
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "qwen-vlm",
                        "artifact_path": str(artifact),
                        "start_command": ["ninfer-serve", "{artifact}"],
                        "environment": {
                            "NINFER_MAX_CONTEXT": "${NINFER_TEST_CONTEXT}"
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    model = manager_module._load_catalog(str(catalog))[0]

    assert model.environment["NINFER_MAX_CONTEXT"] == "8192"


def test_explicit_load_starts_worker_and_explicit_unload_stops_it(
    monkeypatch,
    tmp_path,
):
    artifact = tmp_path / "model.ninfer"
    artifact.write_bytes(b"ninfer")
    model = manager_module.ModelSpec(
        model_id="qwen-vlm",
        display_name="Qwen VLM",
        capability="vision",
        artifact_path=str(artifact),
        start_command=(
            "ninfer-serve",
            "{artifact}",
            "--port={worker_port}",
        ),
    )
    started = {}

    class FakeProcess:
        return_code = None

        def poll(self):
            return self.return_code

        def terminate(self):
            self.return_code = 0

        def wait(self, timeout):
            del timeout
            return self.return_code

        def kill(self):
            self.return_code = -9

    def fake_popen(command, **kwargs):
        started["command"] = command
        started["environment"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(
        manager_module.subprocess,
        "Popen",
        fake_popen,
    )
    manager = manager_module.NInferRuntimeManager(
        models=(model,),
        worker_host="127.0.0.1",
        worker_port=8082,
        startup_timeout_sec=1.0,
        shutdown_timeout_sec=1.0,
        api_key="",
    )
    monkeypatch.setattr(
        manager,
        "_worker_is_ready",
        lambda _model_id: True,
    )

    status, payload = manager.request_load("qwen-vlm")

    assert status == 202
    assert payload["state"] == "loading"
    wait_for_state(manager, "qwen-vlm", "loaded")
    assert started["command"] == [
        "ninfer-serve",
        str(artifact),
        "--port=8082",
    ]
    assert started["environment"]["NINFER_PORT"] == "8082"

    status, payload = manager.request_unload("qwen-vlm")

    assert status == 202
    assert payload["state"] == "unloading"
    wait_for_state(manager, "qwen-vlm", "unloaded")
