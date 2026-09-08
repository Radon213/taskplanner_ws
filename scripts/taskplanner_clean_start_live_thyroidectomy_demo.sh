#!/usr/bin/env bash
# Warm-restart the small Taskplanner Live core for the thyroidectomy demo.
# The NInfer manager/loaded model and healthy operational ASR are preserved.
# Opening the shortcut never starts a scenario or emits a robot command.
set -Eeuo pipefail

SELF_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT_DIR="$(cd "$(dirname "${SELF_PATH}")/.." && pwd)"
TASKPLANNER_LAUNCHER="${ROOT_DIR}/scripts/taskplanner"
MODE="live"
BUNDLE="thyroidectomy_demo"
BUILD_OPTION="--ensure-build"
DRY_RUN="false"
ACTION="start"
# VIPLab emits the camera streams; the reviewed 192.168.1.7 runtime publishes
# CAM3/CAM4 typed RF-DETR observations. This workstation owns no detector.
CAMERA_PROFILE="${TASKPLANNER_CAMERA_PROFILE:-kalibr}"
# A desktop launch is intentionally safe by default: it never opts into an
# external controller merely because a developer shell has stale exports.  The
# two operations can still be selected independently with the reviewed flags
# below when an operator has verified the matching server/contract.
TOOL_ENDPOINT_SOURCE="virtual"
RETRACTION_ENDPOINT_SOURCE="virtual"

usage() {
  cat <<'EOF'
Taskplanner warm Live restart — thyroidectomy demo

Usage:
  taskplanner_clean_start_live_thyroidectomy_demo.sh [--rebuild|--dry-run|--status|--terminal]
                                                    [--kalibr-cameras|--full-vision]
                                                    [--tool-endpoint external|virtual]
                                                    [--retraction-endpoint external|virtual]

Actions:
  (default)    Restarts only the Live core. A healthy NInfer model remains
               loaded; the independent TTS owner is ensured present while
               Integrated Debug/local perception stay off.
  --rebuild    Rebuilds once, then performs the same scoped warm restart.
  --dry-run    Prints the scoped warm-restart commands without changing state.
  --status     Shows Taskplanner-owned runtime status without restarting it.
  --kalibr-cameras
               Default. Receives VIPLab's current /synced/cam_1..4 streams.
               Consumes CAM3/CAM4 typed RF-DETR results from 192.168.1.7.
  --full-vision
               Use after a fresh /synced/flir stream is also being published.
               Also enables the raw FLIR observer input; RF-DETR stays remote.
  --tool-endpoint external|virtual
               Startup route for the tool-handover Action. Default: virtual.
  --retraction-endpoint external|virtual
               Startup route for the retraction Service. Default: virtual.
               The two routes may be mixed; each keeps its own controller
               contract and readiness check.
  --terminal   Internal desktop-launcher flag; runs in the current terminal.

The resulting runtime is Live (actual integration UI) with
thyroidectomy_demo selected, the requested Action/Service endpoints (both
virtual by default), and the procedure left idle.  It does not issue a
scenario-start or robot-motion command.
EOF
}

die() {
  printf '오류: %s\n' "$*" >&2
  exit 1
}

open_terminal_if_needed() {
  local self="$1"
  if [[ -t 1 || "${ACTION}" != "start" || "${DRY_RUN}" == "true" ]]; then
    return 0
  fi
  if command -v ptyxis >/dev/null 2>&1; then
    exec ptyxis --new-window -T "Taskplanner 실제 통합 · 갑상선절제술(시연)" -- "${self}" --terminal
  fi
  if command -v x-terminal-emulator >/dev/null 2>&1; then
    exec x-terminal-emulator -e "${self}" --terminal
  fi
  die "터미널을 열 수 없습니다. 터미널에서 다음 파일을 실행하세요: ${self}"
}

while (($#)); do
  case "$1" in
    --rebuild)
      BUILD_OPTION="--build"
      ;;
    --dry-run)
      DRY_RUN="true"
      ;;
    --status)
      ACTION="status"
      ;;
    --kalibr-cameras)
      CAMERA_PROFILE="kalibr"
      ;;
  --full-vision)
      CAMERA_PROFILE="full_vision"
      ;;
    --tool-endpoint)
      (($# >= 2)) || die "--tool-endpoint에는 external 또는 virtual이 필요합니다."
      TOOL_ENDPOINT_SOURCE="$2"
      shift
      ;;
    --tool-endpoint=*)
      TOOL_ENDPOINT_SOURCE="${1#*=}"
      ;;
    --retraction-endpoint)
      (($# >= 2)) || die "--retraction-endpoint에는 external 또는 virtual이 필요합니다."
      RETRACTION_ENDPOINT_SOURCE="$2"
      shift
      ;;
    --retraction-endpoint=*)
      RETRACTION_ENDPOINT_SOURCE="${1#*=}"
      ;;
    --terminal)
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "지원하지 않는 옵션입니다: $1"
      ;;
  esac
  shift
