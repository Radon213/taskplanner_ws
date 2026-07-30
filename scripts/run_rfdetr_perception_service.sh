#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${RFDETR_PYTHON:-python3}"
RFDETR_BIND_HOST="${RFDETR_BIND_HOST:-127.0.0.1}"
RFDETR_PORT="${RFDETR_PORT:-8010}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "RF-DETR Python runtime not found: ${PYTHON_BIN}" >&2
  exit 1
fi

export PYTHONPATH="${ROOT_DIR}/src/vlm_node${PYTHONPATH:+:${PYTHONPATH}}"
echo "RF-DETR service URL: http://${RFDETR_BIND_HOST}:${RFDETR_PORT}" >&2
exec "${PYTHON_BIN}" -m vlm_node.rfdetr_service \
  --host "${RFDETR_BIND_HOST}" \
  --port "${RFDETR_PORT}" \
  "$@"
