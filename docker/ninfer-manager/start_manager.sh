#!/usr/bin/env bash
set -euo pipefail

catalog_path="/tmp/taskplanner-ninfer-models.json"

python3 /opt/taskplanner-ninfer-manager/build_catalog.py \
  --output "${catalog_path}"

exec python3 /opt/taskplanner-ninfer-manager/ninfer_runtime_manager.py \
  --catalog "${catalog_path}"
