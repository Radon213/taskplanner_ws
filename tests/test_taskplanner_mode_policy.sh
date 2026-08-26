#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/scripts/lib/taskplanner_mode_policy.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

array_contains() {
  local expected="$1"
  shift
  local value
  for value in "$@"; do
    [[ "${value}" == "${expected}" ]] && return 0
  done
  return 1
}

assert_contains() {
  local expected="$1"
  shift
  array_contains "${expected}" "$@" || fail "missing service: ${expected}"
}

assert_not_contains() {
  local expected="$1"
  shift
  ! array_contains "${expected}" "$@" || fail "unexpected service: ${expected}"
}

assert_unique() {
  local -a values=("$@")
  local unique_count
  unique_count="$(printf '%s\n' "${values[@]}" | sort -u | wc -l)"
  [[ "${unique_count}" == "${#values[@]}" ]] || fail "service list contains duplicates"
}

for mode in live llm-surgeon replay debug; do
  taskplanner_mode_is_valid "${mode}" || fail "valid mode rejected: ${mode}"
done
for mode in "" shadow ops lab production; do
  ! taskplanner_mode_is_valid "${mode}" || fail "invalid mode accepted: ${mode}"
done

export TASKPLANNER_RUNTIME_MODE=stale
export INPUT_PROFILE=simulation
export EXECUTION_BACKEND=mock
export VLM_PROVIDER_ID=vllm
export VLM_MODEL_ID=legacy
export PERCEPTION_PROVIDER=builtin_rfdetr
export PERCEPTION_LOCATION=local
export PERCEPTION_ENDPOINT=http://127.0.0.1:8010
export ENABLE_RFDETR_PERCEPTION=true
export CAM4_INPUT_TOPIC=/preview/cam4
enforce_runtime_mode_contract live
[[ "${TASKPLANNER_RUNTIME_MODE}" == "live" ]] || fail "Live runtime marker drifted"
[[ "${INPUT_PROFILE}/${EXECUTION_BACKEND}" == "external/action" ]] || fail "Live execution lane drifted"
[[ "${VLM_PROVIDER_ID}/${VLM_MODEL_ID}" == "ninfer/qwen3.6-35b-a3b" ]] || fail "Live model contract drifted"
[[ "${PERCEPTION_PROVIDER}/${PERCEPTION_LOCATION}" == "external_rfdetr_topics/remote" ]] || fail "Live perception placement drifted"
[[ -z "${PERCEPTION_ENDPOINT}" && "${ENABLE_RFDETR_PERCEPTION}" == "false" ]] || fail "Live local detector was enabled"
[[ "${CAM4_INPUT_TOPIC}" == "/synced/cam_4/color/image_raw/compressed" ]] || fail "Live synchronized camera contract drifted"

enforce_runtime_mode_contract llm-surgeon
[[ "${TASKPLANNER_RUNTIME_MODE}" == "llm-surgeon" ]] || fail "LLM Surgeon runtime marker drifted"
[[ "${INPUT_PROFILE}/${EXECUTION_BACKEND}" == "simulation/mock" ]] || fail "LLM Surgeon execution lane drifted"
enforce_runtime_mode_contract replay
[[ -z "${TASKPLANNER_RUNTIME_MODE+x}" ]] || fail "Replay retained an operational runtime marker"

expected_profiles=(
  --profile live
  --profile llm-surgeon
  --profile replay
  --profile shadow
  --profile debug
  --profile dev
  --profile ops
  --profile lab
)
[[ "${TASKPLANNER_ALL_PROFILE_ARGS[*]}" == "${expected_profiles[*]}" ]] ||
  fail "all-profile order drifted"

unset TASKPLANNER_LIVE_ENABLE_OPS TASKPLANNER_LIVE_ENABLE_MULTICAM
unset TASKPLANNER_LIVE_ENABLE_TTS TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG
mode_uses_operational_asr_sidecar live || fail "Live must own operational ASR"
! mode_uses_operational_asr_sidecar replay || fail "Replay must not own operational ASR"
! mode_uses_ops_plane live || fail "Ops must be disabled by default"
! mode_uses_multicam_observer live || fail "Live multicam must be disabled by default"
mode_uses_multicam_observer debug || fail "Debug must own its observer"
! mode_uses_tts_sidecar live || fail "Live TTS must be disabled by default"
! mode_requires_integrated_debug_observer live || fail "Integrated Debug must be disabled by default"
taskplanner_same_mode_warm_restart_allowed live true false || fail "healthy same-mode Live warm restart rejected"
! taskplanner_same_mode_warm_restart_allowed debug true false || fail "standalone Debug must keep its independent restart path"
! taskplanner_same_mode_warm_restart_allowed live true true || fail "an explicit build must keep the full restart path"
! taskplanner_same_mode_warm_restart_allowed live false false || fail "an unhealthy runtime must keep the full restart path"
taskplanner_select_optional_recreate_args true
[[ "${#TASKPLANNER_SELECTED_RECREATE_ARGS[@]}" == "0" ]] ||
  fail "same-mode warm optional services must use plain Compose up"
