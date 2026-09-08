#!/usr/bin/env bash
set -euo pipefail

webapp_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${webapp_dir}"
source scripts/webapp-build-state.sh

# Capture both sides of the build so an edit that lands during bundling cannot
# be incorrectly marked current. The next `ui apply` is then the only work
# needed; no runtime owner is restarted.
source_digest_before="$(webapp_source_digest)"
npm run build:runtime
source_digest_after="$(webapp_source_digest)"
if [[ "${source_digest_before}" != "${source_digest_after}" ]]; then
  printf 'webapp sources changed during build; bundle is intentionally left unstamped\n' >&2
  exit 3
fi
webapp_write_build_stamp "${source_digest_after}"
printf 'Taskplanner UI bundle applied (%s)\n' "${source_digest_after:0:12}"
