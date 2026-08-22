#!/usr/bin/env python3
"""CPU-only compatibility gate for the unified perception environment.

The default check never instantiates a model and never enables CUDA.  Optional
``--checkpoint NAME:KIND:PATH`` arguments load a mounted RF-DETR checkpoint on
CPU without running inference; KIND is ``small`` or ``seg``.
"""

from __future__ import annotations

import argparse
import ast
import gc
from importlib import metadata, util
import inspect
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


# Set this before importing torch (directly or through RF-DETR).  The checker
# must be safe to run on the live Taskplanner host without reserving VRAM.
os.environ["CUDA_VISIBLE_DEVICES"] = ""


UPSTREAM_COMMIT = "0f9e93115b8cc1d470398c92e010e3fc6ef1de5d"
EXPECTED_DISTRIBUTIONS = {
    "fastapi": "0.141.1",
    "mediapipe": "0.10.18",
    "numpy": "1.26.4",
    "opencv-contrib-python": "4.11.0.86",
    "rfdetr": "1.9.0",
    "supervision": "0.29.1",
    "torch": "2.8.0",
    "torchvision": "0.23.0",
    "uvicorn": "0.52.0",
}
OPENCV_DISTRIBUTIONS = {
    "opencv-contrib-python",
    "opencv-contrib-python-headless",
    "opencv-python",
    "opencv-python-headless",
}


class CompatibilityError(RuntimeError):
    """Raised when an environment violates the unified profile."""


def _normalized_distribution_names() -> dict[str, str]:
    result: dict[str, str] = {}
    for distribution in metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            continue
        normalized = raw_name.lower().replace("_", "-")
        result[normalized] = distribution.version
    return result


def _base_version(version: str) -> str:
    return version.split("+", 1)[0]


def _check_versions() -> dict[str, str]:
    installed = _normalized_distribution_names()
    actual: dict[str, str] = {}
    for name, expected in EXPECTED_DISTRIBUTIONS.items():
        version = installed.get(name)
        if version is None:
            raise CompatibilityError(f"missing required distribution: {name}")
        if _base_version(version) != expected:
            raise CompatibilityError(
                f"{name} version drift: expected {expected}, found {version}"
            )
        actual[name] = version

    providers = sorted(OPENCV_DISTRIBUTIONS.intersection(installed))
    if providers != ["opencv-contrib-python"]:
        raise CompatibilityError(
            "exactly opencv-contrib-python must provide cv2; found "
            + (", ".join(providers) if providers else "none")
        )
    return actual


def _check_pip_metadata() -> list[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
    )
    lines = [
        line.strip()
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip() and line.strip() != "No broken requirements found."
    ]
    allowed: list[str] = []
    unexpected: list[str] = []
    for line in lines:
        normalized = line.lower()
        if (
            "supervision 0.29.1" in normalized
            and "opencv-python" in normalized
            and "not installed" in normalized
        ):
            allowed.append(line)
        else:
            unexpected.append(line)
    if unexpected:
        raise CompatibilityError(
            "unexpected pip metadata errors: " + " | ".join(unexpected)
        )
    if completed.returncode and not allowed:
        raise CompatibilityError(
            f"pip check failed without the documented OpenCV metadata gap "
            f"(exit {completed.returncode})"
        )
    return allowed


