#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="${ROOT_DIR}/scripts/taskplanner"
DESKTOP_LAUNCHER="${ROOT_DIR}/scripts/taskplanner_clean_start_live_thyroidectomy_demo.sh"
WEBAPP_LAUNCHER="${ROOT_DIR}/webapp/scripts/start-production.sh"
EXECUTION_LAYER="${ROOT_DIR}/scripts/lib/taskplanner_execution.sh"
PACKAGE_PLAN="${ROOT_DIR}/scripts/taskplanner_package_plan.py"

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
PYTHONDONTWRITEBYTECODE=1 python3 - "${PACKAGE_PLAN}" <<'PY'
import ast
from pathlib import Path
import sys

ast.parse(Path(sys.argv[1]).read_text(encoding="utf-8"))
PY

launcher_source="$(<"${LAUNCHER}")"
assert_contains "${launcher_source}" 'restart asr'
assert_contains "${launcher_source}" 'ASR_RESTART_REQUIRE_ACTIVE_LIVE'
execution_source="$(<"${EXECUTION_LAYER}")"
package_plan_source="$(<"${PACKAGE_PLAN}")"
assert_contains "${package_plan_source}" '.taskplanner-package-contracts-v2'
assert_contains "${execution_source}" 'taskplanner_prepare_mode_build_plan()'
assert_contains "${execution_source}" '--packages-select${package_args}'
assert_contains "${execution_source}" '--symlink-install --packages-select'
assert_contains "${execution_source}" 'scripts/taskplanner_package_plan.py'
assert_not_contains "${package_plan_source}" 'service-fingerprint'
assert_not_contains "${package_plan_source}" 'invalidate-bytecode'
assert_not_contains "${execution_source}" 'invalidate-python-bytecode'
assert_not_contains "${execution_source}" 'service_source_generation'
assert_contains "${execution_source}" 'taskplanner_resume_stopped_core()'
assert_contains "${execution_source}" 'taskplanner_resume_owner_service()'
assert_contains "${launcher_source}" 'ensure_runtime_control_service_best_effort'
assert_contains "${package_plan_source}" 'build_dependencies'
assert_contains "${launcher_source}" 'resolve_optional_perception_start_plan()'
assert_contains "${launcher_source}" 'perception owner did not start; inspect/restart only the perception owner'
assert_not_contains "${launcher_source}" 'pnu_live_preflight.py'
assert_not_contains "${launcher_source}" 'validate_compose_mounted_token_files'

# The direct typed voice lane uses the installed surgical_msgs interface. Keep
# the retired custom envelope package out of the Live closure so a package.xml
# edit cannot silently reintroduce a custom-IDL rebuild into warm restarts.
PYTHONDONTWRITEBYTECODE=1 python3 - "${PACKAGE_PLAN}" "${ROOT_DIR}" <<'PY'
import importlib.util
from pathlib import Path
import sys

module_path = Path(sys.argv[1])
root = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("taskplanner_package_plan_test", module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
graph = module.PackageGraph.discover(root)
assert "rosbridge_server" not in graph.roots, graph.roots
assert "rosapi" not in graph.roots, graph.roots
ordered = graph.closure(
    ["bringup", "integration_debug", "taskplanner_bt_trees"], "live"
)
mode_packages = set(ordered)
assert "voice_command" in mode_packages, ordered
assert "bringup" in mode_packages, ordered
assert "taskplanner_command_msgs" not in mode_packages, ordered
assert "taskplanner_command_msgs" not in graph.dependencies["voice_command"]
PY

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
  "${webapp_contract_dir}/dist" \
  "${webapp_contract_dir}/.taskplanner"
cp "${WEBAPP_LAUNCHER}" "${webapp_contract_dir}/scripts/start-production.sh"
cp "${ROOT_DIR}/webapp/scripts/webapp-build-state.sh" \
  "${webapp_contract_dir}/scripts/webapp-build-state.sh"
for runtime_script in \
  serve-production.mjs check-dev-server-contract.mjs \
  start-development.sh apply-build.sh; do
  printf '%s\n' "${runtime_script}" \
    >"${webapp_contract_dir}/scripts/${runtime_script}"
done
for source_file in \
  index.html package.json package-lock.json tsconfig.json vite.config.ts; do
  printf '%s\n' "${source_file}" >"${webapp_contract_dir}/${source_file}"
done
printf 'source\n' >"${webapp_contract_dir}/src/app.ts"
printf 'built\n' >"${webapp_contract_dir}/dist/index.html"
contract_digest="$(
  cd "${webapp_contract_dir}"
  bash scripts/start-production.sh --print-source-digest
)"
printf '%s\n' "${contract_digest}" \
  >"${webapp_contract_dir}/.taskplanner/build-source.sha256"
