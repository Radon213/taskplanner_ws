#!/usr/bin/env bash
set -euo pipefail
export FASTDDS_BUILTIN_TRANSPORTS=${FASTDDS_BUILTIN_TRANSPORTS:-UDPv4}

TEST="${1:-}"
PROC="thyroidectomy"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${TASKPLANNER_WS_ROOT:-$(cd -- "${SCRIPT_DIR}/.." && pwd)}"
OUTDIR="${BASE_DIR}/reports/event_logs"
TS="$(date +%Y%m%d_%H%M%S)"

OBSERVE_SEC="${OBSERVE_SEC:-20}"
MAX_WAIT_SEC="${MAX_WAIT_SEC:-45}"
WARMUP_SEC="${WARMUP_SEC:-6}"
START_PHASE="${START_PHASE:-P03}"
WAIT_FOR_IDLE_SEC="${WAIT_FOR_IDLE_SEC:-18}"

RAW="${OUTDIR}/${PROC}_${TEST}_${TS}_raw_events.log"
CORE="${OUTDIR}/${PROC}_${TEST}_${TS}_core_events.log"

mkdir -p "${OUTDIR}"
cd "${BASE_DIR}"

if [[ -z "${TEST}" ]]; then
  echo "usage: ./scripts/run_event_log_test.sh <test_name>"
  echo "tests: sudden_calling_test sudden_stop_test sudden_bleeding visualerror fallingtool mayo_recovery_after_handover fastchanging paused_voice_reject_test unknown_tool_reject_test"
  exit 1
fi

ros() {
  docker compose exec -T taskplanner-runtime bash -lc "source install/setup.bash && $*"
}

log_core() {
  echo -e "$*" | tee -a "${CORE}"
}

run_core() {
  local title="$1"
  local cmd="$2"
  {
    echo
    echo "===== ${title} ====="
    echo "\$ ${cmd}"
  } >> "${CORE}"
  set +e
  ros "${cmd}" >> "${CORE}" 2>&1
  local code=$?
  set -e
  echo "[exit_code] ${code}" >> "${CORE}"
  return 0
}

start_topic_loggers() {
  local topics=(
    "/simulation/state"
    "/simulation/event"
    "/simulation/control_state"
    "/surgeon/request"
    "/surgeon/actor_event"
  "/surgeon/outward_signal"
    "/surgeon/llm_decision"
    "/surgeon/actor_overlay"
    "/vlm/health"
    "/vlm/result"
    "/vlm/reducer_decisions"
    "/bt/decision"
  "/bt/decision_summary"
    "/bt/skill_command"
    "/skill/status"
    "/skill/events"
    "/twin/world_state"
    "/twin/events"
    "/twin/reducer_decisions"
    "/simulation/surgeon_override"
    "/surgeon/phase_transition_cue"
    "/vlm/tool_observations"
    "/vlm/phase_evidence"
    "/vlm/inference_proposals"
    "/twin/important_event"
  )

  echo "===== RAW EVENT LOG START $(date) =====" >> "${RAW}"
  echo "test=${TEST}" >> "${RAW}"
  echo "observe_sec=${OBSERVE_SEC}" >> "${RAW}"
  echo >> "${RAW}"

  PIDS=()
  for topic in "${topics[@]}"; do
    (
      ros "ros2 topic echo ${topic}" 2>&1 \
        | sed -u "s#^#[${topic}] #"
    ) >> "${RAW}" &
    PIDS+=("$!")
  done
}


stop_topic_loggers() {
  echo >> "${RAW}"
  echo "===== RAW EVENT LOG STOP REQUEST $(date) =====" >> "${RAW}"
  for pid in "${PIDS[@]:-}"; do
    kill "${pid}" 2>/dev/null || true
  done
}

wait_topic_loggers() {
  for pid in "${PIDS[@]:-}"; do
    wait "${pid}" || true
  done
  echo >> "${RAW}"
  echo "===== RAW EVENT LOG END $(date) =====" >> "${RAW}"
}

