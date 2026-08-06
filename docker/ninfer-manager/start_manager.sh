#!/usr/bin/env bash
set -euo pipefail

catalog_path="/tmp/taskplanner-ninfer-models.json"

python3 - "${catalog_path}" <<'PY'
import json
import os
from pathlib import Path
import sys

catalog_path = Path(sys.argv[1])
runtime_root = Path(
    os.environ.get("NINFER_RUNTIME_ROOT", "/opt/taskplanner/ninfer")
)

known_models = (
    {
        "id": "qwen3.6-35b-a3b",
        "display_name": "Qwen3.6 35B A3B",
        "relative_path": os.environ.get(
            "NINFER_35B_ARTIFACT_REL",
            "models/qwen3_6_35b_a3b.ninfer",
        ),
        "max_context": os.environ.get("NINFER_35B_MAX_CONTEXT", "8192"),
    },
    {
        "id": "qwen3.6-27b",
        "display_name": "Qwen3.6 27B",
        "relative_path": os.environ.get(
            "NINFER_27B_ARTIFACT_REL",
            "models/qwen3_6_27b.ninfer",
        ),
        "max_context": os.environ.get("NINFER_27B_MAX_CONTEXT", "8192"),
    },
)

models = []
for model in known_models:
    artifact = runtime_root / model["relative_path"]
    if not artifact.is_file():
        continue
    models.append(
        {
            "id": model["id"],
            "display_name": model["display_name"],
            "capability": "vision",
            "artifact_path": str(artifact),
            "start_command": [
                "/usr/local/bin/taskplanner-ninfer-worker",
                "vision",
            ],
            "environment": {
                "NINFER_MAX_CONTEXT": model["max_context"],
            },
        }
    )

catalog_path.write_text(
    json.dumps({"models": models}, separators=(",", ":")),
    encoding="utf-8",
)
PY

exec python3 /opt/taskplanner-ninfer-manager/ninfer_runtime_manager.py \
  --catalog "${catalog_path}"
