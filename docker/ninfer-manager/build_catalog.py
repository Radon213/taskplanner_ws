#!/usr/bin/env python3
"""Build the single-model Production NInfer catalog."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


PRODUCTION_MODEL_ID = "qwen3.6-35b-a3b"
PRODUCTION_MODEL_DISPLAY_NAME = "Qwen3.6 35B A3B"
DEFAULT_ARTIFACT_REL = "models/qwen3_6_35b_a3b.ninfer"


def build_catalog(*, runtime_root: Path) -> dict[str, object]:
    """Return the catalog for the one supported NInfer Production model."""

    artifact = runtime_root / os.environ.get(
        "NINFER_35B_ARTIFACT_REL",
        DEFAULT_ARTIFACT_REL,
    )
    if not artifact.is_file():
        return {"models": []}

    return {
        "models": [
            {
                "id": PRODUCTION_MODEL_ID,
                "display_name": PRODUCTION_MODEL_DISPLAY_NAME,
                "capability": "vision",
                "artifact_path": str(artifact),
                "start_command": [
                    "/usr/local/bin/taskplanner-ninfer-worker",
                    "vision",
                ],
                "environment": {
                    "NINFER_MAX_CONTEXT": os.environ.get(
                        "NINFER_35B_MAX_CONTEXT",
                        "8192",
                    ),
                },
            }
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    runtime_root = Path(
        os.environ.get("NINFER_RUNTIME_ROOT", "/opt/taskplanner/ninfer")
    )
    args.output.write_text(
        json.dumps(
            build_catalog(runtime_root=runtime_root),
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
