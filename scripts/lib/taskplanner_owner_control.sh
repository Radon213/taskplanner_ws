#!/usr/bin/env bash

# Lightweight mutation layer over config/taskplanner_runtime_owners.toml.
# Status remains read-only in the Python helper. This file scopes mutation to
# one declared Compose owner and never performs a broad readiness census.
# Canonical owner-set deployment remains in taskplanner up.

taskplanner_owner_registry_command() {
  # Keep owner capability selection in the TOML registry.  When the launcher
  # has selected mode env files, pass the same ordered files into the registry
  # so an optional owner is not started merely because the shell did not source
  # Docker's env-file inputs.
  local -a command=(
    python3 "${ROOT_DIR}/scripts/taskplanner_owner_registry.py"
    --root "${ROOT_DIR}"
  )
  local env_file
  if declare -p ENV_FILES >/dev/null 2>&1; then
    for env_file in "${ENV_FILES[@]}"; do
      [[ -f "${env_file}" ]] || continue
      command+=(--env-file "${env_file}")
    done
  fi
  "${command[@]}" "$@"
}

taskplanner_owner_resolve() {
  local owner="$1"
  local mode="$2"
  local resolved
  resolved="$(
    taskplanner_owner_registry_command resolve "${owner}" --mode "${mode}"
  )" || return
  IFS='|' read -r \
    TASKPLANNER_OWNER_NAME \
    TASKPLANNER_OWNER_SERVICE \
    TASKPLANNER_OWNER_RESTART_STRATEGY \
    TASKPLANNER_OWNER_COMPOSE_PROFILE \
    TASKPLANNER_OWNER_LAUNCH_FILE \
    TASKPLANNER_OWNER_MODE_SUPPORTED \
    TASKPLANNER_OWNER_RESTART_IMPACT \
    TASKPLANNER_OWNER_AFFECTED_OWNERS \
    TASKPLANNER_OWNER_BUILD_ROOTS \
    TASKPLANNER_OWNER_ALIASES \
    TASKPLANNER_OWNER_ENABLED <<<"${resolved}"
  # The registry may intentionally preserve a short-lived CLI alias (for
  # example `core` -> `state-core`).  Downstream commands always use the
  # canonical resolved name, so aliases never become a second owner identity.
  [[ -n "${TASKPLANNER_OWNER_NAME}" ]] || return 1
}

taskplanner_owner_exists() {
  taskplanner_owner_resolve "$1" "${2:-live}" >/dev/null 2>&1
}

taskplanner_owner_is_enabled() {
  taskplanner_owner_resolve "$1" "${2:-live}" >/dev/null 2>&1 &&
    [[ "${TASKPLANNER_OWNER_MODE_SUPPORTED}" == "true" &&
       "${TASKPLANNER_OWNER_ENABLED}" == "true" ]]
}

taskplanner_owner_list() {
  taskplanner_owner_registry_command list
}

taskplanner_owner_build_roots() {
  local owner="$1"
  local mode="$2"
  taskplanner_owner_registry_command build-roots "${owner}" --mode "${mode}"
}

taskplanner_owner_restart_impact() {
  local owner="$1"
  local mode="$2"
  taskplanner_owner_registry_command impact "${owner}" --mode "${mode}"
}

taskplanner_owner_services_for_mode() {
  taskplanner_owner_registry_command services --mode "$1"
}

taskplanner_all_split_owner_services() {
  local service
  local -A seen=()
  while IFS= read -r service; do
    [[ -n "${service}" && "${service}" != "shadow-runner" ]] || continue
    [[ -z "${seen[${service}]+x}" ]] || continue
    seen["${service}"]=1
    printf '%s\n' "${service}"
  done < <(
    taskplanner_owner_services_for_mode live
    taskplanner_owner_services_for_mode llm-surgeon
    # Cleanup must include the optional virtual Debug provider even when the
    # *next* mode's env file disables it.  Otherwise a Debug-to-Live switch
    # can leave the old emulator running beside the operational endpoint.
    TASKPLANNER_DEBUG_ENABLE_VIRTUAL_ROBOT=true \
      taskplanner_owner_services_for_mode debug
  )
}