def _check_imports(*, import_taskplanner_service: bool) -> dict[str, Any]:
    import cv2
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions, vision
    from rfdetr import RFDETRSegSmall, RFDETRSmall
    import supervision as sv
    import torch

    if int(np.__version__.split(".", 1)[0]) >= 2:
        raise CompatibilityError(f"MediaPipe requires NumPy <2, found {np.__version__}")
    if cv2.__version__ != "4.11.0":
        raise CompatibilityError(f"unexpected cv2 runtime: {cv2.__version__}")
    if torch.version.cuda != "12.9":
        raise CompatibilityError(
            f"Taskplanner CUDA baseline drift: expected 12.9, found {torch.version.cuda}"
        )
    if torch.cuda.is_available():
        raise CompatibilityError("CPU-only checker unexpectedly sees a CUDA device")

    for model_class in (RFDETRSmall, RFDETRSegSmall):
        for method_name, required_parameters in {
            "from_checkpoint": {"path"},
            "predict": {"images", "threshold", "include_source_image"},
            "optimize_for_inference": {
                "compile",
                "batch_size",
                "dtype",
                "inplace",
            },
        }.items():
            method = getattr(model_class, method_name, None)
            if method is None:
                raise CompatibilityError(
                    f"RF-DETR API missing: {model_class.__name__}.{method_name}"
                )
            parameters = set(inspect.signature(method).parameters)
            missing = required_parameters.difference(parameters)
            if missing:
                raise CompatibilityError(
                    f"RF-DETR API drift in {model_class.__name__}.{method_name}: "
                    f"missing {sorted(missing)}"
                )

    # Exercise the shared image/observation primitives without model inference.
    image = np.zeros((12, 16, 3), dtype=np.uint8)
    image[2:6, 3:9] = (5, 120, 240)
    ok, encoded = cv2.imencode(".png", image)
    decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR) if ok else None
    if decoded is None or not np.array_equal(image, decoded):
        raise CompatibilityError("OpenCV encode/decode smoke test failed")
    detections = sv.Detections(
        xyxy=np.array([[3.0, 2.0, 9.0, 6.0]], dtype=np.float32),
        confidence=np.array([0.9], dtype=np.float32),
        class_id=np.array([0], dtype=int),
    )
    if len(detections) != 1:
        raise CompatibilityError("supervision Detections smoke test failed")
    if not all((mp.Image, BaseOptions, vision.HandLandmarker)):
        raise CompatibilityError("MediaPipe Tasks Hand Landmarker API missing")

    service_imported = False
    if import_taskplanner_service:
        from vlm_node import rfdetr_service

        encoded_jpeg = rfdetr_service._encode_jpeg(image, 90)
        if not encoded_jpeg:
            raise CompatibilityError("Taskplanner RF-DETR image helper failed")
        service_imported = True

    return {
        "cv2": cv2.__version__,
        "numpy": np.__version__,
        "torch": torch.__version__,
        "torch_compiled_cuda": torch.version.cuda,
        "rfdetr_classes": [RFDETRSmall.__name__, RFDETRSegSmall.__name__],
        "mediapipe_hand_api": True,
        "taskplanner_service_imported": service_imported,
    }


