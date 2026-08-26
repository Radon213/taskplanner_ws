#!/usr/bin/env bash

# Declarative runtime-mode ownership for scripts/taskplanner.
#
# This module decides which optional process lanes belong to a launcher mode
# and which stale containers must be converged away.  It deliberately owns no
# transition authority: stopped-state checks, cleanup timing, Compose calls,
# and readiness waits remain in the launcher.

TASKPLANNER_ALL_PROFILE_ARGS=(
  --profile live
  --profile llm-surgeon
  --profile replay
  --profile shadow
  --profile debug
  --profile dev
  --profile ops
  --profile lab
)

TASKPLANNER_DEBUG_FAILURE_CLEANUP_SERVICES=(
  taskplanner-runtime
  public-rosbridge
  public-rosbridge-lan-proxy
  taskplanner-asr
  taskplanner-tts
  shadow-runner
  object-perception
  pnu-perception
  integration-debug
  integration-debug-lan-proxy
)

TASKPLANNER_OPERATIONAL_FAILURE_CLEANUP_SERVICES=(
  taskplanner-runtime
  public-rosbridge
  public-rosbridge-lan-proxy
  taskplanner-asr
  taskplanner-tts
  shadow-runner
  object-perception
  pnu-perception
  integration-debug
  integration-debug-lan-proxy
  integration-debug-tailscale-proxy
  webapp-lan-proxy
  monitor-media-gateway
  vllm-manager
)

taskplanner_mode_is_valid() {
  case "${1:-}" in
    live|llm-surgeon|replay|debug) return 0 ;;
    *) return 1 ;;
  esac
}

enforce_runtime_mode_contract() {
  # Compose gives inherited shell variables priority over --env-file. Pin the
  # non-negotiable identity of each operational mode before rendering it.
  case "${1:-}" in
    live)
      export TASKPLANNER_RUNTIME_MODE=live
      export INPUT_PROFILE=external
      export EXECUTION_BACKEND=action
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
      # Production consumes typed DDS facts from the reviewed external host;
      # it never owns an RF-DETR HTTP worker or local checkpoint.
      export ENABLE_RFDETR_PERCEPTION=false
      export PERCEPTION_PROVIDER=external_rfdetr_topics
      export PERCEPTION_LOCATION=remote
      export PERCEPTION_ENDPOINT=
      export PERCEPTION_BACKEND=external
      export TASKPLANNER_RFDETR_SOURCE_HOST=192.168.1.7
      # Pin the synchronized camera namespace too, so a stale terminal cannot
      # silently substitute a preview/unsynchronized ingress.
      export FLIR_INPUT_TOPIC=/synced/flir/color/image_raw/compressed
      export CAM3_INPUT_TOPIC=/synced/cam_3/color/image_raw/compressed
      export CAM4_INPUT_TOPIC=/synced/cam_4/color/image_raw/compressed
      export VITE_EXTERNAL_CAM1_TOPIC=/synced/cam_1/color/image_raw/compressed
      export VITE_EXTERNAL_CAM2_TOPIC=/synced/cam_2/color/image_raw/compressed
      export VITE_EXTERNAL_CAM3_TOPIC=/synced/cam_3/color/image_raw/compressed
      export VITE_EXTERNAL_CAM4_TOPIC=/synced/cam_4/color/image_raw/compressed
      export VITE_EXTERNAL_FLIR_TOPIC=/synced/flir/color/image_raw/compressed
      export CV_CAM4_RGB_TOPIC=/synced/cam_4/color/image_raw/compressed
      export CV_CAM4_CAMERA_INFO_TOPIC=/synced/cam_4/color/camera_info
      export CV_CAM4_NATIVE_DEPTH_COMPRESSED_TOPIC=/synced/cam_4/depth/image_rect_raw/compressedDepth
      export CV_CAM4_DEPTH_CAMERA_INFO_TOPIC=/synced/cam_4/depth/camera_info
      export CV_CAM4_DEPTH_TO_COLOR_EXTRINSICS_TOPIC=/synced/cam_4/extrinsics/depth_to_color
      export CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC=/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth
      export CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC=/synced/cam_4/aligned_depth_to_color/camera_info
      ;;
    llm-surgeon)
      export TASKPLANNER_RUNTIME_MODE=llm-surgeon
      export INPUT_PROFILE=simulation
      export EXECUTION_BACKEND=mock
      ;;
    replay|debug)
      # These modes do not start taskplanner-runtime. Remove a stale marker so
      # config inspection cannot authorize the shared operational service.
      unset TASKPLANNER_RUNTIME_MODE
      ;;
  esac
}