taskplanner_owner_status() {
  local owner="${1:-all}"
  local mode="${2:-live}"
  taskplanner_owner_registry_command status "${owner}" --mode "${mode}"
}

taskplanner_owner_scenario_state_root() {
  # Resolve the ScenarioStore's one host-state bind mount from Compose.  This
  # keeps owner restart bootstrap aligned with a user-configured state root
  # instead of copying a home-directory default into each restart path.
  "${COMPOSE[@]}" --profile owners config --format json |
    python3 -c '
import json
import sys

config = json.load(sys.stdin)
for volume in config["services"]["taskplanner-scenario"].get("volumes", []):
    if volume.get("type") == "bind" and volume.get("target") == "/taskplanner-scenario-state":
        print(volume["source"])
        break
else:
    raise SystemExit("taskplanner-scenario state bind mount is missing")
'
}

taskplanner_owner_prepare_persisted_scenario_bundle() {
  # A scoped owner restart must construct against the ScenarioStore-selected
  # bundle, not the bundle configured when the original Compose container was
  # first created.  This is a local bootstrap hint only: ScenarioStore remains
  # the sole parser/selection writer, and unavailable state never prevents an
  # otherwise valid owner restart.
  local mode="$1"
  local state_root selection_path bundle_name
  TASKPLANNER_OWNER_SELECTED_BUNDLE=""
  case "${mode}" in
    live|llm-surgeon) ;;
    *) return 0 ;;
  esac
  [[ "${TASKPLANNER_SCENARIO_RESTORE_PERSISTED_SELECTION:-true}" == "true" ]] ||
    return 0
  [[ "${DRY_RUN:-false}" != "true" ]] || return 0
  state_root="$(taskplanner_owner_scenario_state_root 2>/dev/null)" || {
    printf 'warning: cannot resolve ScenarioStore state; restarting owner with its current default\n' >&2
    return 0
  }
  [[ -n "${state_root}" ]] || return 0
  selection_path="${state_root}/selected_bundle.json"
  bundle_name="$(
    python3 "${ROOT_DIR}/scripts/taskplanner_scenario_selection.py" \
      "${selection_path}"
  )"
  [[ -n "${bundle_name}" ]] || return 0
  TASKPLANNER_OWNER_SELECTED_BUNDLE="${bundle_name}"
  case "${mode}" in
    live) export TASKPLANNER_LIVE_DEFAULT_BUNDLE="${bundle_name}" ;;
    llm-surgeon) export TASKPLANNER_DEFAULT_BUNDLE="${bundle_name}" ;;
  esac
}

taskplanner_owner_container_selected_bundle() {
  local mode="$1"
  local container_id="$2"
  local variable
  case "${mode}" in
    live) variable="TASKPLANNER_LIVE_DEFAULT_BUNDLE" ;;
    llm-surgeon) variable="TASKPLANNER_DEFAULT_BUNDLE" ;;
    *) return 0 ;;
  esac
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
    "${container_id}" 2>/dev/null |
    awk -F= -v key="${variable}" '$1 == key { print substr($0, length(key) + 2); exit }'
}

# Print PROFILE|SERVICE for the process that currently owns scenario reload.
# The ScenarioStore is the only configuration owner. A missing/stopped owner
# fails closed; reload never redirects to the retired monolithic runtime.
taskplanner_scenario_reload_target() {
  local mode="$1"
  local container_ids dedicated_profile dedicated_service
  taskplanner_owner_resolve scenario "${mode}" || return
  [[ "${TASKPLANNER_OWNER_MODE_SUPPORTED}" == "true" ]] || return 1
  dedicated_profile="${TASKPLANNER_OWNER_COMPOSE_PROFILE}"
  dedicated_service="${TASKPLANNER_OWNER_SERVICE}"
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    printf '%s|%s\n' "${dedicated_profile}" "${dedicated_service}"
    return 0
  fi
  container_ids="$(
    "${COMPOSE[@]}" --profile "${dedicated_profile}" \
      ps --status running -q "${dedicated_service}" 2>/dev/null || true
  )"
  if [[ -n "${container_ids}" && "${container_ids}" != *$'\n'* ]]; then
    printf '%s|%s\n' "${dedicated_profile}" "${dedicated_service}"
    return 0
  fi

  return 1
}