def _module_from_path(name: str, path: Path):
    spec = util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise CompatibilityError(f"cannot import {path}")
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _top_level_import_roots(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def _read_git_head(root: Path) -> str | None:
    """Read HEAD without requiring git in the lean runtime image."""
    dot_git = root / ".git"
    if dot_git.is_file():
        marker = dot_git.read_text(encoding="utf-8").strip()
        if not marker.startswith("gitdir: "):
            raise CompatibilityError(f"unrecognised gitdir marker: {dot_git}")
        dot_git = (root / marker.removeprefix("gitdir: ")).resolve()
    if not dot_git.is_dir():
        return None
    head = (dot_git / "HEAD").read_text(encoding="utf-8").strip()
    if not head.startswith("ref: "):
        return head
    reference = head.removeprefix("ref: ")
    loose_ref = dot_git / reference
    if loose_ref.is_file():
        return loose_ref.read_text(encoding="utf-8").strip()
    packed_refs = dot_git / "packed-refs"
    if packed_refs.is_file():
        for line in packed_refs.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            commit, name = line.split(" ", 1)
            if name == reference:
                return commit
    raise CompatibilityError(f"cannot resolve git reference {reference} in {dot_git}")


def _check_upstream(upstream_root: Path, expected_commit: str) -> dict[str, Any]:
    root = upstream_root.resolve()
    if not root.is_dir():
        raise CompatibilityError(f"upstream root does not exist: {root}")
    if not (root / ".git").exists():
        raise CompatibilityError(
            f"cannot verify upstream commit because {root / '.git'} is missing"
        )
    if shutil.which("git"):
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        actual_commit = completed.stdout.strip()
    else:
        actual_commit = _read_git_head(root)
    if actual_commit != expected_commit:
        raise CompatibilityError(
            f"upstream commit drift: expected {expected_commit}, found {actual_commit}"
        )

    hand_core = root / (
        "components/hand_keypoints_ros/ros2_ws/src/hand_keypoint_ros/"
        "hand_keypoint_ros/core.py"
    )
    hand_imports = _top_level_import_roots(hand_core)
    forbidden_hand_imports = {"torch", "transformers"}.intersection(hand_imports)
    if forbidden_hand_imports:
        raise CompatibilityError(
            "Hand mono-depth dependencies became eager imports: "
            + ", ".join(sorted(forbidden_hand_imports))
        )
    hand_module = _module_from_path("_pnu_hand_core_compat", hand_core)
    depth, valid = hand_module.sample_depth_batch(
        __import__("numpy").full((8, 8), 0.42, dtype="float32"),
        __import__("numpy").array([[4.0, 4.0]], dtype="float32"),
        win=1,
    )
    if not bool(valid[0]) or abs(float(depth[0]) - 0.42) > 1e-5:
        raise CompatibilityError("Hand real-depth helper smoke test failed")

    tool_src = root / "components/tool_runtime_v1_6/algorithm/src"
    sys.path.insert(0, str(tool_src))
    try:
        from pnu_surgical_tool.rfdetr_inference import (
            DetectorConfig,
            class_agnostic_nms_indices,
        )

        config = DetectorConfig(
            checkpoint_path="/nonexistent/tool.pth",
            ontology_path=root
            / "components/tool_runtime_v1_6/algorithm/model/ontology.json",
            optimize=False,
        )
        if config.optimize:
            raise CompatibilityError("Tool CPU smoke configuration was not retained")
        keep = class_agnostic_nms_indices(
            __import__("numpy").array([[0, 0, 4, 4], [0, 0, 4, 4]], dtype=float),
            __import__("numpy").array([0.9, 0.8], dtype=float),
            0.5,
        )
        if keep.tolist() != [0]:
            raise CompatibilityError("Tool NMS helper smoke test failed")
    finally:
        sys.path.remove(str(tool_src))

    blood_path = root / "components/blood_detection/offline_blood_segmentation.py"
    blood_module = _module_from_path("_pnu_blood_compat", blood_path)
    mask = __import__("numpy").zeros((5, 5), dtype=bool)
    mask[1:3, 2:4] = True
    if blood_module.centroid(mask) != [2.5, 1.5]:
        raise CompatibilityError("Blood mask helper smoke test failed")

    return {
        "root": str(root),
        "expected_commit": expected_commit,
        "actual_commit": actual_commit,
        "hand_top_level_imports": sorted(hand_imports),
        "hand_mono_depth_lazy": True,
        "tool_core_imported": True,
        "blood_core_imported": True,
    }


def _parse_checkpoint(value: str) -> tuple[str, str, Path]:
    try:
        name, kind, raw_path = value.split(":", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "checkpoint must be NAME:KIND:PATH where KIND is small or seg"
        ) from exc
    if not name or kind not in {"small", "seg"} or not raw_path:
        raise argparse.ArgumentTypeError(
            "checkpoint must be NAME:KIND:PATH where KIND is small or seg"
        )
    return name, kind, Path(raw_path)


def _load_checkpoints_cpu(
    checkpoints: list[tuple[str, str, Path]],
) -> list[dict[str, Any]]:
    from rfdetr import RFDETRSegSmall, RFDETRSmall
    import torch

    if torch.cuda.is_available():
        raise CompatibilityError("checkpoint gate must not see CUDA")
    results: list[dict[str, Any]] = []
    for name, kind, path in checkpoints:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise CompatibilityError(f"checkpoint not found: {resolved}")
        model_class = RFDETRSegSmall if kind == "seg" else RFDETRSmall
        model = model_class.from_checkpoint(str(resolved))
        results.append(
            {
                "name": name,
                "kind": kind,
                "path": str(resolved),
                "bytes": resolved.stat().st_size,
                "loaded_class": type(model).__name__,
            }
        )
        del model
        gc.collect()
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("unified",), default="unified")
    parser.add_argument("--import-taskplanner-service", action="store_true")
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--expected-upstream-commit", default=UPSTREAM_COMMIT)
    parser.add_argument(
        "--checkpoint",
        action="append",
        default=[],
        type=_parse_checkpoint,
        metavar="NAME:KIND:PATH",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result: dict[str, Any] = {
        "schema": "taskplanner.perception_compatibility.v1",
        "profile": args.profile,
        "status": "pass",
        "gpu_inference": False,
        "versions": _check_versions(),
        "known_metadata_gaps": _check_pip_metadata(),
        "imports": _check_imports(
            import_taskplanner_service=args.import_taskplanner_service
        ),
        "upstream": None,
        "checkpoints": [],
    }
    if args.upstream_root is not None:
        result["upstream"] = _check_upstream(
            args.upstream_root, args.expected_upstream_commit
        )
    if args.checkpoint:
        result["checkpoints"] = _load_checkpoints_cpu(args.checkpoint)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except CompatibilityError as exc:
        print(f"compatibility check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
