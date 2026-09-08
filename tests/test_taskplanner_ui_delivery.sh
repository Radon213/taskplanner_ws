#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${ROOT_DIR}/scripts/taskplanner"
UI_LAUNCHER="${ROOT_DIR}/scripts/taskplanner_ui.sh"
EXECUTION_LAYER="${ROOT_DIR}/scripts/lib/taskplanner_execution.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

dev_plan="$(bash "${LAUNCHER}" ui dev --mode live --dry-run)"
[[ "${dev_plan}" == *"docker-compose.ui-dev.yml"* ]] ||
  fail "ui dev did not select the HMR-only Compose override"
[[ "${dev_plan}" == *"up -d --no-deps --force-recreate webapp"* ]] ||
  fail "ui dev did not scope its recreate to webapp"
for unrelated_owner in \
  taskplanner-state-core taskplanner-asr ninfer-manager public-rosbridge; do
  [[ "${dev_plan}" != *"${unrelated_owner}"* ]] ||
    fail "ui dev touched unrelated owner ${unrelated_owner}"
done

restart_plan="$(bash "${LAUNCHER}" ui restart --mode live --dry-run)"
[[ "${restart_plan}" == *"up -d --no-deps --force-recreate webapp"* ]] ||
  fail "ui restart did not scope its recreate to webapp"
for unrelated_owner in \
  taskplanner-state-core taskplanner-asr ninfer-manager public-rosbridge; do
  [[ "${restart_plan}" != *"${unrelated_owner}"* ]] ||
    fail "ui restart touched unrelated owner ${unrelated_owner}"
done

apply_plan="$(bash "${LAUNCHER}" ui apply --mode live --dry-run)"
[[ "${apply_plan}" == *"exec -T --user"* ]] ||
  fail "ui apply did not use the webapp-only execution path"
[[ "${apply_plan}" == *"webapp bash scripts/apply-build.sh"* ]] ||
  fail "ui apply did not select the focused build helper"
[[ "${apply_plan}" != *"--force-recreate"* ]] ||
  fail "ui apply must not recreate webapp"
for unrelated_owner in \
  taskplanner-state-core taskplanner-asr ninfer-manager public-rosbridge; do
  [[ "${apply_plan}" != *"${unrelated_owner}"* ]] ||
    fail "ui apply touched unrelated owner ${unrelated_owner}"
done

python3 - "${UI_LAUNCHER}" "${EXECUTION_LAYER}" "${ROOT_DIR}/docker-compose.ui-dev.yml" <<'PY'
import sys
from pathlib import Path

ui_launcher = Path(sys.argv[1]).read_text(encoding="utf-8")
execution = Path(sys.argv[2]).read_text(encoding="utf-8")
dev_compose = Path(sys.argv[3]).read_text(encoding="utf-8")

assert "ui dev" in ui_launcher
assert "ui apply" in ui_launcher
assert "ui status" in ui_launcher
assert "ui restart" in ui_launcher
assert "exec -T --user" in ui_launcher
assert "run --rm --no-deps" in ui_launcher
assert "force-recreate webapp" in ui_launcher
assert "wait_for_static_build" in ui_launcher
assert "/healthz" in ui_launcher

refresh_start = execution.index("refresh_webapp_if_stale() {")
refresh_end = execution.index("\nwarm_restart_runtime_service() {", refresh_start)
refresh = execution[refresh_start:refresh_end]
assert "taskplanner_apply_webapp_bundle" in refresh
assert "--force-recreate" not in refresh
assert "apply-build.sh" in execution

assert "start-development.sh" in dev_compose
assert "/@vite/client" in dev_compose
assert "createHotContext" in dev_compose
PY

printf 'Taskplanner UI delivery tests passed.\n'
