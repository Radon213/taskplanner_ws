#!/usr/bin/env bash

# Shared build-state owner for the static dashboard and the researcher-facing
# UI iteration commands.  Keep the stamp outside `dist`: Vite deliberately
# empties its output directory before each build.

webapp_source_digest() {
  local -a digest_files=(
    index.html package.json package-lock.json tsconfig*.json vite.config.ts
    scripts/start-production.sh scripts/start-development.sh
    scripts/apply-build.sh scripts/webapp-build-state.sh
    scripts/serve-production.mjs scripts/check-dev-server-contract.mjs
  )
  local required_path
  for required_path in "${digest_files[@]}" src public; do
    [[ -e "${required_path}" ]] || {
      printf 'webapp source digest input is missing: %s\n' "${required_path}" >&2
      return 1
    }
  done
  {
    sha256sum "${digest_files[@]}"
    find src public -type f -print0 | sort -z | xargs -0 sha256sum
  } | sha256sum | cut -d' ' -f1
}

webapp_build_stamp_path() {
  printf '%s\n' ".taskplanner/build-source.sha256"
}

webapp_dist_is_present() {
  [[ -s dist/index.html ]]
}

webapp_read_build_stamp() {
  local stamp_path
  stamp_path="$(webapp_build_stamp_path)"
  if [[ -r "${stamp_path}" ]]; then
    cat "${stamp_path}"
    return 0
  fi

  # One-time migration from bundles built before the stamp moved outside
  # Vite's cleaned output directory. New builds never write this location.
  if [[ -r dist/.taskplanner-source.sha256 ]]; then
    cat dist/.taskplanner-source.sha256
  fi
}

webapp_write_build_stamp() {
  local digest="$1"
  local stamp_path stamp_dir
  [[ "${digest}" =~ ^[[:xdigit:]]{64}$ ]] || {
    printf 'invalid webapp source digest\n' >&2
    return 2
  }
  webapp_dist_is_present || {
    printf 'cannot stamp a missing webapp dist bundle\n' >&2
    return 1
  }
  stamp_path="$(webapp_build_stamp_path)"
  stamp_dir="$(dirname "${stamp_path}")"
  mkdir -p "${stamp_dir}"
  printf '%s\n' "${digest}" >"${stamp_path}"
}

webapp_build_is_current() {
  local source_digest built_digest
  source_digest="$(webapp_source_digest)" || return 1
  webapp_dist_is_present || return 1
  built_digest="$(webapp_read_build_stamp)"
  [[ "${built_digest}" == "${source_digest}" ]]
}
