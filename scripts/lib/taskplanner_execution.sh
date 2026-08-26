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

webapp_build_is_current() {
  (
    cd "${ROOT_DIR}/webapp"
    bash scripts/start-production.sh --check-build-current
  )
}

refresh_webapp_if_stale() {
  local -a profile_args=("$@")
  if webapp_build_is_current; then
    return 0
  fi
  printf 'Taskplanner webapp source/build stamp changed; rebuilding the browser bundle\n'
  # Isolate --force-recreate to the static UI. The following normal `up`
  # remains responsible for aggregate health waiting and must not recreate
  # NInfer or another preserved sidecar just because frontend source changed.
  run_or_print \
    "${COMPOSE[@]}" "${profile_args[@]}" \
    up -d "${BUILD_ARGS[@]}" --no-deps --force-recreate webapp
}

warm_restart_runtime_service() {
  local mode="$1"
  local service="$2"
  local previous_container current_container
  previous_container="$(
    "${COMPOSE[@]}" --profile "${mode}" ps -q "${service}"
  )"
  [[ -n "${previous_container}" ]] || return 1

  # Compose recreates the service only when its rendered container contract
  # changed. If the contract is unchanged, restart the existing container so
  # bind-mounted Python/launch/config edits still take effect without a remove.
  run_or_print \
    "${COMPOSE[@]}" --profile "${mode}" \
    up -d --no-deps "${service}"
  current_container="$(
    "${COMPOSE[@]}" --profile "${mode}" ps -q "${service}"
  )"
  [[ -n "${current_container}" ]] || return 1
  if [[ "${current_container}" == "${previous_container}" ]]; then
    run_or_print \
      "${COMPOSE[@]}" --profile "${mode}" \
      restart "${service}"
  fi
}

taskplanner_cross_warm_restart_boundary() {
  local mode="$1"
  local service="$2"

  # Stable I/O preservation is valid only after the caller selected the
  # healthy same-mode path. Recheck the stopped reservation immediately before
  # the first authoritative mutation; every fallible sidecar/build preflight
  # must already have completed.
  RUNTIME_FAILURE_CLEANUP_SERVICES=("${service}")
  verify_same_mode_warm_restart_interlock "${mode}" || return
  RUNTIME_FAILURE_CLEANUP_ARMED=true
  clear_active_runtime_mode || return
  warm_restart_runtime_service "${mode}" "${service}"
}

run_serial_workspace_build() {
  local mode="$1"
  local build_command
  local -a build_profile_args=(--profile "${mode}" --profile dev)
  if mode_requires_integrated_debug_observer "${mode}" &&
      [[ "${INTEGRATED_DEBUG_ENABLED:-true}" == "true" ]]; then
    build_profile_args+=(--profile debug)
  fi

  if [[ "${mode}" == "debug" ]]; then
    build_command="colcon --log-base log/docker build --build-base build/docker --install-base ${CONTAINER_INSTALL_REL} --symlink-install --packages-up-to integration_debug vlm_node hand_keypoint_interfaces surgical_perception_msgs --cmake-args -DBUILD_TESTING=OFF && test -f ${CONTAINER_INSTALL_REL}/setup.bash && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/integration_debug_node && test -x ${CONTAINER_INSTALL_REL}/vlm_node/lib/vlm_node/pnu_perception_bridge && test -f ${CONTAINER_INSTALL_REL}/hand_keypoint_interfaces/share/ament_index/resource_index/packages/hand_keypoint_interfaces && test -f ${CONTAINER_INSTALL_REL}/surgical_perception_msgs/share/ament_index/resource_index/packages/surgical_perception_msgs"
  else
    # Dedicated container build and install roots prevent a host ROS build from
    # replacing the Jazzy-generated interfaces consumed by runtime containers.
    # Bind-mounted containers still share one atomic container-only overlay.
    build_command="colcon --log-base log/docker build --build-base build/docker --install-base ${CONTAINER_INSTALL_REL} --symlink-install --cmake-args -DBUILD_TESTING=OFF && test -f ${CONTAINER_INSTALL_REL}/setup.bash && test -e ${CONTAINER_INSTALL_REL}/bringup/share/bringup/launch/taskplanner_live.launch.py && test -x ${CONTAINER_INSTALL_REL}/integration_debug/lib/integration_debug/operational_asr_node && test -x ${CONTAINER_INSTALL_REL}/voice_command/lib/voice_command/vlm_function_admission_gate"
  fi

  # Every runtime container bind-mounts this workspace, including build/docker,
  # install/docker, and log/docker. Build once in a foreground one-off
  # container and wait for its artifact checks before starting any reader.
  # The process-wide launcher lock also prevents a second launcher invocation
  # from starting services in the middle of this critical section.
  run_or_print \
    "${COMPOSE[@]}" "${build_profile_args[@]}" \
    run --rm --no-deps "${BUILD_ARGS[@]}" -T \
    --user "$(id -u):$(id -g)" \
    -e HOME=/tmp/taskplanner-builder \
    -e TASKPLANNER_SKIP_WORKSPACE_SETUP=true \
    taskplanner-dev bash -lc "${build_command}"

  if [[ "${DRY_RUN}" != "true" ]]; then
    if [[ "${mode}" == "debug" ]]; then
      record_debug_install_contract
    else
      record_runtime_install_contract
    fi
    printf 'Taskplanner workspace build complete (%s)\n' "${mode}"
  fi
}

