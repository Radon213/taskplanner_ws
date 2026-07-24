#!/usr/bin/env bash
set -u

interval_sec="${1:-5}"

level_for() {
  local value="${1:-}"
  local warn="$2"
  local stop="$3"
  if [[ -z "$value" ]]; then
    printf 'N/A'
  elif awk -v value="$value" -v stop="$stop" 'BEGIN { exit !(value >= stop) }'; then
    printf 'STOP'
  elif awk -v value="$value" -v warn="$warn" 'BEGIN { exit !(value >= warn) }'; then
    printf 'WARN'
  else
    printf 'OK'
  fi
}

while true; do
  sensor_output="$(sensors 2>/dev/null || true)"
  cpu_temp="$(awk '/Package id 0:/ {value=$4; gsub(/[+°C]/, "", value); print value; exit}' <<<"$sensor_output")"
  nvme_temp="$(awk '
    /Composite:/ {
      value=$2
      gsub(/[+°C]/, "", value)
      if (value + 0 > maximum + 0) maximum=value
    }
    END { if (maximum != "") print maximum }
  ' <<<"$sensor_output")"
  gpu_temp="$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -n 1 || true)"
  gpu_util="$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -n 1 || true)"

  cpu_level="$(level_for "$cpu_temp" 85 90)"
  gpu_level="$(level_for "$gpu_temp" 80 85)"
  nvme_level="$(level_for "$nvme_temp" 70 80)"

  printf '%s CPU=%s°C[%s] GPU=%s°C util=%s%%[%s] NVMe(max)=%s°C[%s]\n' \
    "$(date '+%H:%M:%S')" \
    "${cpu_temp:-N/A}" "$cpu_level" \
    "${gpu_temp:-N/A}" "${gpu_util:-N/A}" "$gpu_level" \
    "${nvme_temp:-N/A}" "$nvme_level"

  if [[ "$cpu_level" == "STOP" || "$gpu_level" == "STOP" || "$nvme_level" == "STOP" ]]; then
    printf 'STOP 기준 초과: Isaac timeline과 VLM 요청을 중지하고 냉각하세요.\n' >&2
  fi
  sleep "$interval_sec"
done
