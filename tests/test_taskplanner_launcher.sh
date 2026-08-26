#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${ROOT_DIR}/scripts/taskplanner"
DESKTOP_LAUNCHER="${ROOT_DIR}/scripts/taskplanner_clean_start_live_thyroidectomy_demo.sh"
WEBAPP_LAUNCHER="${ROOT_DIR}/webapp/scripts/start-production.sh"
EXECUTION_LAYER="${ROOT_DIR}/scripts/lib/taskplanner_execution.sh"

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

bash -n \
  "${LAUNCHER}" \
  "${DESKTOP_LAUNCHER}" \
  "${WEBAPP_LAUNCHER}" \
  "${EXECUTION_LAYER}"

launcher_source="$(<"${LAUNCHER}")"
assert_contains "${launcher_source}" '.taskplanner-asr-abi-contract-v1'
assert_contains "${launcher_source}" 'asr_install_contract_fingerprint()'
assert_contains "${launcher_source}" 'record_asr_install_contract'

webapp_source_digest="$(
  cd "${ROOT_DIR}/webapp"
  bash "${WEBAPP_LAUNCHER}" --print-source-digest
)"
[[ "${webapp_source_digest}" =~ ^[0-9a-f]{64}$ ]] ||
  fail 'webapp source digest contract must return one SHA-256 value'

webapp_contract_dir="$(mktemp -d)"
trap 'rm -rf "${webapp_contract_dir}"' EXIT
mkdir -p \
  "${webapp_contract_dir}/scripts" \
  "${webapp_contract_dir}/src" \
  "${webapp_contract_dir}/public" \
  "${webapp_contract_dir}/monitor" \
  "${webapp_contract_dir}/dist/monitor"
cp "${WEBAPP_LAUNCHER}" "${webapp_contract_dir}/scripts/start-production.sh"
for runtime_script in \
  serve-production.mjs check-dev-server-contract.mjs check-monitor-bundle.cjs; do
  printf '%s\n' "${runtime_script}" \
    >"${webapp_contract_dir}/scripts/${runtime_script}"
done
for source_file in \
  index.html package.json package-lock.json tsconfig.json vite.config.ts; do
  printf '%s\n' "${source_file}" >"${webapp_contract_dir}/${source_file}"
done
printf 'source\n' >"${webapp_contract_dir}/src/app.ts"
printf 'built\n' >"${webapp_contract_dir}/dist/index.html"
printf 'monitor\n' >"${webapp_contract_dir}/dist/monitor/index.html"
contract_digest="$(
  cd "${webapp_contract_dir}"
  bash scripts/start-production.sh --print-source-digest
)"
printf '%s\n' "${contract_digest}" \
  >"${webapp_contract_dir}/dist/.taskplanner-source.sha256"
(
  cd "${webapp_contract_dir}"
  bash scripts/start-production.sh --check-build-current
) || fail 'matching webapp source/build stamp must be current'
printf '%064d\n' 1 >"${webapp_contract_dir}/dist/.taskplanner-source.sha256"
if (
  cd "${webapp_contract_dir}"
  bash scripts/start-production.sh --check-build-current
); then
  fail 'mismatched webapp source/build stamp must be stale'
fi

# A stale frontend gets its own recreate command; the aggregate health wait
# must not inherit --force-recreate and churn NInfer or other preserved lanes.
python3 - "${LAUNCHER}" "${EXECUTION_LAYER}" <<'PY'
import sys
from pathlib import Path

launcher = Path(sys.argv[1]).read_text(encoding="utf-8")
execution = Path(sys.argv[2]).read_text(encoding="utf-8")
helper_start = execution.index("refresh_webapp_if_stale() {")
helper_end = execution.index("\nwarm_restart_runtime_service() {", helper_start)
helper = execution[helper_start:helper_end]
assert '--no-deps --force-recreate webapp' in helper
assert 'webapp_build_is_current' in helper

