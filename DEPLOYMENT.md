# Taskplanner deployment and startup

This repository uses explicit Docker Compose profiles. Nothing in the
repository installs a boot service, and every Compose service has
`restart: "no"`. Starting Docker at OS boot therefore does not start
Taskplanner.

## Runtime layout

| Mode | Started services | Intended input |
| --- | --- | --- |
| `live` | model control planes, webapp, RF-DETR attempt, ROS runtime/bridge | External cameras, speech, humanoid Action, and retraction controller contracts |
| `llm-surgeon` | model control planes, webapp, ROS runtime/bridge | LLM surgeon validation with mock Actions |
| `replay` | model control planes, webapp, RF-DETR attempt, shadow replay/ROS bridge | External rosbag dataset and timestamped annotations |

In `live` mode, bed-mounted robot integration is retraction-only. Applicable
deployments must provide `/surgery/tool_change/request`,
`/surgery/retraction/adjust`, and `/external/bed_robot_arms/status` as defined in
`docs/EXTERNAL_INPUT_CONTRACT.md`. There is no bed-mounted suction-arm API;
clinical suction instruments and speech remain part of Taskplanner's ordinary
tool/evidence path.

The vLLM manager starts with `VLLM_MANAGER_AUTO_START=false`, so its catalog is
available while no vLLM worker model is loaded. The Compose-managed
`ninfer-manager` provides the same unloaded catalog behavior for local
`.ninfer` artifacts. Neither manager installs a host boot service, and neither
starts a model worker until an explicit dashboard/API load request.

When locally installed, the launcher starts the LM Studio catalog server and
the Unsloth Studio backend without loading a model. It then queries all four
providers concurrently and uses their native lifecycle endpoints. Missing
provider applications are reported as offline without blocking Taskplanner.
`scripts/taskplanner down` stops provider control planes that the launcher
started. A remote server exposing only the OpenAI-compatible `/v1/models`
endpoint remains queryable but cannot be loaded or unloaded remotely.

RF-DETR is different from the generative VLM/LLM workers. Live and replay modes
attempt to start the RF-DETR evidence service and load its two checkpoints.
That attempt is non-blocking. With `REQUIRE_PERCEPTION_ON_START=false`, missing
weights, an unavailable GPU, or a failed perception container does not prevent
ROS, speech input, the digital twin, or voice-driven planning from starting.

Video timing is never controlled by VLM throughput. Live cameras and replay
publish at their own source cadence. The VLM has one in-flight request and one
replaceable pending slot; newly arriving frames replace older pending frames,
and the newest frame starts immediately after the current response. There is no
inference backlog and no moving "valid input window." If the VLM disconnects,
times out, or returns no result, visual evidence becomes unavailable while
timestamped speech and other public inputs continue through the digital twin
and BT.

## External assets

Copy `.env.example` to `.env` and point these roots at host-managed storage:

```text
${SHADOW_DATASET_ROOT}/
  0704_6/metadata.yaml
  0704_7/metadata.yaml
  ...

${TASKPLANNER_ANNOTATION_ROOT}/
  cases/0704_6/annotation_manifest.json
  catalogs/
  schema/

${RFDETR_MODEL_ROOT}/
  flir/checkpoint_best_total.pth
  cam4/checkpoint_best_total.pth

${HF_CACHE_DIR}/
${VLLM_CACHE_DIR}/
${TASKPLANNER_RUN_ROOT}/
```

The rosbag dataset, RF-DETR weights, Hugging Face/vLLM caches, and generated
run traces are bind-mounted and are not part of the application image. Local
LM Studio, Unsloth Studio, vLLM, and NInfer model downloads are also managed
outside this repository.

For NInfer, point `.env` at the host-managed runtime and CUDA toolkit:

```text
NINFER_RUNTIME_ROOT=/absolute/path/to/ninfer
NINFER_CUDA_ROOT=/usr/local/cuda-13.1
NINFER_35B_ARTIFACT_REL=models/qwen3_6_35b_a3b.ninfer
```

