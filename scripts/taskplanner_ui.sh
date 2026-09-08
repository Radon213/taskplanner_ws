#!/usr/bin/env bash
set -euo pipefail

# Research-speed UI delivery owner. This script deliberately manages only the
# `webapp` Compose service; it has no ROS, ASR, VLM, or scenario authority.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEBAPP_DIR="${ROOT_DIR}/webapp"
WEBAPP_START_SCRIPT="${WEBAPP_DIR}/scripts/start-production.sh"
DEV_COMPOSE_FILE="${ROOT_DIR}/docker-compose.ui-dev.yml"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/taskplanner"
RUNTIME_STATE_FILE="${TASKPLANNER_RUNTIME_CONTROL_STATE_FILE:-${RUNTIME_DIR}/active-runtime-mode.json}"
DRY_RUN=false
UI_MODE=""
COMPOSE=()

usage() {
  cat <<'EOF'
Taskplanner UI iteration commands

Usage:
  scripts/taskplanner ui dev [--mode live|llm-surgeon|replay|debug] [--dry-run]
  scripts/taskplanner ui apply [--mode live|llm-surgeon|replay|debug] [--dry-run]
  scripts/taskplanner ui status [--mode live|llm-surgeon|replay|debug]
  scripts/taskplanner ui restart [--mode live|llm-surgeon|replay|debug] [--dry-run]

Commands:
  dev     Replace only the webapp service with Vite HMR on 127.0.0.1:4173.
          ROS, ASR, VLM, NInfer, rosbridge, and runtime owners stay running.
  apply   Build the browser bundle and write its source stamp outside dist.
          It runs in the webapp environment and never recreates a service.
  status  Show whether the current webapp is static or Vite HMR plus build
          stamp freshness. It does not change runtime state.
  restart Recreate only the static webapp owner after a browser-server source
          change. It verifies the served build hash and never restarts ROS,
          ASR, VLM, NInfer, rosbridge, or another runtime owner.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

print_command() {
  printf 'DRY-RUN +'
  printf ' %q' "$@"
  printf '\n'
}

run_or_print() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    print_command "$@"
  else
    "$@"
  fi
}

validate_mode() {
  case "$1" in
    live|llm-surgeon|replay|debug) ;;
    *) die "mode must be live, llm-surgeon, replay, or debug" ;;
  esac
}

