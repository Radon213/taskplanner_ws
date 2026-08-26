from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "ninfer_catalog_builder",
    ROOT / "docker" / "ninfer-manager" / "build_catalog.py",
)
assert SPEC is not None and SPEC.loader is not None
catalog_builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = catalog_builder
SPEC.loader.exec_module(catalog_builder)


def test_catalog_exposes_only_reviewed_35b_model(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    model_root = runtime_root / "models"
    model_root.mkdir(parents=True)
    reviewed = model_root / "qwen3_6_35b_a3b.ninfer"
    reviewed.write_bytes(b"reviewed")

    # A stale 27B artifact and its old environment variables must not create a
    # second catalog row or offer an alternate load target in any NInfer mode.
    legacy = model_root / "qwen3_6_27b.ninfer"
    legacy.write_bytes(b"legacy")
    monkeypatch.setenv("NINFER_27B_ARTIFACT_REL", "models/qwen3_6_27b.ninfer")
    monkeypatch.setenv("NINFER_27B_MAX_CONTEXT", "32768")

    payload = catalog_builder.build_catalog(runtime_root=runtime_root)

    assert [row["id"] for row in payload["models"]] == ["qwen3.6-35b-a3b"]
    assert payload["models"][0]["artifact_path"] == str(reviewed)


def test_catalog_fails_closed_when_reviewed_artifact_is_missing(tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    (runtime_root / "models").mkdir(parents=True)
    (runtime_root / "models" / "qwen3_6_27b.ninfer").write_bytes(b"legacy")

    assert catalog_builder.build_catalog(runtime_root=runtime_root) == {
        "models": []
    }