(
  cd "${webapp_contract_dir}"
  bash scripts/start-production.sh --check-build-current
) || fail 'matching webapp source/build stamp must be current'
printf '%064d\n' 1 >"${webapp_contract_dir}/.taskplanner/build-source.sha256"
if (
  cd "${webapp_contract_dir}"
  bash scripts/start-production.sh --check-build-current
); then
  fail 'mismatched webapp source/build stamp must be stale'
fi

# A stale frontend is built through its own webapp execution path. Core startup
# requests independent UI/NInfer owners, but a source-only browser change must
# not recreate the running dashboard or wait for a model/bridge health barrier.
python3 - "${LAUNCHER}" "${EXECUTION_LAYER}" <<'PY'
import sys
from pathlib import Path

launcher = Path(sys.argv[1]).read_text(encoding="utf-8")
execution = Path(sys.argv[2]).read_text(encoding="utf-8")
helper_start = execution.index("refresh_webapp_if_stale() {")
helper_end = execution.index("\nwarm_restart_runtime_service() {", helper_start)
helper = execution[helper_start:helper_end]
assert 'webapp_build_is_current' in helper
assert 'taskplanner_apply_webapp_bundle' in helper
assert '--force-recreate' not in helper
assert 'exec -T --user' in execution
assert 'run --rm --no-deps' in execution

assert 'up -d "${BUILD_ARGS[@]}" webapp' in launcher
assert 'up -d "${BUILD_ARGS[@]}" ninfer-manager' in launcher
assert '--wait --wait-timeout "${WAIT_TIMEOUT_SEC}" \n      ninfer-manager webapp' not in launcher
assert 'TASKPLANNER_NINFER_AUTOLOAD_ON_COLD_START' in launcher
refresh_index = launcher.rindex('refresh_webapp_if_stale "${runtime_profile_args[@]}"')
assert refresh_index < launcher.index('up -d "${BUILD_ARGS[@]}" webapp', refresh_index)
assert 'source "${ROOT_DIR}/scripts/lib/taskplanner_execution.sh"' in launcher
assert 'RUNTIME_FAILURE_CLEANUP_SERVICES=("${TASKPLANNER_OPERATIONAL_FAILURE_CLEANUP_SERVICES[@]}")' not in launcher
assert 'RUNTIME_FAILURE_CLEANUP_SERVICES=("${TASKPLANNER_DEBUG_FAILURE_CLEANUP_SERVICES[@]}")' not in launcher
assert 'track_runtime_failure_cleanup_service() {' in launcher
assert 'if ((${#RUNTIME_FAILURE_CLEANUP_SERVICES[@]} > 0)); then' in launcher
for name in (
    'optional_recreate_args',
    'asr_recreate_args',
    'tts_recreate_args',
    'multicam_recreate_args',
    'public_rosbridge_recreate_args',
):
    assert f'{name}=()' in launcher
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
# The split perception owner receives the reviewed profile and never renders
# the retired monolith's empty `rfdetr_service_url:=` launch alias.
assert_contains "${live_config}" 'TASKPLANNER_OWNER_LAUNCH: taskplanner_perception.launch.py'
assert_contains "${live_config}" 'TASKPLANNER_OWNER_LAUNCH: taskplanner_cam4_mayo.launch.py'
assert_not_contains "${live_config}" 'rfdetr_service_url:='
for production_exclusion in \
  '  vllm-manager:' \
  '  object-perception:' \
  '  pnu-perception:' \
  '  taskplanner-tts:' \
  '  multicam-observer:' \
  '  integration-debug:'; do
  assert_not_contains "${live_config}" "${production_exclusion}"
done

