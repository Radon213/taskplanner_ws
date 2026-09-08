#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
source "${ROOT_DIR}/scripts/lib/taskplanner_mode_policy.sh"
source "${ROOT_DIR}/scripts/lib/taskplanner_execution.sh"
source "${ROOT_DIR}/scripts/lib/taskplanner_owner_control.sh"

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

# The state core is a split `owners` service in Live/LLM mode.  Its warm
# restart must retain that profile; otherwise Compose sees no matching service
# and the failure-cleanup trap can remove the already-stopped core.  Replay
# intentionally uses its own runner profile even though it shares the state
# owner registry entry.
[[ "$(taskplanner_runtime_compose_profile_for_mode live)" == "owners" ]] ||
  fail "Live state-core restart lost the owners Compose profile"
[[ "$(taskplanner_runtime_compose_profile_for_mode llm-surgeon)" == "owners" ]] ||
  fail "LLM state-core restart lost the owners Compose profile"
[[ "$(taskplanner_runtime_compose_profile_for_mode replay)" == "replay" ]] ||
  fail "Replay state-core restart did not retain the replay Compose profile"

# A managed Live/LLM build must validate the independently launchable owner
# set, not the retained composite mock/live launch files.  Those files remain
# source/test references only and must never make an owner-plane build appear
# valid.
build_contract_source="$(sed -n '/run_serial_workspace_build() {/,/^}/p' "${ROOT_DIR}/scripts/lib/taskplanner_execution.sh")"
[[ "${build_contract_source}" != *"taskplanner_live.launch.py"* ]] ||
  fail "Live scoped build still depends on the retained composite launch"
[[ "${build_contract_source}" != *"taskplanner_mock.launch.py"* ]] ||
  fail "LLM scoped build still depends on the retained composite launch"
for owner_launch in \
  taskplanner_state_core.launch.py taskplanner_command.launch.py \
  taskplanner_tool_state.launch.py taskplanner_perception.launch.py \
  taskplanner_cam4_mayo.launch.py \
  taskplanner_projection.launch.py taskplanner_execution.launch.py \
  taskplanner_operator_bridge.launch.py taskplanner_scenario.launch.py \
  taskplanner_simulation_input.launch.py taskplanner_surgery_record.launch.py \
  taskplanner_rosbag_recorder.launch.py; do
  [[ "${build_contract_source}" == *"${owner_launch}"* ]] ||
    fail "scoped build does not validate ${owner_launch}"
done

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

# ROS overlay work and Docker image construction are independent opt-ins. A
# scoped colcon build must not silently rebuild NInfer or the shared image.
TRACE=()
run_serial_workspace_build() {
  TRACE+=("workspace:$1")
}
build_runtime_images_once() {
  TRACE+=("images:$1")
}
build_shared_runtime_image_once() {
  TRACE+=("shared-image:$1")
}
BUILD_ARGS=()
TASKPLANNER_BUILD_PACKAGES=(bringup)
BUILD_REQUESTED=false
IMAGE_BUILD_REQUESTED=false
taskplanner_build_selected_runtime live
assert_trace ""
BUILD_REQUESTED=true
taskplanner_build_selected_runtime live
assert_trace "workspace:live"

TRACE=()
BUILD_ARGS=(--build)
TASKPLANNER_BUILD_PACKAGES=()
BUILD_REQUESTED=false
IMAGE_BUILD_REQUESTED=true
taskplanner_build_selected_runtime live
assert_trace "shared-image:live images:live"

TRACE=()
BUILD_ARGS=()
TASKPLANNER_BUILD_PACKAGES=(bringup)
BUILD_REQUESTED=true
IMAGE_BUILD_REQUESTED=false
run_serial_workspace_build() {
  TRACE+=("workspace:$1")
  return 1
}
if taskplanner_build_selected_runtime replay; then
  fail "failed workspace build was accepted"
fi
assert_trace "workspace:replay"

