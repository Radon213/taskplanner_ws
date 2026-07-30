#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_DIR="${1:-}"

usage() {
  cat <<'EOF'
Usage:
  # Safe default: record validated references without duplicating NAS data.
  TASKPLANNER_SOURCE_MEDIA_ROOT=/path/to/0704_original_media \
  TASKPLANNER_SHADOW_PACKAGE_ROOT=/path/to/0704_rosbag2 \
  TASKPLANNER_REVIEW_MEDIA_ROOT=/path/to/review_media \
  TASKPLANNER_PERCEPTION_ASSET_ROOT=/path/to/0704_RFDETR \
  scripts/package_replay_data.sh <deployment-release-dir>

Attaches a validated replay/evaluation asset map to an existing Taskplanner
deployment release. Large assets remain in their canonical storage locations
and are referenced by absolute and release-relative paths.

To create a physically materialized copy, set both:
  TASKPLANNER_DATA_MODE=copy
  TASKPLANNER_ALLOW_FULL_COPY=true

Copy mode is rejected on rclone/FUSE destinations unless
TASKPLANNER_ALLOW_VFS_COPY=true is also set. Use direct rclone transfer instead
of a VFS mount for large remote packages.

Optional inputs:
  TASKPLANNER_ANNOTATIONS_ROOT
  TASKPLANNER_REPORTS_ROOT
  TASKPLANNER_DERIVED_BAGS_ROOT
  TASKPLANNER_AUDIO_SOURCE_ROOT
  TASKPLANNER_KEYFRAME_ROOT
  TASKPLANNER_LEGACY_PERCEPTION_ROOT
  TASKPLANNER_LEGACY_DETECTION_ROOT

The script also includes the repository-local case annotations, proposals,
reports, and derived annotated bags. Model-provider downloads, credentials,
caches, and transient runtime traces are never copied.
EOF
}

if [[ -z "${RELEASE_DIR}" || "${RELEASE_DIR}" == "-h" || "${RELEASE_DIR}" == "--help" ]]; then
  usage
  [[ -n "${RELEASE_DIR}" ]] && exit 0
  exit 2
fi

RELEASE_DIR="$(realpath "${RELEASE_DIR}")"
[[ -d "${RELEASE_DIR}/source/taskplanner_ws" ]] || {
  printf 'error: deployment release not found at %s\n' "${RELEASE_DIR}" >&2
  exit 2
}

SOURCE_MEDIA_ROOT="${TASKPLANNER_SOURCE_MEDIA_ROOT:-}"
SHADOW_PACKAGE_ROOT="${TASKPLANNER_SHADOW_PACKAGE_ROOT:-}"
REVIEW_MEDIA_ROOT="${TASKPLANNER_REVIEW_MEDIA_ROOT:-}"
PERCEPTION_ASSET_ROOT="${TASKPLANNER_PERCEPTION_ASSET_ROOT:-}"
AUDIO_SOURCE_ROOT="${TASKPLANNER_AUDIO_SOURCE_ROOT:-}"
KEYFRAME_ROOT="${TASKPLANNER_KEYFRAME_ROOT:-}"
LEGACY_PERCEPTION_ROOT="${TASKPLANNER_LEGACY_PERCEPTION_ROOT:-}"
LEGACY_DETECTION_ROOT="${TASKPLANNER_LEGACY_DETECTION_ROOT:-}"
DERIVED_BAGS_ROOT="${TASKPLANNER_DERIVED_BAGS_ROOT:-${ROOT_DIR}/annotated_bags}"
ANNOTATIONS_ROOT="${TASKPLANNER_ANNOTATIONS_ROOT:-${RELEASE_DIR}/source/taskplanner_ws/annotations}"
REPORTS_ROOT="${TASKPLANNER_REPORTS_ROOT:-${RELEASE_DIR}/source/taskplanner_ws/reports}"
DATA_MODE="${TASKPLANNER_DATA_MODE:-reference}"

require_directory() {
  local path="$1"
  local label="$2"
  [[ -n "${path}" && -d "${path}" ]] || {
    printf 'error: %s directory is missing: %s\n' "${label}" "${path}" >&2
    exit 2
  }
}

require_directory "${SOURCE_MEDIA_ROOT}" "original media"
require_directory "${SHADOW_PACKAGE_ROOT}" "shadow rosbag package"
require_directory "${REVIEW_MEDIA_ROOT}" "synchronized review media"
require_directory "${PERCEPTION_ASSET_ROOT}" "RF-DETR assets"
require_directory "${ANNOTATIONS_ROOT}/clinical_video/cases" \
  "clinical annotations"