live_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${LAUNCHER}" up live --dry-run --ensure-build
)"
# A current package-contract stamp may short-circuit the optional runtime
# import probe, so the dry-run need not print its setup command. It must never
# fall back to the host/release overlay when it does print one.
assert_not_contains "${live_output}" '/workspaces/taskplanner_ws/install/setup.bash'
assert_contains "${live_output}" '--profile live --profile owners up -d webapp'
assert_contains "${live_output}" '--profile live up -d ninfer-manager'
assert_contains "${live_output}" '--profile live up -d taskplanner-asr'
assert_not_contains "${live_output}" 'invalidate-python-bytecode'
assert_contains "${live_output}" '--profile live --profile owners up -d taskplanner-state-core taskplanner-command taskplanner-tool-state taskplanner-cam4-mayo taskplanner-projection taskplanner-execution taskplanner-operator-bridge taskplanner-scenario taskplanner-surgery-record taskplanner-rosbag-recorder taskplanner-debug-observer'
assert_contains "${live_output}" '--profile live --profile owners up -d taskplanner-perception'
assert_contains "${live_output}" '--profile live --profile ops up -d taskplanner-tts'
assert_not_contains "${live_output}" 'up -d --force-recreate taskplanner-state-core'
assert_contains "${live_output}" '--profile live up -d public-rosbridge'
assert_not_contains "${live_output}" '--force-recreate --wait --wait-timeout 300 taskplanner-asr'
assert_not_contains "${live_output}" '--force-recreate --wait --wait-timeout 300 public-rosbridge'
assert_not_contains "${live_output}" '+ ensure-ninfer-model-loaded qwen3.6-35b-a3b'
assert_not_contains "${live_output}" '+ wait-for-websocket'
assert_contains "${live_output}" 'owner start requested; inspect owner status'

# A non-build mode transition replaces only mutually-exclusive cores. Optional
# sidecars have their own owner/restart command and are not torn down merely
# because the operator selected Live again.
live_stop_command="$(grep -F ' stop ' <<<"${live_output}" | head -n 1)"
for core_service in taskplanner-state-core taskplanner-command taskplanner-tool-state taskplanner-perception taskplanner-cam4-mayo taskplanner-projection taskplanner-execution taskplanner-operator-bridge taskplanner-scenario taskplanner-simulation-input taskplanner-surgery-record taskplanner-rosbag-recorder shadow-runner; do
  [[ "$(tr ' ' '\n' <<<"${live_stop_command}" | grep -Fxc "${core_service}" || true)" == "1" ]] ||
    fail "Live convergence must stop exactly one ${core_service} core"
done
for preserved_sidecar in \
  webapp-lan-proxy \
  integration-debug-tailscale-proxy \
  vllm-manager \
  taskplanner-asr \
  public-rosbridge; do
  [[ "$(tr ' ' '\n' <<<"${live_stop_command}" | grep -Fxc "${preserved_sidecar}" || true)" == "0" ]] ||
    fail "Live non-build convergence must preserve ${preserved_sidecar}"
done

# TTS is a default Live presentation owner. Other optional services may be
# stopped when converging to Production and must not be started or waited on by
# the default Live path.
for forbidden_start in \
  'up -d vllm-manager' \
  'up -d object-perception' \
  'up -d pnu-perception' \
  'up -d multicam-observer' \
  'up -d integration-debug' \
  'up -d webapp-lan-proxy' \
  'up -d integration-debug-tailscale-proxy'; do
  assert_not_contains "${live_output}" "${forbidden_start}"
done
assert_not_contains "${live_output}" ' --remove-orphans '
assert_not_contains "${live_output}" ' up -d --build '

# Debug is an independent observer/control plane.  It never resolves or starts
# perception, ASR, recorder, VLM, PNU, or the retired composite Debug service.
# Consequently a stale perception environment cannot slow down or reject a
# Debug session.
invalid_perception_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
  ENABLE_PNU_DEBUG_PERCEPTION=true \
  PERCEPTION_PROVIDER=not-a-provider \
    "${LAUNCHER}" up debug --dry-run --ensure-build 2>&1
)"
assert_contains "${invalid_perception_output}" '--profile debug --profile owners up -d taskplanner-debug-observer taskplanner-debug-control taskplanner-debug-virtual'
assert_not_contains "${invalid_perception_output}" 'PERCEPTION_PROVIDER must resolve'
assert_not_contains "${invalid_perception_output}" 'perception startup plan is unavailable'
assert_not_contains "${invalid_perception_output}" 'up -d integration-debug'
assert_not_contains "${invalid_perception_output}" 'up -d pnu-perception'
assert_not_contains "${invalid_perception_output}" 'up -d taskplanner-asr'

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
assert_contains "${live_build_output}" '--profile live --profile ops build ninfer-manager taskplanner-tts'
assert_not_contains "${live_build_output}" ' up -d --build '
live_build_stop_command="$(grep -F ' stop ' <<<"${live_build_output}" | head -n 1)"
[[ "$(tr ' ' '\n' <<<"${live_build_stop_command}" | grep -Fxc taskplanner-asr || true)" == "1" ]] ||
  fail 'Live build convergence must stop the old ASR process before replacing the generated ROS overlay'