done

[[ -x "${TASKPLANNER_LAUNCHER}" ]] || die "Taskplanner 런처를 찾을 수 없습니다: ${TASKPLANNER_LAUNCHER}"

normalize_endpoint_source() {
  local source="${1,,}"
  case "${source}" in
    external|virtual)
      printf '%s' "${source}"
      ;;
    *)
      die "알 수 없는 엔드포인트 소스입니다: ${1} (external 또는 virtual)"
      ;;
  esac
}

TOOL_ENDPOINT_SOURCE="$(normalize_endpoint_source "${TOOL_ENDPOINT_SOURCE}")"
RETRACTION_ENDPOINT_SOURCE="$(normalize_endpoint_source "${RETRACTION_ENDPOINT_SOURCE}")"

open_terminal_if_needed "${SELF_PATH}"

if [[ "${ACTION}" == "status" ]]; then
  exec "${TASKPLANNER_LAUNCHER}" status
fi

command -v flock >/dev/null 2>&1 || die "안전한 재기동을 위해 flock이 필요합니다."
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp}/taskplanner"
mkdir -p "${RUNTIME_DIR}"
## ``desktop-clean-live-demo.lock`` was leaked by an older launcher into a
## long-lived model-manager process.  Use the corrected lock generation so a
## previously completed desktop launch cannot block the updated shortcut.
exec {DESKTOP_LAUNCHER_LOCK_FD}>"${RUNTIME_DIR}/desktop-clean-live-demo-v2.lock"
flock -n "${DESKTOP_LAUNCHER_LOCK_FD}" || die "다른 Taskplanner 데스크톱 재기동이 진행 중입니다."

# Keep the lock in this short-lived launcher only.  Docker Compose and any
# model manager it starts must not inherit the file description; otherwise a
# finished launch can leave the desktop shortcut permanently "busy".
run_taskplanner() {
  "${TASKPLANNER_LAUNCHER}" "$@" {DESKTOP_LAUNCHER_LOCK_FD}>&-
}

# Do not inherit stale developer-shell overrides.  These values make the
# container's DDS file path valid and retain Live's actual integration profile.
unset CYCLONEDDS_URI
export ROS_DOMAIN_ID=0
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
export TASKPLANNER_CONTAINER_CYCLONEDDS_URI="file:///workspaces/taskplanner_ws/config/cyclonedds_lan.xml"
export INPUT_PROFILE=external
export EXECUTION_BACKEND=action
export VITE_DEFAULT_RUNTIME_MODE=live
export TASKPLANNER_LIVE_ENABLE_OPS=false
# Debug remains an independent observer/control workspace.  This quick Live
# shortcut must not silently pull its PipeWire and surgery-record prerequisites
# into every warm restart; launch Debug explicitly when it is needed.
export TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG=false
export TASKPLANNER_LIVE_ENABLE_MULTICAM=false
# Live always owns the independent Supertonic TTS observer.  Do not re-add a
# toggle here: a missing audio device must surface in the owner status rather
# than silently disabling deterministic execution announcements.
# This desktop workflow owns one reviewed model runtime: NInfer with the
# deployed Qwen3.6 35B A3B artifact.  Do not start the LM Studio/Unsloth
# desktop control planes, and keep the vLLM manager in its unloaded state.
# Disabling the unused providers also prevents a UI/provider transition from
# allocating a second large model while the NInfer worker owns VRAM.
export TASKPLANNER_START_LMSTUDIO_CONTROL_PLANE=false
export TASKPLANNER_START_UNSLOTH_CONTROL_PLANE=false
export LMSTUDIO_PROVIDER_ENABLED=false
export UNSLOTH_PROVIDER_ENABLED=false
export VLLM_PROVIDER_ENABLED=false
export VLLM_MANAGER_AUTO_START=false
export NINFER_PROVIDER_ENABLED=true
export NINFER_PROVIDER_MANAGED=true
export NINFER_MANAGER_ENABLED=true
export TASKPLANNER_NINFER_AUTOLOAD_MODEL_ID=qwen3.6-35b-a3b
export VLM_BASE_URL=http://127.0.0.1:8080
export VLM_PROVIDER_ID=ninfer
export VLM_MODEL_ID=qwen3.6-35b-a3b
export PERCEPTION_PROVIDER=external_rfdetr_topics
export PERCEPTION_LOCATION=remote
export PERCEPTION_ENDPOINT=
export PERCEPTION_BACKEND=external
export ENABLE_RFDETR_PERCEPTION=false
export TASKPLANNER_RFDETR_SOURCE_HOST=192.168.1.7