active_mode_or_live() {
  python3 - "${RUNTIME_STATE_FILE}" <<'PY'
import json
import sys
from pathlib import Path

state_file = Path(sys.argv[1])
try:
    parsed = json.loads(state_file.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    parsed = {}
mode = parsed.get("mode") if isinstance(parsed, dict) else None
print(mode if mode in {"live", "llm-surgeon", "replay", "debug"} else "live")
PY
}

compose_init() {
  local mode="$1"
  local env_file
  COMPOSE=(
    docker compose
    --project-directory "${ROOT_DIR}"
    -f "${ROOT_DIR}/docker-compose.yml"
  )
  for env_file in \
    "${ROOT_DIR}/.env.example" \
    "${ROOT_DIR}/.env" \
    "${ROOT_DIR}/docker/orchestration/${mode}.env"; do
    [[ -f "${env_file}" ]] && COMPOSE+=(--env-file "${env_file}")
  done
}

acquire_ui_lock() {
  [[ "${DRY_RUN}" == "true" ]] && return 0
  command -v flock >/dev/null 2>&1 || die "flock is required for Taskplanner UI commands"
  mkdir -p "${RUNTIME_DIR}"
  exec {UI_LOCK_FD}>"${RUNTIME_DIR}/launcher.lock"
  flock -w "${TASKPLANNER_UI_LOCK_SEC:-10}" "${UI_LOCK_FD}" ||
    die "another Taskplanner start/stop operation is still in progress"
}

webapp_container_id() {
  "${COMPOSE[@]}" ps -q webapp 2>/dev/null || true
}

webapp_is_running() {
  local container_id
  container_id="$(webapp_container_id)"
  [[ -n "${container_id}" ]] || return 1
  [[ "$(docker inspect --format '{{.State.Running}}' "${container_id}" 2>/dev/null || true)" == "true" ]]
}

assert_webapp_build_current() {
  (
    cd "${WEBAPP_DIR}"
    bash scripts/start-production.sh --check-build-current
  ) || die "webapp build stamp is not current after apply"
}

wait_for_hmr() {
  local attempt hmr_client
  for attempt in $(seq 1 30); do
    hmr_client="$(curl --fail --silent --show-error --max-time 1 http://127.0.0.1:4173/@vite/client 2>/dev/null || true)"
    if [[ "${hmr_client}" == *"createHotContext"* ]]; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

ui_dev() {
  acquire_ui_lock
  run_or_print \
    "${COMPOSE[@]}" -f "${DEV_COMPOSE_FILE}" \
    up -d --no-deps --force-recreate webapp
  [[ "${DRY_RUN}" == "true" ]] && return 0
  wait_for_hmr || die "webapp did not expose Vite HMR on 127.0.0.1:4173"
  printf 'Taskplanner UI development server ready: http://127.0.0.1:4173/\n'
}

ui_apply() {
  local user_spec
  user_spec="$(id -u):$(id -g)"
  acquire_ui_lock
  if [[ "${DRY_RUN}" == "true" ]]; then
    print_command \
      "${COMPOSE[@]}" exec -T --user "${user_spec}" \
      webapp bash scripts/apply-build.sh
    return 0
  fi

  if webapp_is_running; then
    "${COMPOSE[@]}" exec -T --user "${user_spec}" \
      webapp bash scripts/apply-build.sh
  else
    "${COMPOSE[@]}" run --rm --no-deps --user "${user_spec}" \
      webapp bash scripts/apply-build.sh
  fi
  assert_webapp_build_current
  printf 'Taskplanner UI apply complete; no ROS or sidecar restart was requested.\n'
}

wait_for_static_build() {
  local expected_hash observed attempt
  expected_hash="$(sha256sum "${WEBAPP_DIR}/dist/index.html" | cut -c1-12)"
  for attempt in $(seq 1 30); do
    observed="$(curl --fail --silent --show-error --max-time 1 \
      http://127.0.0.1:4173/healthz 2>/dev/null || true)"
    if [[ "${observed}" == *"\"build\":\"${expected_hash}\""* ]]; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

ui_restart() {
  acquire_ui_lock
  [[ "${DRY_RUN}" == "true" ]] || assert_webapp_build_current
  run_or_print \
    "${COMPOSE[@]}" \
    up -d --no-deps --force-recreate webapp
  [[ "${DRY_RUN}" == "true" ]] && return 0
  wait_for_static_build || die "webapp restarted but did not serve the current browser build"
  printf 'Taskplanner UI restart complete; no ROS or sidecar restart was requested.\n'
}

ui_status() {
  local build_state="stale" container_id command_line server_state="stopped"
  if (
    cd "${WEBAPP_DIR}"
    bash scripts/start-production.sh --check-build-current
  ); then
    build_state="current"
  fi

  container_id="$(webapp_container_id)"
  if [[ -n "${container_id}" ]] && webapp_is_running; then
    command_line="$(docker inspect --format '{{join .Config.Cmd " "}}' "${container_id}" 2>/dev/null || true)"
    if [[ "${command_line}" == *"start-development.sh"* || "${command_line}" == *"npm run dev"* ]]; then
      server_state="vite-hmr"
    else
      server_state="static"
    fi
  fi

  printf 'webapp server: %s\n' "${server_state}"
  printf 'webapp build stamp: %s\n' "${build_state}"
  printf 'webapp source: %s\n' "${WEBAPP_DIR}"
  if [[ "${server_state}" == "vite-hmr" ]]; then
    printf 'HMR URL: http://127.0.0.1:4173/\n'
  fi
}

command_name="${1:-status}"
shift || true
case "${command_name}" in
  dev|apply|status|restart)
    while (($#)); do
      case "$1" in
        --mode)
          (($# >= 2)) || die "--mode requires a mode name"
          UI_MODE="$2"
          shift
          ;;
        --mode=*) UI_MODE="${1#*=}" ;;
        --dry-run)
          [[ "${command_name}" != "status" ]] || die "status does not accept --dry-run"
          DRY_RUN=true
          ;;
        -h|--help)
          usage
          exit 0
          ;;
        *) die "unknown ui option: $1" ;;
      esac
      shift
    done
    if [[ -z "${UI_MODE}" ]]; then
      UI_MODE="$(active_mode_or_live)"
    fi
    validate_mode "${UI_MODE}"
    compose_init "${UI_MODE}"
    case "${command_name}" in
      dev) ui_dev ;;
      apply) ui_apply ;;
      status) ui_status ;;
      restart) ui_restart ;;
    esac
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    die "ui command must be dev, apply, or status"
    ;;
esac
