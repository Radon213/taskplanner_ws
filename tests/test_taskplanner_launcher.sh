#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local haystack="$1"
  local needle="$2"
  [[ "${haystack}" == *"${needle}"* ]] || fail "missing: ${needle}"
}

assert_not_contains() {
  local haystack="$1"
  local needle="$2"
  [[ "${haystack}" != *"${needle}"* ]] || fail "unexpected: ${needle}"
}

PNU_TOKEN_TEST_ROOT="$(mktemp -d)"
PNU_TOKEN_ESCAPE_ROOT="$(mktemp -d)"
trap 'rm -rf -- "${PNU_TOKEN_TEST_ROOT}" "${PNU_TOKEN_ESCAPE_ROOT}"' EXIT
printf '%s\n' 'launcher-test-token' >"${PNU_TOKEN_TEST_ROOT}/token"
printf '  %s  \n' 'launcher-test-token' >"${PNU_TOKEN_TEST_ROOT}/worker-token"
printf '%s\n' 'different-launcher-test-token' \
  >"${PNU_TOKEN_TEST_ROOT}/mismatched-token"
printf '%s\n' 'escaped-launcher-test-token' >"${PNU_TOKEN_ESCAPE_ROOT}/token"

bash -n "${ROOT_DIR}/scripts/taskplanner"

assert_single_serial_build() {
  local output="$1"
  local mode_label="$2"
  local build_count stop_line build_line first_start_line
  build_count="$(grep -F -c 'colcon\ build' <<<"${output}" || true)"
  [[ "${build_count}" == "1" ]] ||
    fail "${mode_label} --build must run exactly one foreground colcon build"
  stop_line="$(grep -n -F ' stop ' <<<"${output}" | head -n 1 | cut -d: -f1)"
  build_line="$(grep -n -F ' run --rm --no-deps --build -T ' <<<"${output}" | cut -d: -f1)"
  first_start_line="$(grep -n -F ' up -d ' <<<"${output}" | head -n 1 | cut -d: -f1)"
  [[ -n "${stop_line}" && -n "${build_line}" && -n "${first_start_line}" && \
    "${stop_line}" -lt "${build_line}" && \
    "${build_line}" -lt "${first_start_line}" ]] ||
    fail "${mode_label} must stop workspace readers, build once, then start services"
}

live_output="$(
  TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=false \
  TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=true \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_contains "${live_output}" \
  "--profile dev run --rm --no-deps -T --user"
assert_contains "${live_output}" \
  "taskplanner-install-check"
assert_contains "${live_output}" \
  "VoiceCommandIntent"
install_check_line="$(grep -n -F 'taskplanner-install-check' <<<"${live_output}" | head -n 1 | cut -d: -f1)"
first_runtime_stop_line="$(grep -n -F ' stop taskplanner-runtime' <<<"${live_output}" | head -n 1 | cut -d: -f1)"
[[ -n "${install_check_line}" && -n "${first_runtime_stop_line}" && \
  "${install_check_line}" -lt "${first_runtime_stop_line}" ]] || \
  fail "no-build install validation must finish before the active runtime is stopped"
assert_contains "${live_output}" \
  "--profile live --profile debug up -d --force-recreate integration-debug"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate integration-debug-lan-proxy"
assert_not_contains "${live_output}" \
  "--profile live --profile debug up -d --force-recreate integration-debug integration-debug-lan-proxy"
assert_contains "${live_output}" \
  "--profile live up -d integration-debug-tailscale-proxy"
assert_contains "${live_output}" \
  "stop taskplanner-runtime public-rosbridge public-rosbridge-lan-proxy taskplanner-asr shadow-runner pnu-perception integration-debug integration-debug-lan-proxy"
assert_contains "${live_output}" \
  "rm -f taskplanner-runtime public-rosbridge public-rosbridge-lan-proxy taskplanner-asr shadow-runner pnu-perception integration-debug integration-debug-lan-proxy"
assert_not_contains "${live_output}" \
  "stop taskplanner-runtime public-rosbridge public-rosbridge-lan-proxy taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy integration-debug-tailscale-proxy"
assert_contains "${live_output}" \
  "--profile live --profile debug up -d --remove-orphans --wait --wait-timeout 300 vllm-manager ninfer-manager webapp"
assert_contains "${live_output}" \
  "--profile live up -d multicam-observer"
assert_not_contains "${live_output}" \
  "--wait --wait-timeout 300 vllm-manager ninfer-manager webapp multicam-observer"
assert_not_contains "${live_output}" \
  "--force-recreate --remove-orphans --wait --wait-timeout 300 vllm-manager ninfer-manager webapp"
assert_contains "${live_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 /live live\\ ROS\\ bridge\\ router"
assert_contains "${live_output}" \
  "+ wait_for_multicam_observer live"
assert_not_contains "${live_output}" "stop multicam-observer"
assert_not_contains "${live_output}" "rm -f multicam-observer"
assert_contains "${live_output}" \
  "+ wait-for-ros-semantic-ready live taskplanner-runtime /simulation/state surgical_msgs/msg/SimulationState /simulation/control surgical_msgs/srv/ControlSimulation"
assert_contains "${live_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 / debug\\ ROS\\ bridge\\ router"
assert_contains "${live_output}" \
  "+ wait-for-ros-semantic-ready debug integration-debug /integration/debug/status std_msgs/msg/String /integration/debug/check_readiness std_srvs/srv/Trigger"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 taskplanner-asr"
asr_start_line="$(grep -n -- 'taskplanner-asr$' <<<"${live_output}" | tail -n 1 | cut -d: -f1)"
runtime_start_line="$(grep -n -- 'up -d --force-recreate taskplanner-runtime$' <<<"${live_output}" | cut -d: -f1)"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 public-rosbridge"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate public-rosbridge-lan-proxy"
assert_contains "${live_output}" \
  "--profile live up -d object-perception"
assert_contains "${live_output}" \
  "--profile live up -d --wait --wait-timeout 30 object-perception"
disabled_bridge_output="$(ENABLE_PUBLIC_ROSBRIDGE=false "${ROOT_DIR}/scripts/taskplanner" up live --dry-run)"
assert_not_contains "${disabled_bridge_output}" \
  "--profile live up -d --force-recreate public-rosbridge"
assert_not_contains "${disabled_bridge_output}" \
  "--profile live up -d --force-recreate public-rosbridge-lan-proxy"
[[ -n "${asr_start_line}" && -n "${runtime_start_line}" && \
  "${asr_start_line}" -lt "${runtime_start_line}" ]] || \
  fail "live must health-wait for taskplanner-asr before taskplanner-runtime"

external_cv_output="$(
  PERCEPTION_BACKEND=external \
  RFDETR_SERVICE_URL=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_not_contains "${external_cv_output}" \
  "--profile live up -d object-perception"
assert_not_contains "${external_cv_output}" \
  "--profile live up -d --wait --wait-timeout 30 object-perception"
assert_not_contains "${external_cv_output}" \
  "--profile live up -d pnu-perception"

remote_builtin_output="$(
  PERCEPTION_PROVIDER=builtin_rfdetr \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=http://192.168.1.20:8010 \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_not_contains "${remote_builtin_output}" \
  "--profile live up -d object-perception"