aggregate = '''up -d "${BUILD_ARGS[@]}" \\
      --wait --wait-timeout "${WAIT_TIMEOUT_SEC}" \\
      ninfer-manager webapp'''
assert aggregate in launcher
assert '--force-recreate' not in aggregate
assert launcher.index('refresh_webapp_if_stale "${runtime_profile_args[@]}"') < launcher.index(aggregate)
assert 'source "${ROOT_DIR}/scripts/lib/taskplanner_execution.sh"' in launcher
PY

# Production is one fixed provider plus remote typed RF-DETR inputs. Stale
# shell selections must not resurrect local perception or a second model.
live_config="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
  VLM_PROVIDER_ID=vllm \
  VLM_MODEL_ID=legacy/model \
  VLLM_PROVIDER_ENABLED=true \
  LMSTUDIO_PROVIDER_ENABLED=true \
  UNSLOTH_PROVIDER_ENABLED=true \
  PERCEPTION_PROVIDER=builtin_rfdetr \
  PERCEPTION_LOCATION=local \
  PERCEPTION_ENDPOINT=http://127.0.0.1:8010 \
  PERCEPTION_BACKEND=local \
    "${LAUNCHER}" config live
)"
for required in \
  'VLM_PROVIDER_ID: ninfer' \
  'VLM_MODEL_ID: qwen3.6-35b-a3b' \
  'VLLM_PROVIDER_ENABLED: "false"' \
  'LMSTUDIO_PROVIDER_ENABLED: "false"' \
  'UNSLOTH_PROVIDER_ENABLED: "false"' \
  'NINFER_PROVIDER_ENABLED: "true"' \
  'ENABLE_RFDETR_PERCEPTION: "false"' \
  'PERCEPTION_PROVIDER: external_rfdetr_topics' \
  'PERCEPTION_LOCATION: remote' \
  'PERCEPTION_BACKEND: external' \
  'RFDETR_SERVICE_URL: ""' \
  'CAM3_TOOL_OBSERVATIONS_TOPIC: /perception/cam_3/tool/observations' \
  'CAM4_TOOL_OBSERVATIONS_TOPIC: /perception/cam_4/tool/observations' \
  'WEBAPP_BUILD_ON_START: "false"'; do
  assert_contains "${live_config}" "${required}"
done
# Production deliberately leaves the deprecated local RF-DETR URL empty.  Do
# not turn that empty value into the invalid ros2 launch token
# `rfdetr_service_url:=`; the alias must be omitted entirely in that case.
assert_contains "${live_config}" '$${RFDETR_SERVICE_URL:+rfdetr_service_url:=$${RFDETR_SERVICE_URL}}'
assert_not_contains "${live_config}" 'rfdetr_service_url:=$${RFDETR_SERVICE_URL-}'
for production_exclusion in \
  '  vllm-manager:' \
  '  object-perception:' \
  '  pnu-perception:' \
  '  taskplanner-tts:' \
  '  multicam-observer:' \
  '  integration-debug:' \
  '  monitor-media-gateway:'; do
  assert_not_contains "${live_config}" "${production_exclusion}"
done

live_output="$(TASKPLANNER_RUNTIME_CONTROL_CHILD=1 "${LAUNCHER}" up live --dry-run)"
assert_contains "${live_output}" '/workspaces/taskplanner_ws/install/docker/setup.bash'
assert_not_contains "${live_output}" '/workspaces/taskplanner_ws/install/setup.bash'
assert_contains "${live_output}" '--profile live up -d --wait --wait-timeout 300 ninfer-manager webapp'
assert_contains "${live_output}" '--profile live up -d --wait --wait-timeout 300 taskplanner-asr'
assert_contains "${live_output}" '--profile live up -d taskplanner-runtime'
assert_not_contains "${live_output}" '--profile live up -d --force-recreate taskplanner-runtime'
assert_contains "${live_output}" '--profile live up -d --wait --wait-timeout 300 public-rosbridge'
assert_contains "${live_output}" '+ ensure-ninfer-model-loaded qwen3.6-35b-a3b'
assert_contains "${live_output}" '+ wait-for-websocket 127.0.0.1 9090 / live\ ROS\ bridge\ router'

