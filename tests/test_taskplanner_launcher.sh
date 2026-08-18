#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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

bash -n "${ROOT_DIR}/scripts/taskplanner"

assert_single_serial_build() {
  local output="$1"
  local mode_label="$2"
  local build_count stop_line build_line first_start_line
  build_count="$(grep -F -c 'colcon\ build' <<<"${output}" || true)"
  [[ "${build_count}" == "1" ]] ||
    fail "${mode_label} --build must run exactly one foreground colcon build"
  stop_line="$(grep -n -F ' stop ' <<<"${output}" | head -n 1 | cut -d: -f1)"
  build_line="$(grep -n -F ' run --rm --no-deps --build -T ' <<<"${output}" | cut -d: -f1)"
  first_start_line="$(grep -n -F ' up -d ' <<<"${output}" | head -n 1 | cut -d: -f1)"
  [[ -n "${stop_line}" && -n "${build_line}" && -n "${first_start_line}" && \
    "${stop_line}" -lt "${build_line}" && \
    "${build_line}" -lt "${first_start_line}" ]] ||
    fail "${mode_label} must stop workspace readers, build once, then start services"
}

live_output="$(
  TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=false \
  TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=true \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run
)"
assert_contains "${live_output}" \
  "--profile live --profile debug up -d --force-recreate integration-debug integration-debug-lan-proxy"
assert_contains "${live_output}" \
  "--profile live up -d integration-debug-tailscale-proxy"
assert_contains "${live_output}" \
  "stop taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner integration-debug integration-debug-lan-proxy"
assert_contains "${live_output}" \
  "rm -f taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner integration-debug integration-debug-lan-proxy"
assert_not_contains "${live_output}" \
  "stop taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy integration-debug-tailscale-proxy"
assert_contains "${live_output}" \
  "--profile live --profile debug up -d --remove-orphans --wait --wait-timeout 300 vllm-manager ninfer-manager webapp"
assert_contains "${live_output}" \
  "--profile live up -d multicam-observer"
assert_not_contains "${live_output}" \
  "--wait --wait-timeout 300 vllm-manager ninfer-manager webapp multicam-observer"
assert_not_contains "${live_output}" \
  "--force-recreate --remove-orphans --wait --wait-timeout 300 vllm-manager ninfer-manager webapp"
assert_contains "${live_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 /live live\\ ROS\\ bridge\\ router"
assert_contains "${live_output}" \
  "+ wait_for_multicam_observer live"
assert_not_contains "${live_output}" "stop multicam-observer"
assert_not_contains "${live_output}" "rm -f multicam-observer"
assert_contains "${live_output}" \
  "+ wait-for-ros-semantic-ready live taskplanner-runtime /simulation/state surgical_msgs/msg/SimulationState /simulation/control surgical_msgs/srv/ControlSimulation"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 taskplanner-asr"
asr_start_line="$(grep -n -- 'taskplanner-asr$' <<<"${live_output}" | tail -n 1 | cut -d: -f1)"
runtime_start_line="$(grep -n -- 'up -d --force-recreate taskplanner-runtime$' <<<"${live_output}" | cut -d: -f1)"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 public-rosbridge"
assert_contains "${live_output}" \
  "--profile live up -d object-perception"
assert_contains "${live_output}" \
  "--profile live up -d --wait --wait-timeout 30 object-perception"
disabled_bridge_output="$(ENABLE_PUBLIC_ROSBRIDGE=false "${ROOT_DIR}/scripts/taskplanner" up live --dry-run)"
assert_not_contains "${disabled_bridge_output}" \
  "--profile live up -d --force-recreate public-rosbridge"
[[ -n "${asr_start_line}" && -n "${runtime_start_line}" && \
  "${asr_start_line}" -lt "${runtime_start_line}" ]] || \
  fail "live must health-wait for taskplanner-asr before taskplanner-runtime"

external_cv_output="$(PERCEPTION_BACKEND=external "${ROOT_DIR}/scripts/taskplanner" up live --dry-run)"
assert_not_contains "${external_cv_output}" \
  "--profile live up -d object-perception"
