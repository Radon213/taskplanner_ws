#!/usr/bin/env bash
set -euo pipefail

# Keep Docker/YAML free of nested shell programs. The first pass prepares the
# bind-mounted dependency and build directories as root, then re-enters this
# script as the unprivileged desktop user for fingerprinting, building and
# serving the static application.
webapp_source_digest() {
  local -a digest_files=(
    index.html package.json package-lock.json tsconfig*.json vite.config.ts
    scripts/start-production.sh scripts/serve-production.mjs
    scripts/check-dev-server-contract.mjs scripts/check-monitor-bundle.cjs
  )
  local required_path
  for required_path in "${digest_files[@]}" src public monitor; do
    [[ -e "${required_path}" ]] || {
      printf 'webapp source digest input is missing: %s\n' "${required_path}" >&2
      return 1
    }
  done
  {
    sha256sum "${digest_files[@]}"
    find src public monitor -type f -print0 | sort -z | xargs -0 sha256sum
  } | sha256sum | cut -d' ' -f1
}

webapp_build_is_current() {
  local source_digest built_digest
  source_digest="$(webapp_source_digest)" || return 1
  [[ -s dist/index.html && -s dist/monitor/index.html ]] || return 1
  built_digest="$(
    test -r dist/.taskplanner-source.sha256 &&
      cat dist/.taskplanner-source.sha256 || true
  )"
  [[ "${built_digest}" == "${source_digest}" ]]
}

# The host launcher uses the exact same digest contract as the in-container
# builder. These read-only modes must stay before dependency installation,
# directory ownership changes, and server startup.
if [[ "${1:-}" == "--print-source-digest" ]]; then
  (($# == 1)) || {
    printf 'usage: %s [--print-source-digest|--check-build-current]\n' "$0" >&2
    exit 2
  }
  webapp_source_digest
  exit 0
fi
if [[ "${1:-}" == "--check-build-current" ]]; then
  (($# == 1)) || {
    printf 'usage: %s [--print-source-digest|--check-build-current]\n' "$0" >&2
    exit 2
  }
  webapp_build_is_current
  exit
fi
(($# == 0)) || {
  printf 'usage: %s [--print-source-digest|--check-build-current]\n' "$0" >&2
  exit 2
}

if [[ "${TASKPLANNER_WEBAPP_DROPPED_PRIVILEGES:-false}" == "true" ]]; then
  webapp_source_digest="$(webapp_source_digest)"
  webapp_build_stamp="dist/.taskplanner-source.sha256"
  webapp_built_digest="$(
    test -r "${webapp_build_stamp}" && cat "${webapp_build_stamp}" || true
  )"

  if [[ "${WEBAPP_BUILD_ON_START:-false}" == "true" \
      || ! -s dist/index.html \
      || ! -s dist/monitor/index.html \
      || "${webapp_built_digest}" != "${webapp_source_digest}" ]]; then
    npm run build:runtime
    printf '%s\n' "${webapp_source_digest}" >"${webapp_build_stamp}"
  fi

  exec npm run start:production -- \
    --host 127.0.0.1 \
    --port "${WEBAPP_PORT:-4173}" \
    --media /var/run/taskplanner-monitor-media
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