# A dry-run warm restart has exactly one service target. It may recreate the
# core when Compose input changed or restart it when unchanged, but it never
# plans/builds the workspace or starts a sidecar.
TRACE=()
DRY_RUN=true
COMPOSE=(docker compose --project-directory "${ROOT_DIR}")
run_or_print() {
  TRACE+=("$*")
}
warm_restart_runtime_service live taskplanner-state-core
[[ "${#TRACE[@]}" == "2" ]] || fail "warm core restart emitted unexpected commands"
[[ "${TRACE[*]}" == *"up -d --no-deps taskplanner-state-core"* ]] ||
  fail "warm core restart did not keep its Compose scope"
[[ "${TRACE[*]}" == *"restart taskplanner-state-core"* ]] ||
  fail "warm core restart did not retain the direct restart branch"
[[ "${TRACE[*]}" != *"taskplanner-asr"* && "${TRACE[*]}" != *"webapp"* ]] ||
  fail "warm core restart touched an unrelated sidecar"
DRY_RUN=false

# A stopped-core recovery never recreates the browser or public bridge. The
# state/command/execution owner plane is restored before the optional NInfer
# and ASR producers, so either optional producer can fail independently.
TRACE=()
DRY_RUN=true
RUNTIME_FAILURE_CLEANUP_ARMED=false
COMPOSE=(docker compose --project-directory "${ROOT_DIR}")
run_or_print() {
  TRACE+=("$*")
}
print_command() {
  TRACE+=("$*")
}
start_operational_asr_capture() {
  TRACE+=("start-asr-capture")
}
write_active_runtime_mode() {
  TRACE+=("marker:$1")
}
taskplanner_resume_stopped_core live
[[ "${TRACE[*]}" == *"verify-stopped-core-resume live"* ]] ||
  fail "resume did not perform its narrow stopped-core ownership check"
for owner_service in \
  taskplanner-state-core taskplanner-command taskplanner-tool-state \
  taskplanner-perception taskplanner-cam4-mayo taskplanner-projection taskplanner-execution \
  taskplanner-operator-bridge taskplanner-scenario; do
  [[ "${TRACE[*]}" == *"preserve-or-start-owner live ${owner_service}"* ]] ||
    fail "resume did not restore owner ${owner_service}"
done
[[ "${TRACE[*]}" == *"preserve-or-start-owner live ninfer-manager"* ]] ||
  fail "resume did not attempt optional NInfer manager ownership"
[[ "${TRACE[*]}" == *"preserve-or-start-owner live taskplanner-asr"* ]] ||
  fail "resume did not attempt optional ASR owner ownership"
[[ "${TRACE[*]}" != *"webapp"* && "${TRACE[*]}" != *"public-rosbridge"* ]] ||
  fail "resume touched a preserved control-plane sidecar"
[[ "${TRACE[*]}" != *"--force-recreate"* && "${TRACE[*]}" != *"taskplanner_package_plan.py"* ]] ||
  fail "resume widened into a recreate or build-plan path"

# A failed optional producer must not undo an already-restored core plane or
# suppress the active-mode marker.  This is intentionally a direct shell
# contract rather than a Docker/ROS probe.
TRACE=()
taskplanner_assert_resume_safe() {
  TRACE+=("verify-stopped-core-resume:$1")
}
taskplanner_owner_services_for_mode() {
  printf '%s\n' taskplanner-state-core taskplanner-command
}
taskplanner_resume_owner_service() {
  TRACE+=("resume:$1:$2")
  [[ "$2" != "ninfer-manager" && "$2" != "taskplanner-asr" ]]
}
start_operational_asr_capture() {
  TRACE+=("start-asr-capture")
  return 1
}
write_active_runtime_mode() {
  TRACE+=("marker:$1")
}
taskplanner_resume_stopped_core live
[[ "${TRACE[*]}" == *"resume:live:taskplanner-state-core"* ]] ||
  fail "optional producer failure prevented state-core resume"
[[ "${TRACE[*]}" == *"resume:live:taskplanner-command"* ]] ||
  fail "optional producer failure prevented command-owner resume"