assert_not_contains "${external_cv_output}" \
  "--profile live up -d --wait --wait-timeout 30 object-perception"

live_build_output="$("${ROOT_DIR}/scripts/taskplanner" up live --dry-run --build)"
assert_single_serial_build "${live_build_output}" "live"
assert_contains "${live_build_output}" \
  "--profile live --profile dev --profile debug run --rm --no-deps --build -T"
assert_contains "${live_build_output}" \
  "taskplanner-dev bash -lc colcon\\ build\\ --symlink-install\\ --cmake-args"
assert_not_contains "${live_build_output}" \
  "colcon\\ build\\ --symlink-install\\ --packages-select"

llm_output="$("${ROOT_DIR}/scripts/taskplanner" up llm-surgeon --dry-run)"
assert_contains "${llm_output}" \
  "--profile llm-surgeon --profile debug up -d --force-recreate integration-debug integration-debug-lan-proxy"
assert_contains "${llm_output}" \
  "--profile llm-surgeon up -d integration-debug-tailscale-proxy"
assert_contains "${llm_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 /llm llm-surgeon\\ ROS\\ bridge\\ router"
assert_contains "${llm_output}" \
  "+ wait-for-ros-semantic-ready llm-surgeon taskplanner-runtime /simulation/state surgical_msgs/msg/SimulationState /simulation/control surgical_msgs/srv/ControlSimulation"
assert_not_contains "${llm_output}" \
  "--profile llm-surgeon up -d --force-recreate --wait --wait-timeout 300 taskplanner-asr"
llm_build_output="$("${ROOT_DIR}/scripts/taskplanner" up llm-surgeon --dry-run --build)"
assert_single_serial_build "${llm_build_output}" "llm-surgeon"

degraded_live_output="$(
  TASKPLANNER_PIPEWIRE_SOCKET=/definitely/missing/taskplanner-pipewire \
  PUZZLE_SURGERY_RECORD_API_KEY_FILE=/definitely/missing/taskplanner-key \
    "${ROOT_DIR}/scripts/taskplanner" up live --dry-run 2>&1
)"
assert_contains "${degraded_live_output}" \
  "warning: optional integrated Debug sidecar skipped:"
assert_not_contains "${degraded_live_output}" \
  "--profile live --profile debug up -d --force-recreate integration-debug integration-debug-lan-proxy"
assert_contains "${degraded_live_output}" \
  "+ wait-for-ros-semantic-ready live taskplanner-runtime /simulation/state surgical_msgs/msg/SimulationState /simulation/control surgical_msgs/srv/ControlSimulation"

replay_build_output="$("${ROOT_DIR}/scripts/taskplanner" up replay --dry-run --build)"
assert_single_serial_build "${replay_build_output}" "replay"
assert_contains "${replay_build_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 /shadow replay\\ ROS\\ bridge\\ router"
assert_contains "${replay_build_output}" \
  "--profile replay up -d --build --remove-orphans --wait --wait-timeout 300 vllm-manager ninfer-manager webapp"
assert_contains "${replay_build_output}" \
  "--profile replay up -d --build multicam-observer"
assert_contains "${replay_build_output}" \
  "+ wait_for_multicam_observer replay"
assert_contains "${replay_build_output}" \
  "integration-debug integration-debug-lan-proxy multicam-observer"
assert_contains "${replay_build_output}" \
  "+ wait-for-ros-semantic-ready replay shadow-runner /shadow/replay_state surgical_msgs/msg/ShadowReplayState /shadow/control_replay surgical_msgs/srv/ControlShadowReplay"

python3 - "${ROOT_DIR}/config/cyclonedds_lan.xml" <<'PY'
import sys
from xml.etree import ElementTree

root = ElementTree.parse(sys.argv[1]).getroot()
namespace = {"c": "https://cdds.io/config"}
domain = root.find("c:Domain", namespace)
assert domain is not None
assert domain.attrib == {"Id": "any"}
fragment_size = domain.findtext("c:General/c:FragmentSize", namespaces=namespace)
assert fragment_size == "1344B"
peers = domain.findall("c:Discovery/c:Peers/c:Peer", namespace)
assert peers
assert all(set(peer.attrib) == {"Address"} for peer in peers)
PY