mode_requires_integrated_debug_observer() {
  [[ "$1" == "live" && \
    "${TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG:-false}" == "true" ]]
}

mode_uses_multicam_observer() {
  case "$1" in
    debug) return 0 ;;
    live)
      [[ "${TASKPLANNER_LIVE_ENABLE_MULTICAM:-${TASKPLANNER_LIVE_ENABLE_OPS:-false}}" == "true" ]]
      ;;
    *) return 1 ;;
  esac
}

mode_uses_ops_plane() {
  [[ "$1" == "live" && "${TASKPLANNER_LIVE_ENABLE_OPS:-false}" == "true" ]]
}

mode_uses_operational_asr_sidecar() {
  [[ "$1" == "live" ]]
}

mode_uses_tts_sidecar() {
  [[ "$1" == "live" && "${TASKPLANNER_LIVE_ENABLE_TTS:-false}" == "true" ]]
}

taskplanner_same_mode_warm_restart_allowed() {
  local mode="${1:-}"
  local active_runtime_ready="${2:-false}"
  local build_requested="${3:-false}"
  [[ "${mode}" != "debug" && \
    "${active_runtime_ready}" == "true" && \
    "${build_requested}" != "true" ]]
}

taskplanner_select_optional_recreate_args() {
  local same_mode_warm_restart="${1:-false}"
  TASKPLANNER_SELECTED_RECREATE_ARGS=()
  if [[ "${same_mode_warm_restart}" != "true" ]]; then
    TASKPLANNER_SELECTED_RECREATE_ARGS=(--force-recreate)
  fi
}

taskplanner_select_operational_warm_cleanup_services() {
  local mode="$1"
  local local_object_perception_enabled="${2:-false}"
  local local_pnu_perception_enabled="${3:-false}"
  local integrated_debug_enabled="${4:-false}"

  # A same-profile warm restart keeps the stable I/O plane alive. Only lanes
  # that the requested profile no longer owns are converged away here.
  TASKPLANNER_SELECTED_RESET_SERVICES=(vllm-manager)
  if [[ "${mode}" != "live" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(taskplanner-asr)
  fi
  if ! mode_uses_tts_sidecar "${mode}"; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(taskplanner-tts)
  fi
  if [[ "${local_object_perception_enabled}" != "true" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(object-perception)
  fi
  if [[ "${local_pnu_perception_enabled}" != "true" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(pnu-perception)
  fi
  if ! mode_uses_multicam_observer "${mode}"; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(multicam-observer)
  fi
  if [[ "${integrated_debug_enabled}" != "true" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(integration-debug)
  fi
  if ! mode_uses_ops_plane "${mode}"; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(
      public-rosbridge-lan-proxy
      webapp-lan-proxy
      monitor-media-gateway
      integration-debug-lan-proxy
      integration-debug-tailscale-proxy
    )
  fi
}

taskplanner_select_debug_reset_services() {
  local build_requested="${1:-false}"
  TASKPLANNER_SELECTED_RESET_SERVICES=(
    taskplanner-runtime
    public-rosbridge
    public-rosbridge-lan-proxy
    taskplanner-asr
    taskplanner-tts
    shadow-runner
    object-perception
    pnu-perception
    integration-debug
    integration-debug-lan-proxy
  )
  if [[ "${build_requested}" == "true" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(multicam-observer)
  fi
}

taskplanner_select_operational_reset_services() {
  local mode="$1"
  local build_requested="${2:-false}"
  local local_object_perception_enabled="${3:-false}"
  local local_pnu_perception_enabled="${4:-false}"

  TASKPLANNER_SELECTED_RESET_SERVICES=(
    taskplanner-runtime
    public-rosbridge
    public-rosbridge-lan-proxy
    shadow-runner
    # Explicit Ops/Lab accessories must not survive convergence to a mode
    # that does not own them, including containers from an older reboot.
    webapp-lan-proxy
    monitor-media-gateway
    integration-debug-tailscale-proxy
    vllm-manager
  )
  if [[ "${mode}" != "live" ]] || [[ "${build_requested}" == "true" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(taskplanner-asr)
  fi
  if ! mode_uses_tts_sidecar "${mode}"; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(taskplanner-tts)
  fi
  if [[ "${local_object_perception_enabled}" != "true" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(object-perception)
  fi
  if [[ "${local_pnu_perception_enabled}" != "true" ]]; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(pnu-perception)
  fi
  TASKPLANNER_SELECTED_RESET_SERVICES+=(
    integration-debug
    integration-debug-lan-proxy
  )
  if [[ "${build_requested}" == "true" ]] || \
      ! mode_uses_multicam_observer "${mode}"; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(multicam-observer)
  fi
}