assert_not_contains "${remote_builtin_output}" \
  "--profile live up -d --wait --wait-timeout 30 object-perception"

local_builtin_output="$(
  PERCEPTION_PROVIDER=builtin_rfdetr \
  PERCEPTION_LOCATION=local \
  PERCEPTION_ENDPOINT=http://127.0.0.1:8010 \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_contains "${local_builtin_output}" \
  "--profile live up -d object-perception"

local_pnu_output="$(
  PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=local \
  PERCEPTION_ENDPOINT=http://127.0.0.1:8020 \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_contains "${local_pnu_output}" \
  "--profile live up -d pnu-perception"
assert_contains "${local_pnu_output}" \
  "--profile live up -d --wait --wait-timeout 30 pnu-perception"
assert_contains "${local_pnu_output}" \
  "+ pnu-model-digest-preflight taskplanner-runtime local tool\\,blood\\,hand"
assert_not_contains "${local_pnu_output}" \
  "--profile live up -d object-perception"

remote_pnu_output="$(
  PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_not_contains "${remote_pnu_output}" \
  "--profile live up -d pnu-perception"
assert_contains "${remote_pnu_output}" \
  "shadow-runner object-perception pnu-perception integration-debug"
assert_contains "${remote_pnu_output}" \
  "+ pnu-model-digest-preflight taskplanner-runtime remote tool\\,blood\\,hand"
assert_not_contains "${remote_pnu_output}" "launcher-test-token"

missing_pnu_digest_pins_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
  PNU_EXPECTED_MODEL_DIGESTS_JSON='{}' \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${missing_pnu_digest_pins_output}" 2>&1; then
  fail "remote PNU perception must reject missing model digest pins"
fi
assert_contains "$(cat "${missing_pnu_digest_pins_output}")" \
  "MODEL_DIGEST_PIN_MISSING"
assert_contains "$(cat "${missing_pnu_digest_pins_output}")" \
  "blood,hand,tool"
rm -f "${missing_pnu_digest_pins_output}"

partial_pnu_digest_pins_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
  PNU_EXPECTED_MODEL_DIGESTS_JSON='{"tool":"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430"}' \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${partial_pnu_digest_pins_output}" 2>&1; then
  fail "remote PNU perception must reject a partial model digest map"
fi
assert_contains "$(cat "${partial_pnu_digest_pins_output}")" \
  "MODEL_DIGEST_PIN_MISSING"
assert_contains "$(cat "${partial_pnu_digest_pins_output}")" \
  "blood,hand"
rm -f "${partial_pnu_digest_pins_output}"

short_pnu_digest_pin_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
  PNU_EXPECTED_MODEL_DIGESTS_JSON='{"tool":"253617aa","blood":"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75","hand":"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"}' \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${short_pnu_digest_pin_output}" 2>&1; then
  fail "remote PNU perception must reject a partial SHA-256 value"
fi
assert_contains "$(cat "${short_pnu_digest_pin_output}")" \
  "INVALID_MODEL_DIGEST_PINS"
assert_contains "$(cat "${short_pnu_digest_pin_output}")" \
  "64-character lowercase SHA-256"
rm -f "${short_pnu_digest_pin_output}"

insecure_remote_pnu_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=http://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${insecure_remote_pnu_output}" 2>&1; then
  fail "remote PNU perception must require HTTPS by default"
fi
assert_contains "$(cat "${insecure_remote_pnu_output}")" \
  "remote PNU perception requires an HTTPS endpoint"
rm -f "${insecure_remote_pnu_output}"

trusted_lan_http_pnu_output="$(
  PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=http://192.168.1.20:8020 \
  PNU_ALLOW_INSECURE_REMOTE_HTTP=true \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_not_contains "${trusted_lan_http_pnu_output}" \
  "--profile live up -d pnu-perception"
assert_not_contains "${trusted_lan_http_pnu_output}" "launcher-test-token"

missing_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/missing-token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${missing_pnu_token_output}" 2>&1; then
  fail "remote PNU perception must reject a missing mounted token file"
fi
assert_contains "$(cat "${missing_pnu_token_output}")" \
  "configured token file does not exist for taskplanner-runtime"
rm -f "${missing_pnu_token_output}"

printf '%s\n' 'two tokens' >"${PNU_TOKEN_TEST_ROOT}/invalid-token"
invalid_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/invalid-token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${invalid_pnu_token_output}" 2>&1; then
  fail "remote PNU perception must reject an invalid mounted token file"
fi
assert_contains "$(cat "${invalid_pnu_token_output}")" \
  "must contain exactly one non-whitespace UTF-8 token"
rm -f "${invalid_pnu_token_output}"

printf '\377\n' >"${PNU_TOKEN_TEST_ROOT}/non-utf8-token"
non_utf8_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/non-utf8-token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${non_utf8_pnu_token_output}" 2>&1; then
  fail "remote PNU perception must reject a non-UTF-8 token file"
fi
assert_contains "$(cat "${non_utf8_pnu_token_output}")" \
  "configured token file is not valid UTF-8"
rm -f "${non_utf8_pnu_token_output}"

head -c 4097 /dev/zero >"${PNU_TOKEN_TEST_ROOT}/oversized-token"
oversized_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/oversized-token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${oversized_pnu_token_output}" 2>&1; then
  fail "remote PNU perception must reject a token file over 4096 bytes"
fi
assert_contains "$(cat "${oversized_pnu_token_output}")" \
  "configured token file exceeds 4096 bytes"
rm -f "${oversized_pnu_token_output}"

traversal_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/../perception/token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${traversal_pnu_token_output}" 2>&1; then
  fail "remote PNU perception must reject token path traversal"
fi
assert_contains "$(cat "${traversal_pnu_token_output}")" \
  "token path must not contain path traversal"
rm -f "${traversal_pnu_token_output}"

ln -s "${PNU_TOKEN_ESCAPE_ROOT}/token" "${PNU_TOKEN_TEST_ROOT}/escaped-token"
symlink_escape_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/escaped-token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${symlink_escape_pnu_token_output}" 2>&1; then
  fail "remote PNU perception must reject a token symlink escaping its mount"
fi
assert_contains "$(cat "${symlink_escape_pnu_token_output}")" \
  "configured token file escapes the perception mount"
rm -f "${symlink_escape_pnu_token_output}"

local_asymmetric_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=local \
  PERCEPTION_ENDPOINT=http://127.0.0.1:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
  PNU_WORKER_API_TOKEN_FILE= \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${local_asymmetric_pnu_token_output}" 2>&1; then
  fail "local PNU perception must reject asymmetric token paths"
fi
assert_contains "$(cat "${local_asymmetric_pnu_token_output}")" \
  "local PNU authentication requires both client and worker token paths, or neither"
rm -f "${local_asymmetric_pnu_token_output}"

local_authenticated_pnu_output="$(
  PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=local \
  PERCEPTION_ENDPOINT=http://127.0.0.1:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
  PNU_WORKER_API_TOKEN_FILE=/run/taskplanner/perception/worker-token \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_contains "${local_authenticated_pnu_output}" \
  "--profile live up -d pnu-perception"