prepare_simulation() {
  log_core "===== TEST METADATA ====="
  log_core "procedure=${PROC}"
  log_core "test=${TEST}"
  log_core "timestamp=${TS}"
  log_core "start_phase=${START_PHASE}"
  log_core "warmup_sec=${WARMUP_SEC}"
  log_core "observe_sec=${OBSERVE_SEC}"
  log_core "raw_log=${RAW}"
  log_core "core_log=${CORE}"

  run_core "LM STUDIO MODEL CHECK" "python3 - <<'PY'
import os
import urllib.request

base_url = os.environ.get('VLM_BASE_URL', 'http://127.0.0.1:8001').rstrip('/')
print(urllib.request.urlopen(f'{base_url}/v1/models', timeout=5).read().decode()[:500])
PY"

  run_core "RESET SIMULATION" "ros2 service call /simulation/control surgical_msgs/srv/ControlSimulation \"{command: 'reset', start_phase_id: ''}\""
  sleep 3

  run_core "START SIMULATION" "ros2 service call /simulation/control surgical_msgs/srv/ControlSimulation \"{command: 'start', start_phase_id: '${START_PHASE}'}\""
  sleep "${WARMUP_SEC}"

  run_core "PRE-INJECTION STATE SNAPSHOT" "timeout 3 ros2 topic echo /simulation/state --once"
  run_core "PRE-INJECTION VLM HEALTH" "timeout 5 ros2 topic echo /vlm/health --once"
}

