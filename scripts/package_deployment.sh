#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BTOPS_DIR="${BTOPS_DIR:-${ROOT_DIR}/../btops_ws}"
DEST_ROOT="${1:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/package_deployment.sh <destination-root>

Creates a versioned, checksummed deployment source package containing:
  - committed taskplanner_ws and btops_ws source snapshots
  - restorable Git bundles for both repositories
  - deployment documentation and environment templates
  - a machine-readable version manifest

Datasets, annotations, model weights, caches, and runtime output are external
assets and are intentionally not included by this source-packaging step.
Use `scripts/package_replay_data.sh` to attach an authorized, checksummed
replay/evaluation data companion to the resulting release.
EOF
}

if [[ -z "${DEST_ROOT}" || "${DEST_ROOT}" == "-h" || "${DEST_ROOT}" == "--help" ]]; then
  usage
  [[ -n "${DEST_ROOT}" ]] && exit 0
  exit 2
fi

require_clean_repository() {
  local repository="$1"
  local label="$2"
  if [[ -n "$(git -C "${repository}" status --porcelain --untracked-files=normal)" ]]; then
    printf 'error: %s has uncommitted deployment files\n' "${label}" >&2
    git -C "${repository}" status --short >&2
    exit 2
  fi
}

[[ -d "${BTOPS_DIR}/.git" ]] || {
  printf 'error: BT Ops repository not found at %s\n' "${BTOPS_DIR}" >&2
  exit 2
}

require_clean_repository "${ROOT_DIR}" "taskplanner_ws"
require_clean_repository "${BTOPS_DIR}" "btops_ws"

TASKPLANNER_SHA="$(git -C "${ROOT_DIR}" rev-parse HEAD)"
BTOPS_SHA="$(git -C "${BTOPS_DIR}" rev-parse HEAD)"
PINNED_BTOPS_SHA="$(
  sed -n 's/^BTOPS_REF=//p' "${ROOT_DIR}/.env.example" | head -n 1
)"
if [[ "${PINNED_BTOPS_SHA}" != "${BTOPS_SHA}" ]]; then
  printf 'error: .env.example pins BT Ops %s but checkout is %s\n' \
    "${PINNED_BTOPS_SHA}" "${BTOPS_SHA}" >&2
  exit 2
fi

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RELEASE_ID="${TIMESTAMP}-${TASKPLANNER_SHA:0:12}"
RELEASES_DIR="${DEST_ROOT}/releases"
FINAL_DIR="${RELEASES_DIR}/${RELEASE_ID}"
STAGING_DIR="${DEST_ROOT}/.staging-${RELEASE_ID}-$$"

mkdir -p "${RELEASES_DIR}"
[[ ! -e "${FINAL_DIR}" ]] || {
  printf 'error: release already exists: %s\n' "${FINAL_DIR}" >&2
  exit 2
}
trap 'rm -rf "${STAGING_DIR}"' EXIT
mkdir -p \
  "${STAGING_DIR}/archives" \
  "${STAGING_DIR}/config" \
  "${STAGING_DIR}/docs" \
  "${STAGING_DIR}/manifests" \
  "${STAGING_DIR}/source/taskplanner_ws" \
  "${STAGING_DIR}/source/btops_ws"

git -C "${ROOT_DIR}" archive \
  --format=tar.gz \
  --prefix=taskplanner_ws/ \
  -o "${STAGING_DIR}/archives/taskplanner_ws-${TASKPLANNER_SHA:0:12}.tar.gz" \
  HEAD
git -C "${BTOPS_DIR}" archive \
  --format=tar.gz \
  --prefix=btops_ws/ \
  -o "${STAGING_DIR}/archives/btops_ws-${BTOPS_SHA:0:12}.tar.gz" \
  HEAD
git -C "${ROOT_DIR}" bundle create \
  "${STAGING_DIR}/archives/taskplanner_ws-${TASKPLANNER_SHA:0:12}.bundle" \
  HEAD
git -C "${BTOPS_DIR}" bundle create \
  "${STAGING_DIR}/archives/btops_ws-${BTOPS_SHA:0:12}.bundle" \
  HEAD

extract_snapshot_for_nas() (
  set -euo pipefail
  local archive_path="$1"
  local destination="$2"
  local extraction_dir
  extraction_dir="$(mktemp -d "${TMPDIR:-/tmp}/taskplanner-package.XXXXXX")"
  trap 'rm -rf "${extraction_dir}"' EXIT

  tar -xzf "${archive_path}" \
    --strip-components=1 \
    -C "${extraction_dir}"
  # Some NAS/FUSE mounts reject symlink creation. Keep the canonical Git
  # representation in archives/ and materialize links only in the convenience
  # source tree so it remains directly usable on those mounts.
  cp -RL --preserve=mode,timestamps \
    "${extraction_dir}/." \
    "${destination}/"
)

extract_snapshot_for_nas \
  "${STAGING_DIR}/archives/taskplanner_ws-${TASKPLANNER_SHA:0:12}.tar.gz" \
  "${STAGING_DIR}/source/taskplanner_ws"
extract_snapshot_for_nas \
  "${STAGING_DIR}/archives/btops_ws-${BTOPS_SHA:0:12}.tar.gz" \
  "${STAGING_DIR}/source/btops_ws"

