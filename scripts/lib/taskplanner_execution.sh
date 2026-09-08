#!/usr/bin/env bash

# Shared Compose execution mechanics for scripts/taskplanner.
#
# taskplanner_mode_policy.sh decides which services a mode owns.  This module
# executes that already-selected policy: Compose convergence, serialized
# builds, the same-mode warm-restart boundary, and fail-closed readiness.  It
# deliberately does not decide whether a transition is authorized.
#
# The launcher supplies the small command hooks used below (`die`,
# `run_or_print`, `run_best_effort`, transition interlocks, marker helpers and
# install-contract recorders) plus the COMPOSE/BUILD/timeout globals. Keeping
# those authority hooks in the entrypoint makes this file independently
# sourceable by shell contract tests without granting it another transition
# policy.

taskplanner_compose_init() {
  COMPOSE=(docker compose --project-directory "${ROOT_DIR}")
  COMPOSE+=(-f "${COMPOSE_FILE}")
  COMPOSE+=("${ENV_ARGS[@]}")
  ALL_PROFILE_ARGS=("${TASKPLANNER_ALL_PROFILE_ARGS[@]}")
}

taskplanner_compose_stop_remove() {
  local -a services=("$@")
  ((${#services[@]} > 0)) || return 0
  run_or_print \
    "${COMPOSE[@]}" "${ALL_PROFILE_ARGS[@]}" \
    stop "${services[@]}" || return
  run_or_print \
    "${COMPOSE[@]}" "${ALL_PROFILE_ARGS[@]}" \
    rm -f "${services[@]}"
}

taskplanner_runtime_service_for_mode() {
  # The service that represents a selected mode is an owner-registry fact,
  # not a second launcher-maintained table.  `anchor` returns
  # owner|service|compose-profile|restart-strategy.
  local anchor owner service _profile _strategy
  anchor="$(
    python3 "${ROOT_DIR}/scripts/taskplanner_owner_registry.py" \
      --root "${ROOT_DIR}" anchor --mode "$1"
  )" || return 1
  IFS='|' read -r owner service _profile _strategy <<<"${anchor}"
  [[ -n "${owner}" && -n "${service}" ]] || return 1
  printf '%s\n' "${service}"
}

taskplanner_runtime_compose_profile_for_mode() {
  local anchor _owner _service profile _strategy
  # Replay's anchor shares the state-core registry entry, but it is launched
  # by the replay Compose profile rather than the operational `owners`
  # profile. Resolve this mode-level exception before consulting the owner
  # profile so a Live/LLM owner declaration cannot redirect replay.
  if [[ "$1" == "replay" ]]; then
    printf '%s\n' replay
    return 0
  fi
  anchor="$(
    python3 "${ROOT_DIR}/scripts/taskplanner_owner_registry.py" \
      --root "${ROOT_DIR}" anchor --mode "$1"
  )" || return 1
  IFS='|' read -r _owner _service profile _strategy <<<"${anchor}"
  if [[ -n "${profile}" ]]; then
    printf '%s\n' "${profile}"
    return 0
  fi
  return 1
}

taskplanner_same_mode_core_running() {
  local mode="$1"
  local service
  service="$(taskplanner_runtime_service_for_mode "${mode}")" || return 1
  [[ "${DRY_RUN:-false}" != "true" ]] || return 1

  # This is intentionally a small ownership check, not a full preflight.  A
  # warm restart is allowed to restart its already-owned core while VLM, ASR,
  # camera, browser, and bridge status continue to be reported independently.
  python3 - "${RUNTIME_CONTROL_STATE_FILE}" "${ROOT_DIR}" "${mode}" "${service}" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

state_file = Path(sys.argv[1])
root = Path(sys.argv[2]).resolve()
mode = sys.argv[3]
service = sys.argv[4]
try:
    payload = json.loads(state_file.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
if not isinstance(payload, dict) or payload.get("mode") != mode:
    raise SystemExit(1)
try:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project.working_dir={root}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--filter",
            "status=running",
            "--format",
            "{{.ID}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.0,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
raise SystemExit(0 if result.returncode == 0 and len(result.stdout.split()) == 1 else 1)
PY
}

taskplanner_missing_split_owner_services() {
  # Print split owner services missing from an otherwise active mode. This is
  # deliberately a container-only reconciliation check. It does not inspect
  # ROS topics, VLM/ASR/camera health, or a scenario preflight: each remains
  # an independent owner concern. It prevents a same-mode warm restart from
  # reporting success while a command, execution, ScenarioStore, or
  # operator-bridge owner stays down.

  local mode="$1"
  local running_services service anchor_service
  case "${mode}" in
    live|llm-surgeon|debug) ;;
    *) return 0 ;;
  esac
  [[ "${DRY_RUN:-false}" != "true" ]] || return 0

  running_services="$(
    docker ps \
      --filter "label=com.docker.compose.project.working_dir=${ROOT_DIR}" \
      --filter status=running \
      --format '{{.Label "com.docker.compose.service"}}'
  )" || return 1

  anchor_service="$(taskplanner_runtime_service_for_mode "${mode}")" || return 1
  while IFS= read -r service; do
    [[ -n "${service}" && "${service}" != "${anchor_service}" ]] || continue
    if ! grep -Fqx "${service}" <<<"${running_services}"; then
      printf '%s\n' "${service}"
    fi
  done < <(taskplanner_owner_services_for_mode "${mode}")
}