inject_case() {
  case "${TEST}" in
    sudden_calling_test)
      log_core "===== INJECTION: sudden voice calling T04 ====="
      run_core "INJECT VOICE REQUEST T04" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T04',
        voice_text: 'Bovie please',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      ;;

    sudden_stop_test)
      log_core "===== INJECTION: sudden stop ====="
      run_core "SUDDEN STOP CONTROL" "ros2 service call /simulation/control surgical_msgs/srv/ControlSimulation \"{command: 'stop', start_phase_id: ''}\""
      ;;

    sudden_bleeding)
      log_core "===== INJECTION: sudden bleeding/hemostasis interrupt ====="
      run_core "SKIP BLEEDING FIELD EVENT" "echo 'skip actor_event bleeding field_event: no subscriber; use P06 phase_transition_cue instead' && sleep 0.3"
      run_core "PUBLISH PHASE TRANSITION CUE P06" "timeout 8 ros2 topic pub --once -w 1 /surgeon/phase_transition_cue surgical_msgs/msg/PhaseTransitionCue \"{
        source: 'manual_test',
        cue_id: 'manual_sudden_bleeding',
        current_phase: '${START_PHASE}',
        target_phase: 'P06',
        confidence: 0.98,
        reason: 'blood or fluid obscures the operative field'
      }\""
      sleep 1
      run_core "INJECT SUCTION REQUEST T10" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T10',
        voice_text: 'Suction please, bleeding.',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      ;;

    visualerror)
      log_core "===== INJECTION: visual error / impossible tool observation ====="
      run_core "PUBLISH IMPOSSIBLE TOOL OBSERVATION T04 SURGEON_HAND" "timeout 8 ros2 topic pub --once -w 1 /vlm/tool_observations surgical_msgs/msg/ToolObservation \"{
        instrument_id: 'T04',
        location_id: 'surgeon_hand',
        location_type: 'surgeon_hand',
        confidence: 0.99,
        visible: true
      }\""
      ;;

    fallingtool)
      log_core "===== INJECTION: floor dropped tool requiring human recovery ====="
      run_core "REQUEST T08 BEFORE FLOOR DROP" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T08',
        voice_text: 'Mosquito forceps please',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      sleep 6
      run_core "PUBLISH T08 FLOOR ZONE OBSERVATION" "timeout 8 ros2 topic pub --once -w 1 /vlm/tool_observations surgical_msgs/msg/ToolObservation \"{
        instrument_id: 'T08',
        location_id: 'floor_zone',
        location_type: 'floor_zone',
        confidence: 0.99,
        visible: true
      }\""
      sleep 5
      run_core "PUBLISH HUMAN RECOVERED DROPPED TOOL" "ros2 topic pub --once /surgeon/actor_event surgical_msgs/msg/SurgeonActorEvent \"{
        event_type: 'human_recovered_dropped_tool',
        tool_id: 'T08',
        phase_id: '',
        voice_text: '',
        note: 'human removed dropped tool and replaced sterile equivalent',
        ready_for_handover: false,
        ready_for_retrieval: false,
        override: true
      }\""
      ;;

    mayo_recovery_after_handover)
      log_core "===== INJECTION: mayo recovery after handover ====="
      run_core "REQUEST T08 BEFORE MAYO RECOVERY" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T08',
        voice_text: 'Mosquito forceps please',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      sleep 6
      run_core "PUBLISH SURGEON PLACED T08 ON MAYO RECOVERY" "ros2 topic pub --once /surgeon/actor_event surgical_msgs/msg/SurgeonActorEvent \"{
        event_type: 'place_on_mayo_recovery',
        tool_id: 'T08',
        phase_id: '',
        voice_text: '',
        note: 'surgeon placed used tool on mayo recovery zone',
        ready_for_handover: false,
        ready_for_retrieval: true,
        override: true
      }\""
      run_core "PUBLISH CONFIRMING T08 MAYO RECOVERY OBSERVATION" "timeout 8 ros2 topic pub --once -w 1 /vlm/tool_observations surgical_msgs/msg/ToolObservation \"{
        instrument_id: 'T08',
        location_id: 'mayo_recovery_zone',
        location_type: 'mayo_recovery_zone',
        confidence: 0.99,
        visible: true
      }\""
      ;;

    fastchanging)
      log_core "===== INJECTION: fast changing surgeon requests ====="
      run_core "FAST REQUEST 1 T04" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T04',
        voice_text: 'Bovie please',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      sleep 2
      run_core "FAST REQUEST 2 T10" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T10',
        voice_text: 'No, suction please',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      sleep 2
      run_core "FAST REQUEST 3 T07" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T07',
        voice_text: 'Switch to bipolar',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      ;;

    paused_voice_reject_test)
      log_core "===== INJECTION: paused state voice override reject ====="
      run_core "PAUSE SIMULATION" "ros2 service call /simulation/control surgical_msgs/srv/ControlSimulation \"{command: 'pause', start_phase_id: ''}\""
      sleep 1
      run_core "TRY VOICE REQUEST WHILE PAUSED" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'T04',
        voice_text: 'Bovie please while paused',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      ;;

    unknown_tool_reject_test)
      log_core "===== INJECTION: unknown tool reject ====="
      run_core "INJECT UNKNOWN TOOL REQUEST" "ros2 service call /simulation/inject_surgeon_override surgical_msgs/srv/InjectSurgeonOverride \"{
        event_type: 'voice_request',
        requested_tool: 'bone_saw',
        voice_text: 'Bone saw please',
        ready_for_handover: true,
        ready_for_retrieval: false,
        clear_pending_requests: true
      }\""
      ;;

    *)
      echo "Unknown test: ${TEST}"
      echo "Allowed: sudden_calling_test sudden_stop_test sudden_bleeding visualerror fallingtool mayo_recovery_after_handover fastchanging paused_voice_reject_test unknown_tool_reject_test"
      exit 2
      ;;
  esac
}

post_snapshots() {
  sleep 2
  run_core "POST-INJECTION SURGEON REQUEST" "timeout 5 ros2 topic echo /surgeon/request"
  run_core "POST-INJECTION BT DECISION" "timeout 5 ros2 topic echo /bt/decision"
  run_core "POST-INJECTION TWIN EVENT" "timeout 5 ros2 topic echo /twin/events"
  run_core "POST-INJECTION VLM RESULT" "timeout 8 ros2 topic echo /vlm/result"
  run_core "POST-INJECTION SIMULATION STATE" "timeout 5 ros2 topic echo /simulation/state"
}

cleanup_after_logging() {
  run_core "CLEANUP RESET AFTER LOGGING" "ros2 service call /simulation/control surgical_msgs/srv/ControlSimulation \"{command: 'reset', start_phase_id: ''}\""
}