# Converging to Production must also remove optional containers created by an
# older release with a persistent Docker restart policy.
live_stop_command="$(grep -F ' stop ' <<<"${live_output}" | head -n 1)"
for stale_service in \
  webapp-lan-proxy \
  monitor-media-gateway \
  integration-debug-tailscale-proxy \
  vllm-manager; do
  [[ "$(tr ' ' '\n' <<<"${live_stop_command}" | grep -Fxc "${stale_service}" || true)" == "1" ]] ||
    fail "Live convergence must stop exactly one ${stale_service} container"
done

# Optional services may be stopped when converging to Production, but must not
# be started or waited on by the default Live path.
for forbidden_start in \
  'up -d vllm-manager' \
  'up -d object-perception' \
  'up -d pnu-perception' \
  'up -d --wait --wait-timeout 300 taskplanner-tts' \
  'up -d multicam-observer' \
  'up -d --force-recreate integration-debug' \
  'up -d webapp-lan-proxy' \
  'up -d monitor-media-gateway' \
  'up -d integration-debug-tailscale-proxy'; do
  assert_not_contains "${live_output}" "${forbidden_start}"
done
assert_not_contains "${live_output}" ' --remove-orphans '
assert_not_contains "${live_output}" ' up -d --build '

# A requested rebuild is serialized once. Container/host CMake caches use
# different roots, and --build is not forwarded to every service startup.
live_build_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${LAUNCHER}" up live --dry-run --build
)"
[[ "$(grep -F -c 'colcon\ --log-base' <<<"${live_build_output}" || true)" == "1" ]] ||
  fail 'Live --build must run exactly one foreground colcon build'
assert_contains "${live_build_output}" '--build-base\ build/docker\ --install-base\ install/docker'
assert_contains "${live_build_output}" '-e TASKPLANNER_SKIP_WORKSPACE_SETUP=true'
live_builder_command="$(grep -F ' run --rm --no-deps ' <<<"${live_build_output}" | grep -F 'colcon\ --log-base' | head -n 1)"
[[ "$(tr ' ' '\n' <<<"${live_builder_command}" | grep -Fxc -- '--build' || true)" == "1" ]] ||
  fail 'explicit Live --build must rebuild the shared image exactly once'
assert_contains "${live_build_output}" '--profile live build ninfer-manager'
assert_not_contains "${live_build_output}" ' up -d --build '
live_build_stop_command="$(grep -F ' stop ' <<<"${live_build_output}" | head -n 1)"
[[ "$(tr ' ' '\n' <<<"${live_build_stop_command}" | grep -Fxc taskplanner-asr || true)" == "1" ]] ||
  fail 'Live build convergence must stop the old ASR process before replacing the generated ROS overlay'

# A stale --ensure-build only refreshes install/docker in the existing image.
# It must not rebuild the taskplanner or NInfer images.  This workspace starts
# the test without a trusted install/docker stamp, so the dry run exposes the
# stale branch without starting any service.
if [[ ! -s "${ROOT_DIR}/install/docker/.taskplanner-runtime-abi-contract-v1" ]]; then
  live_ensure_output="$(
    TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
      "${LAUNCHER}" up live --dry-run --ensure-build 2>&1
  )"
  assert_contains "${live_ensure_output}" 'runtime install is stale or incomplete; rebuilding the selected live runtime before startup'
  assert_contains "${live_ensure_output}" 'colcon\ --log-base'
  ensure_builder_command="$(grep -F ' run --rm --no-deps ' <<<"${live_ensure_output}" | grep -F 'colcon\ --log-base' | head -n 1)"
  [[ "$(tr ' ' '\n' <<<"${ensure_builder_command}" | grep -Fxc -- '--build' || true)" == "0" ]] ||
    fail 'stale Live --ensure-build must reuse the existing shared image'
  assert_not_contains "${live_ensure_output}" '--profile live build ninfer-manager'
  ensure_stop_command="$(grep -F ' stop ' <<<"${live_ensure_output}" | head -n 1)"
  [[ "$(tr ' ' '\n' <<<"${ensure_stop_command}" | grep -Fxc taskplanner-asr || true)" == "1" ]] ||
    fail 'stale Live --ensure-build must stop the old ASR process before overlay replacement'