[[ "${TRACE[*]}" == *"marker:live"* ]] ||
  fail "optional producer failure prevented active marker restore"
DRY_RUN=false

# The warm boundary remains narrow, but it must reject the one owner-local
# fact that state-core cannot safely reconstruct: a positively reported
# in-flight endpoint request. Arm cleanup only after that check, then clear
# the marker and touch only the selected core service.
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
taskplanner_cross_warm_restart_boundary live taskplanner-state-core
assert_trace "verify:live clear restart:live:taskplanner-state-core"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "true" ]] ||
  fail "warm failure cleanup was not armed"
[[ "${RUNTIME_FAILURE_CLEANUP_SERVICES[*]}" == "taskplanner-state-core" ]] ||
  fail "warm cleanup escaped the selected core"

TRACE=()
RUNTIME_FAILURE_CLEANUP_ARMED=false
verify_same_mode_warm_restart_interlock() {
  TRACE+=("verify:$1")
  return 1
}
clear_active_runtime_mode() {
  TRACE+=("clear")
}
warm_restart_runtime_service() {
  TRACE+=("restart:$1:$2")
}
if taskplanner_cross_warm_restart_boundary live taskplanner-state-core; then
  fail "state-core warm restart crossed an active execution interlock"
fi
assert_trace "verify:live"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "false" ]] ||
  fail "state-core warm restart armed cleanup before the execution interlock"

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
if taskplanner_cross_warm_restart_boundary live taskplanner-state-core; then
  fail "failed marker clear crossed the core restart boundary"
fi
assert_trace "verify:live clear"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "true" ]] ||
  fail "cleanup must remain armed after marker-clear failure"

# Owner launch acceptance is distinct from each owner's later readiness. The
# active marker must not wait on rosbridge, semantic/preflight, VLM, ASR, or
# camera probes; their independent owner status remains observable instead.
TRACE=()
DRY_RUN=true
RUNTIME_FAILURE_CLEANUP_ARMED=true
write_active_runtime_mode() {
  TRACE+=("marker:$1")
}
taskplanner_finalize_runtime_readiness live true --profile live --profile debug
assert_trace "marker:live"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "false" ]] ||
  fail "failure cleanup remained armed after owner launch acceptance"

TRACE=()
RUNTIME_FAILURE_CLEANUP_ARMED=true
write_active_runtime_mode() {
  TRACE+=("marker:$1")
  return 1
}
if taskplanner_finalize_runtime_readiness live false --profile live; then
  fail "failed active-marker publication was accepted"
fi
assert_trace "marker:live"
[[ "${RUNTIME_FAILURE_CLEANUP_ARMED}" == "true" ]] ||
  fail "cleanup disarmed after active-marker publication failed"

# A direct config reload invokes only the currently running scenario owner;
# it never asks the launcher to plan/build or touch an unrelated sidecar.
TRACE=()
DRY_RUN=true
COMPOSE=(docker compose --project-directory "${ROOT_DIR}")
run_or_print() {
  TRACE+=("$*")
}
taskplanner_scenario_reload_target() {
  printf '%s\n' 'owners|taskplanner-scenario'
}
taskplanner_reload_runtime_config live thyroidectomy_demo
[[ "${#TRACE[@]}" == "1" ]] || fail "config reload emitted extra commands"
[[ "${TRACE[0]}" == *"--profile owners exec -T taskplanner-scenario"* ]] ||
  fail "config reload did not target the scenario owner"
[[ "${TRACE[0]}" == *"/simulation/select_bundle surgical_msgs/srv/SelectSimulationBundle"* ]] ||
  fail "config reload did not use the typed scenario endpoint"
[[ "${TRACE[0]}" == *"reload_if_changed: true"* ]] ||
  fail "config reload did not request a direct reload"