live_config="$("${ROOT_DIR}/scripts/taskplanner" config live)"
assert_contains "${live_config}" "taskplanner-asr:"
assert_contains "${live_config}" "integration-debug:"
assert_contains "${live_config}" "TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK: \"true\""
assert_contains "${live_config}" "TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE: \"false\""
assert_contains "${live_config}" "TASKPLANNER_ASR_CAPTURE_LOCK: /taskplanner-runs/asr/microphone.lock"
assert_contains "${live_config}" "SENTENCE_INPUT_TOPIC: /sensors/surgeon/sentence"
assert_contains "${live_config}" "operational_asr_node"
assert_contains "${live_config}" "/input/asr/runtime_status"
assert_contains "${live_config}" "Node name: taskplanner_asr"
assert_not_contains "${live_config}" "colcon build"
printf '%s\n' "${live_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
asr = config["services"]["taskplanner-asr"]
runtime = config["services"]["taskplanner-runtime"]
public_bridge = config["services"]["public-rosbridge"]
integration_debug = config["services"]["integration-debug"]
multicam_observer = config["services"]["multicam-observer"]
webapp = config["services"]["webapp"]
assert asr["profiles"] == ["live"]
assert asr["healthcheck"]["interval"] == "5s"
assert asr["healthcheck"]["start_interval"] == "1s"
assert asr["healthcheck"]["timeout"] == "6s"
assert asr["healthcheck"]["start_period"] == "10s"
assert asr["healthcheck"]["retries"] == 60
assert asr["stop_grace_period"] == "45s"
assert runtime["stop_signal"] == "SIGINT"
assert runtime["stop_grace_period"] == "15s"
rendered_asr_command = " ".join(asr["command"])
assert "exec /workspaces/taskplanner_ws/install/integration_debug/lib/integration_debug/operational_asr_node" in rendered_asr_command
assert "exec ros2 run" not in rendered_asr_command
rendered_public_bridge_command = " ".join(public_bridge["command"])
assert "exec /workspaces/taskplanner_ws/install/surgical_interop_gateway/lib/surgical_interop_gateway/public_rosbridge" in rendered_public_bridge_command
assert "exec ros2 run" not in rendered_public_bridge_command
assert public_bridge["healthcheck"]["start_interval"] == "1s"
assert public_bridge["healthcheck"]["interval"] == "5s"
assert public_bridge["healthcheck"]["start_period"] == "5s"
assert public_bridge["healthcheck"]["timeout"] == "2s"
assert public_bridge["healthcheck"]["retries"] == 60
rendered_public_health = " ".join(public_bridge["healthcheck"]["test"])
assert "taskplanner_public_bridge_health.py" in rendered_public_health
assert "socket.create_connection" not in rendered_public_health
assert asr["image"] == runtime["image"]
assert asr["network_mode"] == runtime["network_mode"] == "host"
assert asr["user"]
for key in (
    "ROS_DOMAIN_ID",
    "ROS_AUTOMATIC_DISCOVERY_RANGE",
    "RMW_IMPLEMENTATION",
    "CYCLONEDDS_URI",
    "SENTENCE_INPUT_TOPIC",
):
    assert asr["environment"][key] == runtime["environment"][key], key