taskplanner_restart_dedicated_owner() {
  local mode="$1"
  local owner="$2"
  local ensure_build="${3:-false}"
  local previous_container previous_running current_container launch_path
  local previous_bundle recreate_for_selected_bundle="false"
  taskplanner_owner_resolve "${owner}" "${mode}" ||
    die "unknown runtime owner: ${owner}"
  [[ "${TASKPLANNER_OWNER_MODE_SUPPORTED}" == "true" ]] ||
    die "owner ${owner} is not available in ${mode} mode"
  [[ "${TASKPLANNER_OWNER_ENABLED}" == "true" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} is disabled by its current capability setting"
  [[ "${TASKPLANNER_OWNER_RESTART_STRATEGY}" == "dedicated" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} has no dedicated restart boundary yet"
  [[ -n "${TASKPLANNER_OWNER_SERVICE}" &&
     -n "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" ]] ||
    die "owner ${owner} has an incomplete Compose mapping"

  if [[ "${ensure_build}" == "true" ]]; then
    taskplanner_build_owner_if_needed "${mode}" "${TASKPLANNER_OWNER_NAME}"
  fi

  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    print_command verify-owner-container-deployed "${TASKPLANNER_OWNER_NAME}" "${TASKPLANNER_OWNER_SERVICE}"
    if [[ "${TASKPLANNER_OWNER_NAME}" == "execution" ]]; then
      taskplanner_verify_execution_restart_allowed "${TASKPLANNER_OWNER_SERVICE}"
    fi
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      up -d --no-deps "${TASKPLANNER_OWNER_SERVICE}"
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      restart "${TASKPLANNER_OWNER_SERVICE}"
    return 0
  fi

  launch_path="${ROOT_DIR}/src/bringup/launch/${TASKPLANNER_OWNER_LAUNCH_FILE}"
  [[ -f "${launch_path}" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} launch is not installed in source yet: ${TASKPLANNER_OWNER_LAUNCH_FILE}"

  previous_container="$(
    "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      ps -aq "${TASKPLANNER_OWNER_SERVICE}"
  )"
  [[ -n "${previous_container}" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} is not independently deployed"
  [[ "${previous_container}" != *$'\n'* ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} has more than one Compose container"
  previous_running="$(
    docker inspect --format '{{.State.Running}}' "${previous_container}" 2>/dev/null || true
  )"
  taskplanner_owner_prepare_persisted_scenario_bundle "${mode}"
  if [[ -n "${TASKPLANNER_OWNER_SELECTED_BUNDLE:-}" ]]; then
    previous_bundle="$(
      taskplanner_owner_container_selected_bundle "${mode}" "${previous_container}"
    )"
    if [[ "${previous_bundle}" != "${TASKPLANNER_OWNER_SELECTED_BUNDLE}" ]]; then
      recreate_for_selected_bundle="true"
    fi
  fi

  # A running execution owner can still hold an in-memory Action/Service
  # request, so retain its own fresh route-state check before it is restarted.
  # An exited owner has no such in-process request and cannot publish that
  # state at all; requiring it here made the recovery command self-dependent.
  # `up --no-deps` below is intentionally allowed to recover that isolated
  # process without inventing a manager/preflight transaction.
  if [[ "${TASKPLANNER_OWNER_NAME}" == "execution" && "${previous_running}" == "true" ]]; then
    taskplanner_verify_execution_restart_allowed "${TASKPLANNER_OWNER_SERVICE}"
  fi

  # `up` starts an exited owner and recreates only when its own Compose
  # contract changed.  If the same running container remains, restart it once
  # so bind-mounted Python changes become active.
  if [[ "${recreate_for_selected_bundle}" == "true" ]]; then
    # Environment interpolation happens when Compose creates the owner.  A
    # plain container restart would retain the old default_bundle and silently
    # reconstruct a topology for the wrong scenario.
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      up -d --no-deps --force-recreate "${TASKPLANNER_OWNER_SERVICE}"
  else
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      up -d --no-deps "${TASKPLANNER_OWNER_SERVICE}"
  fi
  current_container="$(
    "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      ps -q "${TASKPLANNER_OWNER_SERVICE}"
  )"
  [[ -n "${current_container}" ]] ||
    die "owner ${owner} did not start"
  if [[ "${recreate_for_selected_bundle}" != "true" &&
        "${current_container}" == "${previous_container}" &&
        "${previous_running}" == "true" ]]; then
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      restart "${TASKPLANNER_OWNER_SERVICE}"
  fi
}

taskplanner_restart_sidecar_owner() {
  # A sidecar is an independent Compose owner whose executable is already
  # installed. Unlike a split ROS-launch owner, it does not need a bringup
  # launch-file sentinel; restarting it is still strictly one container.
  local mode="$1"
  local owner="$2"
  local ensure_build="${3:-false}"
  local previous_container previous_running current_container
  taskplanner_owner_resolve "${owner}" "${mode}" ||
    die "unknown runtime owner: ${owner}"
  [[ "${TASKPLANNER_OWNER_MODE_SUPPORTED}" == "true" ]] ||
    die "owner ${owner} is not available in ${mode} mode"
  [[ "${TASKPLANNER_OWNER_ENABLED}" == "true" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} is disabled by its current capability setting"
  [[ "${TASKPLANNER_OWNER_RESTART_STRATEGY}" == "sidecar" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} has no sidecar restart boundary"
  [[ -n "${TASKPLANNER_OWNER_SERVICE}" &&
     -n "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" ]] ||
    die "owner ${owner} has an incomplete Compose mapping"

  if [[ "${ensure_build}" == "true" ]]; then
    taskplanner_build_owner_if_needed "${mode}" "${TASKPLANNER_OWNER_NAME}"
  fi

  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      up -d --no-deps "${TASKPLANNER_OWNER_SERVICE}"
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      restart "${TASKPLANNER_OWNER_SERVICE}"
    return 0
  fi

  previous_container="$(
    "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      ps -q "${TASKPLANNER_OWNER_SERVICE}"
  )"
  [[ -z "${previous_container}" || "${previous_container}" != *$'\n'* ]] ||
    die "owner ${owner} has more than one Compose container"
  previous_running="false"
  if [[ -n "${previous_container}" ]]; then
    previous_running="$(
      docker inspect --format '{{.State.Running}}' "${previous_container}" 2>/dev/null || true
    )"
  fi
  run_or_print \
    "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
    up -d --no-deps "${TASKPLANNER_OWNER_SERVICE}"
  current_container="$(
    "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      ps -q "${TASKPLANNER_OWNER_SERVICE}"
  )"
  [[ -n "${current_container}" ]] ||
    die "owner ${owner} did not start"
  if [[ -n "${previous_container}" &&
        "${current_container}" == "${previous_container}" &&
        "${previous_running}" == "true" ]]; then
    run_or_print \
      "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
      restart "${TASKPLANNER_OWNER_SERVICE}"
  fi
}