# Pin camera acquisition separately from the operator-view contract. The
# planner still consumes the synchronized source frames, while CAM3/CAM4 in
# the browser consume the remote final overlays. Do not inherit stale shell
# values that could silently put raw pixels back on the operator surface.
export VITE_EXTERNAL_CAM1_TOPIC=/synced/cam_1/color/image_raw/compressed
export VITE_EXTERNAL_CAM2_TOPIC=/synced/cam_2/color/image_raw/compressed
export VITE_EXTERNAL_CAM3_OPERATOR_OVERLAY_TOPIC=/perception/cam_3/overlay/compressed
export VITE_EXTERNAL_CAM4_OPERATOR_OVERLAY_TOPIC=/perception/cam_4/overlay/compressed
export VITE_EXTERNAL_FLIR_TOPIC=/synced/flir/color/image_raw/compressed
export CAM3_INPUT_TOPIC=/synced/cam_3/color/image_raw/compressed
export CAM4_INPUT_TOPIC=/synced/cam_4/color/image_raw/compressed
export FLIR_INPUT_TOPIC=/synced/flir/color/image_raw/compressed
# The surgical monitor is an observer surface and must show the camera before
# a procedure starts as well as while it is active.  This publishes pixels
# only; it does not authorize a planner transition or robot command.
export PUBLISH_FLIR_WHILE_IDLE=true

case "${CAMERA_PROFILE}" in
  kalibr|calibration|observer)
    CAMERA_PROFILE="kalibr"
    export REQUIRE_PERCEPTION_ON_START=false
    # The monitor's FLIR mini-view consumes /surgery/images/flir/compressed.
    # Keep the read-only alias relay enabled even though RF-DETR itself reads
    # CAM3/CAM4 directly from the current Kalibr /synced inputs.
    export PUBLISH_CAMERA_ALIASES=true
    ;;
  full|full_vision)
    CAMERA_PROFILE="full_vision"
    export REQUIRE_PERCEPTION_ON_START=false
    export PUBLISH_CAMERA_ALIASES=true
    ;;
  *)
    die "알 수 없는 카메라 프로필입니다: ${CAMERA_PROFILE} (kalibr 또는 full_vision)"
    ;;
esac

# The Live profile remains the integration-mode runtime.  Keep the Action and
# Service route source explicit across warm restart *and* dashboard-controlled
# Live re-entry; a mixed route must never silently collapse back to one source.
# Defaults stay virtual/virtual, so opening the desktop shortcut alone cannot
# emit traffic to external hardware.
export TASKPLANNER_LIVE_DEFAULT_BUNDLE="${BUNDLE}"
export TASKPLANNER_LIVE_ROBOT_ENDPOINT_SOURCE="${TOOL_ENDPOINT_SOURCE}"
export TASKPLANNER_LIVE_RETRACTION_ENDPOINT_SOURCE="${RETRACTION_ENDPOINT_SOURCE}"
export TASKPLANNER_RUNTIME_CONTROL_LIVE_ROBOT_ENDPOINT_SOURCE="${TOOL_ENDPOINT_SOURCE}"
export TASKPLANNER_RUNTIME_CONTROL_LIVE_RETRACTION_ENDPOINT_SOURCE="${RETRACTION_ENDPOINT_SOURCE}"
export TASKPLANNER_ROBOT_ENDPOINT_SOURCE="${TOOL_ENDPOINT_SOURCE}"

printf '\n실제 통합 모드 · 갑상선절제술(시연) 코어를 warm restart 합니다.\n'
printf '카메라 프로필: %s\n' "${CAMERA_PROFILE}"
printf '인식 결과: 192.168.1.7 typed ROS topics (로컬 RF-DETR/PNU 미기동)\n'
printf '실행 경로: 도구전달 Action=%s · 리트랙션 Service=%s\n' \
  "${TOOL_ENDPOINT_SOURCE}" "${RETRACTION_ENDPOINT_SOURCE}"
if [[ "${DRY_RUN}" == "true" ]]; then
  run_taskplanner up "${MODE}" "${BUILD_OPTION}" --dry-run
  exit 0
fi

run_taskplanner up "${MODE}" "${BUILD_OPTION}"

printf '\n완료: 실제 통합 UI가 선택한 Action/Service 엔드포인트와 함께 시작되었습니다.\n'
printf '번들: 갑상선절제술(시연) · 시나리오: 중지 상태\n'
printf '카메라: VIPLab /synced/cam_1..4\n'
if [[ "${CAMERA_PROFILE}" == "kalibr" ]]; then
  printf '비전: CAM3/CAM4 typed RF-DETR @ 192.168.1.7 · FLIR 선택 사항\n'
else
  printf '비전: FLIR raw view + CAM3/CAM4 typed RF-DETR @ 192.168.1.7\n'
fi
printf 'NInfer: 이미 healthy/loaded이면 재로딩하지 않음\n'
printf 'UI: http://127.0.0.1:4173/\n'
printf '주의: 이 재기동은 물리 로봇 E-stop이 아닙니다. 물리 동작 중이면 장비의 독립 안전 절차를 사용하세요.\n\n'
run_taskplanner status
