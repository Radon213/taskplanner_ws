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
  "stop taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy"
assert_contains "${live_output}" \
  "rm -f taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 taskplanner-asr"
asr_start_line="$(grep -n -- 'taskplanner-asr$' <<<"${live_output}" | tail -n 1 | cut -d: -f1)"
runtime_start_line="$(grep -n -- 'up -d --force-recreate taskplanner-runtime$' <<<"${live_output}" | cut -d: -f1)"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 public-rosbridge"
assert_contains "${live_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 object-perception"
disabled_bridge_output="$(ENABLE_PUBLIC_ROSBRIDGE=false "${ROOT_DIR}/scripts/taskplanner" up live --dry-run)"
assert_not_contains "${disabled_bridge_output}" \
  "--profile live up -d --force-recreate public-rosbridge"
[[ -n "${asr_start_line}" && -n "${runtime_start_line}" && \
  "${asr_start_line}" -lt "${runtime_start_line}" ]] || \
  fail "live must health-wait for taskplanner-asr before taskplanner-runtime"

external_cv_output="$(PERCEPTION_BACKEND=external "${ROOT_DIR}/scripts/taskplanner" up live --dry-run)"
assert_not_contains "${external_cv_output}" \
  "--profile live up -d --force-recreate --wait --wait-timeout 300 object-perception"

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
assert_not_contains "${llm_output}" \
  "--profile llm-surgeon up -d --force-recreate --wait --wait-timeout 300 taskplanner-asr"
llm_build_output="$("${ROOT_DIR}/scripts/taskplanner" up llm-surgeon --dry-run --build)"
assert_single_serial_build "${llm_build_output}" "llm-surgeon"

replay_build_output="$("${ROOT_DIR}/scripts/taskplanner" up replay --dry-run --build)"
assert_single_serial_build "${replay_build_output}" "replay"

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
webapp = config["services"]["webapp"]
assert asr["profiles"] == ["live"]
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

debug_output="$("${ROOT_DIR}/scripts/taskplanner" up debug --dry-run)"
assert_contains "${debug_output}" \
  "--profile debug up -d --force-recreate webapp integration-debug integration-debug-lan-proxy"
debug_build_output="$("${ROOT_DIR}/scripts/taskplanner" up debug --dry-run --build)"
assert_single_serial_build "${debug_build_output}" "debug"
assert_contains "${debug_build_output}" \
  "stop taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy"
assert_contains "${debug_build_output}" \
  "rm -f taskplanner-runtime public-rosbridge taskplanner-asr shadow-runner object-perception integration-debug integration-debug-lan-proxy"
assert_contains "${debug_build_output}" \
  "colcon\\ build\\ --symlink-install\\ --packages-select\\ surgical_interop_msgs\\ surgical_msgs\\ rosbridge_test_msgs\\ integration_debug"

debug_config="$("${ROOT_DIR}/scripts/taskplanner" config debug)"
replay_config="$("${ROOT_DIR}/scripts/taskplanner" config replay)"
assert_not_contains "${debug_config}" "colcon build"
assert_not_contains "${replay_config}" "colcon build"
printf '%s\n' "${replay_config}" | python3 -c '
import sys
import yaml

config = yaml.safe_load(sys.stdin)
environment = config["services"]["shadow-runner"]["environment"]
assert environment["ROS_DOMAIN_ID"] == "71"
assert environment["ROS_AUTOMATIC_DISCOVERY_RANGE"] == "LOCALHOST"
assert environment["RMW_IMPLEMENTATION"] == "rmw_cyclonedds_cpp"
assert "CYCLONEDDS_URI" not in environment
assert "FASTRTPS_DEFAULT_PROFILES_FILE" not in environment
'
grep -q 'acquire_launcher_lock' "${ROOT_DIR}/scripts/taskplanner" ||
  fail "launcher lifecycle must be serialized across concurrent invocations"
[[ "$(grep -c '^    acquire_launcher_lock$' "${ROOT_DIR}/scripts/taskplanner")" == "2" ]] ||
  fail "both launcher up and down must acquire the lifecycle lock"
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
