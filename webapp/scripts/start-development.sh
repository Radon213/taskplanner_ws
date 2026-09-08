#!/usr/bin/env bash
set -euo pipefail

# Development owns only the webapp process. It intentionally never computes a
# source digest or creates `dist`, so saving a TSX/CSS file stays on Vite's HMR
# path instead of accidentally becoming a production rebuild.
if [[ "${TASKPLANNER_WEBAPP_DEVELOPMENT_DROPPED_PRIVILEGES:-false}" == "true" ]]; then
  exec npm run dev -- \
    --host 127.0.0.1 \
    --port "${WEBAPP_PORT:-4173}"
fi

webapp_uid="${TASKPLANNER_WEBAPP_UID:-1000}"
webapp_gid="${TASKPLANNER_WEBAPP_GID:-1000}"
webapp_lock_digest="$(sha256sum package-lock.json | cut -d' ' -f1)"
webapp_dependency_stamp="node_modules/.taskplanner-package-lock.sha256"
webapp_installed_digest="$(
  test -r "${webapp_dependency_stamp}" && cat "${webapp_dependency_stamp}" || true
)"

if [[ "${WEBAPP_INSTALL_ON_START:-false}" == "true" \
    || ! -x node_modules/.bin/vite \
    || "${webapp_installed_digest}" != "${webapp_lock_digest}" ]]; then
  npm ci --no-audit --no-fund
  printf '%s\n' "${webapp_lock_digest}" >"${webapp_dependency_stamp}"
  chown -R "${webapp_uid}:${webapp_gid}" node_modules
fi

install -d -o "${webapp_uid}" -g "${webapp_gid}" node_modules/.vite-temp
webapp_home="/tmp/taskplanner-webapp-home"
install -d -o "${webapp_uid}" -g "${webapp_gid}" "${webapp_home}"

script_path="$(realpath "$0")"
exec setpriv \
  --reuid="${webapp_uid}" \
  --regid="${webapp_gid}" \
  --clear-groups \
  env \
    HOME="${webapp_home}" \
    TASKPLANNER_WEBAPP_DEVELOPMENT_DROPPED_PRIVILEGES=true \
    bash "${script_path}"
