#!/usr/bin/env bash

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${TASKPLANNER_WS_ROOT:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
LOG_DIR="${TASKPLANNER_LOG_DIR:-${REPO_ROOT}/reports/event_logs}"

get_runtime_container() {
  docker ps --format '{{.Names}}' | grep -E 'taskplanner.*runtime|taskplanner-runtime' | head -1
}

ros_exec() {
  local RUNTIME
  RUNTIME="$(get_runtime_container)"

  if [ -z "$RUNTIME" ]; then
    echo "[ERROR] taskplanner runtime 컨테이너를 찾지 못했습니다."
    echo "먼저 터미널 1에서 demo docker를 실행하세요."
    return 1
  fi

  docker exec "$RUNTIME" bash -lc "
    source /opt/ros/humble/setup.bash 2>/dev/null || source /opt/ros/jazzy/setup.bash
    source /workspaces/taskplanner_ws/install/setup.bash
    $*
  "
}

record_test() {
  local TEST_NAME="$1"
  local DURATION="${2:-60}"

  mkdir -p "$LOG_DIR"

  local RUNTIME
  RUNTIME="$(get_runtime_container)"

  if [ -z "$RUNTIME" ]; then
    echo "[ERROR] taskplanner runtime 컨테이너를 찾지 못했습니다."
    echo "먼저 터미널 1에서 demo docker를 실행하세요."
    return 1
  fi

  local TS
  TS="$(date +%Y%m%d_%H%M%S)"
  local LOG_FILE="$LOG_DIR/${TEST_NAME}_${TS}_events.log"

  echo "============================================================"
  echo "[RECORD TEST]"
  echo "test name : $TEST_NAME"
  echo "duration  : ${DURATION}s"
  echo "log file  : $LOG_FILE"
  echo "============================================================"

  timeout "${DURATION}s" docker exec -i "$RUNTIME" bash -lc "
    source /opt/ros/humble/setup.bash 2>/dev/null || source /opt/ros/jazzy/setup.bash
    source /workspaces/taskplanner_ws/install/setup.bash
    ros2 topic echo /simulation/event
  " | awk '
    BEGIN { block = ""; skip = 0 }

    /^---$/ {
      if (block != "" && skip == 0) {
        print block
        print "---"
        fflush()
      }
      block = ""
      skip = 0
      next
    }

    {
      block = block $0 "\n"
      if ($0 ~ /^event_type: VLMProposalAccepted/) skip = 1
      if ($0 ~ /^event_type: VoiceTranscriptObserved/) skip = 1
      if ($0 ~ /^event_type: ToolCleaningProgress/) skip = 1
    }

    END {
      if (block != "" && skip == 0) {
        print block
      }
    }
  ' | tee "$LOG_FILE"

  echo ""
  echo "============================================================"
  echo "[RECORD DONE]"
  echo "saved: $LOG_FILE"
  echo "============================================================"
}

record_test_manual() {
  local TEST_NAME="$1"

  mkdir -p "$LOG_DIR"

  local RUNTIME
  RUNTIME="$(get_runtime_container)"

  if [ -z "$RUNTIME" ]; then
    echo "[ERROR] taskplanner runtime 컨테이너를 찾지 못했습니다."
    echo "먼저 터미널 1에서 demo docker를 실행하세요."
    return 1
  fi

  local TS
  TS="$(date +%Y%m%d_%H%M%S)"
  local LOG_FILE="$LOG_DIR/${TEST_NAME}_${TS}_events.log"

  echo "============================================================"
  echo "[MANUAL RECORD TEST]"
  echo "test name : $TEST_NAME"
  echo "log file  : $LOG_FILE"
  echo "종료하려면 Ctrl+C"
  echo "============================================================"

  docker exec -i "$RUNTIME" bash -lc "
    source /opt/ros/humble/setup.bash 2>/dev/null || source /opt/ros/jazzy/setup.bash
    source /workspaces/taskplanner_ws/install/setup.bash
    ros2 topic echo /simulation/event
  " | tee "$LOG_FILE"
}

control_sim() {
  local CMD="$1"
  local PHASE="${2:-}"

  ros_exec "ros2 service call /simulation/control surgical_msgs/srv/ControlSimulation \"{command: '$CMD', start_phase_id: '$PHASE'}\""
}

select_bundle() {
  local BUNDLE="$1"

  ros_exec "ros2 service call /simulation/select_bundle surgical_msgs/srv/SelectSimulationBundle \"{bundle_name: '$BUNDLE', restart_if_running: true}\""
}

prepare_test() {
  local BUNDLE="${1:-thyroidectomy}"
  local PHASE="${2:-P04}"

  echo "============================================================"
  echo "[PREPARE TEST]"
  echo "bundle: $BUNDLE"
  echo "phase : $PHASE"
  echo "브라우저 Start 버튼은 누르지 않아도 됩니다."
  echo "============================================================"

  select_bundle "$BUNDLE"
  sleep 1
  control_sim "start" "$PHASE"
  sleep 2
}

inject_request() {
  local TOOL="$1"
  local TEXT="$2"

  ros_exec "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{event_type: 'voice_request', requested_tool: '$TOOL', voice_text: '$TEXT', ready_for_handover: true, ready_for_retrieval: false, clear_pending_requests: true}\""
}

inject_return() {
  local TOOL="$1"

  ros_exec "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{event_type: 'return_tool', requested_tool: '$TOOL', voice_text: '', ready_for_handover: false, ready_for_retrieval: true, clear_pending_requests: true}\""
}

inject_tool_observation() {
  local TOOL="$1"
  local LOCATION_TYPE="$2"
  local LOCATION_ID="${3:-$LOCATION_TYPE}"
  local CONFIDENCE="${4:-0.99}"

  ros_exec "ros2 topic pub --once /vlm/tool_observations surgical_msgs/msg/ToolObservation \"{instrument_id: '$TOOL', location_id: '$LOCATION_ID', location_type: '$LOCATION_TYPE', confidence: $CONFIDENCE, visible: true}\""
}

inject_actor_event() {
  local EVENT_TYPE="$1"
  local TOOL="$2"
  local NOTE="${3:-manual test event}"

  ros_exec "ros2 topic pub --once /surgeon/actor_event surgical_msgs/msg/SurgeonActorEvent \"{event_type: '$EVENT_TYPE', tool_id: '$TOOL', phase_id: '', voice_text: '', note: '$NOTE', ready_for_handover: false, ready_for_retrieval: false, override: true}\""
}

thyroid_prepare() {
  prepare_test thyroidectomy P04
}

sudden_calling_test() {
  echo "============================================================"
  echo "[TEST] sudden_calling_test"
  echo "수술: thyroidectomy"
  echo "목적: 도구 전달 중 갑자기 다른 도구를 부르는 상황"
  echo "============================================================"

  thyroid_prepare

  echo "[1] T10 suction 요청"
  inject_request T10 "Unexpected bleeding. Suction please."

  sleep 0.4

  echo "[2] T10 handover 중 갑자기 T04 Bovie 요청"
  inject_request T04 "Bovie please. I need it now."

  echo "[3] 시스템 대처 관찰"
  sleep 12

  echo "[4] simulation stop"
  control_sim stop
}

sudden_stop_test() {
  echo "============================================================"
  echo "[TEST] sudden_stop_test"
  echo "수술: thyroidectomy"
  echo "목적: 도구 전달 중 갑작스러운 stop 상황"
  echo "============================================================"

  thyroid_prepare

  echo "[1] T04 Bovie 요청"
  inject_request T04 "Bovie please."

  sleep 0.5

  echo "[2] sudden stop"
  control_sim stop

  echo "[3] stop 이후 추가 동작 관찰"
  sleep 5
}

sudden_bleeding() {
  echo "============================================================"
  echo "[TEST] sudden_bleeding"
  echo "수술: thyroidectomy"
  echo "목적: 갑작스러운 출혈 상황"
  echo "============================================================"

  thyroid_prepare

  echo "[1] T10 suction 요청"
  inject_request T10 "Unexpected bleeding. Suction please."

  sleep 5

  echo "[2] T07 bipolar cautery 요청"
  inject_request T07 "Bipolar cautery please."

  sleep 5

  echo "[3] T04 Bovie 요청"
  inject_request T04 "Bovie please."

  echo "[4] 시스템 대처 관찰"
  sleep 12

  echo "[5] simulation stop"
  control_sim stop
}

visualerror() {
  echo "============================================================"
  echo "[TEST] visualerror"
  echo "수술: thyroidectomy"
  echo "목적: 잘못된 도구 ID 요청 후 correction 상황"
  echo "============================================================"

  thyroid_prepare

  echo "[1] Bovie라고 말했지만 requested_tool은 T05로 주입"
  inject_request T05 "Bovie please."

  sleep 5

  echo "[2] correction: 올바른 T04 Bovie 요청"
  inject_request T04 "Correction. Bovie is needed. Please hand over T04."

  echo "[3] 시스템 대처 관찰"
  sleep 12

  echo "[4] simulation stop"
  control_sim stop
}

fallingtool() {
  echo "============================================================"
  echo "[TEST] fallingtool"
  echo "수술: thyroidectomy"
  echo "목적: 사용 중 도구가 바닥에 떨어졌을 때 사람 개입 전까지 hold"
  echo "============================================================"

  thyroid_prepare

  echo "[1] T08 mosquito forceps 요청"
  inject_request T08 "Mosquito forceps please."

  sleep 6

  echo "[2] T08 floor_zone 관찰 주입"
  inject_tool_observation T08 floor_zone floor_zone 0.99

  sleep 5

  echo "[3] 사람이 떨어진 도구를 제거하고 sterile replacement를 준비했다는 이벤트"
  inject_actor_event human_recovered_dropped_tool T08 "human removed dropped tool and replaced sterile equivalent"

  echo "[4] 시스템 대처 관찰"
  sleep 12

  echo "[5] simulation stop"
  control_sim stop
}

mayo_recovery_after_handover() {
  echo "============================================================"
  echo "[TEST] mayo_recovery_after_handover"
  echo "수술: thyroidectomy"
  echo "목적: 사용된 도구가 Mayo recovery zone에 놓인 뒤 retrieve_from_mayo 실행"
  echo "============================================================"

  thyroid_prepare

  echo "[1] T08 mosquito forceps 요청"
  inject_request T08 "Mosquito forceps please."

  sleep 6

  echo "[2] 집도의가 T08을 Mayo recovery zone에 내려놓음"
  inject_actor_event place_on_mayo_recovery T08 "surgeon placed used tool on mayo recovery zone"

  echo "[3] 시스템 대처 관찰"
  sleep 15

  echo "[4] simulation stop"
  control_sim stop
}

fastchanging() {
  echo "============================================================"
  echo "[TEST] fastchanging"
  echo "수술: thyroidectomy"
  echo "목적: 집도의 요청이 빠르게 바뀌는 상황"
  echo "============================================================"

  thyroid_prepare

  echo "[1] T10 suction 요청"
  inject_request T10 "Suction please."

  sleep 0.6

  echo "[2] T07 bipolar로 빠른 변경"
  inject_request T07 "Actually, bipolar cautery please."

  sleep 0.6

  echo "[3] T04 Bovie로 빠른 변경"
  inject_request T04 "No, Bovie please."

  sleep 0.6

  echo "[4] T05 retractor로 빠른 변경"
  inject_request T05 "Retractor please."

  echo "[5] 시스템 대처 관찰"
  sleep 15

  echo "[6] simulation stop"
  control_sim stop
}

show_recent_logs() {
  ls -lt "$LOG_DIR" | head
}

check_log_core() {
  local FILE="$1"

  grep -n -C 8 -E 'RobotTaskStarted|RobotGraspedTool|ToolHandoverCompleted|RobotTaskCompleted|SurgeonRequestObserved|SurgeonActorEventObserved|ToolReceivedFromSurgeon|ToolReturnedToTray|return_tool|instrument_id' "$FILE"
}

# ============================================================
# OVERRIDE: robust runtime container finder
# ============================================================
get_runtime_container() {
  unset DOCKER_HOST

  if docker ps --format '{{.Names}}' | grep -q '^taskplanner_ws-taskplanner-runtime-1$'; then
    echo "taskplanner_ws-taskplanner-runtime-1"
    return 0
  fi

  docker ps --format '{{.Names}}' | grep -E 'taskplanner.*runtime|runtime' | head -1
}


# ============================================================
# OVERRIDE: robust logging utilities
# raw log + core filtered log 생성
# ============================================================

make_core_log() {
  local RAW_FILE="$1"
  local CORE_FILE="$2"

  awk '
    BEGIN {
      block = ""
      skip = 0
      keep = 0
    }

    /^---$/ {
      if (block != "" && skip == 0 && keep == 1) {
        print block
        print "---"
      }
      block = ""
      skip = 0
      keep = 0
      next
    }

    {
      block = block $0 "\n"

      if ($0 ~ /^event_type: VLMProposalAccepted/) skip = 1
      if ($0 ~ /^event_type: VoiceTranscriptObserved/) skip = 1
      if ($0 ~ /^event_type: ToolCleaningProgress/) skip = 1

      if ($0 ~ /^event_type: RobotTaskStarted/) keep = 1
      if ($0 ~ /^event_type: RobotGraspedTool/) keep = 1
      if ($0 ~ /^event_type: ToolHandoverCompleted/) keep = 1
      if ($0 ~ /^event_type: RobotTaskCompleted/) keep = 1
      if ($0 ~ /^event_type: SurgeonRequestObserved/) keep = 1
      if ($0 ~ /^event_type: SurgeonActorEventObserved/) keep = 1
      if ($0 ~ /^event_type: ToolReceivedFromSurgeon/) keep = 1
      if ($0 ~ /^event_type: ToolReturnedToTray/) keep = 1
      if ($0 ~ /return_tool/) keep = 1
    }

    END {
      if (block != "" && skip == 0 && keep == 1) {
        print block
      }
    }
  ' "$RAW_FILE" > "$CORE_FILE"
}

record_test() {
  local TEST_NAME="$1"
  local DURATION="${2:-60}"

  mkdir -p "$LOG_DIR"

  local RUNTIME
  RUNTIME="$(get_runtime_container)"

  if [ -z "$RUNTIME" ]; then
    echo "[ERROR] taskplanner runtime 컨테이너를 찾지 못했습니다."
    echo "현재 docker ps 확인:"
    docker ps --format "table {{.Names}}\t{{.Status}}"
    return 1
  fi

  local TS
  TS="$(date +%Y%m%d_%H%M%S)"

  local RAW_FILE="$LOG_DIR/${TEST_NAME}_${TS}_raw_events.log"
  local CORE_FILE="$LOG_DIR/${TEST_NAME}_${TS}_core_events.log"

  echo "============================================================"
  echo "[RECORD TEST]"
  echo "test name : $TEST_NAME"
  echo "duration  : ${DURATION}s"
  echo "raw log   : $RAW_FILE"
  echo "core log  : $CORE_FILE"
  echo "============================================================"

  timeout "${DURATION}s" docker exec -i "$RUNTIME" bash -lc "
    source /opt/ros/humble/setup.bash 2>/dev/null || source /opt/ros/jazzy/setup.bash
    source /workspaces/taskplanner_ws/install/setup.bash
    stdbuf -oL -eL ros2 topic echo /simulation/event
  " | tee "$RAW_FILE"

  make_core_log "$RAW_FILE" "$CORE_FILE"

  echo ""
  echo "============================================================"
  echo "[RECORD DONE]"
  echo "raw saved : $RAW_FILE"
  echo "core saved: $CORE_FILE"
  echo "============================================================"
}

run_logged_test() {
  local TEST_FUNC="$1"
  local EXTRA_WAIT="${2:-3}"

  mkdir -p "$LOG_DIR"

  local RUNTIME
  RUNTIME="$(get_runtime_container)"

  if [ -z "$RUNTIME" ]; then
    echo "[ERROR] taskplanner runtime 컨테이너를 찾지 못했습니다."
    docker ps --format "table {{.Names}}\t{{.Status}}"
    return 1
  fi

  if ! declare -F "$TEST_FUNC" >/dev/null; then
    echo "[ERROR] 함수가 없습니다: $TEST_FUNC"
    echo "먼저 source 했는지 확인하세요:"
    echo "source \"${REPO_ROOT}/scripts/demo_test_tools.sh\""
    return 1
  fi

  local TS
  TS="$(date +%Y%m%d_%H%M%S)"

  local RAW_FILE="$LOG_DIR/${TEST_FUNC}_${TS}_raw_events.log"
  local CORE_FILE="$LOG_DIR/${TEST_FUNC}_${TS}_core_events.log"

  echo "============================================================"
  echo "[RUN LOGGED TEST]"
  echo "test func : $TEST_FUNC"
  echo "raw log   : $RAW_FILE"
  echo "core log  : $CORE_FILE"
  echo "url       : http://127.0.0.1:4173"
  echo "============================================================"

  setsid bash -c "docker exec -i '$RUNTIME' bash -lc '
    source /opt/ros/humble/setup.bash 2>/dev/null || source /opt/ros/jazzy/setup.bash
    source /workspaces/taskplanner_ws/install/setup.bash
    stdbuf -oL -eL ros2 topic echo /simulation/event
  ' | tee '$RAW_FILE'" &
  LOGGER_PGID=$!

  echo "[1] 로그 기록 시작"
  sleep 2

  echo "[2] 실험 실행: $TEST_FUNC"
  "$TEST_FUNC"

  echo "[3] 실험 종료 후 ${EXTRA_WAIT}s 추가 기록"
  sleep "$EXTRA_WAIT"

  echo "[4] 로그 기록 종료"
  kill -TERM "-$LOGGER_PGID" >/dev/null 2>&1 || true
  sleep 1

  make_core_log "$RAW_FILE" "$CORE_FILE"

  echo "============================================================"
  echo "[DONE]"
  echo "raw log:"
  echo "$RAW_FILE"
  echo ""
  echo "core log:"
  echo "$CORE_FILE"
  echo "============================================================"
}

show_latest_core_log() {
  local LATEST
  LATEST="$(ls -t "$LOG_DIR"/*_core_events.log 2>/dev/null | head -1)"

  if [ -z "$LATEST" ]; then
    echo "[ERROR] core log가 없습니다."
    return 1
  fi

  echo "[LATEST CORE LOG]"
  echo "$LATEST"
  echo "------------------------------------------------------------"
  cat "$LATEST"
}


# ============================================================
# OVERRIDE: quiet run_logged_test
# 터미널에는 raw event를 출력하지 않고 파일로만 저장
# ============================================================

run_logged_test() {
  local TEST_FUNC="$1"
  local EXTRA_WAIT="${2:-3}"

  mkdir -p "$LOG_DIR"

  local RUNTIME
  RUNTIME="$(get_runtime_container)"

  if [ -z "$RUNTIME" ]; then
    echo "[ERROR] taskplanner runtime 컨테이너를 찾지 못했습니다."
    docker ps --format "table {{.Names}}\t{{.Status}}"
    return 1
  fi

  if ! declare -F "$TEST_FUNC" >/dev/null; then
    echo "[ERROR] 함수가 없습니다: $TEST_FUNC"
    echo "먼저 source 했는지 확인하세요:"
    echo "source \"${REPO_ROOT}/scripts/demo_test_tools.sh\""
    return 1
  fi

  local TS
  TS="$(date +%Y%m%d_%H%M%S)"

  local RAW_FILE="$LOG_DIR/${TEST_FUNC}_${TS}_raw_events.log"
  local CORE_FILE="$LOG_DIR/${TEST_FUNC}_${TS}_core_events.log"

  echo "============================================================"
  echo "[RUN LOGGED TEST]"
  echo "test func : $TEST_FUNC"
  echo "raw log   : $RAW_FILE"
  echo "core log  : $CORE_FILE"
  echo "url       : http://127.0.0.1:4173"
  echo "============================================================"

  setsid bash -c "docker exec -i '$RUNTIME' bash -lc '
    source /opt/ros/humble/setup.bash 2>/dev/null || source /opt/ros/jazzy/setup.bash
    source /workspaces/taskplanner_ws/install/setup.bash
    stdbuf -oL -eL ros2 topic echo /simulation/event
  ' > '$RAW_FILE' 2>&1" &
  LOGGER_PGID=$!

  echo "[1] 로그 기록 시작, 터미널 출력 없이 파일 저장 중"
  sleep 2

  echo "[2] 실험 실행: $TEST_FUNC"
  "$TEST_FUNC"

  echo "[3] 실험 종료 후 ${EXTRA_WAIT}s 추가 기록"
  sleep "$EXTRA_WAIT"

  echo "[4] 로그 기록 종료"
  kill -TERM "-$LOGGER_PGID" >/dev/null 2>&1 || true
  sleep 1

  make_core_log "$RAW_FILE" "$CORE_FILE"

  echo "============================================================"
  echo "[DONE]"
  echo "raw log:"
  echo "$RAW_FILE"
  echo ""
  echo "core log:"
  echo "$CORE_FILE"
  echo "============================================================"
}