# Build contracts are mode/package scoped. A changed package selects its real
# reverse-dependency closure but not an unrelated package in the same mode.
ORIGINAL_ROOT_DIR="${ROOT_DIR}"
ORIGINAL_INSTALL_ROOT="${CONTAINER_INSTALL_ROOT:-}"
ORIGINAL_RUNTIME_CONTROL_STATE_FILE="${RUNTIME_CONTROL_STATE_FILE:-}"
CONTRACT_TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${CONTRACT_TEST_ROOT}"' EXIT
ROOT_DIR="${CONTRACT_TEST_ROOT}"
CONTAINER_INSTALL_ROOT="${CONTRACT_TEST_ROOT}/install/docker"
mkdir -p "${ROOT_DIR}/scripts" "${ROOT_DIR}/config" "${ROOT_DIR}/src" "${CONTAINER_INSTALL_ROOT}"
cp "${ORIGINAL_ROOT_DIR}/scripts/taskplanner_package_plan.py" \
  "${ROOT_DIR}/scripts/taskplanner_package_plan.py"
cp "${ORIGINAL_ROOT_DIR}/scripts/taskplanner_owner_registry.py" \
  "${ROOT_DIR}/scripts/taskplanner_owner_registry.py"
cp "${ORIGINAL_ROOT_DIR}/config/taskplanner_runtime_owners.toml" \
  "${ROOT_DIR}/config/taskplanner_runtime_owners.toml"
printf 'setup\n' >"${CONTAINER_INSTALL_ROOT}/setup.bash"

# Same-mode detection needs only the active-mode marker and one core-container
# query; it never imports the broad runtime-control health/preflight graph.
mkdir -p "${ROOT_DIR}/fake-bin"
printf '%s\n' '#!/usr/bin/env bash' 'printf "core-container\\n"' \
  >"${ROOT_DIR}/fake-bin/docker"
chmod +x "${ROOT_DIR}/fake-bin/docker"
printf '%s\n' '{"mode":"live"}' >"${ROOT_DIR}/active-runtime-mode.json"
RUNTIME_CONTROL_STATE_FILE="${ROOT_DIR}/active-runtime-mode.json"
ORIGINAL_PATH="${PATH}"
PATH="${ROOT_DIR}/fake-bin:${PATH}"
DRY_RUN=false
taskplanner_same_mode_core_running live ||
  fail "matching active core was not accepted for the fast restart path"
! taskplanner_same_mode_core_running replay ||
  fail "a different active mode was accepted for the fast restart path"
PATH="${ORIGINAL_PATH}"

make_contract_package() {
  local package_name="$1"
  local dependency="${2:-}"
  local dependency_tag="${3:-exec_depend}"
  local package_relative="${4:-${package_name}}"
  local package_root="${ROOT_DIR}/src/${package_relative}"
  mkdir -p \
    "${package_root}" \
    "${CONTAINER_INSTALL_ROOT}/${package_name}/share/${package_name}" \
    "${CONTAINER_INSTALL_ROOT}/${package_name}/share/ament_index/resource_index/packages"
  if [[ -n "${dependency}" ]]; then
    printf '<package format="3"><name>%s</name><version>0.1.0</version><description>x</description><maintainer email="x@example.invalid">x</maintainer><license>Apache-2.0</license><%s>%s</%s></package>\n' \
      "${package_name}" "${dependency_tag}" "${dependency}" "${dependency_tag}" \
      >"${package_root}/package.xml"
  else
    printf '<package format="3"><name>%s</name><version>0.1.0</version><description>x</description><maintainer email="x@example.invalid">x</maintainer><license>Apache-2.0</license></package>\n' \
      "${package_name}" >"${package_root}/package.xml"
  fi
  cp "${package_root}/package.xml" \
    "${CONTAINER_INSTALL_ROOT}/${package_name}/share/${package_name}/package.xml"
  printf '%s\n' "${package_name}" \
    >"${CONTAINER_INSTALL_ROOT}/${package_name}/share/ament_index/resource_index/packages/${package_name}"
}