build_runtime_images_once() {
  local mode="$1"
  # Only an explicit --build refreshes images. A stale --ensure-build must
  # rebuild the isolated install/docker overlay without paying for unrelated
  # NInfer/accessory image builds.
  ((${#BUILD_ARGS[@]} > 0)) || return 0
  local -a image_profile_args=(--profile "${mode}")
  local -a image_services=(ninfer-manager)
  if mode_uses_tts_sidecar "${mode}"; then
    image_profile_args+=(--profile ops)
    image_services+=(taskplanner-tts)
  fi
  if mode_uses_ops_plane "${mode}"; then
    if ! mode_uses_tts_sidecar "${mode}"; then
      image_profile_args+=(--profile ops)
    fi
    image_services+=(monitor-media-gateway)
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

taskplanner_build_selected_runtime() {
  local mode="$1"
  [[ "${BUILD_REQUESTED}" == "true" ]] || return 0
  run_serial_workspace_build "${mode}" || return
  build_runtime_images_once "${mode}"
}

wait_for_websocket_endpoint() {
  local host="$1"
  local port="$2"
  local path="$3"
  local label="$4"
  local timeout_sec="${5:-${WAIT_TIMEOUT_SEC}}"
  local owner_container_id="${6:-}"
  if [[ "${DRY_RUN}" == "true" ]]; then
    printf '+ wait-for-websocket %q %q %q %q\n' \
      "${host}" "${port}" "${path}" "${label}"
    return 0
  fi
  [[ "${port}" =~ ^[0-9]+$ ]] || die "${label} port is not numeric: ${port}"
  (( port >= 1 && port <= 65535 )) || die "${label} port is invalid: ${port}"
  [[ "${path}" == /* ]] || die "${label} websocket path must start with /"
  python3 - "${host}" "${port}" "${path}" "${timeout_sec}" "${label}" "${owner_container_id}" <<'PY'
import base64
import os
import socket
import subprocess
import sys
import time

host, port_raw, path, timeout_raw, label, owner_container_id = sys.argv[1:]
port = int(port_raw)
timeout = max(1.0, float(timeout_raw))
deadline = time.monotonic() + timeout
last_error = "endpoint did not accept a websocket handshake"

while time.monotonic() < deadline:
    if owner_container_id:
        try:
            inspected = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    owner_container_id,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=0.75,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SystemExit(f"{label} owner state could not be checked: {error}")
        if inspected.returncode != 0 or inspected.stdout.strip() != "true":
            raise SystemExit(f"{label} owner container exited before the endpoint became ready")
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection((host, port), timeout=1.0) as connection:
            connection.settimeout(1.0)
            connection.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response and len(response) < 8192:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        status_line = bytes(response).split(b"\r\n", 1)[0]
        if status_line.startswith((b"HTTP/1.1 101 ", b"HTTP/1.0 101 ")):
            print(f"{label}: websocket ready on {host}:{port}{path}")
            raise SystemExit(0)
        last_error = f"unexpected handshake response: {status_line[:120]!r}"
    except OSError as error:
        last_error = str(error)
    time.sleep(0.1)

raise SystemExit(f"{label} was not ready after {timeout:g}s: {last_error}")
PY
}

wait_for_mode_rosbridge() {
  local mode="$1"
  local router_port runtime_service owner_container_id=""
  local websocket_path="/"
  case "${mode}" in
    live)
      runtime_service="taskplanner-runtime"
      if mode_uses_ops_plane "${mode}"; then
        router_port="$(
          compose_environment_value \
            "${mode}" webapp VITE_ROSBRIDGE_TAILSCALE_PORT 9091
        )"
        websocket_path="$(
          compose_environment_value \
            "${mode}" webapp VITE_ROSBRIDGE_LIVE_TAILSCALE_PATH /live
        )"
      else
        router_port="$(
          compose_environment_value \
            "${mode}" taskplanner-runtime ROSBRIDGE_PORT 9090
        )"
      fi
      ;;
    llm-surgeon)
      runtime_service="taskplanner-runtime"
      router_port="$(
        compose_environment_value \
          "${mode}" webapp VITE_ROSBRIDGE_TAILSCALE_PORT 9091
      )"
      websocket_path="$(
        compose_environment_value \
          "${mode}" webapp VITE_ROSBRIDGE_LLM_TAILSCALE_PATH /llm
      )"
      ;;
    replay)
      runtime_service="shadow-runner"
      router_port="$(
        compose_environment_value \
          "${mode}" webapp VITE_ROSBRIDGE_TAILSCALE_PORT 9091
      )"
      websocket_path="$(
        compose_environment_value \
          "${mode}" webapp VITE_ROSBRIDGE_SHADOW_TAILSCALE_PATH /shadow
      )"
      ;;
    debug)
      runtime_service="integration-debug"
      router_port="$(
        compose_environment_value \
          "${mode}" webapp VITE_ROSBRIDGE_TAILSCALE_PORT 9091
      )"
      ;;
    *)
      die "unsupported ROS bridge readiness mode: ${mode}"
      ;;
  esac
  local timeout_sec="${TASKPLANNER_ROSBRIDGE_WAIT_TIMEOUT_SEC:-30}"
  [[ "${timeout_sec}" =~ ^[0-9]+$ && "${timeout_sec}" -ge 1 ]] ||
    die "TASKPLANNER_ROSBRIDGE_WAIT_TIMEOUT_SEC must be a positive integer"
  if [[ "${DRY_RUN}" != "true" ]]; then
    owner_container_id="$(
      "${COMPOSE[@]}" --profile "${mode}" ps -q "${runtime_service}"
    )"
    [[ -n "${owner_container_id}" ]] ||
      die "${mode} runtime container is unavailable before ROS bridge readiness"
  fi
  wait_for_websocket_endpoint \
    127.0.0.1 "${router_port}" "${websocket_path}" \
    "${mode} ROS bridge router" "${timeout_sec}" "${owner_container_id}"
}

wait_for_multicam_observer() {
  local mode="$1"
  local router_port
  router_port="$(
    compose_environment_value \
      "${mode}" webapp VITE_ROSBRIDGE_DEBUG_PORT 9091
  )"
  # Camera publishers and /multicam_node/capture_status are intentionally not
  # boot prerequisites. The operator UI reports their absence separately;
  # startup only verifies that the read-only observer websocket is reachable.
  wait_for_websocket_endpoint \
    127.0.0.1 "${router_port}" /multicam "multicam observer" \
    "${TASKPLANNER_MULTICAM_OBSERVER_WAIT_TIMEOUT_SEC:-8}"
}

wait_for_mode_semantic_ready() {
  local mode="$1"
  local service topic message_type control_service control_service_type
  local expected_case="" expected_bundle=""
  case "${mode}" in
    live|llm-surgeon)
      service="taskplanner-runtime"
      topic="/simulation/state"
      message_type="surgical_msgs/msg/SimulationState"
      control_service="/simulation/control"
      control_service_type="surgical_msgs/srv/ControlSimulation"
      # The external Live launch consumes its own bundle variable. Do not
      # validate it against the mock/replay default.
      expected_bundle="$(
        compose_environment_value \
          "${mode}" taskplanner-runtime TASKPLANNER_LIVE_DEFAULT_BUNDLE thyroidectomy_demo
      )"
      ;;
    replay)
      service="shadow-runner"
      topic="/shadow/replay_state"
      message_type="surgical_msgs/msg/ShadowReplayState"
      control_service="/shadow/control_replay"
      control_service_type="surgical_msgs/srv/ControlShadowReplay"
      expected_case="$(
        compose_environment_value \
          "${mode}" shadow-runner SHADOW_CASE_ID "${SHADOW_CASE_ID:-0704_6}"
      )"
      ;;
    debug)
      service="integration-debug"
      topic="/integration/debug/status"
      message_type="std_msgs/msg/String"
      control_service="/integration/debug/check_readiness"
      control_service_type="std_srvs/srv/Trigger"
      ;;
    *)
      die "unsupported semantic readiness mode: ${mode}"
      ;;
  esac
  if [[ "${DRY_RUN}" == "true" ]]; then
    printf '+ wait-for-ros-semantic-ready %q %q %q %q %q %q %q\n' \
      "${mode}" "${service}" "${topic}" "${message_type}" \
      "${control_service}" "${control_service_type}" "${expected_bundle}"
    return 0
  fi

  local timeout_sec="${TASKPLANNER_SEMANTIC_READY_TIMEOUT_SEC:-30}"
  [[ "${timeout_sec}" =~ ^[0-9]+$ && "${timeout_sec}" -ge 1 ]] ||
    die "TASKPLANNER_SEMANTIC_READY_TIMEOUT_SEC must be a positive integer"
  local deadline=$((SECONDS + timeout_sec))
  while (( SECONDS < deadline )); do
    if "${COMPOSE[@]}" --profile "${mode}" exec -T "${service}" \
        bash -lc 'set -o pipefail
          source /opt/ros/jazzy/setup.bash
          source /opt/btops_ws/install/setup.bash
          source /workspaces/taskplanner_ws/install/docker/setup.bash
          actual_type="$(timeout 8 ros2 service type "$1")"
          [[ "${actual_type}" == "$2" ]]
          if [[ "$5" == "live" || "$5" == "llm-surgeon" ]]; then
            publisher_info="$(timeout 8 ros2 topic info --no-daemon -v "$3")"
            [[ "$(grep -c "Node name: or_digital_twin$" <<<"${publisher_info}")" == "1" ]]
            [[ "$(awk "/^Publisher count:/{print \$3; exit}" <<<"${publisher_info}")" == "1" ]]
            transition_service="/simulation/check_transition_ready"
            transition_type="std_srvs/srv/Trigger"
            [[ "$(timeout 8 ros2 service type "${transition_service}")" == "${transition_type}" ]]
            timeout 8 ros2 service call "${transition_service}" "${transition_type}" "{}" |
              grep -Eq "success=(True|true)"
          elif [[ "$5" == "debug" ]]; then
            timeout 8 ros2 service call "$1" "$2" "{}" |
              grep -Eq "success=(True|False), message="
          fi
          timeout 8 ros2 topic echo --once --no-daemon --spin-time 1 --timeout 5 --flow-style --full-length "$3" "$4" |
            python3 /workspaces/taskplanner_ws/scripts/taskplanner_ros_readiness.py --mode "$5" --expected-case "$6" --expected-bundle "$7"' \
        -- "${control_service}" "${control_service_type}" "${topic}" \
        "${message_type}" "${mode}" "${expected_case}" "${expected_bundle}"; then
      printf '%s ROS semantic readiness: received %s\n' "${mode}" "${topic}"
      return 0
    fi
    sleep 0.2
  done
  die "${mode} ROS semantic readiness timed out waiting for ${topic}"
}

taskplanner_finalize_runtime_readiness() {
  local mode="$1"
  local integrated_debug_enabled="$2"
  shift 2
  local -a status_profile_args=("$@")

  if [[ "${DRY_RUN}" != "true" ]]; then
    "${COMPOSE[@]}" "${status_profile_args[@]}" ps
  fi
  wait_for_mode_rosbridge "${mode}" || return
  wait_for_mode_semantic_ready "${mode}" || return
  if [[ "${integrated_debug_enabled}" == "true" ]]; then
    wait_for_mode_rosbridge debug || return
    wait_for_mode_semantic_ready debug || return
  fi
  write_active_runtime_mode "${mode}" || return
  RUNTIME_FAILURE_CLEANUP_ARMED=false
  if mode_uses_multicam_observer "${mode}"; then
    run_best_effort \
      "multicam observer is unavailable; camera monitoring remains degraded" \
      wait_for_multicam_observer "${mode}"
  fi
}
