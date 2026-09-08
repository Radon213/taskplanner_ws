#!/usr/bin/env bash
set -euo pipefail

# Keep Docker/YAML free of nested shell programs. The first pass prepares the
# bind-mounted dependency and build directories as root, then re-enters this
# script as the unprivileged desktop user for fingerprinting, building and
# serving the static application.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/webapp-build-state.sh"

# The host launcher uses the exact same digest contract as the in-container
# builder. These read-only modes must stay before dependency installation,
# directory ownership changes, and server startup.
if [[ "${1:-}" == "--print-source-digest" ]]; then
  (($# == 1)) || {
    printf 'usage: %s [--print-source-digest|--check-build-current|--write-build-stamp <digest>]\n' "$0" >&2
    exit 2
  }
  webapp_source_digest
  exit 0
fi
if [[ "${1:-}" == "--check-build-current" ]]; then
  (($# == 1)) || {
    printf 'usage: %s [--print-source-digest|--check-build-current|--write-build-stamp <digest>]\n' "$0" >&2
    exit 2
  }
  webapp_build_is_current
  exit
fi
if [[ "${1:-}" == "--write-build-stamp" ]]; then
  (($# == 2)) || {
    printf 'usage: %s [--print-source-digest|--check-build-current|--write-build-stamp <digest>]\n' "$0" >&2
    exit 2
  }
  webapp_write_build_stamp "$2"
  exit
fi
(($# == 0)) || {
  printf 'usage: %s [--print-source-digest|--check-build-current|--write-build-stamp <digest>]\n' "$0" >&2
  exit 2
}

if [[ "${TASKPLANNER_WEBAPP_DROPPED_PRIVILEGES:-false}" == "true" ]]; then
  webapp_source_digest="$(webapp_source_digest)"
  webapp_built_digest="$(webapp_read_build_stamp)"

  if [[ "${WEBAPP_BUILD_ON_START:-false}" == "true" \
      || "${webapp_built_digest}" != "${webapp_source_digest}" ]] \
      || ! webapp_dist_is_present; then
    npm run build:runtime
    webapp_write_build_stamp "${webapp_source_digest}"
  fi

  exec npm run start:production -- \
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

install -d -o "${webapp_uid}" -g "${webapp_gid}" \
  dist node_modules/.vite-temp
webapp_home="/tmp/taskplanner-webapp-home"
install -d -o "${webapp_uid}" -g "${webapp_gid}" "${webapp_home}"
chown -R "${webapp_uid}:${webapp_gid}" dist

script_path="$(realpath "$0")"
exec setpriv \
  --reuid="${webapp_uid}" \
  --regid="${webapp_gid}" \
  --clear-groups \
  env \
    HOME="${webapp_home}" \
    TASKPLANNER_WEBAPP_DROPPED_PRIVILEGES=true \
    bash "${script_path}"