assert_not_contains "${local_authenticated_pnu_output}" "launcher-test-token"

local_mismatched_pnu_token_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=local \
  PERCEPTION_ENDPOINT=http://127.0.0.1:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
  PNU_WORKER_API_TOKEN_FILE=/run/taskplanner/perception/mismatched-token \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${local_mismatched_pnu_token_output}" 2>&1; then
  fail "local PNU perception must reject mismatched client and worker tokens"
fi
assert_contains "$(cat "${local_mismatched_pnu_token_output}")" \
  "local PNU client and worker token files do not match"
assert_not_contains "$(cat "${local_mismatched_pnu_token_output}")" \
  "different-launcher-test-token"
rm -f "${local_mismatched_pnu_token_output}"

unauthenticated_remote_pnu_output="$(mktemp)"
if PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_CLIENT_API_TOKEN_FILE= \
  PNU_ALLOW_UNAUTHENTICATED_REMOTE=false \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run \
    >"${unauthenticated_remote_pnu_output}" 2>&1; then
  fail "remote PNU perception must require authentication by default"
fi
assert_contains "$(cat "${unauthenticated_remote_pnu_output}")" \
  "remote PNU perception requires PNU_CLIENT_API_TOKEN_FILE"
rm -f "${unauthenticated_remote_pnu_output}"

invalid_perception_output="$(mktemp)"
if PERCEPTION_PROVIDER=builtin_rfdetr \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=http://127.0.0.1:8010 \
  "${ROOT_DIR}/scripts/taskplanner" up live --dry-run >"${invalid_perception_output}" 2>&1; then
  fail "remote perception must reject a loopback endpoint"
fi
assert_contains "$(cat "${invalid_perception_output}")" \
  "PERCEPTION_LOCATION=remote requires a non-loopback worker endpoint"
rm -f "${invalid_perception_output}"

live_build_output="$("${ROOT_DIR}/scripts/taskplanner" up live --dry-run --build)"
assert_single_serial_build "${live_build_output}" "live"
assert_contains "${live_build_output}" \
  "--profile live --profile dev --profile debug run --rm --no-deps --build -T"
assert_contains "${live_build_output}" \
  "taskplanner-dev bash -lc colcon\\ build\\ --symlink-install\\ --cmake-args"
assert_not_contains "${live_build_output}" \
  "colcon\\ build\\ --symlink-install\\ --packages-select"
assert_not_contains "${live_build_output}" \
  "taskplanner-install-check"

llm_output="$("${ROOT_DIR}/scripts/taskplanner" up llm-surgeon --dry-run)"
assert_not_contains "${llm_output}" \
  "--profile llm-surgeon --profile debug up -d --force-recreate integration-debug"
assert_not_contains "${llm_output}" \
  "--profile llm-surgeon up -d multicam-observer"
assert_not_contains "${llm_output}" \
  "+ wait_for_multicam_observer llm-surgeon"
assert_contains "${llm_output}" \
  "--profile llm-surgeon up -d --force-recreate integration-debug-lan-proxy"
assert_contains "${llm_output}" \
  "--profile llm-surgeon up -d integration-debug-tailscale-proxy"
assert_contains "${llm_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 /llm llm-surgeon\\ ROS\\ bridge\\ router"
assert_contains "${llm_output}" \
  "+ wait-for-ros-semantic-ready llm-surgeon taskplanner-runtime /simulation/state surgical_msgs/msg/SimulationState /simulation/control surgical_msgs/srv/ControlSimulation"
assert_not_contains "${llm_output}" \
  "--profile llm-surgeon up -d --force-recreate --wait --wait-timeout 300 taskplanner-asr"
llm_build_output="$("${ROOT_DIR}/scripts/taskplanner" up llm-surgeon --dry-run --build)"
assert_single_serial_build "${llm_build_output}" "llm-surgeon"
assert_contains "${llm_build_output}" \
  "--profile llm-surgeon --profile dev run --rm --no-deps --build -T"
assert_not_contains "${llm_build_output}" \
  "--profile llm-surgeon --profile dev --profile debug run"

required_live_observer_preview="$(
  TASKPLANNER_PIPEWIRE_SOCKET=/definitely/missing/taskplanner-pipewire \
  PUZZLE_SURGERY_RECORD_API_KEY_FILE=/definitely/missing/taskplanner-key \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run 2>&1
)"
assert_not_contains "${required_live_observer_preview}" \
  "optional integrated Debug sidecar skipped"
assert_contains "${required_live_observer_preview}" \
  "--profile live --profile debug up -d --force-recreate integration-debug"
assert_contains "${required_live_observer_preview}" \
  "+ wait-for-ros-semantic-ready live taskplanner-runtime /simulation/state surgical_msgs/msg/SimulationState /simulation/control surgical_msgs/srv/ControlSimulation"

default_live_output="$("${ROOT_DIR}/scripts/taskplanner" up --dry-run)"
assert_contains "${default_live_output}" \
  "docker/orchestration/live.env"
assert_contains "${default_live_output}" \
  "--profile live --profile debug up -d --force-recreate integration-debug"

replay_build_output="$("${ROOT_DIR}/scripts/taskplanner" up replay --dry-run --build)"
assert_single_serial_build "${replay_build_output}" "replay"
assert_contains "${replay_build_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 /shadow replay\\ ROS\\ bridge\\ router"
assert_contains "${replay_build_output}" \
  "--profile replay up -d --build --remove-orphans --wait --wait-timeout 300 vllm-manager ninfer-manager webapp"
assert_contains "${replay_build_output}" \
  "--profile replay up -d --build object-perception"
assert_not_contains "${replay_build_output}" \
  "--profile replay up -d --build multicam-observer"
assert_contains "${replay_build_output}" \
  "--profile replay up -d --force-recreate --wait --wait-timeout 300 public-rosbridge"
assert_contains "${replay_build_output}" \
  "--profile replay up -d --force-recreate public-rosbridge-lan-proxy"
assert_contains "${replay_build_output}" \
  "--profile replay up -d --force-recreate integration-debug-lan-proxy"
assert_not_contains "${replay_build_output}" \
  "+ wait_for_multicam_observer replay"
assert_contains "${replay_build_output}" \
  "integration-debug integration-debug-lan-proxy multicam-observer"
assert_contains "${replay_build_output}" \
  "+ wait-for-ros-semantic-ready replay shadow-runner /shadow/replay_state surgical_msgs/msg/ShadowReplayState /shadow/control_replay surgical_msgs/srv/ControlShadowReplay"

python3 - "${ROOT_DIR}/config/cyclonedds_lan.xml" <<'PY'
import sys
from xml.etree import ElementTree