wait_for_idle_before_injection() {
  log_core "===== WAIT FOR ROBOT IDLE BEFORE INJECTION ====="
  local deadline=$((SECONDS + WAIT_FOR_IDLE_SEC))
  local snap
  snap="$(mktemp)"

  while (( SECONDS < deadline )); do
    set +e
    ros "timeout 2 ros2 topic echo /simulation/state --once" > "${snap}" 2>&1
    local code=$?
    set -e

    {
      echo
      echo "----- idle check $(date) exit_code=${code} -----"
      cat "${snap}"
    } >> "${CORE}"

    if grep -q "robot_state: idle" "${snap}" && grep -q "active_robot_task_id: ''" "${snap}"; then
      log_core "[IDLE_READY] robot_state is idle and no active robot task."
      rm -f "${snap}"
      return 0
    fi

    sleep 1
  done

  log_core "[IDLE_WAIT_TIMEOUT] robot did not become fully idle within ${WAIT_FOR_IDLE_SEC}s. Continue injection for stress-test."
  rm -f "${snap}"
  return 0
}



state_has_tool_handed_over() {
  local snap="$1"
  local tool="$2"
  local required_phase="${3:-}"

  python3 -c '
import re
import sys
from pathlib import Path

path, tool, required_phase = sys.argv[1], sys.argv[2], sys.argv[3]
text = Path(path).read_text(errors="replace")

if required_phase and f"filtered_phase: {required_phase}" not in text:
    sys.exit(1)

blocks = re.split(r"\n- stamp:", text)
for block in blocks:
    if re.search(r"\binstrument_id:\s*" + re.escape(tool) + r"\b", block):
        if "location_type: surgeon_hand" in block and "status: handed_over" in block and "owner: surgeon" in block:
            sys.exit(0)

sys.exit(1)
' "$snap" "$tool" "$required_phase"
}

wait_for_tool_handed_over() {
  local tool="$1"
  local label="$2"
  local required_phase="${3:-}"
  local deadline=$((SECONDS + MAX_WAIT_SEC))
  local snap
  snap="$(mktemp)"

  log_core "===== WAIT FOR TEST COMPLETION: ${label} ====="
  log_core "target_tool=${tool}"
  log_core "required_phase=${required_phase}"
  log_core "max_wait_sec=${MAX_WAIT_SEC}"

  while (( SECONDS < deadline )); do
    set +e
    ros "timeout 2 ros2 topic echo /simulation/state --once" > "${snap}" 2>&1
    local code=$?
    set -e

    if state_has_tool_handed_over "${snap}" "${tool}" "${required_phase}"; then
      log_core "[TEST_COMPLETED] ${label}: ${tool} handed_over to surgeon."
      {
        echo
        echo "----- completion snapshot $(date) -----"
        grep -E "filtered_phase|robot_state|surgeon_request_tool|active_robot_task_tool_id|instrument_id: ${tool}|location_type: surgeon_hand|status: handed_over|owner: surgeon" "${snap}" || true
      } >> "${CORE}"
      rm -f "${snap}"
      return 0
    fi

    log_core "[WAITING] ${label}: waiting for ${tool} handover..."
    sleep 1
  done

  log_core "[TEST_COMPLETION_TIMEOUT] ${label}: ${tool} was not handed_over within ${MAX_WAIT_SEC}s."
  {
    echo
    echo "----- timeout final snapshot $(date) -----"
    cat "${snap}"
  } >> "${CORE}"
  rm -f "${snap}"
  return 0
}

wait_until_test_done() {
  case "${TEST}" in
    sudden_calling_test)
      wait_for_tool_handed_over "T04" "sudden_calling_test target Bovie/T04 handover"
      ;;

    fastchanging)
      wait_for_fastchanging_done
      ;;

    sudden_bleeding)
      wait_for_tool_handed_over "T10" "sudden_bleeding emergency suction/T10 handover" "P06"
      ;;

    visualerror|fallingtool|mayo_recovery_after_handover)
      log_core "===== WAIT FOR TEST COMPLETION: ${TEST} ====="
      log_core "visual/reducer test uses fixed observation window ${OBSERVE_SEC}s"
      sleep "${OBSERVE_SEC}"
      ;;

    sudden_stop_test|paused_voice_reject_test|unknown_tool_reject_test)
      log_core "===== WAIT FOR TEST COMPLETION: ${TEST} ====="
      log_core "service/reject/stop test uses short observation window 5s"
      sleep 5
      ;;

    *)
      log_core "===== WAIT FOR TEST COMPLETION: fallback fixed window ====="
      sleep "${OBSERVE_SEC}"
      ;;
  esac
}