fi

# Replay and Debug used to forward --build into several sequential `up`
# commands.  Both now build their distinct images once and then reuse them.
for build_mode in replay debug; do
  mode_build_output="$(
    TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
      "${LAUNCHER}" up "${build_mode}" --dry-run --build
  )"
  [[ "$(grep -F -c 'colcon\ --log-base' <<<"${mode_build_output}" || true)" == "1" ]] ||
    fail "${build_mode} --build must run exactly one foreground colcon build"
  [[ "$(grep -F -c -- "--profile ${build_mode} build ninfer-manager" <<<"${mode_build_output}" || true)" == "1" ]] ||
    fail "${build_mode} --build must build NInfer exactly once"
  assert_not_contains "${mode_build_output}" ' up -d --build '
done

# The same-mode warm boundary is authoritative even for direct launcher use:
# reserve/recheck must precede marker clear and Compose restart. Its failure
# cleanup may remove only the failed core, never the preserved public bridge.
python3 - "${LAUNCHER}" "${EXECUTION_LAYER}" <<'PY'
import sys
from pathlib import Path

launcher = Path(sys.argv[1]).read_text(encoding="utf-8")
execution = Path(sys.argv[2]).read_text(encoding="utf-8")
start = execution.index('taskplanner_cross_warm_restart_boundary() {')
end = execution.index('\nrun_serial_workspace_build() {', start)
boundary = execution[start:end]
ordered = (
    'RUNTIME_FAILURE_CLEANUP_SERVICES=("${service}")',
    'verify_same_mode_warm_restart_interlock "${mode}"',
    'RUNTIME_FAILURE_CLEANUP_ARMED=true',
    'clear_active_runtime_mode',
    'warm_restart_runtime_service "${mode}" "${service}"',
)
positions = [boundary.index(token) for token in ordered]
assert positions == sorted(positions), positions
assert 'public-rosbridge' not in boundary
call = '''taskplanner_cross_warm_restart_boundary \\
        "${mode}" "${runtime_service}"'''
assert call in launcher
assert launcher.index(call) < launcher.index('elif [[ "${mode}" == "replay" ]]', launcher.index(call))
assert 'final_transition_interlock_is_safe' in launcher
PY

# The desktop shortcut is a scoped warm restart. It must not issue the old
# global `down`, reselect the already-loaded model through ROS, or start local
# perception/accessory profiles.
desktop_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${DESKTOP_LAUNCHER}" --dry-run --terminal
)"
assert_contains "${desktop_output}" 'warm restart'
assert_contains "${desktop_output}" '192.168.1.7 typed ROS topics'
assert_contains "${desktop_output}" '+ ensure-ninfer-model-loaded qwen3.6-35b-a3b'
assert_not_contains "${desktop_output}" ' taskplanner down'
assert_not_contains "${desktop_output}" 'select_model_provider'
assert_not_contains "${desktop_output}" 'up -d object-perception'
assert_not_contains "${desktop_output}" 'up -d pnu-perception'

# Ops stays opt-in and composes independently from the Production core.
ops_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
  TASKPLANNER_LIVE_ENABLE_OPS=true \
  TASKPLANNER_LIVE_ENABLE_MULTICAM=true \
  TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG=true \
  TASKPLANNER_LIVE_ENABLE_TTS=true \
    "${LAUNCHER}" up live --dry-run
)"
assert_contains "${ops_output}" '--profile live --profile ops up -d --force-recreate --wait --wait-timeout 300 taskplanner-tts'
assert_contains "${ops_output}" 'up -d multicam-observer'
assert_contains "${ops_output}" 'up -d --force-recreate integration-debug'
assert_contains "${ops_output}" 'up -d webapp-lan-proxy monitor-media-gateway integration-debug-lan-proxy integration-debug-tailscale-proxy'
assert_contains "${ops_output}" '+ wait-for-websocket 127.0.0.1 9091 /live live\ ROS\ bridge\ router'