taskplanner_restore_missing_split_owners() {
  # Start only missing split owners before the narrow core warm restart.

  local mode="$1"
  local missing_output service
  local -a missing_services=()
  [[ "${mode}" == "live" || "${mode}" == "llm-surgeon" || "${mode}" == "debug" ]] || return 0
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    # A dry run cannot inspect the running set.  Keep its advertised scope
    # truthful: the normal same-mode path never plans/builds or touches a
    # sidecar, and this reconciliation only happens when a concrete owner is
    # observed missing.
    return 0
  fi
  missing_output="$(taskplanner_missing_split_owner_services "${mode}")" ||
    die "could not inspect split owner state for ${mode}"
  while IFS= read -r service; do
    [[ -n "${service}" ]] && missing_services+=("${service}")
  done <<<"${missing_output}"
  ((${#missing_services[@]} > 0)) || return 0

  printf 'Taskplanner %s: restoring missing owner(s): %s\n' \
    "${mode}" "${missing_services[*]}"
  if [[ "${mode}" == "debug" ]]; then
    run_or_print \
      "${COMPOSE[@]}" --profile debug --profile owners \
      up -d --no-deps "${missing_services[@]}"
  else
    run_or_print \
      "${COMPOSE[@]}" --profile "${mode}" --profile owners \
      up -d --no-deps "${missing_services[@]}"
  fi
}

taskplanner_reload_runtime_config() {
  local mode="$1"
  local bundle_name="$2"
  local owner_target owner_profile service request command
  case "${mode}" in
    live|llm-surgeon) ;;
    *) die "config reload is available only for live or llm-surgeon" ;;
  esac
  [[ "${bundle_name}" =~ ^[A-Za-z][A-Za-z0-9_-]{0,127}$ ]] ||
    die "config bundle name must use letters, numbers, underscores, or hyphens"
  owner_target="$(taskplanner_scenario_reload_target "${mode}")" ||
    die "could not resolve the ${mode} scenario configuration owner"
  IFS='|' read -r owner_profile service <<<"${owner_target}"
  [[ -n "${owner_profile}" && -n "${service}" ]] ||
    die "scenario configuration owner has an incomplete runtime mapping"
  printf -v request \
    '{bundle_name: "%s", restart_if_running: false, preview_only: false, reload_if_changed: true, expected_candidate_revision: ""}' \
    "${bundle_name}"
  command='set -e
set +u
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source /workspaces/taskplanner_ws/install/docker/setup.bash
set -u
ros2 service call "$1" "$2" "$3"'
  run_or_print \
    "${COMPOSE[@]}" --profile "${owner_profile}" exec -T "${service}" \
    bash -lc "${command}" taskplanner-config-reload \
    /simulation/select_bundle surgical_msgs/srv/SelectSimulationBundle "${request}"
}

_taskplanner_package_contract_tool() {
  local action="$1"
  local mode="$2"
  local explicit_rebuild="$3"
  local expected_generation="$4"
  shift 4
  local -a command=(
    python3
    "${ROOT_DIR}/scripts/taskplanner_package_plan.py"
    "${action}"
    --root "${ROOT_DIR}"
    --install-root "${CONTAINER_INSTALL_ROOT}"
    --mode "${mode}"
  )
  if [[ "${explicit_rebuild}" == "true" ]]; then
    command+=(--explicit-rebuild)
  fi
  if [[ -n "${expected_generation}" ]]; then
    command+=(--expected-generation "${expected_generation}")
  fi
  command+=("$@")
  "${command[@]}"
}

taskplanner_prepare_mode_build_plan() {
  local mode="$1"
  local explicit_rebuild="${2:-false}"
  taskplanner_select_mode_build_roots "${mode}" || return
  local output kind value
  output="$(_taskplanner_package_contract_tool \
    plan "${mode}" "${explicit_rebuild}" "" \
    "${TASKPLANNER_SELECTED_BUILD_ROOT_PACKAGES[@]}")" || return

  TASKPLANNER_MODE_BUILD_PACKAGES=()
  TASKPLANNER_CHANGED_BUILD_PACKAGES=()
  TASKPLANNER_BUILD_PACKAGES=()
  TASKPLANNER_MODE_BUILD_BASELINE_PACKAGES=()
  TASKPLANNER_MODE_BUILD_GENERATION=""
  TASKPLANNER_MODE_BUILD_MISSING_STAMP=false
  TASKPLANNER_MODE_BUILD_MISMATCHED_STAMP=false
  TASKPLANNER_MODE_BUILD_MISSING_ARTIFACT=false
  while IFS=$'\t' read -r kind value; do
    case "${kind}" in
      GENERATION) TASKPLANNER_MODE_BUILD_GENERATION="${value}" ;;
      MODE) TASKPLANNER_MODE_BUILD_PACKAGES+=("${value}") ;;
      CHANGED) TASKPLANNER_CHANGED_BUILD_PACKAGES+=("${value}") ;;
      BUILD) TASKPLANNER_BUILD_PACKAGES+=("${value}") ;;
      BASELINE) TASKPLANNER_MODE_BUILD_BASELINE_PACKAGES+=("${value}") ;;
      MISSING_STAMP) TASKPLANNER_MODE_BUILD_MISSING_STAMP=true ;;
      MISMATCHED_STAMP) TASKPLANNER_MODE_BUILD_MISMATCHED_STAMP=true ;;
      MISSING_ARTIFACT) TASKPLANNER_MODE_BUILD_MISSING_ARTIFACT=true ;;
      "") ;;
      *) return 1 ;;
    esac
  done <<<"${output}"
  [[ "${TASKPLANNER_MODE_BUILD_GENERATION}" =~ ^[0-9a-f]{64}$ ]] || return 1
  ((${#TASKPLANNER_MODE_BUILD_PACKAGES[@]} > 0)) || return 1
}

taskplanner_prepare_owner_build_plan() {
  # An owner-local installed entrypoint build is intentionally narrower than a
  # mode build.  The owner registry supplies its direct package roots, and the
  # package planner adds only their local dependency/build-consumer closure.
  # This keeps a new command adapter or owner launch file from forcing every
  # ROS process through the selected mode's contract.
  local mode="$1"
  local owner="$2"
  local explicit_rebuild="${3:-false}"
  local output kind value
  mapfile -t TASKPLANNER_OWNER_BUILD_ROOT_PACKAGES < <(
    taskplanner_owner_build_roots "${owner}" "${mode}"
  )
  ((${#TASKPLANNER_OWNER_BUILD_ROOT_PACKAGES[@]} > 0)) || return 1
  output="$(_taskplanner_package_contract_tool \
    plan "${mode}" "${explicit_rebuild}" "" \
    "${TASKPLANNER_OWNER_BUILD_ROOT_PACKAGES[@]}")" || return

  TASKPLANNER_MODE_BUILD_PACKAGES=()
  TASKPLANNER_CHANGED_BUILD_PACKAGES=()
  TASKPLANNER_BUILD_PACKAGES=()
  TASKPLANNER_MODE_BUILD_BASELINE_PACKAGES=()
  TASKPLANNER_MODE_BUILD_GENERATION=""
  TASKPLANNER_MODE_BUILD_MISSING_STAMP=false
  TASKPLANNER_MODE_BUILD_MISMATCHED_STAMP=false
  TASKPLANNER_MODE_BUILD_MISSING_ARTIFACT=false
  while IFS=$'\t' read -r kind value; do
    case "${kind}" in
      GENERATION) TASKPLANNER_MODE_BUILD_GENERATION="${value}" ;;
      MODE) TASKPLANNER_MODE_BUILD_PACKAGES+=("${value}") ;;
      CHANGED) TASKPLANNER_CHANGED_BUILD_PACKAGES+=("${value}") ;;
      BUILD) TASKPLANNER_BUILD_PACKAGES+=("${value}") ;;
      BASELINE) TASKPLANNER_MODE_BUILD_BASELINE_PACKAGES+=("${value}") ;;
      MISSING_STAMP) TASKPLANNER_MODE_BUILD_MISSING_STAMP=true ;;
      MISMATCHED_STAMP) TASKPLANNER_MODE_BUILD_MISMATCHED_STAMP=true ;;
      MISSING_ARTIFACT) TASKPLANNER_MODE_BUILD_MISSING_ARTIFACT=true ;;
      "") ;;
      *) return 1 ;;
    esac
  done <<<"${output}"
  [[ "${TASKPLANNER_MODE_BUILD_GENERATION}" =~ ^[0-9a-f]{64}$ ]] || return 1
}