for service in (runtime, asr, public_bridge, integration_debug):
    environment = service["environment"]
    assert environment["ROS_DOMAIN_ID"] == "0"
    assert environment["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "SUBNET"
    assert environment["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
    assert environment["CYCLONEDDS_URI"].endswith(
        "/config/cyclonedds_lan.xml"
    )
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in environment
assert integration_debug["environment"]["TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE"] == (
    "secure_operational_debug_rosbridge"
)
assert "rosbridge_executable:=$${TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE}" in (
    " ".join(integration_debug["command"])
)
assert runtime["environment"]["CAM4_INPUT_TOPIC"] == (
    "/synced/cam_4/color/image_raw/compressed"
)
assert runtime["environment"]["FLIR_INPUT_TOPIC"] == (
    "/synced/flir/color/image_raw/compressed"
)
assert runtime["environment"]["CV_CAM4_RGB_TOPIC"] == (
    "/synced/cam_4/color/image_raw/compressed"
)
assert runtime["environment"]["CV_CAM4_CAMERA_INFO_TOPIC"] == (
    "/synced/cam_4/color/camera_info"
)
assert runtime["environment"]["CV_CAM4_ALIGNED_DEPTH_TOPIC"] == (
    "/synced/cam_4/depth/image_rect_raw"
)
for camera in ("CAM1", "CAM2", "CAM3", "CAM4"):
    assert webapp["environment"][f"VITE_EXTERNAL_{camera}_TOPIC"].startswith(
        "/synced/"
    )
assert webapp["environment"]["VITE_EXTERNAL_FLIR_TOPIC"] == (
    "/synced/flir/color/image_raw/compressed"
)
targets = {volume["target"] for volume in asr["volumes"]}
assert "/workspaces/taskplanner_ws" in targets
assert "/taskplanner-runs" in targets
assert "/run/taskplanner-pipewire/pipewire-0" in targets
assert "taskplanner-asr" not in runtime.get("depends_on", {})
assert public_bridge["profiles"] == ["live", "llm-surgeon"]
assert public_bridge["network_mode"] == runtime["network_mode"] == "host"
assert public_bridge["ipc"] == runtime["ipc"] == "host"
assert public_bridge["mem_limit"] == "536870912"
assert public_bridge["memswap_limit"] == "536870912"
assert public_bridge["pids_limit"] == 128
assert public_bridge["read_only"] is True
assert public_bridge["restart"] == "unless-stopped"
assert public_bridge["environment"]["ROS_DOMAIN_ID"] == runtime["environment"]["ROS_DOMAIN_ID"]
assert public_bridge["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"] == runtime["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"]
assert public_bridge["environment"]["RMW_IMPLEMENTATION"] == runtime["environment"]["RMW_IMPLEMENTATION"]
rendered_public_command = " ".join(public_bridge["command"])
assert "--port $${PUBLIC_ROSBRIDGE_PORT}" in rendered_public_command
assert "--max_message_size" not in rendered_public_command
assert public_bridge["environment"]["PUBLIC_ROSBRIDGE_PORT"] == "9092"
assert public_bridge["environment"]["ENABLE_PUBLIC_ROSBRIDGE"] == "true"
assert runtime["environment"]["PERCEPTION_BACKEND"] == "local"
for key in (
    "CV_CONTRACT_STATUS_TOPIC",
    "CV_CAM4_RGB_TOPIC",
    "CV_CAM4_RGB_ALIAS_TOPIC",
    "CV_CAM4_CAMERA_INFO_TOPIC",
    "CV_CAM4_ALIGNED_DEPTH_TOPIC",
    "CV_HANDOVER_TRAY_RGB_TOPIC",
    "CV_HANDOVER_TRAY_CAMERA_INFO_TOPIC",
    "CV_HANDOVER_TRAY_ALIGNED_DEPTH_TOPIC",
):
    assert key in runtime["environment"], key
assert "PUBLIC_ROSBRIDGE_PORT" in " ".join(public_bridge["healthcheck"]["test"])
proxy = config["services"]["integration-debug-lan-proxy"]
command = proxy["command"]
assert "9092=127.0.0.1:9092" in command
tailscale_proxy = config["services"]["integration-debug-tailscale-proxy"]
assert tailscale_proxy["profiles"] == ["live", "llm-surgeon", "replay", "debug"]
tailscale_command = tailscale_proxy["command"]
assert "9091/live=127.0.0.1:9090" in tailscale_command
assert "9091/llm=127.0.0.1:9090" in tailscale_command
assert "9091/shadow=127.0.0.1:9099" in tailscale_command
assert "9091/multicam=127.0.0.1:9094" in tailscale_command
assert multicam_observer["profiles"] == ["live", "llm-surgeon", "replay", "debug"]
assert multicam_observer["network_mode"] == "host"
assert multicam_observer["read_only"] is True
assert multicam_observer["cap_drop"] == ["ALL"]
assert multicam_observer["security_opt"] == ["no-new-privileges:true"]
assert multicam_observer["restart"] == "unless-stopped"
assert multicam_observer["environment"]["ROS_DOMAIN_ID"] == "0"
assert multicam_observer["environment"]["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "SUBNET"
assert multicam_observer["environment"]["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
assert multicam_observer["environment"]["CYCLONEDDS_URI"].endswith(
    "/config/cyclonedds_lan.xml"
)
assert multicam_observer["environment"]["ROSBRIDGE_MULTICAM_PORT"] == "9094"
assert "/multicam_node/capture_status" not in " ".join(
    multicam_observer["healthcheck"]["test"]
)
assert "multicam_observer.launch.py" in " ".join(multicam_observer["command"])
assert webapp["network_mode"] == "host"
assert "ports" not in webapp
assert webapp["environment"]["TASKPLANNER_RUNTIME_CONTROL_URL"] == "http://127.0.0.1:8150"
assert webapp["environment"]["TASKPLANNER_RUNTIME_CONTROL_TOKEN_FILE"] == "/run/taskplanner-secrets/runtime-control-token"
assert webapp["environment"]["VITE_ROSBRIDGE_LIVE_TAILSCALE_PORT"] == "9091"
assert webapp["environment"]["VITE_ROSBRIDGE_LIVE_TAILSCALE_PATH"] == "/live"
assert webapp["environment"]["VITE_ROSBRIDGE_TAILSCALE_PORT"] == "9091"
assert webapp["environment"]["VITE_ROSBRIDGE_LLM_TAILSCALE_PATH"] == "/llm"
assert webapp["environment"]["VITE_ROSBRIDGE_SHADOW_TAILSCALE_PATH"] == "/shadow"
'

stale_fastdds_config="$(
  FASTRTPS_DEFAULT_PROFILES_FILE=/tmp/must-not-leak.xml \
    "${ROOT_DIR}/scripts/taskplanner" config live
)"
printf '%s\n' "${stale_fastdds_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
for name in (
    "taskplanner-runtime",
    "taskplanner-asr",
    "public-rosbridge",
    "integration-debug",
):
    environment = config["services"][name]["environment"]
    assert environment["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
    assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in environment, name
'

llm_config="$("${ROOT_DIR}/scripts/taskplanner" config llm-surgeon)"
assert_not_contains "${llm_config}" "taskplanner-asr:"
assert_not_contains "${llm_config}" "operational_asr_node"
assert_not_contains "${llm_config}" "colcon build"
printf '%s\n' "${llm_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
debug = config["services"]["integration-debug"]
assert debug["environment"]["TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE"] == (
    "secure_operational_debug_rosbridge"
)
'

debug_output="$("${ROOT_DIR}/scripts/taskplanner" up debug --dry-run)"
assert_contains "${debug_output}" \
  "--profile debug up -d webapp"
assert_contains "${debug_output}" \
  "--profile debug up -d multicam-observer"
assert_contains "${debug_output}" \
  "--profile debug up -d --force-recreate integration-debug integration-debug-lan-proxy"
assert_contains "${debug_output}" \
  "--profile debug up -d integration-debug-tailscale-proxy"
assert_contains "${debug_output}" \
  "+ wait-for-websocket 127.0.0.1 9091 / debug\\ ROS\\ bridge\\ router"
assert_contains "${debug_output}" \
  "+ wait_for_multicam_observer debug"
assert_contains "${debug_output}" \
  "+ wait-for-ros-semantic-ready debug integration-debug /integration/debug/status std_msgs/msg/String /integration/debug/check_readiness std_srvs/srv/Trigger"
debug_build_output="$("${ROOT_DIR}/scripts/taskplanner" up debug --dry-run --build)"
assert_single_serial_build "${debug_build_output}" "debug"
assert_contains "${debug_build_output}" \
  "stop taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy multicam-observer"
assert_contains "${debug_build_output}" \
  "rm -f taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy multicam-observer"
assert_contains "${debug_build_output}" \
  "colcon\\ build\\ --symlink-install\\ --packages-select\\ surgical_interop_msgs\\ surgical_msgs\\ rosbridge_test_msgs\\ integration_debug"

debug_config="$("${ROOT_DIR}/scripts/taskplanner" config debug)"
replay_config="$("${ROOT_DIR}/scripts/taskplanner" config replay)"
assert_not_contains "${debug_config}" "colcon build"
assert_not_contains "${replay_config}" "colcon build"
for rendered_config in "${live_config}" "${llm_config}" "${debug_config}" "${replay_config}"; do
  assert_contains "${rendered_config}" "VITE_DEFAULT_RUNTIME_MODE: llm"
done
printf '%s\n' "${debug_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
debug = config["services"]["integration-debug"]
assert debug["environment"]["TASKPLANNER_DEBUG_ROSBRIDGE_EXECUTABLE"] == (
    "secure_debug_rosbridge"
)
'
printf '%s\n' "${replay_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
shadow = config["services"]["shadow-runner"]
environment = shadow["environment"]
assert shadow["stop_signal"] == "SIGINT"
assert shadow["stop_grace_period"] == "15s"
assert environment["ROS_DOMAIN_ID"] == "71"
assert environment["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "LOCALHOST"
assert environment["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
assert "CYCLONEDDS_URI" not in environment
assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in environment
'
grep -q 'acquire_launcher_lock' "${ROOT_DIR}/scripts/taskplanner" ||
  fail "launcher lifecycle must be serialized across concurrent invocations"
[[ "$(grep -c '^    acquire_launcher_lock$' "${ROOT_DIR}/scripts/taskplanner")" -ge "2" ]] ||
  fail "both launcher up and down must acquire the lifecycle lock"
[[ "$(grep -c '^[[:space:]]\+verify_runtime_control_transition_interlock$' "${ROOT_DIR}/scripts/taskplanner")" == "2" ]] ||
  fail "both runtime replacement branches must repeat the stopped-state gate"
gate_output="$(
  TASKPLANNER_RUNTIME_REQUIRE_STOPPED=1 \
  TASKPLANNER_RUNTIME_EXPECTED_ACTIVE_MODE=replay \
    "${ROOT_DIR}/scripts/taskplanner" up replay --dry-run
)"
gate_line="$(grep -n -F '+ verify-runtime-transition-interlock replay' <<<"${gate_output}" | cut -d: -f1)"
stop_line="$(grep -n -F ' stop taskplanner-runtime' <<<"${gate_output}" | head -n 1 | cut -d: -f1)"
[[ -n "${gate_line}" && -n "${stop_line}" && "${gate_line}" -lt "${stop_line}" ]] ||
  fail "controller child must recheck stopped state before marker clear and stop"
python3 - "${ROOT_DIR}/scripts/taskplanner" <<'PY'
import sys
from pathlib import Path

lines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
gates = [
    index
    for index, line in enumerate(lines)
    if line.strip() == "verify_runtime_control_transition_interlock"
]
assert len(gates) == 2
for index in gates:
    assert any(
        line.strip() == "clear_active_runtime_mode"
        for line in lines[index + 1 : index + 8]
    )
PY
grep -q 'exec \${LAUNCHER_LOCK_FD}>&-' "${ROOT_DIR}/scripts/taskplanner" ||
  fail "LM Studio child must not inherit the Taskplanner launcher lock"

grep -qx 'WEBAPP_PORT=4173' \
  "${ROOT_DIR}/docker/orchestration/debug.env" || \
  fail "standalone Debug UI must use port 4173"
grep -qx 'TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=false' \
  "${ROOT_DIR}/docker/orchestration/debug.env" || \
  fail "standalone Debug must default to fail-closed coexistence"
grep -qx 'TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=false' \
  "${ROOT_DIR}/docker/orchestration/live.env" || \
  fail "live integrated Debug must default to fail-closed coexistence"
grep -qx 'TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=true' \
  "${ROOT_DIR}/docker/orchestration/live.env" || \
  fail "live integrated Debug must use the runtime DDS network"
grep -qx 'ENABLE_PUBLIC_ROSBRIDGE=true' \
  "${ROOT_DIR}/docker/orchestration/live.env" || \
  fail "live public read-only bridge must default enabled"
grep -q 'export TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=true' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "live launcher must override an unsafe shell network-lock value"
grep -q 'export TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=false' \
  "${ROOT_DIR}/scripts/taskplanner" || \
  fail "live launcher must override an unsafe shell coexistence value"

temporary_dir="$(mktemp -d)"
trap 'rm -rf "${temporary_dir}"' EXIT
printf '%s\n' \
  '{' \
  '  "schema": "taskplanner.integration_debug.network_settings.v1",' \
  '  "domain_id": 97,' \
  '  "discovery_range": "LOCALHOST",' \
  '  "updated_at": "2026-08-12T00:00:00+00:00"' \
  '}' >"${temporary_dir}/network-settings.json"

network_output="$(
  PYTHONPATH="${ROOT_DIR}/src/integration_debug" \
  TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=true \
  TASKPLANNER_DEBUG_NETWORK_INTERFACE=lo \
  ROS_DOMAIN_ID=0 \
  ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET \
  RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
  CYCLONEDDS_URI=file:///tmp/cyclonedds-test.xml \
  FASTRTPS_DEFAULT_PROFILES_FILE=/tmp/must-not-leak.xml \
    python3 "${ROOT_DIR}/scripts/run_integration_debug.py" \
      --settings "${temporary_dir}/network-settings.json" \
      -- python3 -c \
        'import os; print("ACTIVE_NETWORK=" + os.environ["ROS_DOMAIN_ID"] + "/" + os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"]); print("ACTIVE_RMW=" + os.environ["RMW_IMPLEMENTATION"]); print("FASTRTPS_PRESENT=" + str("FASTRTPS_DEFAULT_PROFILES_FILE" in os.environ)); print("CYCLONEDDS_URI=" + os.environ.get("CYCLONEDDS_URI", ""))'
)"
assert_contains "${network_output}" \
  "Locked integrated Debug Mode to runtime network: domain=0 discovery=SUBNET"
assert_contains "${network_output}" "ACTIVE_NETWORK=0/SUBNET"
assert_contains "${network_output}" "ACTIVE_RMW=rmw_cyclonedds_cpp"
assert_contains "${network_output}" "FASTRTPS_PRESENT=False"
assert_contains "${network_output}" "CYCLONEDDS_URI=file:///tmp/cyclonedds-test.xml"
assert_not_contains "${network_output}" "ACTIVE_NETWORK=97/LOCALHOST"

standalone_network_output="$(
  PYTHONPATH="${ROOT_DIR}/src/integration_debug" \
  TASKPLANNER_DEBUG_LOCK_TO_RUNTIME_NETWORK=false \
  TASKPLANNER_DEBUG_NETWORK_INTERFACE=lo \
  ROS_DOMAIN_ID=0 \
  ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET \
  RMW_IMPLEMENTATION=rmw_cyclonedds_cpp \
  CYCLONEDDS_URI=file:///tmp/must-not-leak.xml \
    python3 "${ROOT_DIR}/scripts/run_integration_debug.py" \
      --settings "${temporary_dir}/network-settings.json" \
      -- python3 -c \
        'import os; print("ACTIVE_NETWORK=" + os.environ["ROS_DOMAIN_ID"] + "/" + os.environ["ROS_AUTOMATIC_DISCOVERY_RANGE"]); print("CYCLONEDDS_PRESENT=" + str("CYCLONEDDS_URI" in os.environ))'
)"
assert_contains "${standalone_network_output}" \
  "Loaded Debug Mode network settings: domain=97 discovery=LOCALHOST"
assert_contains "${standalone_network_output}" "ACTIVE_NETWORK=97/LOCALHOST"
assert_contains "${standalone_network_output}" "CYCLONEDDS_PRESENT=False"

printf 'PASS: launcher serializes colcon before runtime/sidecars and preserves mode gates\n'
