# Taskplanner deployment and startup

This repository uses explicit Docker Compose profiles. Nothing in the
repository installs a boot service, and every Compose service has
`restart: "no"`. Starting Docker at OS boot therefore does not start
Taskplanner.

## Runtime layout

| Mode | Started services | Intended input |
| --- | --- | --- |
| `live` | model control planes, webapp, RF-DETR attempt, ROS runtime/bridge | External cameras, speech, and robot Actions |
| `llm-surgeon` | model control planes, webapp, ROS runtime/bridge | LLM surgeon validation with mock Actions |
| `replay` | model control planes, webapp, RF-DETR attempt, shadow replay/ROS bridge | External rosbag dataset and timestamped annotations |

The vLLM manager starts with `VLLM_MANAGER_AUTO_START=false`, so its catalog is
available while no vLLM worker model is loaded. The launcher also starts a
small transient NInfer control plane on the host. It advertises configured
`.ninfer` artifacts but starts no worker until a dashboard/API load request.
The manager and its worker stop with `scripts/taskplanner down` and never
install a boot service.

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

For NInfer, copy the catalog template and set absolute local paths:

```bash
mkdir -p "${HOME}/.config/taskplanner"
cp config/model_providers/ninfer_models.example.json \
  "${HOME}/.config/taskplanner/ninfer-models.json"
```

Then edit the copied file and set this in `.env`:

```text
NINFER_MODEL_CATALOG_PATH=/home/user/.config/taskplanner/ninfer-models.json
```

The NInfer server binary and `.ninfer` artifact remain outside the application
repository and deployment image.

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
handles profile switching, local provider control planes, writable replay
output directories, and best-effort perception startup.

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
evaluation, attach the restricted data companion after creating the source
package:

```bash
TASKPLANNER_SOURCE_MEDIA_ROOT=/path/to/0704_original_media \
TASKPLANNER_SHADOW_PACKAGE_ROOT=/path/to/0704_rosbag2 \
TASKPLANNER_REVIEW_MEDIA_ROOT=/path/to/review_media \
TASKPLANNER_PERCEPTION_ASSET_ROOT=/path/to/0704_RFDETR \
TASKPLANNER_AUDIO_SOURCE_ROOT=/path/to/0704_audio \
TASKPLANNER_KEYFRAME_ROOT=/path/to/0704_keyframes \
TASKPLANNER_LEGACY_PERCEPTION_ROOT=/path/to/0704_YOLO \
scripts/package_replay_data.sh /path/to/taskplanner-backup/releases/<release>
```

This adds original media, replay bags, synchronized review proxies, canonical
annotations, annotation reports, derived bags, and perception assets under
`data/`, with an independent `DATA_CHECKSUMS.sha256`. Provider-managed
LLM/VLM downloads, credentials, caches, and transient runtime traces remain
external. The data companion is restricted clinical research data and must not
be redistributed without explicit authorization.

## Diagnostics

```bash
scripts/taskplanner status
docker compose logs --tail=100 vllm-manager
journalctl --user -u taskplanner-ninfer-manager.service -n 100 --no-pager
docker compose --profile live logs --tail=100 object-perception
docker compose --profile live logs --tail=100 taskplanner-runtime
docker compose --profile replay logs --tail=100 shadow-runner
```

If RF-DETR fails, fix the checkpoint paths or GPU/container runtime while
continuing voice-only testing. Set `REQUIRE_PERCEPTION_ON_START=true` only for
an evaluation that must reject startup without visual perception.