# A valid existing overlay without v2 package stamps is a baseline candidate,
# not proof that the whole selected mode is stale. The focused import check is
# retained; neither colcon nor image construction is selected.
if [[ ! -d "${ROOT_DIR}/install/docker/.taskplanner-package-contracts-v2/packages" ]]; then
  live_ensure_output="$(
    TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
      "${LAUNCHER}" up live --dry-run --ensure-build 2>&1
  )"
  assert_contains "${live_ensure_output}" 'taskplanner-install-check'
  assert_not_contains "${live_ensure_output}" 'colcon\ --log-base'
  assert_not_contains "${live_ensure_output}" '--profile live build ninfer-manager'
fi

# Image-only work does not run colcon. Conversely a clean scoped ROS build
# does not construct NInfer/taskplanner images.
live_ros_build_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${LAUNCHER}" up live --dry-run --ros-build
)"
assert_not_contains "${live_ros_build_output}" 'colcon\ --log-base'
assert_not_contains "${live_ros_build_output}" '--profile live build ninfer-manager'

live_image_build_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${LAUNCHER}" up live --dry-run --image-build
)"
assert_not_contains "${live_image_build_output}" 'colcon\ --log-base'
assert_contains "${live_image_build_output}" '--profile live --profile dev build taskplanner-dev'
assert_contains "${live_image_build_output}" '--profile live --profile ops build ninfer-manager taskplanner-tts'

# Replay and Debug used to forward --build into several sequential `up`
# commands.  Both now build their distinct images once and then reuse them.
for build_mode in replay debug; do
  mode_build_output="$(
    TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
      "${LAUNCHER}" up "${build_mode}" --dry-run --build
  )"
  [[ "$(grep -F -c 'colcon\ --log-base' <<<"${mode_build_output}" || true)" == "1" ]] ||
    fail "${build_mode} --build must run exactly one foreground colcon build"
  [[ "$(grep -E -c -- "--profile ${build_mode} .*build .*ninfer-manager" <<<"${mode_build_output}" || true)" == "1" ]] ||
    fail "${build_mode} --build must build NInfer exactly once"
  assert_not_contains "${mode_build_output}" ' up -d --build '
done

# Same-mode restart is an early, core-only route. It must run before the
# runtime-control/source/build path and it must not select a sidecar.
python3 - "${LAUNCHER}" "${EXECUTION_LAYER}" <<'PY'
import sys
from pathlib import Path

launcher = Path(sys.argv[1]).read_text(encoding="utf-8")
execution = Path(sys.argv[2]).read_text(encoding="utf-8")
start = execution.index('taskplanner_cross_warm_restart_boundary() {')
end = execution.index('\nrun_serial_workspace_build() {', start)
boundary = execution[start:end]
ordered = (
    'verify_same_mode_warm_restart_interlock "${mode}"',
    'RUNTIME_FAILURE_CLEANUP_SERVICES=("${service}")',
    'RUNTIME_FAILURE_CLEANUP_ARMED=true',
    'clear_active_runtime_mode',
    'warm_restart_runtime_service "${mode}" "${service}"',
)
positions = [boundary.index(token) for token in ordered]
assert positions == sorted(positions), positions
assert 'public-rosbridge' not in boundary
assert 'verify_same_mode_warm_restart_interlock' in boundary
assert 'positive* in-flight report' in boundary
fast_path = launcher.index('taskplanner_same_mode_core_running "${mode}"')
fast_path_end = launcher.index('\n    # Reboot recovery', fast_path)
fast_path_source = launcher[fast_path:fast_path_end]
cross_call = launcher.index(
    'taskplanner_cross_warm_restart_boundary "${mode}" "${runtime_service}"',
    fast_path,
)
assert fast_path < cross_call < launcher.index('ensure_runtime_control_service', fast_path)
assert cross_call < launcher.index('ensure_runtime_install_contract "${mode}"')
assert 'taskplanner_cross_warm_build_boundary' not in launcher
assert 'taskplanner_select_warm_build_reader_services' not in execution
assert 'taskplanner_restore_missing_split_owners' not in fast_path_source
assert 'ensure_live_tts_sidecar' not in fast_path_source
assert 'taskplanner_owner_prepare_persisted_scenario_bundle "${mode}"' in fast_path_source
PY