require_directory "${ANNOTATIONS_ROOT}/observable_tool_events/cases" \
  "observable annotations"
require_directory "${REPORTS_ROOT}" "evaluation reports"
require_directory "${DERIVED_BAGS_ROOT}" "derived annotated bags"

FINAL_DATA_DIR="${RELEASE_DIR}/data"
STAGING_DIR="${RELEASE_DIR}/.data-staging"
[[ ! -e "${FINAL_DATA_DIR}" ]] || {
  printf 'error: data package already exists: %s\n' "${FINAL_DATA_DIR}" >&2
  exit 2
}

if [[ "${DATA_MODE}" == "reference" ]]; then
  asset_args=(--derived-bags "${DERIVED_BAGS_ROOT}")
  if [[ -n "${AUDIO_SOURCE_ROOT}" ]]; then
    require_directory "${AUDIO_SOURCE_ROOT}" "source audio"
    asset_args+=(--audio "${AUDIO_SOURCE_ROOT}")
  fi
  if [[ -n "${KEYFRAME_ROOT}" ]]; then
    require_directory "${KEYFRAME_ROOT}" "keyframes"
    asset_args+=(--keyframes "${KEYFRAME_ROOT}")
  fi
  if [[ -n "${LEGACY_PERCEPTION_ROOT}" ]]; then
    require_directory "${LEGACY_PERCEPTION_ROOT}" "legacy perception assets"
    asset_args+=(--legacy-perception "${LEGACY_PERCEPTION_ROOT}")
  fi
  if [[ -n "${LEGACY_DETECTION_ROOT}" ]]; then
    require_directory "${LEGACY_DETECTION_ROOT}" \
      "legacy CAM4 detection assets"
    asset_args+=(--legacy-detection "${LEGACY_DETECTION_ROOT}")
  fi

  TASKPLANNER_SHA="$(
    python3 - "${RELEASE_DIR}/manifests/SOURCE_VERSIONS.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(payload["repositories"]["taskplanner_ws"]["commit"])
PY
  )"
  python3 "${ROOT_DIR}/scripts/create_replay_asset_map.py" \
    "${RELEASE_DIR}" \
    --taskplanner-commit "${TASKPLANNER_SHA}" \
    --original-media "${SOURCE_MEDIA_ROOT}" \
    --shadow-dataset "${SHADOW_PACKAGE_ROOT}" \
    --review-media "${REVIEW_MEDIA_ROOT}" \
    --rfdetr "${PERCEPTION_ASSET_ROOT}" \
    --annotations "${ANNOTATIONS_ROOT}" \
    --reports "${REPORTS_ROOT}" \
    "${asset_args[@]}"

  cat >> "${RELEASE_DIR}/README.md" <<'EOF'

## Replay and evaluation data map

The `data/` directory contains a validated single-source asset map. Large
clinical media are not duplicated; see `data/DATA_PACKAGE.json` for canonical
absolute and release-relative paths.
EOF

  (
    cd "${RELEASE_DIR}"
    find . -type f ! -name CHECKSUMS.sha256 -print0 \
      | sort -z \
      | xargs -0 sha256sum > CHECKSUMS.sha256
  )

  DEST_ROOT="$(dirname "$(dirname "${RELEASE_DIR}")")"
  if [[ -f "${DEST_ROOT}/LATEST.txt" ]] && \
    [[ "$(cat "${DEST_ROOT}/LATEST.txt")" == "releases/$(basename "${RELEASE_DIR}")" ]]; then
    cp "${RELEASE_DIR}/README.md" "${DEST_ROOT}/README.md"
  fi

  printf 'replay asset map: %s\n' "${FINAL_DATA_DIR}"
  exit 0
fi

if [[ "${DATA_MODE}" != "copy" ]]; then
  printf 'error: unsupported TASKPLANNER_DATA_MODE=%s\n' "${DATA_MODE}" >&2
  exit 2
fi
if [[ "${TASKPLANNER_ALLOW_FULL_COPY:-false}" != "true" ]]; then
  printf '%s\n' \
    'error: copy mode requires TASKPLANNER_ALLOW_FULL_COPY=true' >&2
  exit 2
fi

TARGET_FSTYPE="$(
  findmnt -n -o FSTYPE --target "${RELEASE_DIR}" 2>/dev/null || true
)"
if [[ "${TARGET_FSTYPE}" == fuse.* ]] && \
  [[ "${TASKPLANNER_ALLOW_VFS_COPY:-false}" != "true" ]]; then
  printf '%s\n' \
    "error: refusing full copy through ${TARGET_FSTYPE}; use reference mode or direct rclone transfer" >&2
  exit 2