root = ElementTree.parse(sys.argv[1]).getroot()
namespace = {"c": "https://cdds.io/config"}
domain = root.find("c:Domain", namespace)
assert domain is not None
assert domain.attrib == {"Id": "any"}
fragment_size = domain.findtext("c:General/c:FragmentSize", namespaces=namespace)
assert fragment_size == "1344B"
max_message_size = domain.findtext("c:General/c:MaxMessageSize", namespaces=namespace)
assert max_message_size == "1450B"
max_rexmit_message_size = domain.findtext(
    "c:General/c:MaxRexmitMessageSize", namespaces=namespace
)
assert max_rexmit_message_size == "1450B"
peers = domain.findall("c:Discovery/c:Peers/c:Peer", namespace)
assert peers
assert all(set(peer.attrib) == {"Address"} for peer in peers)
PY

live_config="$("${ROOT_DIR}/scripts/taskplanner" config live)"
assert_contains "${live_config}" "taskplanner-asr:"
assert_contains "${live_config}" "integration-debug:"
assert_contains "${live_config}" "TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK: \"true\""
assert_contains "${live_config}" "TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE: \"false\""
assert_contains "${live_config}" "TASKPLANNER_ASR_CAPTURE_LOCK: /taskplanner-runs/asr/microphone.lock"
assert_contains "${live_config}" "SENTENCE_INPUT_TOPIC: /sensors/surgeon/sentence"
assert_contains "${live_config}" "PUZZLE_ASR_ROUTE_POLICY"
assert_contains "${live_config}" "operational_asr_node"
assert_contains "${live_config}" "/input/asr/runtime_status"
assert_contains "${live_config}" "Node name: taskplanner_asr"
assert_not_contains "${live_config}" "colcon build"
printf '%s\n' "${live_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
asr = config["services"]["taskplanner-asr"]
runtime = config["services"]["taskplanner-runtime"]
public_bridge = config["services"]["public-rosbridge"]
integration_debug = config["services"]["integration-debug"]
pnu = config["services"]["pnu-perception"]
multicam_observer = config["services"]["multicam-observer"]
webapp = config["services"]["webapp"]
expected_pnu_digests = (
    "{\"tool\":\"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430\","
    "\"blood\":\"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75\","
    "\"hand\":\"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1\"}"
)
assert runtime["environment"]["PNU_EXPECTED_MODEL_DIGESTS_JSON"] == expected_pnu_digests
expected_support_plane = {
    "PNU_TOOL_SUPPORT_PLANE_NORMAL": "0.046450707785,0.055576411879,-0.997373347443",
    "PNU_TOOL_SUPPORT_PLANE_OFFSET_M": "0.797796943113",
    "PNU_TOOL_SUPPORT_PLANE_CONFIG_VERSION": "viplab_cam4_146222251000_support_plane_v1_sha256_b683ecd5a5382a4f",
    "PNU_TOOL_SUPPORT_PLANE_INLIER_RATIO": "0.854928571429",
    "PNU_TOOL_SUPPORT_PLANE_RESIDUAL_P95_M": "0.014164174501",
    "PNU_TOOL_SUPPORT_PLANE_VALIDATED": "true",
    "PNU_TOOL_SUPPORT_PLANE_ARTIFACT": "/config/pnu_perception/cam4_support_plane_20260821.json",
    "PNU_TOOL_SUPPORT_PLANE_ARTIFACT_SHA256": "859fac1b528976a10ec9dbd67aa1baa979635d31dd944057e6e0c9594d9e68a2",
    "PNU_TOOL_SUPPORT_PLANE_CAMERA_SERIAL": "146222251000",
    "PNU_TOOL_SUPPORT_PLANE_CAMERA_PROFILE": "RGB 1280x720x15; depth 1280x720x15",
    "PNU_TOOL_SUPPORT_PLANE_FIRMWARE_VERSION": "5.15.0.2",
    "PNU_TOOL_SUPPORT_PLANE_MAX_AGE_DAYS": "30",
}
for key, expected in expected_support_plane.items():
    assert pnu["environment"][key] == expected, key
assert runtime["environment"]["PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION"] == (
    expected_support_plane["PNU_TOOL_SUPPORT_PLANE_CONFIG_VERSION"]
)
assert asr["profiles"] == ["live"]
assert asr["healthcheck"]["interval"] == "5s"
assert asr["healthcheck"]["start_interval"] == "1s"
assert asr["healthcheck"]["timeout"] == "6s"
assert asr["healthcheck"]["start_period"] == "10s"
assert asr["healthcheck"]["retries"] == 60
assert asr["stop_grace_period"] == "45s"
assert runtime["stop_signal"] == "SIGINT"
assert runtime["stop_grace_period"] == "15s"
rendered_asr_command = " ".join(asr["command"])
assert "exec /workspaces/taskplanner_ws/install/integration_debug/lib/integration_debug/operational_asr_node" in rendered_asr_command
assert "exec ros2 run" not in rendered_asr_command
rendered_public_bridge_command = " ".join(public_bridge["command"])
assert "exec /workspaces/taskplanner_ws/install/surgical_interop_gateway/lib/surgical_interop_gateway/public_rosbridge" in rendered_public_bridge_command
assert "exec ros2 run" not in rendered_public_bridge_command
assert public_bridge["healthcheck"]["start_interval"] == "1s"
assert public_bridge["healthcheck"]["interval"] == "5s"
assert public_bridge["healthcheck"]["start_period"] == "5s"
assert public_bridge["healthcheck"]["timeout"] == "2s"
assert public_bridge["healthcheck"]["retries"] == 60
rendered_public_health = " ".join(public_bridge["healthcheck"]["test"])
assert "taskplanner_public_bridge_health.py" in rendered_public_health
assert "socket.create_connection" not in rendered_public_health
assert asr["image"] == runtime["image"]
assert asr["network_mode"] == runtime["network_mode"] == "host"
assert asr["user"]
assert asr["environment"]["PUZZLE_ASR_ROUTE_POLICY"] == ""
assert asr["environment"]["PUZZLE_ASR_LAN_HEALTH_INTERVAL_SEC"] == "1.0"
assert asr["environment"]["PUZZLE_ASR_LAN_HEALTH_FAILURE_INTERVAL_SEC"] == "0.5"
assert asr["environment"]["PUZZLE_ASR_LAN_HEALTH_TIMEOUT_SEC"] == "0.5"
assert asr["environment"]["PUZZLE_ASR_LAN_HEALTH_STALE_AFTER_SEC"] == "2.0"
for key in (
    "ROS_DOMAIN_ID",
    "ROS_AUTOMATIC_DISCOVERY_RANGE",
    "RMW_IMPLEMENTATION",
    "CYCLONEDDS_URI",
    "SENTENCE_INPUT_TOPIC",
):
    assert asr["environment"][key] == runtime["environment"][key], key