# Explicit owner restarts and configuration reloads stay out of the general
# launcher path. They must not trigger package planning, a workspace build, or
# a sidecar restart.
state_core_restart_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${LAUNCHER}" restart state-core live --dry-run
)"
assert_contains "${state_core_restart_output}" 'restarting state-core only'
assert_contains "${state_core_restart_output}" '--profile owners up -d --no-deps taskplanner-state-core'
assert_not_contains "${state_core_restart_output}" 'verify-runtime-transition-interlock'
assert_contains "${state_core_restart_output}" 'verify-state-core-restart-execution live'
assert_not_contains "${state_core_restart_output}" 'verify-execution-restart-state'

asr_restart_output="$(
  "${LAUNCHER}" restart asr --dry-run
)"
assert_contains "${asr_restart_output}" 'Taskplanner Live: restarting ASR only'
assert_contains "${asr_restart_output}" '--profile live up -d --no-deps taskplanner-asr'
assert_contains "${asr_restart_output}" '--profile live restart taskplanner-asr'
assert_contains "${asr_restart_output}" 'exec -T taskplanner-asr'
assert_not_contains "${asr_restart_output}" 'taskplanner_package_plan.py'
assert_not_contains "${asr_restart_output}" 'taskplanner-state-core'
assert_not_contains "${asr_restart_output}" 'invalidate-python-bytecode'

config_reload_output="$(
  "${LAUNCHER}" reload config thyroidectomy_demo --dry-run
)"
assert_contains "${config_reload_output}" 'reloading configuration for thyroidectomy_demo (no build or restart)'
assert_contains "${config_reload_output}" '--profile owners exec -T taskplanner-scenario'
assert_contains "${config_reload_output}" '/simulation/select_bundle surgical_msgs/srv/SelectSimulationBundle'
assert_contains "${config_reload_output}" 'reload_if_changed:\ true'
assert_not_contains "${config_reload_output}" 'taskplanner_package_plan.py'
assert_not_contains "${config_reload_output}" 'taskplanner-asr'
assert_not_contains "${config_reload_output}" 'invalidate-python-bytecode'

# The desktop shortcut is a scoped warm restart. It must not issue the old
# global `down`, reselect the already-loaded model through ROS, or start local
# perception/accessory profiles.
desktop_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
    "${DESKTOP_LAUNCHER}" --dry-run --terminal
)"
assert_contains "${desktop_output}" 'warm restart'
assert_contains "${desktop_output}" '192.168.1.7 typed ROS topics'
assert_not_contains "${desktop_output}" '+ ensure-ninfer-model-loaded qwen3.6-35b-a3b'
assert_not_contains "${desktop_output}" ' taskplanner down'
assert_not_contains "${desktop_output}" 'select_model_provider'
assert_not_contains "${desktop_output}" 'up -d object-perception'
assert_not_contains "${desktop_output}" 'up -d pnu-perception'
assert_not_contains "${desktop_output}" 'up -d integration-debug'
assert_not_contains "${desktop_output}" 'up -d multicam-observer'
assert_not_contains "${desktop_output}" 'up -d webapp-lan-proxy'

# Ops stays opt-in and composes independently from the Production core.
ops_output="$(
  TASKPLANNER_RUNTIME_CONTROL_CHILD=1 \
  TASKPLANNER_LIVE_ENABLE_OPS=true \
  TASKPLANNER_LIVE_ENABLE_MULTICAM=true \
  TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG=true \
    "${LAUNCHER}" up live --dry-run --ensure-build
)"
assert_contains "${ops_output}" '--profile live --profile ops up -d taskplanner-tts'
assert_contains "${ops_output}" 'up -d multicam-observer'
assert_contains "${ops_output}" 'taskplanner-debug-observer'
assert_not_contains "${ops_output}" 'invalidate-python-bytecode'
assert_not_contains "${ops_output}" '--force-recreate taskplanner-tts'
assert_not_contains "${ops_output}" '--force-recreate multicam-observer'
assert_not_contains "${ops_output}" '--force-recreate taskplanner-debug-observer'
assert_contains "${ops_output}" 'up -d webapp-lan-proxy integration-debug-lan-proxy integration-debug-tailscale-proxy'
assert_not_contains "${ops_output}" '+ wait-for-websocket'

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
assert "taskplanner-runtime" not in services
assert "live" not in services["pnu-perception"]["profiles"]
for name in (
    "taskplanner-tts",
    "webapp-lan-proxy",
    "public-rosbridge-lan-proxy",
):
    assert services[name]["profiles"] == ["ops"]