cp "${ROOT_DIR}/.env.example" "${STAGING_DIR}/config/taskplanner.env.example"
cp "${ROOT_DIR}/DEPLOYMENT.md" "${STAGING_DIR}/docs/DEPLOYMENT.md"
cp "${ROOT_DIR}/DISTRIBUTION_NOTICE.md" \
  "${STAGING_DIR}/docs/DISTRIBUTION_NOTICE.md"

python3 - \
  "${STAGING_DIR}/manifests/SOURCE_VERSIONS.json" \
  "${TIMESTAMP}" \
  "${TASKPLANNER_SHA}" \
  "${BTOPS_SHA}" \
  "$(git -C "${ROOT_DIR}" remote get-url origin 2>/dev/null || true)" \
  "$(git -C "${BTOPS_DIR}" remote get-url origin 2>/dev/null || true)" <<'PY'
import json
import sys
from pathlib import Path

(
    output_path,
    created_at,
    taskplanner_sha,
    btops_sha,
    taskplanner_origin,
    btops_origin,
) = sys.argv[1:]
payload = {
    "schema": "taskplanner.deployment_package.v1",
    "created_at_utc": created_at,
    "repositories": {
        "taskplanner_ws": {
            "commit": taskplanner_sha,
            "origin": taskplanner_origin,
        },
        "btops_ws": {
            "commit": btops_sha,
            "origin": btops_origin,
        },
    },
    "included": [
        "committed source snapshots",
        "Git bundles",
        "deployment documentation",
        "environment templates",
    ],
    "external_required_assets": [
        "Shadow replay dataset and timestamped annotations",
        "RF-DETR checkpoints",
        "LM Studio, Unsloth Studio, vLLM, or NInfer model artifacts",
        "Hugging Face and runtime caches",
    ],
}
Path(output_path).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

python3 - \
  "${ROOT_DIR}/.env.example" \
  "${STAGING_DIR}/manifests/CONTAINER_IMAGES.json" \
  "${STAGING_DIR}/manifests/EXTERNAL_ASSETS.example.json" <<'PY'
import json
import sys
from pathlib import Path

env_path, image_output, asset_output = map(Path, sys.argv[1:])
values = {}
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key] = value

images = {
    "schema": "taskplanner.container_images.v1",
    "base_images": {
        "ros": (
            "ros:jazzy-ros-base@sha256:"
            "eac11a5285beeb1e1884e71f7091c610e08452e823bfb3f43afaa334375325f6"
        ),
        "rfdetr": values.get("RFDETR_BASE_IMAGE", ""),
        "vllm": values.get("VLLM_IMAGE", ""),
    },
    "locally_built_images": [
        "taskplanner-ws:dev",
        "taskplanner-rfdetr-perception:0.1.0",
        "taskplanner-vllm-manager:0.1.0",
    ],
}
image_output.write_text(
    json.dumps(images, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)

assets = {
    "schema": "taskplanner.external_assets.v1",
    "assets": [
        {
            "name": "shadow_dataset",
            "required_for": ["replay"],
            "path": "",
            "sha256_manifest": "",
        },
        {
            "name": "rfdetr_checkpoints",
            "required_for": ["live", "replay"],
            "path": "",
            "sha256_manifest": "",
        },
        {
            "name": "local_model_artifacts",
            "required_for": ["real_vlm", "llm_surgeon"],
            "path": "",
            "sha256_manifest": "",
        },
    ],
}
asset_output.write_text(
    json.dumps(assets, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

cat > "${STAGING_DIR}/README.md" <<EOF
# Taskplanner deployment source package

Release: \`${RELEASE_ID}\`

This package contains immutable source snapshots for:

- \`taskplanner_ws\`: \`${TASKPLANNER_SHA}\`
- \`btops_ws\`: \`${BTOPS_SHA}\`

Start with \`docs/DEPLOYMENT.md\`. Copy
\`config/taskplanner.env.example\` to
\`source/taskplanner_ws/.env\`, then configure external dataset and model paths.

Video datasets, annotations, model weights, caches, and runtime traces are not
included by the source-packaging step. Their required locations are documented
in the deployment guide and \`manifests/EXTERNAL_ASSETS.example.json\`.
An authorized replay/evaluation data companion can be attached with
\`scripts/package_replay_data.sh\`.

The expanded \`source/\` tree materializes repository symlinks for NAS/FUSE
compatibility. The exact Git object and file-mode representation is preserved
in \`archives/*.bundle\` and \`archives/*.tar.gz\`.
EOF

(
  cd "${STAGING_DIR}"
  find . -type f ! -name CHECKSUMS.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum > CHECKSUMS.sha256
)

mv "${STAGING_DIR}" "${FINAL_DIR}"
trap - EXIT
printf '%s\n' "releases/${RELEASE_ID}" > "${DEST_ROOT}/LATEST.txt"
cp "${FINAL_DIR}/README.md" "${DEST_ROOT}/README.md"

printf 'deployment package: %s\n' "${FINAL_DIR}"
printf 'taskplanner commit: %s\n' "${TASKPLANNER_SHA}"
printf 'btops commit: %s\n' "${BTOPS_SHA}"