fi

mkdir -p "${STAGING_DIR}"

copy_tree() {
  local source="$1"
  local destination="$2"
  mkdir -p "${destination}"
  # rclone VFS mounts can reject post-upload chmod/mtime updates. Package
  # integrity is enforced with SHA-256, so copy bytes recursively and resume
  # completed files by size without relying on remote filesystem metadata.
  rsync -rL --size-only --partial --inplace --human-readable \
    "${source}/" "${destination}/"
}

case_ids=(
  0704_5 0704_6 0704_7 0704_8 0704_9 0704_10 0704_11
  0704_12 0704_13 0704_14 0704_15 0704_16 0704_17
)
review_case_ids=(
  0704_6 0704_7 0704_8 0704_9 0704_10 0704_11
  0704_12 0704_13 0704_14 0704_15 0704_16 0704_17
)

copy_tree \
  "${ANNOTATIONS_ROOT}/clinical_video" \
  "${STAGING_DIR}/annotations/clinical_video"
copy_tree \
  "${ANNOTATIONS_ROOT}/observable_tool_events" \
  "${STAGING_DIR}/annotations/observable_tool_events"
copy_tree "${REPORTS_ROOT}" "${STAGING_DIR}/evaluation_reports"
copy_tree "${DERIVED_BAGS_ROOT}" "${STAGING_DIR}/derived_bags"

for case_id in "${case_ids[@]}"; do
  require_directory "${SOURCE_MEDIA_ROOT}/${case_id}" \
    "original media for ${case_id}"
  copy_tree \
    "${SOURCE_MEDIA_ROOT}/${case_id}" \
    "${STAGING_DIR}/original_media/${case_id}"
done
if [[ -d "${SOURCE_MEDIA_ROOT}/calibration_result" ]]; then
  copy_tree \
    "${SOURCE_MEDIA_ROOT}/calibration_result" \
    "${STAGING_DIR}/original_media/calibration_result"
fi

for case_id in "${review_case_ids[@]}"; do
  require_directory "${REVIEW_MEDIA_ROOT}/${case_id}" \
    "review media for ${case_id}"
  copy_tree \
    "${REVIEW_MEDIA_ROOT}/${case_id}" \
    "${STAGING_DIR}/review_media/${case_id}"
done

copy_tree "${SHADOW_PACKAGE_ROOT}" "${STAGING_DIR}/shadow_dataset"
copy_tree "${PERCEPTION_ASSET_ROOT}" "${STAGING_DIR}/perception/rfdetr"

if [[ -n "${AUDIO_SOURCE_ROOT}" ]]; then
  require_directory "${AUDIO_SOURCE_ROOT}" "source audio"
  copy_tree "${AUDIO_SOURCE_ROOT}" "${STAGING_DIR}/source_audio"
fi
if [[ -n "${KEYFRAME_ROOT}" ]]; then
  require_directory "${KEYFRAME_ROOT}" "keyframes"
  mkdir -p "${STAGING_DIR}/keyframes"
  for case_id in "${case_ids[@]}"; do
    case_number="${case_id#0704_}"
    for source in \
      "${KEYFRAME_ROOT}/0704_thy_${case_number}_mayo" \
      "${KEYFRAME_ROOT}/0704_thy_${case_number}_surg"; do
      require_directory "${source}" "keyframes for ${case_id}"
      copy_tree "${source}" "${STAGING_DIR}/keyframes/$(basename "${source}")"
    done
  done
fi
if [[ -n "${LEGACY_PERCEPTION_ROOT}" ]]; then
  require_directory "${LEGACY_PERCEPTION_ROOT}" "legacy perception assets"
  copy_tree \
    "${LEGACY_PERCEPTION_ROOT}" \
    "${STAGING_DIR}/perception/legacy"
fi
if [[ -n "${LEGACY_DETECTION_ROOT}" ]]; then
  require_directory "${LEGACY_DETECTION_ROOT}" \
    "legacy CAM4 detection assets"
  copy_tree \
    "${LEGACY_DETECTION_ROOT}" \
    "${STAGING_DIR}/perception/legacy_cam4_detection"
fi

TASKPLANNER_SHA="$(git -C "${ROOT_DIR}" rev-parse HEAD)"
python3 - "${STAGING_DIR}" "${TASKPLANNER_SHA}" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