taskplanner_record_mode_package_contracts() {
  local mode="$1"
  taskplanner_select_mode_build_roots "${mode}" || return
  _taskplanner_package_contract_tool \
    record "${mode}" false "${TASKPLANNER_MODE_BUILD_GENERATION}" \
    "${TASKPLANNER_SELECTED_BUILD_ROOT_PACKAGES[@]}"
}

taskplanner_record_owner_package_contracts() {
  local mode="$1"
  ((${#TASKPLANNER_OWNER_BUILD_ROOT_PACKAGES[@]} > 0)) || return 1
  _taskplanner_package_contract_tool \
    record "${mode}" false "${TASKPLANNER_MODE_BUILD_GENERATION}" \
    "${TASKPLANNER_OWNER_BUILD_ROOT_PACKAGES[@]}"
}

taskplanner_resume_candidate() {
  local mode="$1"
  local service
  service="$(taskplanner_runtime_service_for_mode "${mode}")" || return 1
  [[ "${DRY_RUN:-false}" != "true" ]] || return 1

  # A reboot can discard /run's active-mode marker while Compose restart
  # policies keep the browser/ASR/bridge sidecars alive.  This is deliberately
  # a small container-state probe: it must not inspect the ROS graph, source
  # tree, or unrelated health surfaces before restoring the stopped owner.
  python3 - "${ROOT_DIR}" "${mode}" "${service}" <<'PY'
import subprocess
import sys
from pathlib import Path

root = str(Path(sys.argv[1]).resolve())
mode = sys.argv[2]
service = sys.argv[3]

def running(name: str) -> bool:
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", f"label=com.docker.compose.project.working_dir={root}",
                "--filter", f"label=com.docker.compose.service={name}",
                "--filter", "status=running",
                "--format", "{{.ID}}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SystemExit(1)
    return result.returncode == 0 and bool(result.stdout.split())

# Never convert a genuinely running runtime into a "resume".  The integrated
# Debug observer is intentionally *not* a core here: Live can preserve that
# observer across a reboot and still restore its stopped core without tearing
# down the operator's view.
if any(running(name) for name in ("taskplanner-state-core", "shadow-runner")):
    raise SystemExit(1)

# Require an actual mode-scoped control-plane sidecar; a standalone Debug UI
# alone is not evidence that a Live/Surgeon/Replay core can be safely resumed.
# Live accepts either its public bridge or ASR owner so a reboot can preserve
# one while the other was intentionally stopped by the operator.
sidecars = {"public-rosbridge"}
if mode == "live":
    sidecars.add("taskplanner-asr")
raise SystemExit(0 if any(running(name) for name in sidecars) else 1)
PY
}

taskplanner_assert_resume_safe() {
  local mode="$1"
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    print_command verify-stopped-core-resume "${mode}"
    return 0
  fi
  taskplanner_resume_candidate "${mode}" ||
    die "resume requires a stopped core and at least one preserved ${mode} control-plane sidecar; use 'up ${mode}' for a cold start"
}

taskplanner_resume_owner_service() {
  local mode="$1"
  local service="$2"
  local profile record container_id container_state
  case "${service}" in
    taskplanner-state-core|taskplanner-command|taskplanner-tool-state|taskplanner-perception|taskplanner-cam4-mayo|taskplanner-projection|taskplanner-execution|taskplanner-operator-bridge|taskplanner-scenario|taskplanner-simulation-input|taskplanner-surgery-record)
      profile=owners
      ;;
    *) profile="${mode}" ;;
  esac

  # Do not let Compose reconcile a retained sidecar merely because a source
  # mount/config timestamp changed. A stopped retained container is started by
  # ID; Compose is used only when that owner no longer has a container at all.
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    print_command preserve-or-start-owner "${mode}" "${service}"
    return 0
  fi
  record="$(
    docker ps -a \
      --filter "label=com.docker.compose.project.working_dir=${ROOT_DIR}" \
      --filter "label=com.docker.compose.service=${service}" \
      --format '{{.ID}}\t{{.State}}'
  )" || die "could not inspect retained ${service} owner for resume"
  if [[ -z "${record}" ]]; then
    run_or_print "${COMPOSE[@]}" --profile "${profile}" up -d --no-deps "${service}"
    return 0
  fi
  [[ "${record}" != *$'\n'* ]] ||
    die "resume found more than one retained ${service} owner"
  IFS=$'\t' read -r container_id container_state <<<"${record}"
  [[ -n "${container_id}" && -n "${container_state}" ]] ||
    die "resume could not read the retained ${service} owner state"
  case "${container_state}" in
    running)
      return 0
      ;;
    created|exited)
      run_or_print docker start "${container_id}"
      return 0
      ;;
    *)
      die "resume cannot safely take over ${service} while it is ${container_state}"
      ;;
  esac
}