# Docker context must never include the multi-gigabyte generated evidence.
for ignored in 'reports/release/' 'test_outputs/' 'webapp/output/'; do
  grep -Fxq "${ignored}" "${ROOT_DIR}/.dockerignore" ||
    fail "Docker context exclusion is missing: ${ignored}"
done

python3 - "${ROOT_DIR}/docker-compose.yml" <<'PY'
import sys
import yaml

services = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["services"]
assert services["vllm-manager"]["profiles"] == ["lab"]
assert services["object-perception"]["profiles"] == ["lab"]
assert "live" not in services["pnu-perception"]["profiles"]
for name in (
    "taskplanner-tts",
    "webapp-lan-proxy",
    "public-rosbridge-lan-proxy",
    "monitor-media-gateway",
):
    assert services[name]["profiles"] == ["ops"]
for name in (
    "taskplanner-tts",
    "multicam-observer",
    "integration-debug",
    "webapp-lan-proxy",
    "integration-debug-lan-proxy",
    "public-rosbridge-lan-proxy",
    "integration-debug-tailscale-proxy",
    "monitor-media-gateway",
):
    assert services[name]["restart"] == "no", (
        f"optional service {name} must not resurrect after a host reboot"
    )
assert "depends_on" not in services["webapp"]
assert "vllm-manager" not in services["taskplanner-runtime"]["depends_on"]
assert services["webapp"]["command"] == [
    "bash",
    "/workspaces/taskplanner_ws/webapp/scripts/start-production.sh",
]
assert services["webapp"]["entrypoint"] == []
assert services["webapp"]["environment"]["TASKPLANNER_SKIP_WORKSPACE_SETUP"] == "true"

# Every taskplanner-ws ROS process reads the Jazzy-only container overlay.
# /opt/btops_ws remains an independent image overlay; host/release install/
# references are intentionally outside this Compose runtime contract.
for name in (
    "shadow-runner",
    "taskplanner-asr",
    "taskplanner-tts",
    "taskplanner-runtime",
    "public-rosbridge",
    "multicam-observer",
    "integration-debug",
    "monitor-media-gateway",
):
    service_text = repr(services[name])
    assert "install/docker/setup.bash" in service_text, name
    assert "/workspaces/taskplanner_ws/install/setup.bash" not in service_text, name
    assert "source install/setup.bash" not in service_text, name
    assert "-f install/setup.bash" not in service_text, name
PY

# Assert the fully interpolated Compose model too: the production web command
# remains an argv list, never another nested `bash -lc` program.
python3 -c '
import sys
import yaml

webapp = yaml.safe_load(sys.stdin.read())["services"]["webapp"]
assert webapp["command"] == [
    "bash",
    "/workspaces/taskplanner_ws/webapp/scripts/start-production.sh",
]
assert webapp["entrypoint"] == []
assert webapp["environment"]["TASKPLANNER_SKIP_WORKSPACE_SETUP"] == "true"
assert all(argument not in {"-c", "-lc"} for argument in webapp["command"])
' <<<"${live_config}"

grep -Fq '/workspaces/taskplanner_ws/install/docker/setup.bash' \
  "${ROOT_DIR}/docker/entrypoint.sh" ||
  fail 'container entrypoint must source install/docker'
if grep -Fq '/workspaces/taskplanner_ws/install/setup.bash' \
    "${ROOT_DIR}/docker/entrypoint.sh"; then
  fail 'container entrypoint still sources the host install root'
fi

bash "${ROOT_DIR}/tests/test_taskplanner_mode_policy.sh"
bash "${ROOT_DIR}/tests/test_taskplanner_execution.sh"

printf 'Taskplanner lightweight Production launcher tests passed.\n'