for service in (runtime, asr, public_bridge, integration_debug):
    environment = service["environment"]
    assert environment["ROS_DOMAIN_ID"] == "0"
    assert environment["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "SUBNET"
    assert environment["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
    assert environment["CYCLONEDDS_URI"].endswith(
        "/config/cyclonedds_lan.xml"
    )
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in environment
assert integration_debug["environment"]["TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE"] == (
    "secure_operational_debug_rosbridge"
)
assert integration_debug["environment"]["ENABLE_PNU_DEBUG_PERCEPTION"] == "false"
assert pnu["environment"]["PNU_HEALTH_REQUIRED_ALGORITHMS"] == "tool,blood,hand"
assert "rosbridge_executable:=$${TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE}" in (
    " ".join(integration_debug["command"])
)
assert runtime["environment"]["CAM4_INPUT_TOPIC"] == (
    "/synced/cam_4/color/image_raw/compressed"
)
assert runtime["environment"]["FLIR_INPUT_TOPIC"] == (
    "/synced/flir/color/image_raw/compressed"
)
assert runtime["environment"]["CV_CAM4_RGB_TOPIC"] == (
    "/synced/cam_4/color/image_raw/compressed"
)
assert runtime["environment"]["CV_CAM4_CAMERA_INFO_TOPIC"] == (
    "/synced/cam_4/color/camera_info"
)
assert runtime["environment"]["CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC"] == (
    "/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth"
)
assert runtime["environment"]["CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC"] == (
    "/synced/cam_4/aligned_depth_to_color/camera_info"
)
assert runtime["environment"]["CV_CAM4_DEPTH_TO_COLOR_EXTRINSICS_TOPIC"] == (
    "/synced/cam_4/extrinsics/depth_to_color"
)
for camera in ("CAM1", "CAM2", "CAM3", "CAM4"):
    assert webapp["environment"][f"VITE_EXTERNAL_{camera}_TOPIC"].startswith(
        "/synced/"
    )
assert webapp["environment"]["VITE_EXTERNAL_FLIR_TOPIC"] == (
    "/synced/flir/color/image_raw/compressed"
)
targets = {volume["target"] for volume in asr["volumes"]}
assert "/workspaces/taskplanner_ws" in targets
assert "/taskplanner-runs" in targets
assert "/run/taskplanner-pipewire/pipewire-0" in targets
assert "taskplanner-asr" not in runtime.get("depends_on", {})
assert public_bridge["profiles"] == ["live", "llm-surgeon", "replay"]
assert public_bridge["network_mode"] == runtime["network_mode"] == "host"
assert public_bridge["ipc"] == runtime["ipc"] == "host"
assert public_bridge["mem_limit"] == "536870912"
assert public_bridge["memswap_limit"] == "536870912"
assert public_bridge["pids_limit"] == 128
assert public_bridge["read_only"] is True
assert public_bridge["restart"] == "unless-stopped"
assert public_bridge["environment"]["ROS_DOMAIN_ID"] == runtime["environment"]["ROS_DOMAIN_ID"]
assert public_bridge["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"] == runtime["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"]
assert public_bridge["environment"]["RMW_IMPLEMENTATION"] == runtime["environment"]["RMW_IMPLEMENTATION"]
rendered_public_command = " ".join(public_bridge["command"])
assert "--port $${PUBLIC_ROSBRIDGE_PORT}" in rendered_public_command
assert "--max_message_size" not in rendered_public_command
assert public_bridge["environment"]["PUBLIC_ROSBRIDGE_PORT"] == "9092"
assert public_bridge["environment"]["ENABLE_PUBLIC_ROSBRIDGE"] == "true"
assert runtime["environment"]["PERCEPTION_BACKEND"] == "local"
for key in (
    "CV_CONTRACT_STATUS_TOPIC",
    "CV_CAM4_RGB_TOPIC",
    "CV_CAM4_RGB_ALIAS_TOPIC",
    "CV_CAM4_CAMERA_INFO_TOPIC",
    "CV_CAM4_NATIVE_DEPTH_COMPRESSED_TOPIC",
    "CV_CAM4_DEPTH_CAMERA_INFO_TOPIC",
    "CV_CAM4_DEPTH_TO_COLOR_EXTRINSICS_TOPIC",
    "CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC",
    "CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC",
    "CV_HANDOVER_TRAY_RGB_TOPIC",
    "CV_HANDOVER_TRAY_CAMERA_INFO_TOPIC",
    "CV_HANDOVER_TRAY_ALIGNED_DEPTH_TOPIC",
):
    assert key in runtime["environment"], key
assert "PUBLIC_ROSBRIDGE_PORT" in " ".join(public_bridge["healthcheck"]["test"])
debug_proxy = config["services"]["integration-debug-lan-proxy"]
assert "4173=127.0.0.1:4173" not in debug_proxy["command"]
assert "9092=127.0.0.1:9092" not in debug_proxy["command"]
webapp_proxy = config["services"]["webapp-lan-proxy"]
assert "profiles" not in webapp_proxy
assert webapp_proxy["restart"] == "unless-stopped"
assert webapp_proxy["network_mode"] == "host"
assert webapp_proxy["read_only"] is True
assert webapp_proxy["cap_drop"] == ["ALL"]
assert webapp_proxy["security_opt"] == ["no-new-privileges:true"]
assert "--bind-address" in webapp_proxy["command"]
assert "192.168.1.4" in webapp_proxy["command"]
assert "--allow-network" in webapp_proxy["command"]
assert "192.168.1.0/24" in webapp_proxy["command"]
assert "4173=127.0.0.1:4173" in webapp_proxy["command"]
proxy = config["services"]["public-rosbridge-lan-proxy"]
assert proxy["profiles"] == ["live", "llm-surgeon", "replay"]
command = proxy["command"]
assert "9092=127.0.0.1:9092" in command
tailscale_proxy = config["services"]["integration-debug-tailscale-proxy"]
assert tailscale_proxy["profiles"] == ["live", "llm-surgeon", "replay", "debug"]
tailscale_command = tailscale_proxy["command"]
assert "9091/live=127.0.0.1:9090" in tailscale_command
assert "9091/llm=127.0.0.1:9090" in tailscale_command
assert "9091/shadow=127.0.0.1:9099" in tailscale_command
assert "9091/multicam=127.0.0.1:9094" in tailscale_command
assert multicam_observer["profiles"] == ["live", "llm-surgeon", "replay", "debug"]
assert multicam_observer["network_mode"] == "host"
assert multicam_observer["read_only"] is True
assert multicam_observer["cap_drop"] == ["ALL"]
assert multicam_observer["security_opt"] == ["no-new-privileges:true"]
assert multicam_observer["restart"] == "unless-stopped"
assert multicam_observer["environment"]["ROS_DOMAIN_ID"] == "0"
assert multicam_observer["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "SUBNET"
assert multicam_observer["environment"]["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
assert multicam_observer["environment"]["CYCLONEDDS_URI"].endswith(
    "/config/cyclonedds_lan.xml"
)
assert multicam_observer["environment"]["ROSBRIDGE_MULTICAM_PORT"] == "9094"
assert "/multicam_node/capture_status" not in " ".join(
    multicam_observer["healthcheck"]["test"]
)
assert "multicam_observer.launch.py" in " ".join(multicam_observer["command"])
assert webapp["network_mode"] == "host"
assert "ports" not in webapp
assert webapp["restart"] == "unless-stopped"
assert webapp["depends_on"]["webapp-lan-proxy"]["condition"] == "service_started"
assert webapp["healthcheck"]["interval"] == "5s"
assert webapp["healthcheck"]["start_interval"] == "1s"
assert webapp["healthcheck"]["timeout"] == "3s"
assert webapp["healthcheck"]["retries"] == 12
webapp_health = " ".join(webapp["healthcheck"]["test"])
assert "http://127.0.0.1:4173/" in webapp_health
assert "/src/App.tsx" in webapp_health
assert "/src/hooks/useRosBridge.ts" in webapp_health
assert "/api/runtime/status" in webapp_health
assert "get_content_type" in webapp_health
assert "text/html" in webapp_health
assert "text/javascript" in webapp_health
assert "application/json" in webapp_health
assert "id=\"root\"" in webapp_health
assert "active_mode" in webapp_health
assert "requested_mode" in webapp_health
assert "retryable" in webapp_health
assert webapp["environment"]["TASKPLANNER_RUNTIME_CONTROL_URL"] == "http://127.0.0.1:8150"
assert webapp["environment"]["TASKPLANNER_RUNTIME_CONTROL_TOKEN_FILE"] == "/run/taskplanner-secrets/runtime-control-token"
assert webapp["environment"]["VITE_ROSBRIDGE_LIVE_TAILSCALE_PORT"] == "9091"
assert webapp["environment"]["VITE_ROSBRIDGE_LIVE_TAILSCALE_PATH"] == "/live"
assert webapp["environment"]["VITE_ROSBRIDGE_TAILSCALE_PORT"] == "9091"
assert webapp["environment"]["VITE_ROSBRIDGE_LLM_TAILSCALE_PATH"] == "/llm"
assert webapp["environment"]["VITE_ROSBRIDGE_SHADOW_TAILSCALE_PATH"] == "/shadow"
'

auto_asr_config="$(
  PUZZLE_ASR_ROUTE_POLICY=auto \
    "${ROOT_DIR}/scripts/taskplanner" config live
)"
printf '%s\n' "${auto_asr_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
asr = config["services"]["taskplanner-asr"]
debug = config["services"]["integration-debug"]
assert asr["environment"]["PUZZLE_ASR_ROUTE_POLICY"] == "auto"
assert "PUZZLE_ASR_ROUTE_POLICY" not in debug["environment"]
'

stale_fastdds_config="$(
  FASTRTPS_DEFAULT_PROFILES_FILE=/tmp/must-not-leak.xml \
    "${ROOT_DIR}/scripts/taskplanner" config live
)"
printf '%s\n' "${stale_fastdds_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
for name in (
    "taskplanner-runtime",
    "taskplanner-asr",
    "public-rosbridge",
    "integration-debug",
):
    environment = config["services"][name]["environment"]
    assert environment["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in environment, name
'

llm_config="$("${ROOT_DIR}/scripts/taskplanner" config llm-surgeon)"
assert_not_contains "${llm_config}" "taskplanner-asr:"
assert_not_contains "${llm_config}" "operational_asr_node"
assert_not_contains "${llm_config}" "colcon build"
# Optional ROS launch arguments must be omitted when their environment values
# are empty. Passing an empty ``name:=`` token makes ros2 launch reject the
# entire runtime before the bridge can become ready.
for optional_launch_arg in PERCEPTION_PROVIDER PERCEPTION_LOCATION PERCEPTION_ENDPOINT PNU_SERVICE_URL; do
  optional_launch_name="${optional_launch_arg,,}"
  printf -v optional_launch_token '$${%s:+%s:=$${%s}}' \
    "${optional_launch_arg}" "${optional_launch_name}" "${optional_launch_arg}"
  printf -v legacy_empty_launch_token '%s:=$${%s:-}' \
    "${optional_launch_name}" "${optional_launch_arg}"
  assert_contains "${llm_config}" \
    "${optional_launch_token}"
  assert_not_contains "${llm_config}" \
    "${legacy_empty_launch_token}"
done
empty_optional_arg="$(PERCEPTION_ENDPOINT= bash -c 'printf "%s" ${PERCEPTION_ENDPOINT:+perception_endpoint:=${PERCEPTION_ENDPOINT}}')"
[[ -z "${empty_optional_arg}" ]] || fail "empty perception endpoint must be omitted"
set_optional_arg="$(PERCEPTION_ENDPOINT=http://127.0.0.1:8010 bash -c 'printf "%s" ${PERCEPTION_ENDPOINT:+perception_endpoint:=${PERCEPTION_ENDPOINT}}')"
[[ "${set_optional_arg}" == "perception_endpoint:=http://127.0.0.1:8010" ]] || \
  fail "configured perception endpoint must be passed as a launch argument"
assert_not_contains "${llm_config}" "integration-debug:"

debug_output="$("${ROOT_DIR}/scripts/taskplanner" up debug --dry-run)"
assert_contains "${debug_output}" \
  "--profile debug up -d webapp"
assert_contains "${debug_output}" \
  "--profile debug up -d --wait --wait-timeout 300 ninfer-manager"
assert_contains "${debug_output}" \
  "--profile debug up -d multicam-observer"
assert_contains "${debug_output}" \
  "--profile debug up -d pnu-perception"
assert_contains "${debug_output}" \
  "--profile debug up -d --wait --wait-timeout 30 pnu-perception"
assert_contains "${debug_output}" \
  "--profile debug up -d --force-recreate integration-debug integration-debug-lan-proxy"
assert_contains "${debug_output}" \
  "--profile debug up -d integration-debug-tailscale-proxy"
assert_contains "${debug_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 / debug\\ ROS\\ bridge\\ router"
assert_contains "${debug_output}" \
  "+ wait_for_multicam_observer debug"
assert_contains "${debug_output}" \
  "+ wait-for-ros-semantic-ready debug integration-debug /integration/debug/status std_msgs/msg/String /integration/debug/check_readiness std_srvs/srv/Trigger"
assert_not_contains "${debug_output}" \
  "--profile debug up -d taskplanner-runtime"

disabled_debug_pnu_output="$(
  ENABLE_PNU_DEBUG_PERCEPTION=false \
    "${ROOT_DIR}/scripts/taskplanner" up debug --dry-run
)"
assert_not_contains "${disabled_debug_pnu_output}" \
  "--profile debug up -d pnu-perception"
assert_not_contains "${disabled_debug_pnu_output}" \
  "--profile debug up -d --wait --wait-timeout 30 pnu-perception"

remote_debug_pnu_output="$(
  ENABLE_PNU_DEBUG_PERCEPTION=true \
  PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
    "${ROOT_DIR}/scripts/taskplanner" up debug --dry-run
)"
assert_not_contains "${remote_debug_pnu_output}" \
  "--profile debug up -d pnu-perception"
assert_contains "${remote_debug_pnu_output}" \
  "--profile debug up -d --force-recreate integration-debug integration-debug-lan-proxy"
assert_not_contains "${remote_debug_pnu_output}" "launcher-test-token"

missing_debug_pnu_token_output="$(mktemp)"
if ENABLE_PNU_DEBUG_PERCEPTION=true \
  PERCEPTION_PROVIDER=pnu_hand_blood \
  PERCEPTION_LOCATION=remote \
  PERCEPTION_ENDPOINT=https://192.168.1.20:8020 \
  PNU_SECRET_ROOT="${PNU_TOKEN_TEST_ROOT}" \
  PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/missing-token \
  "${ROOT_DIR}/scripts/taskplanner" up debug --dry-run \
    >"${missing_debug_pnu_token_output}" 2>&1; then
  fail "remote Debug PNU perception must reject a missing mounted token"
fi
assert_contains "$(cat "${missing_debug_pnu_token_output}")" \
  "configured token file does not exist for integration-debug"
rm -f "${missing_debug_pnu_token_output}"

debug_build_output="$("${ROOT_DIR}/scripts/taskplanner" up debug --dry-run --build)"
assert_single_serial_build "${debug_build_output}" "debug"
assert_contains "${debug_build_output}" \
  "stop taskplanner-runtime public-rosbridge public-rosbridge-lan-proxy taskplanner-asr shadow-runner object-perception pnu-perception integration-debug integration-debug-lan-proxy multicam-observer"
assert_contains "${debug_build_output}" \
  "rm -f taskplanner-runtime public-rosbridge public-rosbridge-lan-proxy taskplanner-asr shadow-runner object-perception pnu-perception integration-debug integration-debug-lan-proxy multicam-observer"
assert_contains "${debug_build_output}" \
  "colcon\\ build\\ --symlink-install\\ --packages-up-to\\ integration_debug\\ vlm_node\\ hand_keypoint_interfaces\\ surgical_perception_msgs"
assert_contains "${debug_build_output}" \
  "test\\ -x\\ install/vlm_node/lib/vlm_node/pnu_perception_bridge"
assert_contains "${debug_build_output}" \
  "install/hand_keypoint_interfaces/share/ament_index/resource_index/packages/hand_keypoint_interfaces"
assert_contains "${debug_build_output}" \
  "install/surgical_perception_msgs/share/ament_index/resource_index/packages/surgical_perception_msgs"

debug_config="$("${ROOT_DIR}/scripts/taskplanner" config debug)"
debug_lan_config="$(
  PUZZLE_ASR_ENDPOINT=lan \
  PUZZLE_ASR_LAN_URL=ws://192.168.1.5:1196/ \
    "${ROOT_DIR}/scripts/taskplanner" config debug
)"
replay_config="$("${ROOT_DIR}/scripts/taskplanner" config replay)"
for optional_launch_arg in PERCEPTION_PROVIDER PERCEPTION_LOCATION PERCEPTION_ENDPOINT PNU_SERVICE_URL; do
  optional_launch_name="${optional_launch_arg,,}"
  printf -v optional_launch_token '$${%s:+%s:=$${%s}}' \
    "${optional_launch_arg}" "${optional_launch_name}" "${optional_launch_arg}"
  assert_contains "${replay_config}" "${optional_launch_token}"