make_contract_package vendor_iface "" exec_depend vendor/vendor_iface
make_contract_package core_iface vendor_iface
make_contract_package core_lib core_iface
make_contract_package runtime_app core_lib
make_contract_package compiled_consumer vendor_iface depend
make_contract_package out_of_profile_consumer vendor_iface depend
make_contract_package unrelated_sidecar
taskplanner_select_mode_build_roots() {
  TASKPLANNER_SELECTED_BUILD_ROOT_PACKAGES=(runtime_app compiled_consumer unrelated_sidecar)
}
taskplanner_prepare_mode_build_plan live false
[[ "${TASKPLANNER_MODE_BUILD_MISSING_STAMP}" == "true" ]] ||
  fail "new package-contract baseline was trusted unexpectedly"
[[ "${#TASKPLANNER_BUILD_PACKAGES[@]}" == "0" ]] ||
  fail "valid unstamped installed packages unexpectedly selected a rebuild"
[[ "${TASKPLANNER_MODE_BUILD_BASELINE_PACKAGES[*]}" == \
    "vendor_iface core_iface core_lib runtime_app compiled_consumer unrelated_sidecar" ]] ||
  fail "valid installed package baseline drifted: ${TASKPLANNER_MODE_BUILD_BASELINE_PACKAGES[*]}"
taskplanner_record_mode_package_contracts live
taskplanner_prepare_mode_build_plan live false
[[ "${#TASKPLANNER_BUILD_PACKAGES[@]}" == "0" ]] ||
  fail "matching package stamps selected a rebuild"

# A broken symlink is not an installed resource index.  This is common after
# reusing an install tree created at a different workspace mount path; it must
# force a scoped rebuild instead of being accepted by is_symlink().
runtime_app_index="${CONTAINER_INSTALL_ROOT}/runtime_app/share/ament_index/resource_index/packages/runtime_app"
rm "${runtime_app_index}"
ln -s /missing/taskplanner/resource-index "${runtime_app_index}"
taskplanner_prepare_mode_build_plan live false
array_contains runtime_app "${TASKPLANNER_BUILD_PACKAGES[@]}" ||
  fail "broken resource-index symlink was accepted as installed"
[[ "${TASKPLANNER_MODE_BUILD_MISSING_ARTIFACT}" == "true" ]] ||
  fail "broken resource-index symlink did not mark a missing artifact"
rm "${runtime_app_index}"
printf '%s\n' runtime_app >"${runtime_app_index}"

# Docker's symlink-install overlay uses the container mount path in both the
# install link and its build target.  The host launcher must accept that link
# only when the mapped target really exists.
mkdir -p "${ROOT_DIR}/build/docker/resource"
printf '%s\n' runtime_app >"${ROOT_DIR}/build/docker/resource/runtime_app"
rm "${runtime_app_index}"
ln -s /workspaces/taskplanner_ws/build/docker/resource/runtime_app \
  "${runtime_app_index}"
taskplanner_prepare_mode_build_plan live false
[[ "${#TASKPLANNER_BUILD_PACKAGES[@]}" == "0" ]] ||
  fail "valid container-mount resource-index symlink selected a rebuild"
[[ "${TASKPLANNER_MODE_BUILD_MISSING_ARTIFACT}" != "true" ]] ||
  fail "valid container-mount resource-index symlink was marked missing"
rm "${runtime_app_index}"
printf '%s\n' runtime_app >"${runtime_app_index}"

printf 'changed nested interface input\n' \
  >"${ROOT_DIR}/src/vendor/vendor_iface/CMakeLists.txt"
taskplanner_prepare_mode_build_plan live false
[[ "${TASKPLANNER_CHANGED_BUILD_PACKAGES[*]}" == "vendor_iface" ]] ||
  fail "nested package change was not discovered"
[[ "${TASKPLANNER_BUILD_PACKAGES[*]}" == \
    "vendor_iface compiled_consumer out_of_profile_consumer" ]] ||
  fail "compile-dependency closure drifted: ${TASKPLANNER_BUILD_PACKAGES[*]}"