taskplanner_start_sidecar_owner() {
  # This is intentionally narrower than a runtime start: it starts exactly
  # one declarative sidecar and never creates a ROS graph, runs a build, or
  # reconciles another owner.
  local mode="$1"
  local owner="$2"
  taskplanner_owner_resolve "${owner}" "${mode}" ||
    die "unknown runtime owner: ${owner}"
  [[ "${TASKPLANNER_OWNER_MODE_SUPPORTED}" == "true" ]] ||
    die "owner ${owner} is not available in ${mode} mode"
  [[ "${TASKPLANNER_OWNER_ENABLED}" == "true" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} is disabled by its current capability setting"
  [[ "${TASKPLANNER_OWNER_RESTART_STRATEGY}" == "sidecar" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} has no sidecar start boundary"
  [[ -n "${TASKPLANNER_OWNER_SERVICE}" &&
     -n "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" ]] ||
    die "owner ${owner} has an incomplete Compose mapping"
  run_or_print \
    "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
    up -d --no-deps "${TASKPLANNER_OWNER_SERVICE}"
}

taskplanner_stop_sidecar_owner() {
  # Keep the stopped container as an explicit operator choice.  A later
  # scoped start/restart can reuse that one container without removing any
  # unrelated owner or application data.
  local mode="$1"
  local owner="$2"
  taskplanner_owner_resolve "${owner}" "${mode}" ||
    die "unknown runtime owner: ${owner}"
  [[ "${TASKPLANNER_OWNER_MODE_SUPPORTED}" == "true" ]] ||
    die "owner ${owner} is not available in ${mode} mode"
  [[ "${TASKPLANNER_OWNER_ENABLED}" == "true" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} is disabled by its current capability setting"
  [[ "${TASKPLANNER_OWNER_RESTART_STRATEGY}" == "sidecar" ]] ||
    die "owner ${TASKPLANNER_OWNER_NAME} has no sidecar stop boundary"
  [[ -n "${TASKPLANNER_OWNER_SERVICE}" &&
     -n "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" ]] ||
    die "owner ${owner} has an incomplete Compose mapping"
  run_or_print \
    "${COMPOSE[@]}" --profile "${TASKPLANNER_OWNER_COMPOSE_PROFILE}" \
    stop "${TASKPLANNER_OWNER_SERVICE}"
}

taskplanner_build_owner_if_needed() {
  # Existing Python source is bind-mounted and needs only the selected owner
  # restart.  This path exists for an explicitly requested installed launch or
  # console-entrypoint change.  It computes the owner closure from the same
  # TOML inventory, builds only stale packages, and leaves unrelated owner
  # containers running.  A later generation-overlay mount can make the swap
  # fully atomic without changing this CLI/API contract.
  local mode="$1"
  local owner="$2"
  taskplanner_prepare_owner_build_plan "${mode}" "${owner}" false ||
    die "could not compute the ${owner} package build plan"
  if ((${#TASKPLANNER_BUILD_PACKAGES[@]} == 0)); then
    printf 'Taskplanner %s: %s owner install is current; no build needed\n' \
      "${mode}" "${owner}"
    return 0
  fi
  printf 'Taskplanner %s: building only %s owner package(s): %s\n' \
    "${mode}" "${owner}" "${TASKPLANNER_BUILD_PACKAGES[*]}"
  taskplanner_build_selected_owner "${mode}" "${owner}"
}

taskplanner_verify_execution_restart_allowed() {
  local service="$1"
  local max_age_sec="${TASKPLANNER_EXECUTION_RESTART_STATE_MAX_AGE_SEC:-3.0}"
  local echo_command gate_result
  if [[ "${DRY_RUN:-false}" == "true" ]]; then
    print_command verify-execution-restart-state \
      /integration/execution_route/state restart_allowed
    return 0
  fi
  [[ "${max_age_sec}" =~ ^[0-9]+([.][0-9]+)?$ ]] ||
    die "TASKPLANNER_EXECUTION_RESTART_STATE_MAX_AGE_SEC must be numeric"
  echo_command='set -e
set +u
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source /workspaces/taskplanner_ws/install/docker/setup.bash
set -u
timeout 6 ros2 topic echo --once --no-daemon --spin-time 1 --timeout 4 --flow-style --full-length "$1" "$2"'
  gate_result="$(
    "${COMPOSE[@]}" --profile owners exec -T "${service}" \
      bash -lc "${echo_command}" taskplanner-execution-restart-gate \
      /integration/execution_route/state std_msgs/msg/String |
      python3 "${ROOT_DIR}/scripts/taskplanner_execution_restart_gate.py" \
        --max-age-sec "${max_age_sec}"
  )" || die "execution owner restart rejected: ${gate_result:-route state unavailable}"
  [[ "${gate_result}" == "allowed" ]] ||
    die "execution owner restart rejected: ${gate_result}"
}