done
assert_not_contains "${debug_config}" "colcon build"
assert_not_contains "${replay_config}" "colcon build"
for rendered_config in "${live_config}" "${llm_config}" "${debug_config}" "${replay_config}"; do
  assert_contains "${rendered_config}" "VITE_DEFAULT_RUNTIME_MODE: live"
done
printf '%s\n' "${debug_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
debug = config["services"]["integration-debug"]
pnu = config["services"]["pnu-perception"]
assert debug["environment"]["TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE"] == (
    "secure_debug_rosbridge"
)
environment = debug["environment"]
assert environment["ENABLE_PNU_DEBUG_PERCEPTION"] == "true"
assert environment["PERCEPTION_PROVIDER"] == "pnu_hand_blood"
assert environment["PERCEPTION_LOCATION"] == "local"
assert environment["PERCEPTION_ENDPOINT"] == "http://127.0.0.1:8020"
assert environment["PNU_DEBUG_REQUESTED_ALGORITHMS"] == "tool,blood,hand"
assert environment["PNU_EXPECTED_MODEL_DIGESTS_JSON"] == (
    "{\"tool\":\"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430\","
    "\"blood\":\"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75\","
    "\"hand\":\"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1\"}"
)
assert environment["PNU_DEPTH_SCALE_M_PER_UNIT"] == "0.001"
assert environment["PNU_DEPTH_SCALE_VALIDATED"] == "true"
assert environment["PNU_DEPTH_ALIGNMENT_VALIDATED"] == "true"
assert environment["PNU_ALLOW_INSECURE_REMOTE_HTTP"] == "false"
assert pnu["profiles"] == ["live", "replay", "debug"]
assert pnu["environment"]["PNU_HEALTH_REQUIRED_ALGORITHMS"] == "tool,blood,hand"
healthcheck = " ".join(pnu["healthcheck"]["test"])
assert "PNU_HEALTH_REQUIRED_ALGORITHMS" in healthcheck
assert "p.get('ready') is True" not in healthcheck
mounts = {volume["target"] for volume in debug["volumes"]}
assert "/run/taskplanner/perception" in mounts
'
printf '%s\n' "${debug_lan_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
debug = config["services"]["integration-debug"]
environment = debug["environment"]
assert environment["PUZZLE_ASR_ENDPOINT"] == "lan"
assert environment["PUZZLE_ASR_LAN_URL"] == "ws://192.168.1.5:1196/"
'
printf '%s\n' "${replay_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
shadow = config["services"]["shadow-runner"]
public_bridge = config["services"]["public-rosbridge"]
public_proxy = config["services"]["public-rosbridge-lan-proxy"]
operator_proxy = config["services"]["integration-debug-lan-proxy"]
webapp_proxy = config["services"]["webapp-lan-proxy"]
environment = shadow["environment"]
assert shadow["stop_signal"] == "SIGINT"
assert shadow["stop_grace_period"] == "15s"
assert environment["ROS_DOMAIN_ID"] == "71"
assert environment["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "LOCALHOST"
assert environment["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
assert environment["PUBLISH_SHARED_STATE"] == "true"
assert environment["PUBLISH_SHARED_FREE_TEXT"] == "true"
assert "CYCLONEDDS_URI" not in environment
assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in environment
assert public_bridge["environment"]["ROS_DOMAIN_ID"] == environment["ROS_DOMAIN_ID"]
assert public_bridge["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "LOCALHOST"
assert public_bridge["environment"]["CYCLONEDDS_URI"] == ""
assert public_bridge["environment"]["PUBLIC_ROSBRIDGE_PORT"] == "9092"
assert public_bridge["environment"]["ENABLE_PUBLIC_ROSBRIDGE"] == "true"
assert public_proxy["profiles"] == ["live", "llm-surgeon", "replay"]
assert "9092=127.0.0.1:9092" in public_proxy["command"]
assert operator_proxy["profiles"] == ["live", "llm-surgeon", "debug", "replay"]
assert "4173=127.0.0.1:4173" not in operator_proxy["command"]
assert "9091=127.0.0.1:9091" in operator_proxy["command"]
assert "profiles" not in webapp_proxy
assert "192.168.1.4" in webapp_proxy["command"]
assert "192.168.1.0/24" in webapp_proxy["command"]
assert "4173=127.0.0.1:4173" in webapp_proxy["command"]
'
grep -q 'acquire_launcher_lock' "${ROOT_DIR}/scripts/taskplanner" ||
  fail "launcher lifecycle must be serialized across concurrent invocations"
grep -q -- '--property=Restart=on-failure' "${ROOT_DIR}/scripts/taskplanner" ||
  fail "runtime-control must restart after an unexpected process failure"
grep -q -- '--property=RestartSec=2' "${ROOT_DIR}/scripts/taskplanner" ||
  fail "runtime-control restart backoff must remain bounded"
[[ "$(grep -c '^    acquire_launcher_lock$' "${ROOT_DIR}/scripts/taskplanner")" -ge "2" ]] ||
  fail "both launcher up and down must acquire the lifecycle lock"
[[ "$(grep -c '^[[:space:]]\+verify_runtime_control_transition_interlock$' "${ROOT_DIR}/scripts/taskplanner")" == "2" ]] ||
  fail "both runtime replacement branches must repeat the stopped-state gate"
gate_output="$(
  TASKPLANNER_RUNTIME_REQUIRE_STOPPED=1 \
  TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE=replay \
    "${ROOT_DIR}/scripts/taskplanner" up replay --dry-run
)"
gate_line="$(grep -n -F '+ verify-runtime-transition-interlock replay' <<<"${gate_output}" | cut -d: -f1)"
install_gate_line="$(grep -n -F 'taskplanner-install-check' <<<"${gate_output}" | cut -d: -f1)"
stop_line="$(grep -n -F ' stop taskplanner-runtime' <<<"${gate_output}" | head -n 1 | cut -d: -f1)"
[[ -n "${install_gate_line}" && -n "${gate_line}" && -n "${stop_line}" && \
  "${install_gate_line}" -lt "${gate_line}" && "${gate_line}" -lt "${stop_line}" ]] ||
  fail "controller child must recheck stopped state before marker clear and stop"
grep -q 'owner container exited before the endpoint became ready' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "ROS bridge readiness must fail fast when its core container exits"
grep -q 'runtime startup failed; removing partial runtime services' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "failed runtime starts must clean up partial services"
python3 - "${ROOT_DIR}/scripts/taskplanner" <<'PY'
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
gates = [
    index
    for index, line in enumerate(lines)
    if line.strip() == "verify_runtime_control_transition_interlock"
]
assert len(gates) == 2
for index in gates:
    assert any(
        line.strip() == "clear_active_runtime_mode"
        for line in lines[index + 1 : index + 8]
    )
PY
grep -q 'exec \${LAUNCHER_LOCK_FD}>&-' "${ROOT_DIR}/scripts/taskplanner" ||
  fail "LM Studio child must not inherit the Taskplanner launcher lock"

grep -qx 'WEBAPP_PORT=4173' \
  "${ROOT_DIR}/docker/orchestration/debug.env" || \
  fail "standalone Debug UI must use port 4173"
grep -qx 'TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=false' \
  "${ROOT_DIR}/docker/orchestration/debug.env" || \
  fail "standalone Debug must default to fail-closed coexistence"
grep -qx 'TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=false' \
  "${ROOT_DIR}/docker/orchestration/live.env" || \
  fail "live integrated Debug must default to fail-closed coexistence"
grep -qx 'TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=true' \
  "${ROOT_DIR}/docker/orchestration/live.env" || \
  fail "live integrated Debug must use the runtime DDS network"
grep -qx 'ENABLE_PUBLIC_ROSBRIDGE=true' \
  "${ROOT_DIR}/docker/orchestration/live.env" || \
  fail "live public read-only bridge must default enabled"
grep -q 'export TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=true' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "live launcher must override an unsafe shell network-lock value"
grep -q 'export TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=false' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "live launcher must override an unsafe shell coexistence value"
grep -q 'export ENABLE_PNU_DEBUG_PERCEPTION=false' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "live launcher must prevent a duplicate PNU bridge in its Debug sidecar"
grep -q 'export PNU_WORKER_REQUIRED_ALGORITHMS=tool,blood,hand' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "live launcher must preserve all-model PNU worker readiness"

temporary_dir="$(mktemp -d)"
trap 'rm -rf "${temporary_dir}"' EXIT
printf '%s\n' \
  '{' \
  '  "schema": "taskplanner.integration_debug.network_settings.v1",' \
  '  "domain_id": 97,' \
  '  "discovery_range": "LOCALHOST",' \
  '  "updated_at": "2026-08-12T00:00:00+00:00"' \
  '}' >"${temporary_dir}/network-settings.json"

network_output="$(
  PYTHONPATH="${ROOT_DIR}/src/integration_debug" \
  TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=true \
  TASKPLANNER_DEBUG_NETWORK_INTERFACE=lo \
  ROS_DOMAIN_ID=0 \
  ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET \
  RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
  CYCLONEDDS_URI=file:///tmp/cyclonedds-test.xml \
  FASTRTPS_DEFAULT_PROFILES_FILE=/tmp/must-not-leak.xml \
    python3 "${ROOT_DIR}/scripts/run_integration_debug.py" \
      --settings "${temporary_dir}/network-settings.json" \
      -- python3 -c \
        'import os; print("ACTIVE_NETWORK=" + os.environ["ROS_DOMAIN_ID"] + "/" + os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"]); print("ACTIVE_RMW=" + os.environ["RMW_IMPLEMENTATION"]); print("FASTRTPS_PRESENT=" + str("FASTRTPS_DEFAULT_PROFILES_FILE" in os.environ)); print("CYCLONEDDS_URI=" + os.environ.get("CYCLONEDDS_URI", ""))'
)"
assert_contains "${network_output}" \
  "Locked integrated Debug Mode to runtime network: domain=0 discovery=SUBNET"
assert_contains "${network_output}" "ACTIVE_NETWORK=0/SUBNET"
assert_contains "${network_output}" "ACTIVE_RMW=rmw_cyclonedds_cpp"
assert_contains "${network_output}" "FASTRTPS_PRESENT=False"
assert_contains "${network_output}" "CYCLONEDDS_URI=file:///tmp/cyclonedds-test.xml"
assert_not_contains "${network_output}" "ACTIVE_NETWORK=97/LOCALHOST"

standalone_network_output="$(
  PYTHONPATH="${ROOT_DIR}/src/integration_debug" \
  TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=false \
  TASKPLANNER_DEBUG_NETWORK_INTERFACE=lo \
  ROS_DOMAIN_ID=0 \
  ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET \
  RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
  CYCLONEDDS_URI=file:///tmp/must-not-leak.xml \
    python3 "${ROOT_DIR}/scripts/run_integration_debug.py" \
      --settings "${temporary_dir}/network-settings.json" \
      -- python3 -c \
        'import os; print("ACTIVE_NETWORK=" + os.environ["ROS_DOMAIN_ID"] + "/" + os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"]); print("CYCLONEDDS_PRESENT=" + str("CYCLONEDDS_URI" in os.environ))'
)"
assert_contains "${standalone_network_output}" \
  "Loaded Debug Mode network settings: domain=97 discovery=LOCALHOST"
assert_contains "${standalone_network_output}" "ACTIVE_NETWORK=97/LOCALHOST"
assert_contains "${standalone_network_output}" "CYCLONEDDS_PRESENT=False"

printf 'PASS: launcher serializes colcon before runtime/sidecars and preserves mode gates\n'