! array_contains core_iface "${TASKPLANNER_BUILD_PACKAGES[@]}" ||
  fail "exec_depend consumer incorrectly rebuilt for an interface change"
! array_contains unrelated_sidecar "${TASKPLANNER_BUILD_PACKAGES[@]}" ||
  fail "nested scoped build selected an unrelated installed package"
array_contains out_of_profile_consumer "${TASKPLANNER_BUILD_PACKAGES[@]}" ||
  fail "shared-install compiled consumer was omitted from the ABI rebuild"
taskplanner_record_mode_package_contracts live

printf 'changed native input\n' >"${ROOT_DIR}/src/core_lib/CMakeLists.txt"
taskplanner_prepare_mode_build_plan live false
[[ "${TASKPLANNER_CHANGED_BUILD_PACKAGES[*]}" == "core_lib" ]] ||
  fail "changed package detection drifted: ${TASKPLANNER_CHANGED_BUILD_PACKAGES[*]}"
[[ "${TASKPLANNER_BUILD_PACKAGES[*]}" == "core_lib" ]] ||
  fail "exec_depend-only runtime consumer incorrectly rebuilt: ${TASKPLANNER_BUILD_PACKAGES[*]}"
! array_contains unrelated_sidecar "${TASKPLANNER_BUILD_PACKAGES[@]}" ||
  fail "scoped build selected an unrelated installed package"
taskplanner_record_mode_package_contracts live

# A source-mounted Python adapter never widens the colcon build plan. Restart
# that adapter explicitly instead of rescanning every sidecar source tree.
printf 'new Python adapter\n' >"${ROOT_DIR}/src/core_lib/new_adapter.py"
taskplanner_prepare_mode_build_plan live false
[[ "${#TASKPLANNER_BUILD_PACKAGES[@]}" == "0" ]] ||
  fail "new Python adapter incorrectly selected a colcon build"

# Scenario and catalog data are live-reloaded by their owner.  They are not
# ABI inputs: editing either format must not turn the next cold start into a
# colcon build just because the package also contains native code.
printf 'scenario: revised\n' >"${ROOT_DIR}/src/core_lib/scenario.yaml"
printf '{"catalog": "revised"}\n' >"${ROOT_DIR}/src/core_lib/catalog.json"
taskplanner_prepare_mode_build_plan live false
[[ "${#TASKPLANNER_BUILD_PACKAGES[@]}" == "0" ]] ||
  fail "runtime configuration incorrectly selected a colcon build"

# COLCON_IGNORE applies to every descendant, not just the marked directory.
mkdir -p "${ROOT_DIR}/src/vendor/ignored/hidden_package"
touch "${ROOT_DIR}/src/vendor/ignored/COLCON_IGNORE"
printf '%s\n' \
  '<package format="3"><name>hidden_package</name><version>0.1.0</version><description>x</description><maintainer email="x@example.invalid">x</maintainer><license>Apache-2.0</license></package>' \
  >"${ROOT_DIR}/src/vendor/ignored/hidden_package/package.xml"
PYTHONDONTWRITEBYTECODE=1 python3 - "${ROOT_DIR}/scripts/taskplanner_package_plan.py" "${ROOT_DIR}" <<'PY'
import importlib.util
from pathlib import Path
import sys

module_path = Path(sys.argv[1])
root = Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("taskplanner_package_plan_ignore_test", module_path)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
graph = module.PackageGraph.discover(root)
assert "hidden_package" not in graph.roots, graph.roots
PY

ROOT_DIR="${ORIGINAL_ROOT_DIR}"
CONTAINER_INSTALL_ROOT="${ORIGINAL_INSTALL_ROOT}"
if [[ -n "${ORIGINAL_RUNTIME_CONTROL_STATE_FILE}" ]]; then
  RUNTIME_CONTROL_STATE_FILE="${ORIGINAL_RUNTIME_CONTROL_STATE_FILE}"
else
  unset RUNTIME_CONTROL_STATE_FILE
fi

printf 'Taskplanner common execution-layer tests passed.\n'
