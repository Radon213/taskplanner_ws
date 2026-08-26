#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
source "${ROOT_DIR}/scripts/lib/taskplanner_mode_policy.sh"
source "${ROOT_DIR}/scripts/lib/taskplanner_execution.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_trace() {
  local expected="$1"
  [[ "${TRACE[*]}" == "${expected}" ]] ||
    fail "unexpected execution trace: ${TRACE[*]}"
}

# Compose argv construction belongs to the execution layer while profile
# ownership remains imported from the declarative mode policy.
ENV_ARGS=(--env-file first.env --env-file live.env)
taskplanner_compose_init
[[ "${COMPOSE[*]}" == \
  "docker compose --project-directory ${ROOT_DIR} -f ${COMPOSE_FILE} --env-file first.env --env-file live.env" ]] ||
  fail "Compose base argv drifted: ${COMPOSE[*]}"
[[ "${ALL_PROFILE_ARGS[*]}" == "${TASKPLANNER_ALL_PROFILE_ARGS[*]}" ]] ||
  fail "all-profile argv drifted"

# Stop/remove is one ordered convergence primitive. A failed stop must prevent
# the remove step, preserving evidence instead of masking the first failure.
TRACE=()
run_or_print() {
  TRACE+=("$*")
}
taskplanner_compose_stop_remove stale-a stale-b
[[ "${#TRACE[@]}" == 2 ]] || fail "stop/remove did not emit exactly two calls"
[[ "${TRACE[0]}" == *" stop stale-a stale-b" ]] || fail "stop call drifted"
[[ "${TRACE[1]}" == *" rm -f stale-a stale-b" ]] || fail "remove call drifted"

TRACE=()
run_or_print() {
  TRACE+=("$*")
  [[ "$*" != *" stop reject-me" ]]
}
if taskplanner_compose_stop_remove reject-me; then
  fail "failed Compose stop was accepted"
fi
[[ "${#TRACE[@]}" == 1 ]] || fail "remove ran after a failed stop"

# A selected build always serializes the overlay build before image builds;
# no-build requests remain true no-ops.
TRACE=()
run_serial_workspace_build() {
  TRACE+=("workspace:$1")
}
build_runtime_images_once() {
  TRACE+=("images:$1")
}
BUILD_REQUESTED=false
taskplanner_build_selected_runtime live
assert_trace ""
BUILD_REQUESTED=true
taskplanner_build_selected_runtime live
assert_trace "workspace:live images:live"

TRACE=()
run_serial_workspace_build() {
  TRACE+=("workspace:$1")
  return 1
}
if taskplanner_build_selected_runtime replay; then
  fail "failed workspace build was accepted"
fi
assert_trace "workspace:replay"

# The warm boundary is deliberately narrow: reserve/recheck first, then arm
# cleanup, clear the marker, and finally touch only the selected core service.
TRACE=()
RUNTIME_FAILURE_CLEANUP_ARMED=false
RUNTIME_FAILURE_CLEANUP_SERVICES=(unrelated)
verify_same_mode_warm_restart_interlock() {
  TRACE+=("verify:$1")
}
clear_active_runtime_mode() {
  TRACE+=("clear")
}
warm_restart_runtime_service() {
  TRACE+=("restart:$1:$2")
}
taskplanner_cross_warm_restart_boundary live taskplanner-runtime
assert_trace "verify:live clear restart:live:taskplanner-runtime"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "true" ]] ||
  fail "warm failure cleanup was not armed"
[[ "${RUNTIME_FAILURE_CLEANUP_SERVICES[*]}" == "taskplanner-runtime" ]] ||
  fail "warm cleanup escaped the selected core"

TRACE=()
RUNTIME_FAILURE_CLEANUP_ARMED=false
verify_same_mode_warm_restart_interlock() {
  TRACE+=("verify:$1")
  return 1
}
if taskplanner_cross_warm_restart_boundary live taskplanner-runtime; then
  fail "rejected warm reservation crossed the restart boundary"
fi
assert_trace "verify:live"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "false" ]] ||
  fail "cleanup armed before warm reservation was accepted"

TRACE=()
RUNTIME_FAILURE_CLEANUP_ARMED=false
verify_same_mode_warm_restart_interlock() {
  TRACE+=("verify:$1")
}
clear_active_runtime_mode() {
  TRACE+=("clear")
  return 1
}
warm_restart_runtime_service() {
  TRACE+=("restart:$1:$2")
}
if taskplanner_cross_warm_restart_boundary live taskplanner-runtime; then
  fail "failed marker clear crossed the core restart boundary"
fi
assert_trace "verify:live clear"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "true" ]] ||
  fail "cleanup must remain armed after marker-clear failure"

# Deployment readiness remains distinct from controller cheap liveness. The
# active marker is published only after route and semantic checks for every
# required launcher-owned surface.
TRACE=()
DRY_RUN=true
RUNTIME_FAILURE_CLEANUP_ARMED=true
wait_for_mode_rosbridge() {
  TRACE+=("route:$1")
}
wait_for_mode_semantic_ready() {
  TRACE+=("semantic:$1")
}
write_active_runtime_mode() {
  TRACE+=("marker:$1")
}
mode_uses_multicam_observer() {
  return 1
}
taskplanner_finalize_runtime_readiness live true --profile live --profile debug
assert_trace "route:live semantic:live route:debug semantic:debug marker:live"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "false" ]] ||
  fail "failure cleanup remained armed after readiness publication"

TRACE=()
RUNTIME_FAILURE_CLEANUP_ARMED=true
wait_for_mode_rosbridge() {
  TRACE+=("route:$1")
}
wait_for_mode_semantic_ready() {
  TRACE+=("semantic:$1")
  return 1
}
if taskplanner_finalize_runtime_readiness live false --profile live; then
  fail "semantic readiness failure was accepted"
fi
assert_trace "route:live semantic:live"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "true" ]] ||
  fail "cleanup disarmed before semantic readiness succeeded"

TRACE=()
RUNTIME_FAILURE_CLEANUP_ARMED=true
wait_for_mode_rosbridge() {
  TRACE+=("route:$1")
}
wait_for_mode_semantic_ready() {
  TRACE+=("semantic:$1")
}
write_active_runtime_mode() {
  TRACE+=("marker:$1")
  return 1
}
if taskplanner_finalize_runtime_readiness live false --profile live; then
  fail "failed active-marker publication was accepted"
fi
assert_trace "route:live semantic:live marker:live"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "true" ]] ||
  fail "cleanup disarmed after active-marker publication failed"

printf 'Taskplanner common execution-layer tests passed.\n'