The Compose-managed control plane discovers supported artifacts beneath
`NINFER_RUNTIME_ROOT` and exposes only files that exist. The NInfer server
binary, `.ninfer` artifacts, and CUDA libraries remain outside the application
repository and deployment image; the manager bind-mounts them read-only.

## Start and stop

```bash
cd /path/to/taskplanner_ws
cp .env.example .env

scripts/taskplanner up live
scripts/taskplanner up llm-surgeon
SHADOW_CASE_ID=0704_12 scripts/taskplanner up replay

scripts/taskplanner status
scripts/taskplanner down
```

Each `up` command stops mode-specific containers left by another profile,
recreates the common control plane with vLLM and NInfer workers unloaded,
starts installed LM Studio and Unsloth catalog backends, prepares
mode-appropriate perception/video services, and starts the selected ROS
runtime. The dashboard remains at `http://127.0.0.1:4173`.

Normal startup reuses the repository's existing `build/` and `install/`
artifacts plus the persistent `taskplanner_web_node_modules` volume. It does not
run `colcon build`, rebuild images, or run `npm install`. For a first deployment
or after dependency/interface changes, request those operations explicitly:

```bash
scripts/taskplanner up live --build
```

`--build` sets `TASKPLANNER_REBUILD_ON_START=true` and
`WEBAPP_INSTALL_ON_START=true` for that invocation. The same flags can be set
independently in `.env`. Without existing `install/setup.bash` or Vite in
`node_modules`, fast startup exits with a clear instruction instead of silently
performing package installation.

To inspect commands without changing the machine:

```bash
scripts/taskplanner up replay --dry-run
scripts/taskplanner help
scripts/taskplanner config live
```

The container-only portion can be inspected with:

```bash
docker compose \
  --env-file .env.example \
  --env-file .env \
  --env-file docker/orchestration/live.env \
  --profile live up -d
```

Use the matching environment file and profile for `llm-surgeon` or `replay`.
Use `scripts/taskplanner up ...` for normal operation because the launcher also
handles profile switching, writable replay output directories, and best-effort
perception startup. The same Compose profile also starts the vLLM and NInfer
control planes, so direct profile-aware Compose startup retains model catalog
and lifecycle support.

## Model loading

After a profile is ready, choose a provider/model in the dashboard. Selecting
a managed, unloaded model begins loading it and the UI reports the transitional
state until the provider reports readiness. Large-model requests use the
provider runtime timeout instead of the ordinary 10-second service timeout.
LM Studio, Unsloth Studio, vLLM, and NInfer all use this path when their native
lifecycle APIs are available.

The NInfer catalog remains visible while its worker is offline. A missing
artifact is shown as unavailable instead of triggering an automatic download
or load.

Starting a new profile intentionally recreates the vLLM manager and starts the
NInfer manager without a worker. This prevents a previous model from silently
consuming VRAM after a mode change.

## Clean boot migration

Older local experiments may have created systemd user services or containers
with persistent restart policies. Remove those one time:

```bash
systemctl --user disable --now taskplanner-rfdetr-perception.service 2>/dev/null || true
systemctl --user disable --now taskplanner-ninfer-qwen36.service 2>/dev/null || true
scripts/taskplanner down
```

The launcher does not install or enable systemd units. Verify the resulting
host policy with:

```bash
systemctl --user list-unit-files 'taskplanner*' --no-pager
docker compose --profile live --profile llm-surgeon --profile replay config |
  grep -n 'restart:'
```

Every rendered service should report `restart: "no"`.

## Source package

Create a reproducible source package after committing both repositories:

```bash
scripts/package_deployment.sh /path/to/taskplanner-backup
```

The versioned release contains committed Taskplanner and BT Ops source
snapshots, Git bundles, environment templates, documentation, a machine-readable
source/version manifest, container image manifest, external-asset template, and
SHA-256 checksums. External datasets, annotations, model weights, caches, and
runtime traces are deliberately excluded.

For an authorized internal release that must reproduce Shadow Replay and its
evaluation, attach a validated restricted-data asset map after creating the
source package:

