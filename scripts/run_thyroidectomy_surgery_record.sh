#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_FILE="${ROOT_DIR}/config/thyroidectomy_surgery_record.env"
MODE="${1:---check}"

usage() {
  cat <<'EOF'
Prepare or run one audited thyroidectomy replay and emit surgery_record_input.txt.

Usage:
  scripts/run_thyroidectomy_surgery_record.sh --check
  scripts/run_thyroidectomy_surgery_record.sh --execute

--check performs read-only dataset, source, and loaded-NInfer-model checks.
--execute follows the release-validated Shadow Replay flow.

Optional environment overrides for sequential audited batches:
  SHADOW_CASE_ID=0704_7
  SURGERY_RECORD_REUSE_RUNTIME=true
EOF
}

case "${MODE}" in
  --check|--execute) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

case_id_override="${SHADOW_CASE_ID:-}"

set -a
# shellcheck disable=SC1090
source "${PROFILE_FILE}"
set +a

# Allow audited batch callers to select another prepared replay case without
# editing the checked-in replay profile between runs.
if [[ -n "${case_id_override}" ]]; then
  SHADOW_CASE_ID="${case_id_override}"
  export SHADOW_CASE_ID
fi

require_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "${path}" ]]; then
    printf 'BLOCKED: %s is missing: %s\n' "${label}" "${path}" >&2
    return 1
  fi
  printf 'READY: %s: %s\n' "${label}" "${path}"
}

require_dir() {
  local path="$1"
  local label="$2"
  if [[ ! -d "${path}" ]]; then
    printf 'BLOCKED: %s is missing: %s\n' "${label}" "${path}" >&2
    return 1
  fi
  printf 'READY: %s: %s\n' "${label}" "${path}"
}

check_ninfer_model() {
  local require_loaded="${1:-true}"
  python3 - "${VLM_BASE_URL}" "${VLM_MODEL_ID}" "${require_loaded}" <<'PY'
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

base_url, model_id, require_loaded_raw = sys.argv[1:]
require_loaded = require_loaded_raw.lower() == "true"
try:
    with urlopen(f"{base_url.rstrip('/')}/v1/models", timeout=3.0) as response:
        payload = json.load(response)
except (OSError, HTTPError, URLError, ValueError) as exc:
    print(f"BLOCKED: NInfer model catalog unavailable: {exc}", file=sys.stderr)
    raise SystemExit(1)

rows = {
    str(row.get("id", "")): row
    for row in payload.get("data", [])
    if isinstance(row, dict)
}
row = rows.get(model_id)
if row is None:
    print(f"BLOCKED: NInfer model is not catalogued: {model_id}", file=sys.stderr)
    raise SystemExit(1)
loaded = bool(row.get("loaded")) and str(row.get("load_state", "")) == "loaded"
if require_loaded and not loaded:
    print(f"BLOCKED: NInfer model is not loaded: {model_id}", file=sys.stderr)
    raise SystemExit(1)
capabilities = row.get("capabilities", {})
if not isinstance(capabilities, dict) or not bool(capabilities.get("vision")):
    print(
        f"BLOCKED: NInfer model does not advertise vision capability: {model_id}",
        file=sys.stderr,
    )
    raise SystemExit(1)
state = "loaded" if loaded else str(row.get("load_state", "unloaded"))
print(f"READY: NInfer VLM catalogued with vision capability: {model_id} ({state})")
PY
}

