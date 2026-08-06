#!/usr/bin/env bash
set -euo pipefail

profile="${1:-vision}"
shift || true

server_binary="${NINFER_SERVER_BINARY:-/opt/taskplanner/ninfer/build/apps/ninfer-serve}"
artifact="${NINFER_MODEL_ARTIFACT:?NINFER_MODEL_ARTIFACT is required}"
log_root="${NINFER_LOG_ROOT:-/tmp/taskplanner-ninfer}"

if [[ ! -x "${server_binary}" ]]; then
  printf 'NInfer server binary is missing or not executable: %s\n' "${server_binary}" >&2
  exit 127
fi
if [[ ! -f "${artifact}" ]]; then
  printf 'NInfer model artifact is missing: %s\n' "${artifact}" >&2
  exit 2
fi

mkdir -p "${log_root}"
export LD_LIBRARY_PATH="${NINFER_CUDA_LIBRARY_PATH:-/opt/taskplanner/cuda/targets/x86_64-linux/lib}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

server_args=(
  "${artifact}"
  --host "${NINFER_HOST:-127.0.0.1}"
  --port "${NINFER_PORT:-8082}"
  --model-id "${NINFER_MODEL_ID:-qwen3.6-35b-a3b}"
  --max-context "${NINFER_MAX_CONTEXT:-8192}"
  --prefill-chunk 1024
  --kv-dtype int8
  --lm-head-draft
  --no-thinking
  --request-log-jsonl "${log_root}/server.requests.jsonl"
)

case "${profile}" in
  text)
    server_args+=(--spec mtp --draft-tokens 3)
    ;;
  vision)
    server_args+=(--spec mtp --draft-tokens 3 --vision)
    ;;
  dflash)
    server_args+=(--spec dflash --draft-tokens 7)
    ;;
  *)
    printf 'Unknown NInfer profile: %s\n' "${profile}" >&2
    exit 2
    ;;
esac

if [[ -n "${NINFER_API_KEY:-}" ]]; then
  server_args+=(--api-key "${NINFER_API_KEY}")
fi

exec "${server_binary}" "${server_args[@]}" "$@"
