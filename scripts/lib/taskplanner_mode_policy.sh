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
  --profile owners
)

TASKPLANNER_DEBUG_FAILURE_CLEANUP_SERVICES=(
  public-rosbridge
  public-rosbridge-lan-proxy
  local-media-rosbridge
  taskplanner-asr
  taskplanner-tts
  shadow-runner
  object-perception
  pnu-perception
  taskplanner-debug-observer
  taskplanner-debug-control
  taskplanner-debug-virtual
)

TASKPLANNER_OPERATIONAL_FAILURE_CLEANUP_SERVICES=(
  public-rosbridge
  public-rosbridge-lan-proxy
  local-media-rosbridge
  taskplanner-asr
  taskplanner-tts
  shadow-runner
  object-perception
  pnu-perception
  taskplanner-debug-observer
  taskplanner-debug-control
  taskplanner-debug-virtual
  integration-debug-tailscale-proxy
  webapp-lan-proxy
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
      # Keep acquisition on synchronized source frames. Browser CAM3/CAM4 use
      # the separately named remote final-overlay contract below, so a stale
      # terminal cannot silently substitute raw pixels on the operator view.
      export FLIR_INPUT_TOPIC=/synced/flir/color/image_raw/compressed
      export CAM3_INPUT_TOPIC=/synced/cam_3/color/image_raw/compressed
      export CAM4_INPUT_TOPIC=/synced/cam_4/color/image_raw/compressed
      export VITE_EXTERNAL_CAM1_TOPIC=/synced/cam_1/color/image_raw/compressed
      export VITE_EXTERNAL_CAM2_TOPIC=/synced/cam_2/color/image_raw/compressed
      export VITE_EXTERNAL_CAM3_OPERATOR_OVERLAY_TOPIC=/perception/cam_3/overlay/compressed
      export VITE_EXTERNAL_CAM4_OPERATOR_OVERLAY_TOPIC=/perception/cam_4/overlay/compressed
      export VITE_EXTERNAL_FLIR_TOPIC=/synced/flir/color/image_raw/compressed
      export CV_CAM4_RGB_TOPIC=/synced/cam_4/color/image_raw/compressed
      export CV_CAM4_CAMERA_INFO_TOPIC=/synced/cam_4/color/camera_info
      export CV_CAM4_NATIVE_DEPTH_COMPRESSED_TOPIC=/synced/cam_4/depth/image_rect_raw/compressedDepth
      export CV_CAM4_DEPTH_CAMERA_INFO_TOPIC=/synced/cam_4/depth/camera_info
      export CV_CAM4_DEPTH_TO_COLOR_EXTRINSICS_TOPIC=/synced/cam_4/extrinsics/depth_to_color
      export CV_CAM4_ALIGNED_DEPTH_COMPRESSED_TOPIC=/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth
      export CV_CAM4_ALIGNED_DEPTH_CAMERA_INFO_TOPIC=/synced/cam_4/aligned_depth_to_color/camera_info
      # Live's integrated observer is routed through the optional 9091 path
      # router, so its private upstream stays on 9093.
      export ROSBRIDGE_DEBUG_UPSTREAM_PORT=9093
      ;;
    llm-surgeon)
      export TASKPLANNER_RUNTIME_MODE=llm-surgeon
      export INPUT_PROFILE=simulation
      export EXECUTION_BACKEND=mock
      export ROSBRIDGE_DEBUG_UPSTREAM_PORT=9093
      ;;
    replay)
      # These modes do not start the split operational owner set. Remove a
      # stale marker so an old operational environment cannot leak into them.
      unset TASKPLANNER_RUNTIME_MODE
      ;;
    debug)
      # Standalone Debug has no path proxy. Bind its observer directly to the
      # browser's reviewed loopback port instead of starting a network sidecar.
      unset TASKPLANNER_RUNTIME_MODE
      export ROSBRIDGE_DEBUG_UPSTREAM_PORT=9091
      ;;
  esac
}

mode_requires_integrated_debug_observer() {
  [[ "$1" == "live" && \
    "${TASKPLANNER_LIVE_ENABLE_INTEGRATED_DEBUG:-false}" == "true" ]]
}

mode_uses_multicam_observer() {
  case "$1" in
    debug) return 1 ;;
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
  # TTS is an independent Live owner, not an Ops toggle.  It begins model
  # prewarm with Live startup but its availability never admits or blocks a
  # deterministic Action/Service command.
  [[ "$1" == "live" ]]
}

taskplanner_select_mode_build_roots() {
  local mode="${1:-}"
  TASKPLANNER_SELECTED_BUILD_ROOT_PACKAGES=()
  case "${mode}" in
    live)
      # integration_debug owns the independently restartable operational ASR
      # executable. taskplanner_bt_trees is a runtime resource selected by
      # ID, so it is an explicit root even though Python cannot express that
      # dynamic lookup as a package.xml dependency.
      TASKPLANNER_SELECTED_BUILD_ROOT_PACKAGES=(
        bringup
        integration_debug
        taskplanner_bt_trees
      )
      # ``tts_runtime`` has its own sidecar owner and its Python source is
      # bind-mounted.  Normal Live startup must not rebuild it; use
      # ``taskplanner restart tts --ensure-build`` only after an installed
      # entrypoint/package-contract change.
      ;;
    llm-surgeon|replay)
      TASKPLANNER_SELECTED_BUILD_ROOT_PACKAGES=(
        bringup
        taskplanner_bt_trees
      )
      ;;
    debug)
      TASKPLANNER_SELECTED_BUILD_ROOT_PACKAGES=(bringup integration_debug)
      ;;
    *) return 1 ;;
  esac
}

taskplanner_select_debug_reset_services() {
  local build_requested="${1:-false}"
  TASKPLANNER_SELECTED_RESET_SERVICES=(
    public-rosbridge
    public-rosbridge-lan-proxy
    local-media-rosbridge
    taskplanner-asr
    taskplanner-tts
    shadow-runner
    object-perception
    pnu-perception
    taskplanner-debug-observer
    taskplanner-debug-control
    taskplanner-debug-virtual
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
    public-rosbridge
    public-rosbridge-lan-proxy
    local-media-rosbridge
    shadow-runner
    # Explicit Ops/Lab accessories must not survive convergence to a mode
    # that does not own them, including containers from an older reboot.
    webapp-lan-proxy
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
    taskplanner-debug-control
    taskplanner-debug-virtual
  )
  if [[ "${build_requested}" == "true" ]] || \
      ! mode_uses_multicam_observer "${mode}"; then
    TASKPLANNER_SELECTED_RESET_SERVICES+=(multicam-observer)
  fi
}