taskplanner_select_optional_recreate_args false
[[ "${TASKPLANNER_SELECTED_RECREATE_ARGS[*]}" == "--force-recreate" ]] ||
  fail "full transitions must retain forced optional-service recreation"

TASKPLANNER_LIVE_ENABLE_OPS=true
mode_uses_ops_plane live || fail "explicit Live Ops selection ignored"
mode_uses_multicam_observer live || fail "Ops must select multicam unless overridden"
TASKPLANNER_LIVE_ENABLE_MULTICAM=false
! mode_uses_multicam_observer live || fail "explicit multicam=false ignored"
TASKPLANNER_LIVE_ENABLE_TTS=true
mode_uses_tts_sidecar live || fail "explicit Live TTS selection ignored"
TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG=true
mode_requires_integrated_debug_observer live || fail "explicit Integrated Debug selection ignored"

taskplanner_select_debug_reset_services false
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
assert_contains integration-debug "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
assert_not_contains multicam-observer "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
taskplanner_select_debug_reset_services true
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
assert_contains multicam-observer "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"

unset TASKPLANNER_LIVE_ENABLE_OPS TASKPLANNER_LIVE_ENABLE_MULTICAM
unset TASKPLANNER_LIVE_ENABLE_TTS TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG
taskplanner_select_operational_reset_services live false false false
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
for service in \
  taskplanner-runtime public-rosbridge public-rosbridge-lan-proxy \
  taskplanner-tts object-perception pnu-perception integration-debug \
  multicam-observer monitor-media-gateway vllm-manager; do
  assert_contains "${service}" "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
done
assert_not_contains taskplanner-asr "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"

taskplanner_select_operational_reset_services live true false false
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
assert_contains taskplanner-asr "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"

TASKPLANNER_LIVE_ENABLE_TTS=true
TASKPLANNER_LIVE_ENABLE_MULTICAM=true
taskplanner_select_operational_reset_services live false true true
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
for preserved in taskplanner-asr taskplanner-tts object-perception pnu-perception multicam-observer; do
  assert_not_contains "${preserved}" "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
done

taskplanner_select_operational_reset_services replay false false false
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
for released in taskplanner-asr taskplanner-tts object-perception pnu-perception multicam-observer; do
  assert_contains "${released}" "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
done

unset TASKPLANNER_LIVE_ENABLE_OPS TASKPLANNER_LIVE_ENABLE_MULTICAM
unset TASKPLANNER_LIVE_ENABLE_TTS TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG
taskplanner_select_operational_warm_cleanup_services live false false false
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
for preserved in taskplanner-runtime public-rosbridge shadow-runner taskplanner-asr; do
  assert_not_contains "${preserved}" "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
done
for released in \
  taskplanner-tts object-perception pnu-perception multicam-observer \
  integration-debug public-rosbridge-lan-proxy monitor-media-gateway \
  vllm-manager; do
  assert_contains "${released}" "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
done

TASKPLANNER_LIVE_ENABLE_OPS=true
TASKPLANNER_LIVE_ENABLE_MULTICAM=true
TASKPLANNER_LIVE_ENABLE_TTS=true
taskplanner_select_operational_warm_cleanup_services live true true true
assert_unique "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
for preserved in \
  taskplanner-asr taskplanner-tts object-perception pnu-perception \
  multicam-observer integration-debug public-rosbridge-lan-proxy \
  monitor-media-gateway; do
  assert_not_contains "${preserved}" "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"
done
assert_contains vllm-manager "${TASKPLANNER_SELECTED_RESET_SERVICES[@]}"

assert_unique "${TASKPLANNER_DEBUG_FAILURE_CLEANUP_SERVICES[@]}"
assert_unique "${TASKPLANNER_OPERATIONAL_FAILURE_CLEANUP_SERVICES[@]}"

printf 'Taskplanner declarative mode policy tests passed.\n'