data_root = Path(sys.argv[1])
taskplanner_commit = sys.argv[2]
groups = {}
for child in sorted(data_root.iterdir()):
    if child.name.startswith(".") or not child.is_dir():
        continue
    files = [path for path in child.rglob("*") if path.is_file()]
    groups[child.name] = {
        "path": child.name,
        "file_count": len(files),
        "size_bytes": sum(path.stat().st_size for path in files),
    }

payload = {
    "schema": "taskplanner.replay_data_package.v1",
    "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "taskplanner_commit": taskplanner_commit,
    "cases": [f"0704_{number}" for number in range(5, 18)],
    "clinical_review_cases": [f"0704_{number}" for number in range(6, 18)],
    "groups": groups,
    "excluded": [
        "credentials and local environment files",
        "LM Studio, Unsloth Studio, vLLM, and NInfer model downloads",
        "Hugging Face and runtime caches",
        "transient taskplanner output and service logs",
    ],
    "handling": {
        "classification": "restricted clinical research data",
        "redistribution": "owner authorization required",
    },
}
(data_root / "DATA_PACKAGE.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

cat > "${STAGING_DIR}/README.md" <<'EOF'
# Taskplanner replay and evaluation data

This directory is the restricted-data companion to the deployment source
package. It contains the 0704_5 through 0704_17 original multimodal media,
timestamped rosbag replay package, synchronized review proxies, canonical
annotations, proposals and reports, derived annotated bags, and perception
assets.

For replay, set:

```text
SHADOW_DATASET_ROOT=<release>/data/shadow_dataset/bags
TASKPLANNER_ANNOTATION_ROOT=<release>/data/annotations/observable_tool_events
TASKPLANNER_ANNOTATION_CACHE=<release>/data/review_media
RFDETR_MODEL_ROOT=<release>/data/perception/rfdetr/models
```

The exact RF-DETR checkpoint subpaths remain defined by `.env`. Original media
are retained for audit and regeneration; normal replay consumes
`shadow_dataset/bags`.

This is restricted clinical research data. Do not redistribute it without
explicit authorization from the data owner.
EOF

python3 "${ROOT_DIR}/scripts/validate_replay_data_package.py" \
  "${STAGING_DIR}" \
  --report "${STAGING_DIR}/DATA_VALIDATION.json"

(
  cd "${STAGING_DIR}"
  find . -type f ! -name DATA_CHECKSUMS.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum > DATA_CHECKSUMS.sha256
)

mv "${STAGING_DIR}" "${FINAL_DATA_DIR}"

python3 - "${RELEASE_DIR}/manifests/EXTERNAL_ASSETS.json" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
payload = {
    "schema": "taskplanner.external_assets.v1",
    "assets": [
        {
            "name": "shadow_dataset",
            "required_for": ["replay"],
            "path": "data/shadow_dataset/bags",
            "sha256_manifest": "data/DATA_CHECKSUMS.sha256",
            "included": True,
        },
        {
            "name": "annotations",
            "required_for": ["replay", "evaluation"],
            "path": "data/annotations",
            "sha256_manifest": "data/DATA_CHECKSUMS.sha256",
            "included": True,
        },
        {
            "name": "rfdetr_assets",
            "required_for": ["live", "replay"],
            "path": "data/perception/rfdetr",
            "sha256_manifest": "data/DATA_CHECKSUMS.sha256",
            "included": True,
        },
        {
            "name": "local_model_artifacts",
            "required_for": ["real_vlm", "llm_surgeon"],
            "path": "",
            "sha256_manifest": "",
            "included": False,
        },
    ],
}
output.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

cat >> "${RELEASE_DIR}/README.md" <<'EOF'

## Attached replay and evaluation data

The `data/` directory contains the restricted 0704 clinical replay and
evaluation companion package. See `data/README.md`,
`data/DATA_PACKAGE.json`, and `data/DATA_CHECKSUMS.sha256`.
EOF

(
  cd "${RELEASE_DIR}"
  find . -path ./data -prune -o \
    -type f ! -name CHECKSUMS.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum > CHECKSUMS.sha256
)

DEST_ROOT="$(dirname "$(dirname "${RELEASE_DIR}")")"
if [[ -f "${DEST_ROOT}/LATEST.txt" ]] && \
  [[ "$(cat "${DEST_ROOT}/LATEST.txt")" == "releases/$(basename "${RELEASE_DIR}")" ]]; then
  cp "${RELEASE_DIR}/README.md" "${DEST_ROOT}/README.md"
fi

printf 'replay data package: %s\n' "${FINAL_DATA_DIR}"
