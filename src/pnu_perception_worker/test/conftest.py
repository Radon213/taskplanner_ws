from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from pnu_perception_worker.adapters import AdapterIdentity
from pnu_perception_worker.config import WorkerConfig
from pnu_perception_worker.engine import PerceptionEngine


@pytest.fixture
def config(tmp_path: Path) -> WorkerConfig:
    return WorkerConfig(
        upstream_root=tmp_path / "upstream",
        expected_upstream_commit="0" * 40,
        model_root=tmp_path / "models",
        tool_checkpoint=tmp_path / "models/tool.pth",
        blood_checkpoint=tmp_path / "models/blood.pth",
        hand_model=tmp_path / "models/hand_landmarker.task",
        tool_ontology=tmp_path / "upstream/ontology.json",
        device_policy="allow_cpu",
        optimize_rfdetr=False,
    )


class EmptyAdapter:
    def __init__(self, name: str) -> None:
        self.identity = AdapterIdentity(
            name=name,
            backend="mock-cpu",
            version=f"{name}-test-v1",
            digest_sha256=(name[0] * 64),
        )
        self.name = name
        self.calls = 0

    def infer(self, frame_bgr: np.ndarray, request: Any) -> dict[str, Any]:
        self.calls += 1
        if self.name == "hand":
            return {"schema": "pnu.hand.2d.v1", "hands": []}
        return {"schema": f"pnu.{self.name}.2d.v1", "detections": []}


@pytest.fixture
def ready_engine(config: WorkerConfig) -> PerceptionEngine:
    return PerceptionEngine(
        config,
        upstream_revision="0" * 40,
        adapters={name: EmptyAdapter(name) for name in ("tool", "blood", "hand")},
    )


@pytest.fixture
def jpeg_bytes() -> bytes:
    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    frame[4:16, 6:20] = (10, 120, 240)
    ok, encoded = cv2.imencode(".jpg", frame)
    assert ok
    return encoded.tobytes()


@pytest.fixture
def compressed_depth_bytes() -> bytes:
    depth = np.full((24, 32), 800, dtype=np.uint16)
    ok, encoded = cv2.imencode(".png", depth)
    assert ok
    # ROS compressed_depth_image_transport prepends a codec header.
    return b"\x00" * 12 + encoded.tobytes()


def metadata(
    *,
    algorithms: list[str] | None = None,
    depth: bool = False,
    deadline_offset_ms: int = 5_000,
) -> dict[str, Any]:
    source: dict[str, Any] = {
        "rgb": {
            "stamp_ns": 1_725_000_123_456_789_000,
            "frame_id": "cam_4_color_optical_frame",
            "format": "jpeg",
        }
    }
    if depth:
        source["depth"] = {
            "stamp_ns": 1_725_000_123_450_000_000,
            "frame_id": "cam_4_depth_optical_frame",
            "format": "16UC1; compressedDepth png",
            "aligned": False,
        }
    return {
        "schema": "taskplanner.pnu_perception.request.v1",
        "request_id": "cam4-000001",
        "source": source,
        "requested_algorithms": algorithms or ["tool", "blood", "hand"],
        "deadline_unix_ms": int(time.time() * 1000) + deadline_offset_ms,
    }