for name in (
    "taskplanner-tts",
    "multicam-observer",
    "taskplanner-debug-observer",
    "webapp-lan-proxy",
    "integration-debug-lan-proxy",
    "public-rosbridge-lan-proxy",
    "integration-debug-tailscale-proxy",
):
    assert services[name]["restart"] == "no", (
        f"optional service {name} must not resurrect after a host reboot"
    )
for name in (
    "taskplanner-state-core",
    "taskplanner-command",
    "taskplanner-tool-state",
    "taskplanner-perception",
    "taskplanner-cam4-mayo",
    "taskplanner-projection",
    "taskplanner-execution",
    "taskplanner-operator-bridge",
    "taskplanner-scenario",
    "taskplanner-simulation-input",
    "taskplanner-surgery-record",
    "taskplanner-rosbag-recorder",
):
    assert services[name]["profiles"] == ["owners"]
    assert services[name]["restart"] == "no"
    assert services[name]["read_only"] is True
assert "depends_on" not in services["webapp"]
assert services["shadow-runner"]["depends_on"]["ninfer-manager"]["condition"] == "service_started"
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
    "taskplanner-state-core",
    "taskplanner-command",
    "taskplanner-tool-state",
    "taskplanner-perception",
    "taskplanner-cam4-mayo",
    "taskplanner-projection",
    "taskplanner-execution",
    "taskplanner-operator-bridge",
    "taskplanner-scenario",
    "taskplanner-simulation-input",
    "taskplanner-surgery-record",
    "taskplanner-rosbag-recorder",
    "public-rosbridge",
    "multicam-observer",
    "taskplanner-debug-observer",
):
    service_text = repr(services[name])
    assert "install/docker/setup.bash" in service_text, name
    assert "/workspaces/taskplanner_ws/install/setup.bash" not in service_text, name
    assert "source install/setup.bash" not in service_text, name
    assert "-f install/setup.bash" not in service_text, name

record = services["taskplanner-surgery-record"]
record_volumes = record["volumes"]
assert len(record_volumes) == 3
assert any(volume.endswith(":/workspaces/taskplanner_ws:ro") for volume in record_volumes)
assert any(":/taskplanner-surgery-record" in volume for volume in record_volumes)
assert any(
    volume.endswith(":/run/taskplanner-secrets/puzzle-surgery-record-api-key:ro")
    for volume in record_volumes
)
assert record["cap_drop"] == ["ALL"]
assert "https://192.168.1.5:6627/api/v1/surgery/img_texts" in (
    record["environment"]["PUZZLE_SURGERY_RECORD_ENDPOINT"]
)
assert record["environment"]["SSL_CERT_FILE"] == (
    "/workspaces/taskplanner_ws/config/puzzle_surgery_record_root_ca.pem"
)
record_env = set(record["environment"])
assert not record_env & {
    "VLM_API_KEY",
    "ACTOR_API_KEY",
    "PNU_CLIENT_API_TOKEN_FILE",
    "PUZZLE_ASR_URL",
    "XDG_RUNTIME_DIR",
}

recorder = services["taskplanner-rosbag-recorder"]
recorder_volumes = recorder["volumes"]
assert len(recorder_volumes) == 2
assert any(volume.endswith(":/workspaces/taskplanner_ws:ro") for volume in recorder_volumes)
assert any(":/taskplanner-rosbags" in volume for volume in recorder_volumes)
assert recorder["environment"]["TASKPLANNER_OWNER_LAUNCH"] == (
    "taskplanner_rosbag_recorder.launch.py"
)
assert recorder["environment"]["TASKPLANNER_ROSBAG_OUTPUT_DIR"] == "/taskplanner-rosbags"
assert recorder["cap_drop"] == ["ALL"]
recorder_env = set(recorder["environment"])
assert not recorder_env & {
    "VLM_API_KEY",
    "ACTOR_API_KEY",
    "PNU_CLIENT_API_TOKEN_FILE",
    "PUZZLE_ASR_URL",
    "XDG_RUNTIME_DIR",
}
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
bash "${ROOT_DIR}/tests/test_taskplanner_ui_delivery.sh"

printf 'Taskplanner lightweight Production launcher tests passed.\n'