```bash
TASKPLANNER_SOURCE_MEDIA_ROOT=/path/to/0704_original_media \
TASKPLANNER_SHADOW_PACKAGE_ROOT=/path/to/0704_rosbag2 \
TASKPLANNER_REVIEW_MEDIA_ROOT=/path/to/review_media \
TASKPLANNER_PERCEPTION_ASSET_ROOT=/path/to/0704_RFDETR \
TASKPLANNER_ANNOTATIONS_ROOT=/path/to/restricted_case_annotations \
TASKPLANNER_DERIVED_BAGS_ROOT=/path/to/derived_annotated_bags \
TASKPLANNER_AUDIO_SOURCE_ROOT=/path/to/0704_audio \
TASKPLANNER_KEYFRAME_ROOT=/path/to/0704_keyframes \
TASKPLANNER_LEGACY_PERCEPTION_ROOT=/path/to/0704_YOLO \
TASKPLANNER_LEGACY_DETECTION_ROOT=/path/to/0704_legacy_cam4_detection \
scripts/package_replay_data.sh /path/to/taskplanner-backup/releases/<release>
```

The default `reference` mode writes `data/DATA_PACKAGE.json` and validates all
required case media without duplicating large files. It records both absolute
paths and paths relative to the release, so assets already stored beside the
release on the same NAS remain a single source of truth. This avoids filling a
local rclone VFS cache during NAS-to-NAS copies.

Case annotation JSON is normally ignored by Git and should be backed up under
the restricted data area, then supplied through
`TASKPLANNER_ANNOTATIONS_ROOT`. Only schemas and tooling belong in the source
release.

Provider-managed LLM/VLM downloads, credentials, caches, and transient runtime
traces remain external. The mapped assets are restricted clinical research
data and must not be redistributed without explicit authorization.

Configure runtime paths from the generated asset map:

```text
SHADOW_DATASET_ROOT=<shadow_dataset path>/bags
TASKPLANNER_ANNOTATION_ROOT=<annotations path>/observable_tool_events
TASKPLANNER_ANNOTATION_CACHE=<review_media path>
RFDETR_MODEL_ROOT=<rfdetr_assets path>/models
```

Physical materialization is an explicit exceptional operation:

```bash
TASKPLANNER_DATA_MODE=copy \
TASKPLANNER_ALLOW_FULL_COPY=true \
scripts/package_replay_data.sh /local-or-direct-remote/release
```

The script refuses copy mode on a FUSE/rclone destination by default. Use a
direct remote transfer or a NAS-side copy job instead of overriding that guard.

## Release verification

Before packaging a software release candidate, run:

```bash
scripts/taskplanner verify-release --tier rc
```

For the final software-stage evidence, run the configured 100-restart,
24-hour campaign:

```bash
scripts/taskplanner verify-release --tier full
```

The command creates an auditable result bundle under `reports/release/` and
does not load a VLM or copy restricted datasets. Pass explicit read-only data,
annotation, baseline-report, provider, and model arguments to add the 12-case
Shadow Replay accuracy and latency gate. See
[`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md) for the complete
command and acceptance criteria.

This is the software release boundary. Real robot calibration, trajectories,
grasp and release behavior, collision avoidance, E-stop behavior, and the site
network remain a separate physical acceptance gate.

For the retraction controller, that site gate must also verify Tool Change
Service completion/failure semantics, Retraction Adjustment Goal acceptance and
cancel recovery, monotonic status revisions, direct-teach reporting, and
fail-closed behavior for stale or unavailable controller status.

## Diagnostics

```bash
scripts/taskplanner status
docker compose logs --tail=100 vllm-manager
docker compose logs --tail=100 ninfer-manager
docker compose --profile live logs --tail=100 object-perception
docker compose --profile live logs --tail=100 taskplanner-runtime
docker compose --profile replay logs --tail=100 shadow-runner
```

If RF-DETR fails, fix the checkpoint paths or GPU/container runtime while
continuing voice-only testing. Set `REQUIRE_PERCEPTION_ON_START=true` only for
an evaluation that must reject startup without visual perception.