taskplanner_resume_stopped_core() {
  local mode="$1"
  local service owner_service
  local -a owner_services=()
  service="$(taskplanner_runtime_service_for_mode "${mode}")" || return 1

  # Resume owns only the stopped runtime plane.  In particular it never
  # removes/recreates webapp, public rosbridge, or an already-running ASR
  # container, and it never runs a source/build/preflight census.
  taskplanner_assert_resume_safe "${mode}" || return
  if [[ "${mode}" == "live" || "${mode}" == "llm-surgeon" ]]; then
    mapfile -t owner_services < <(taskplanner_owner_services_for_mode "${mode}")
    ((${#owner_services[@]} > 0)) ||
      die "runtime owner registry returned no ${mode} services"
    for owner_service in "${owner_services[@]}"; do
      taskplanner_resume_owner_service "${mode}" "${owner_service}"
    done
  else
    taskplanner_resume_owner_service "${mode}" "${service}"
  fi

  # ASR and NInfer are independent producers, not prerequisites for restoring
  # the stopped state/command/execution owner set.  A model-server or capture
  # failure must therefore be visible in its own owner status without holding
  # the entire Taskplanner down after a reboot.  Keep the convenience resume
  # attempt, but make it explicitly best-effort *after* the core plane exists.
  if ! taskplanner_resume_owner_service "${mode}" ninfer-manager; then
    printf 'warning: could not resume optional NInfer manager; continue with owner-local status\n' >&2
  fi
  if mode_uses_operational_asr_sidecar "${mode}"; then
    if ! taskplanner_resume_owner_service live taskplanner-asr; then
      printf 'warning: could not resume optional ASR owner; continue with owner-local status\n' >&2
    elif [[ "${TASKPLANNER_LIVE_ASR_AUTO_START:-true}" == "true" ]]; then
      if ! start_operational_asr_capture; then
        printf 'warning: ASR capture did not resume; continue with owner-local status\n' >&2
      fi
    fi
  fi
  write_active_runtime_mode "${mode}"
}

webapp_build_is_current() {
  (
    cd "${ROOT_DIR}/webapp"
    bash scripts/start-production.sh --check-build-current
  )
}

taskplanner_apply_webapp_bundle() {
  local -a profile_args=("$@")
  local container_id user_spec
  user_spec="$(id -u):$(id -g)"

  # Build inside the webapp's existing Compose environment so Vite receives
  # the same browser configuration as the static server. This path changes no
  # Compose service: `exec` uses a running webapp and `run --no-deps` is only
  # a short-lived webapp build process for a stopped dashboard.
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    run_or_print \
      "${COMPOSE[@]}" "${profile_args[@]}" \
      exec -T --user "${user_spec}" webapp bash scripts/apply-build.sh
    return 0
  fi

  container_id="$(
    "${COMPOSE[@]}" "${profile_args[@]}" ps -q webapp 2>/dev/null || true
  )"
  if [[ -n "${container_id}" ]] &&
      [[ "$(docker inspect --format '{{.State.Running}}' "${container_id}" 2>/dev/null || true)" == "true" ]]; then
    run_or_print \
      "${COMPOSE[@]}" "${profile_args[@]}" \
      exec -T --user "${user_spec}" webapp bash scripts/apply-build.sh
  else
    run_or_print \
      "${COMPOSE[@]}" "${profile_args[@]}" \
      run --rm --no-deps --user "${user_spec}" webapp bash scripts/apply-build.sh
  fi
}

refresh_webapp_if_stale() {
  local -a profile_args=("$@")
  if webapp_build_is_current; then
    return 0
  fi
  printf 'Taskplanner webapp source/build stamp changed; applying browser bundle without restarting owners\n'
  taskplanner_apply_webapp_bundle "${profile_args[@]}"
  if [[ "${DRY_RUN:-false}" != "true" ]] && ! webapp_build_is_current; then
    die "webapp source/build stamp remains stale after web-only apply"
  fi
}

warm_restart_runtime_service() {
  local mode="$1"
  local service="$2"
  local requested_profile="${3:-}"
  local profile previous_container current_container
  if [[ -n "${requested_profile}" ]]; then
    profile="${requested_profile}"
  else
    profile="$(taskplanner_runtime_compose_profile_for_mode "${mode}")" || return 1
  fi
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    # The real path recreates only when Compose input changed, otherwise it
    # restarts the existing container.  Show both possible core-only actions
    # without querying Docker from a dry run.
    run_or_print \
      "${COMPOSE[@]}" --profile "${profile}" \
      up -d --no-deps "${service}"
    run_or_print \
      "${COMPOSE[@]}" --profile "${profile}" \
      restart "${service}"
    return 0
  fi
  previous_container="$(
    "${COMPOSE[@]}" --profile "${profile}" ps -q "${service}"
  )"
  [[ -n "${previous_container}" ]] || return 1

  # Compose recreates the service only when its rendered container contract
  # changed. If the contract is unchanged, restart the existing container so
  # bind-mounted Python/launch/config edits still take effect without a remove.
  run_or_print \
    "${COMPOSE[@]}" --profile "${profile}" \
    up -d --no-deps "${service}"
  current_container="$(
    "${COMPOSE[@]}" --profile "${profile}" ps -q "${service}"
  )"
  [[ -n "${current_container}" ]] || return 1
  if [[ "${current_container}" == "${previous_container}" ]]; then
    run_or_print \
      "${COMPOSE[@]}" --profile "${profile}" \
      restart "${service}"
  fi
}