ensure_ninfer_model_loaded() {
  python3 - "${VLM_BASE_URL}" "${VLM_MODEL_ID}" <<'PY'
import json
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

base_url, model_id = sys.argv[1:]
base_url = base_url.rstrip("/")

def catalog_row():
    with urlopen(f"{base_url}/v1/models", timeout=3.0) as response:
        payload = json.load(response)
    return next(
        (
            row
            for row in payload.get("data", [])
            if isinstance(row, dict) and str(row.get("id", "")) == model_id
        ),
        None,
    )

try:
    row = catalog_row()
    if row is None:
        raise RuntimeError(f"model is not catalogued: {model_id}")
    if not bool(row.get("loaded")):
        request = Request(
            f"{base_url}/manager/load",
            data=json.dumps({"model_id": model_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10.0) as response:
            accepted = json.load(response)
        print(
            f"WAIT: requested NInfer model load: {model_id} "
            f"({accepted.get('state', 'loading')})"
        )
    deadline = time.monotonic() + 600.0
    last_state = ""
    while time.monotonic() < deadline:
        row = catalog_row()
        state = str((row or {}).get("load_state", "missing"))
        if state != last_state:
            print(f"WAIT: NInfer model state: {state}")
            last_state = state
        if row and bool(row.get("loaded")) and state == "loaded":
            print(f"READY: NInfer model loaded after replay service startup: {model_id}")
            raise SystemExit(0)
        if state == "error":
            raise RuntimeError(str((row or {}).get("detail", "model load failed")))
        time.sleep(2.0)
    raise TimeoutError(f"timed out waiting for NInfer model load: {model_id}")
except (OSError, HTTPError, URLError, ValueError, RuntimeError, TimeoutError) as exc:
    print(f"BLOCKED: unable to load NInfer model: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

check_inputs() {
  local case_annotation
  local procedure_prompt
  case_annotation="${ROOT_DIR}/annotations/observable_tool_events/cases"
  case_annotation+="/${SHADOW_CASE_ID}/annotation_manifest.json"
  procedure_prompt="${ROOT_DIR}/src/procedure_spec/procedure_spec/specs"
  procedure_prompt+="/${SHADOW_BUNDLE_ID}/vlm_procedure_prompt.yaml"

  require_dir "${SHADOW_DATASET_ROOT}/${SHADOW_CASE_ID}" "source replay case"
  require_file "${case_annotation}" "case annotation manifest"
  require_file "${procedure_prompt}" "procedure prompt"
  require_file \
    "${ROOT_DIR}/tools/real_surgery_annotation/render_surgery_record_timeline.py" \
    "surgery-record timeline renderer"
  if [[ "${MODE}" == "--check" ]]; then
    check_ninfer_model true
  else
    check_ninfer_model false
  fi
}

check_inputs

if [[ "${MODE}" == "--check" ]]; then
  printf 'READY: replay preparation is valid; no scenario was started.\n'
  exit 0
fi

# Keep the startup contract identical to the validated reference workflow.
# A sequential batch may reuse an already healthy control plane and loaded
# model; each case still launches its own isolated strict replay process.
if [[ "${SURGERY_RECORD_REUSE_RUNTIME:-false}" != "true" ]]; then
  "${ROOT_DIR}/scripts/taskplanner" up replay --no-build
fi
ensure_ninfer_model_loaded

run_id="${SHADOW_CASE_ID}-ninfer35b-surgery-record-$(date +%Y%m%d%H%M%S)"
output_root="/workspaces/taskplanner_ws/output/shadow_runs"

docker compose \
  --project-directory "${ROOT_DIR}" \
  -f "${ROOT_DIR}/docker-compose.yml" \
  --profile dev \
  run --rm -T \
  -v "${SHADOW_DATASET_ROOT}:/datasets/shadow:ro" \
  taskplanner-dev \
  bash -lc '
    source /opt/ros/jazzy/setup.bash
    source /opt/btops_ws/install/setup.bash
    source install/setup.bash
    exec python3 tools/real_surgery_annotation/run_shadow_replay.py "$@"
  ' bash \
  --source-bag "/datasets/shadow/${SHADOW_CASE_ID}" \
  --bundle "${SHADOW_BUNDLE_ID}" \
  --mode strict \
  --output-root "${output_root}" \
  --run-id "${run_id}" \
  --ros-domain-id "${SURGERY_RECORD_ROS_DOMAIN_ID}" \
  --interactive-replay \
  --replay-mode "${SHADOW_REPLAY_MODE}" \
  --rosbridge-port "${SURGERY_RECORD_ROSBRIDGE_PORT}" \
  --score-provisional-phase \
  --provider-id "${VLM_PROVIDER_ID}" \
  --base-url "${VLM_BASE_URL}" \
  --model-id "${VLM_MODEL_ID}" \
  --api-mode "${VLM_API_MODE}" \
  --publish-period-sec "${VLM_PUBLISH_PERIOD_SEC}" \
  --response-format "${VLM_RESPONSE_FORMAT}" \
  --reasoning-effort "${VLM_REASONING_EFFORT}" \
  --max-output-tokens "${VLM_MAX_OUTPUT_TOKENS}" \
  --vlm-generation-seed "${VLM_GENERATION_SEED}" \
  --vlm-request-timeout-sec 60 \
  --vlm-retry-count 1 \
  --replay-vlm-health-timeout-sec 130 \
  --replay-vlm-wait-timeout-sec 130 \
  --replay-drain-timeout-sec 130 \
  --counterfactual-feedback \
  --type-instance-assumption

printf 'SURGERY_RECORD_PATH=%s\n' \
  "${ROOT_DIR}/output/shadow_runs/${run_id}/surgery_record_input.txt"
