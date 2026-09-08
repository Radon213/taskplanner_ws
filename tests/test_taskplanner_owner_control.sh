#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${ROOT_DIR}/scripts/taskplanner"
OWNER_CONTROL="${ROOT_DIR}/scripts/lib/taskplanner_owner_control.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local value="$1"
  local expected="$2"
  [[ "${value}" == *"${expected}"* ]] || fail "missing: ${expected}"
}

assert_not_contains() {
  local value="$1"
  local unexpected="$2"
  [[ "${value}" != *"${unexpected}"* ]] || fail "unexpected: ${unexpected}"
}

bash -n "${LAUNCHER}" "${OWNER_CONTROL}"
python3 "${ROOT_DIR}/scripts/taskplanner_owner_registry.py" \
  --root "${ROOT_DIR}" validate >/dev/null

owners_output="$("${LAUNCHER}" owners)"
for owner in core command tool-state perception cam4-mayo projection execution bridge scenario simulation-input asr tts surgery-record rosbag-recorder surgimate; do
  assert_contains "${owners_output}" "${owner}"
done

restart_output="$("${LAUNCHER}" restart command live --dry-run)"
assert_contains "${restart_output}" 'restarting command owner only'
assert_contains "${restart_output}" '--profile owners up -d --no-deps taskplanner-command'
assert_contains "${restart_output}" '--profile owners restart taskplanner-command'
assert_not_contains "${restart_output}" 'taskplanner-state-core'
assert_not_contains "${restart_output}" 'taskplanner_package_plan.py'
assert_not_contains "${restart_output}" '--force-recreate taskplanner-command'

cam4_mayo_restart_output="$("${LAUNCHER}" restart cam4-mayo live --dry-run)"
assert_contains "${cam4_mayo_restart_output}" 'restarting cam4-mayo owner only'
assert_contains "${cam4_mayo_restart_output}" 'up -d --no-deps taskplanner-cam4-mayo'
assert_not_contains "${cam4_mayo_restart_output}" 'taskplanner-perception'
assert_not_contains "${cam4_mayo_restart_output}" 'taskplanner-state-core'

rosbag_restart_output="$("${LAUNCHER}" restart rosbag-recorder live --dry-run)"
assert_contains "${rosbag_restart_output}" 'restarting rosbag-recorder owner only'
assert_contains "${rosbag_restart_output}" 'install -d -m 0700'
assert_contains "${rosbag_restart_output}" 'up -d --no-deps taskplanner-rosbag-recorder'
assert_contains "${rosbag_restart_output}" 'restart taskplanner-rosbag-recorder'
assert_not_contains "${rosbag_restart_output}" 'taskplanner-state-core'
assert_not_contains "${rosbag_restart_output}" 'taskplanner_package_plan.py'

tts_restart_output="$("${LAUNCHER}" restart tts live --dry-run)"
assert_contains "${tts_restart_output}" 'restarting tts sidecar only'
assert_contains "${tts_restart_output}" '--profile ops up -d --no-deps taskplanner-tts'
assert_contains "${tts_restart_output}" '--profile ops restart taskplanner-tts'
assert_not_contains "${tts_restart_output}" 'taskplanner-state-core'
assert_not_contains "${tts_restart_output}" 'taskplanner_package_plan.py'

for action in start stop restart; do
  surgimate_output="$("${LAUNCHER}" surgimate "${action}" --mode debug --dry-run)"
  case "${action}" in
    start) action_label="starting" ;;
    stop) action_label="stopping" ;;
    restart) action_label="restarting" ;;
  esac
  assert_contains "${surgimate_output}" "${action_label} surgimate sidecar only"
  if [[ "${action}" == "stop" ]]; then
    assert_contains "${surgimate_output}" '--profile owners stop taskplanner-surgimate'
  else
    assert_contains "${surgimate_output}" '--profile owners up -d --no-deps taskplanner-surgimate'
  fi
  if [[ "${action}" == "restart" ]]; then
    assert_contains "${surgimate_output}" '--profile owners restart taskplanner-surgimate'
  fi
  assert_not_contains "${surgimate_output}" 'taskplanner-state-core'
  assert_not_contains "${surgimate_output}" 'taskplanner_package_plan.py'
done
if "${LAUNCHER}" surgimate start --mode live --dry-run >/dev/null 2>&1; then
  fail 'surgimate lifecycle must reject non-Debug mode'
fi

execution_restart_output="$("${LAUNCHER}" restart execution live --dry-run)"
assert_contains "${execution_restart_output}" 'verify-execution-restart-state'
assert_contains "${execution_restart_output}" '/integration/execution_route/state'

reload_output="$("${LAUNCHER}" reload config thyroidectomy_demo --dry-run)"
assert_contains "${reload_output}" '--profile owners exec -T taskplanner-scenario'
assert_contains "${reload_output}" 'reload_if_changed:'
assert_not_contains "${reload_output}" 'taskplanner-runtime'

live_plan="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${LAUNCHER}" up live --dry-run --ensure-build
)"
assert_contains "${live_plan}" '--profile live --profile owners up -d taskplanner-state-core taskplanner-command taskplanner-tool-state taskplanner-cam4-mayo taskplanner-projection taskplanner-execution taskplanner-operator-bridge taskplanner-scenario taskplanner-surgery-record taskplanner-rosbag-recorder taskplanner-debug-observer'
assert_contains "${live_plan}" '--profile live --profile owners up -d taskplanner-perception'
assert_contains "${live_plan}" 'taskplanner-cam4-mayo'
assert_contains "${live_plan}" 'taskplanner-surgery-record'
assert_contains "${live_plan}" 'taskplanner-rosbag-recorder'
assert_contains "${live_plan}" '--profile owners up -d --no-deps taskplanner-surgimate'
[[ "$(grep -Fc 'taskplanner-surgimate' <<<"${live_plan}")" == "1" ]] || \
  fail 'cold Live start must launch SurgiMate exactly once'
assert_not_contains "$(grep ' up -d ' <<<"${live_plan}")" ' up -d taskplanner-runtime'

debug_plan="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${LAUNCHER}" up debug --dry-run --ensure-build
)"
assert_contains "${debug_plan}" '--profile owners up -d --no-deps taskplanner-surgimate'
[[ "$(grep -Fc 'taskplanner-surgimate' <<<"${debug_plan}")" == "1" ]] || \
  fail 'cold Debug start must launch SurgiMate exactly once'

printf 'Taskplanner owner-control tests passed.\n'