mute_actor_for_manual_test() {
  case "${TEST}" in
    sudden_calling_test|fastchanging|sudden_bleeding)
      log_core "===== MUTE AUTONOMOUS ACTOR FOR MANUAL TEST ====="
      ros "ros2 topic pub --once /simulation/control_state std_msgs/msg/String \"{data: 'mute_actor:600.0'}\"" >> "${CORE}" 2>&1 || true
      ;;
  esac
}



fastchanging_state_outcome() {
  local snap="$1"

  python3 -c '
import re
import sys
from pathlib import Path

path = sys.argv[1]
text = Path(path).read_text(errors="replace")

def tool_handed_over(tool):
    blocks = re.split(r"\n- stamp:", text)
    for block in blocks:
        if re.search(r"\binstrument_id:\s*" + re.escape(tool) + r"\b", block):
            if "location_type: surgeon_hand" in block and "status: handed_over" in block and "owner: surgeon" in block:
                return True
    return False

if tool_handed_over("T07"):
    print("success:T07_handed_over")
    sys.exit(0)

if (
    "surgeon_request_tool: T07" in text
    and "robot_state: idle" in text
    and "active_robot_task_id: '"'"''"'"'" in text
    and "handover_allowed: false" in text
    and "blocking_guard: pending_transition" in text
):
    print("failed:blocked_by_handover_guard")
    sys.exit(2)

failure_patterns = [
    "active_robot_task_type: retrieve",
    "active_robot_task_type: retrieve_from_mayo",
    "active_robot_task_type: retrieve_from_hand",
    "active_robot_task_type: clean",
    "active_robot_task_type: return",
    "surgeon_intent: procedure_finishing",
    "filtered_phase: P05",
]

for pat in failure_patterns:
    if pat in text:
        print("failed:unexpected_recovery_or_finishing_path")
        sys.exit(2)

if "surgeon_request_tool: T07" not in text and "explicit_request_tool: T07" not in text:
    print("failed:T07_request_lost")
    sys.exit(2)

print("continue")
sys.exit(1)
' "$snap"
}

wait_for_fastchanging_done() {
  local deadline=$((SECONDS + MAX_WAIT_SEC))
  local snap
  snap="$(mktemp)"

  log_core "===== WAIT FOR TEST COMPLETION: fastchanging ====="
  log_core "success_condition=T07 handed_over to surgeon"
  log_core "failure_condition=recovery/cleaning/finishing path or request changes away from T07"
  log_core "max_wait_sec=${MAX_WAIT_SEC}"

  while (( SECONDS < deadline )); do
    set +e
    ros "timeout 2 ros2 topic echo /simulation/state --once" > "${snap}" 2>&1
    local echo_code=$?
    outcome="$(fastchanging_state_outcome "${snap}")"
    local outcome_code=$?
    set -e

    if [[ "${outcome_code}" -eq 0 ]]; then
      log_core "[TEST_COMPLETED] fastchanging: ${outcome}"
      cat "${snap}" >> "${CORE}"
      rm -f "${snap}"
      return 0
    fi

    if [[ "${outcome_code}" -eq 2 ]]; then
      log_core "[TEST_FAILED] fastchanging: ${outcome}"
      cat "${snap}" >> "${CORE}"
      rm -f "${snap}"
      return 0
    fi

    log_core "[WAITING] fastchanging: ${outcome}"
    sleep 1
  done

  log_core "[TEST_COMPLETION_TIMEOUT] fastchanging: T07 was not handed_over within ${MAX_WAIT_SEC}s."
  cat "${snap}" >> "${CORE}"
  rm -f "${snap}"
  return 0
}


cleanup_on_signal() {
  trap - INT TERM
  stop_topic_loggers
  cleanup_after_logging
  exit 130
}

trap cleanup_on_signal INT TERM
prepare_simulation
wait_for_idle_before_injection
mute_actor_for_manual_test
start_topic_loggers
sleep 1
inject_case
wait_until_test_done
post_snapshots
stop_topic_loggers
wait_topic_loggers
cleanup_after_logging

echo
echo "[DONE] ${TEST}"
echo "raw log : ${RAW}"
echo "core log: ${CORE}"