taskplanner_cross_warm_restart_boundary() {
  local mode="$1"
  local service="$2"

  # This boundary still owns only state-core and never changes the endpoint
  # route.  Before destroying the Twin's volatile task state, however, reject
  # a *positive* in-flight report from the independent execution owner.  The
  # launcher supplies this narrow hook; it deliberately does not become a
  # global readiness/preflight dependency for ASR, VLM, camera, or DT health.
  verify_same_mode_warm_restart_interlock "${mode}" || return
  RUNTIME_FAILURE_CLEANUP_SERVICES=("${service}")
  RUNTIME_FAILURE_CLEANUP_ARMED=true
  clear_active_runtime_mode || return
  warm_restart_runtime_service "${mode}" "${service}"
}

run_serial_workspace_build() {
  local mode="$1"
  local build_command package_args="" package_name
  local build_preamble="if [ -f ${CONTAINER_INSTALL_REL}/setup.bash ]; then source ${CONTAINER_INSTALL_REL}/setup.bash; fi &&"
  local -a build_profile_args=(--profile "${mode}" --profile dev)
  ((${#TASKPLANNER_BUILD_PACKAGES[@]} > 0)) ||
    die "no packages were selected for the ${mode} build"
  for package_name in "${TASKPLANNER_BUILD_PACKAGES[@]}"; do
    [[ "${package_name}" =~ ^[a-z][a-z0-9_]*$ ]] ||
      die "invalid package in the ${mode} build plan: ${package_name}"
    printf -v package_args '%s %q' "${package_args}" "${package_name}"
  done
  if mode_requires_integrated_debug_observer "${mode}" &&
      [[ "${INTEGRATED_DEBUG_ENABLED:-true}" == "true" ]]; then
    build_profile_args+=(--profile debug)
  fi

  if [[ "${mode}" == "debug" ]]; then
    build_command="${build_preamble} colcon --log-base log/docker build --build-base build/docker --install-base ${CONTAINER_INSTALL_REL} --symlink-install --packages-select${package_args} --cmake-args -DBUILD_TESTING=OFF && test -f ${CONTAINER_INSTALL_REL}/setup.bash && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/integration_debug_observer && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/integration_debug_control && test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/taskplanner_debug_observer.launch.py && test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/taskplanner_debug_control.launch.py"
    if taskplanner_owner_is_enabled debug-virtual debug; then
      build_command+=" && test -x ${CONTAINER_INSTALL_REL}/surgical_interop_execution/lib/surgical_interop_execution/fault_action_emulator && test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/taskplanner_debug_virtual.launch.py"
    fi
  elif [[ "${mode}" == "live" ]]; then
    build_command="${build_preamble} colcon --log-base log/docker build --build-base build/docker --install-base ${CONTAINER_INSTALL_REL} --symlink-install --packages-select${package_args} --cmake-args -DBUILD_TESTING=OFF && test -f ${CONTAINER_INSTALL_REL}/setup.bash && for launch in taskplanner_state_core.launch.py taskplanner_command.launch.py taskplanner_tool_state.launch.py taskplanner_perception.launch.py taskplanner_cam4_mayo.launch.py taskplanner_projection.launch.py taskplanner_execution.launch.py taskplanner_operator_bridge.launch.py taskplanner_scenario.launch.py taskplanner_surgery_record.launch.py taskplanner_rosbag_recorder.launch.py; do test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/\"\${launch}\"; done && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/operational_asr_node && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/operational_surgery_record && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/operational_rosbag_recorder && test -x ${CONTAINER_INSTALL_REL}/voice_command/lib/voice_command/voice_intent_resolver && test -x ${CONTAINER_INSTALL_REL}/voice_command/lib/voice_command/command_router"
    if mode_uses_tts_sidecar "${mode}"; then
      build_command+=" && test -x ${CONTAINER_INSTALL_REL}/tts_runtime/lib/tts_runtime/tts_runtime_node"
    fi
  elif [[ "${mode}" == "llm-surgeon" ]]; then
    build_command="${build_preamble} colcon --log-base log/docker build --build-base build/docker --install-base ${CONTAINER_INSTALL_REL} --symlink-install --packages-select${package_args} --cmake-args -DBUILD_TESTING=OFF && test -f ${CONTAINER_INSTALL_REL}/setup.bash && for launch in taskplanner_state_core.launch.py taskplanner_command.launch.py taskplanner_tool_state.launch.py taskplanner_perception.launch.py taskplanner_cam4_mayo.launch.py taskplanner_projection.launch.py taskplanner_execution.launch.py taskplanner_operator_bridge.launch.py taskplanner_scenario.launch.py taskplanner_simulation_input.launch.py; do test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/\"\${launch}\"; done && test -x ${CONTAINER_INSTALL_REL}/voice_command/lib/voice_command/voice_intent_resolver"
  else
    build_command="${build_preamble} colcon --log-base log/docker build --build-base build/docker --install-base ${CONTAINER_INSTALL_REL} --symlink-install --packages-select${package_args} --cmake-args -DBUILD_TESTING=OFF && test -f ${CONTAINER_INSTALL_REL}/setup.bash && test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/taskplanner_shadow.launch.py && test -x ${CONTAINER_INSTALL_REL}/shadow_evaluation/lib/shadow_evaluation/interactive_replay_controller"
  fi

  # Build only changed packages plus their selected-mode reverse dependencies.
  # Every runtime container bind-mounts the same build/install/log roots, so the
  # launcher still stops all readers and performs this once in a foreground
  # container before any service consumes the updated overlay.
  run_or_print \
    "${COMPOSE[@]}" "${build_profile_args[@]}" \
    run --rm --no-deps "${BUILD_ARGS[@]}" -T \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/taskplanner-builder \
    -e TASKPLANNER_SKIP_WORKSPACE_SETUP=true \
    taskplanner-dev bash -lc "${build_command}"

  if [[ "${DRY_RUN}" != "true" ]]; then
    taskplanner_record_mode_package_contracts "${mode}"
    printf 'Taskplanner scoped workspace build complete (%s: %s)\n' \
      "${mode}" "${TASKPLANNER_BUILD_PACKAGES[*]}"
  fi
}

taskplanner_build_selected_owner() {
  # Do not stop the runtime plane for this path. Owner code is source-mounted;
  # existing processes keep their loaded modules while colcon writes only the
  # selected package closure. The selected owner is restarted after the build
  # returns, which is the point at which it observes the new installed entry
  # point. A future Compose overlay-generation mount can make the install swap
  # atomic without broadening the command surface.
  local mode="$1"
  local owner="$2"
  local package_args="" package_name build_command
  ((${#TASKPLANNER_BUILD_PACKAGES[@]} > 0)) || return 0
  for package_name in "${TASKPLANNER_BUILD_PACKAGES[@]}"; do
    [[ "${package_name}" =~ ^[a-z][a-z0-9_]*$ ]] ||
      die "invalid package in the ${owner} owner build plan: ${package_name}"
    printf -v package_args '%s %q' "${package_args}" "${package_name}"
  done
  build_command="if [ -f ${CONTAINER_INSTALL_REL}/setup.bash ]; then source ${CONTAINER_INSTALL_REL}/setup.bash; fi && colcon --log-base log/docker build --build-base build/docker --install-base ${CONTAINER_INSTALL_REL} --symlink-install --packages-select${package_args} --cmake-args -DBUILD_TESTING=OFF && test -f ${CONTAINER_INSTALL_REL}/setup.bash"
  if [[ "${owner}" == "rosbag-recorder" ]]; then
    build_command+=" && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/operational_rosbag_recorder && test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/taskplanner_rosbag_recorder.launch.py"
  fi
  run_or_print \
    "${COMPOSE[@]}" --profile "${mode}" --profile dev \
    run --rm --no-deps -T \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/taskplanner-owner-builder \
    -e TASKPLANNER_SKIP_WORKSPACE_SETUP=true \
    taskplanner-dev bash -lc "${build_command}"
  if [[ "${DRY_RUN}" != "true" ]]; then
    taskplanner_record_owner_package_contracts "${mode}"
    printf 'Taskplanner %s: owner-local package build complete (%s: %s)\n' \
      "${mode}" "${owner}" "${TASKPLANNER_BUILD_PACKAGES[*]}"
  fi
}

build_runtime_images_once() {
  local mode="$1"
  # Only an explicit --image-build refreshes images. A stale --ensure-build must
  # rebuild the isolated install/docker overlay without paying for unrelated
  # NInfer/accessory image builds.
  ((${#BUILD_ARGS[@]} > 0)) || return 0
  local -a image_profile_args=(--profile "${mode}")
  local -a image_services=(ninfer-manager)
  if mode_uses_tts_sidecar "${mode}"; then
    image_profile_args+=(--profile ops)
    image_services+=(taskplanner-tts)
  fi
  if [[ "${local_object_perception_enabled:-false}" == "true" ]]; then
    image_profile_args+=(--profile lab)
    image_services+=(object-perception)
  fi
  if [[ "${local_pnu_perception_enabled:-false}" == "true" ]]; then
    # PNU belongs to Debug/Lab only; the mode profile already exposes it for
    # Debug, while the Lab profile keeps the helper reusable in isolation.
    [[ "${mode}" == "debug" ]] || image_profile_args+=(--profile lab)
    image_services+=(pnu-perception)
  fi
  # taskplanner-dev already built the shared taskplanner-ws image for the
  # foreground colcon build. Build each distinct selected image once, then
  # stop forwarding --build to every subsequent Compose `up` call.
  run_or_print \
    "${COMPOSE[@]}" "${image_profile_args[@]}" \
    build "${image_services[@]}"
  BUILD_ARGS=()
}

build_shared_runtime_image_once() {
  local mode="$1"
  ((${#BUILD_ARGS[@]} > 0)) || return 0
  # When no package contract is stale there is no foreground colcon builder
  # (`compose run --build`) to refresh the shared taskplanner-ws image.
  run_or_print \
    "${COMPOSE[@]}" --profile "${mode}" --profile dev \
    build taskplanner-dev
}

taskplanner_build_selected_runtime() {
  local mode="$1"
  if [[ "${BUILD_REQUESTED}" == "true" ]]; then
    if ((${#TASKPLANNER_BUILD_PACKAGES[@]} > 0)); then
      run_serial_workspace_build "${mode}" || return
    fi
  fi

  if [[ "${IMAGE_BUILD_REQUESTED:-false}" == "true" ]]; then
    # A foreground ROS build with --build already constructs taskplanner-dev.
    # Image-only work still needs that one explicit shared image build.
    if [[ "${BUILD_REQUESTED}" != "true" ]] || \
        ((${#TASKPLANNER_BUILD_PACKAGES[@]} == 0)); then
      build_shared_runtime_image_once "${mode}" || return
    fi
    build_runtime_images_once "${mode}"
  fi
}

taskplanner_finalize_runtime_readiness() {
  local mode="$1"
  # Retain the stable call signature while the integrated observer is an
  # independently reported plane rather than a launcher admission barrier.
  local _integrated_debug_enabled="$2"
  shift 2
  local -a status_profile_args=("$@")

  if [[ "${DRY_RUN}" != "true" ]]; then
    "${COMPOSE[@]}" "${status_profile_args[@]}" ps
  fi
  # Compose has accepted the selected owner set.  Do not turn independent
  # rosbridge, ScenarioStore, VLM, ASR, perception, or Debug observations
  # into a global start barrier: each owner publishes its actual status and
  # can be restarted by itself.  The execution path still checks its own
  # controller/type/idempotency contract at dispatch time.
  write_active_runtime_mode "${mode}" || return
  RUNTIME_FAILURE_CLEANUP_ARMED=false
  printf 'Taskplanner %s: owner start requested; inspect owner status for independent readiness\n' \
    "${mode}"
}
