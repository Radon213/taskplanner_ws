# Taskplanner Workspace

Version: `0.1.0`

ROS 2 Jazzy workspace for a surgical tool-handover task planner. The current
baseline combines a real VLM path, an OR digital twin, Behavior Tree decisions,
mock robot skill execution, an LLM surgeon actor for validation, and a React
operator dashboard.

## Current Baseline

- Runtime startup is explicit through the `live`, `llm-surgeon`, and `replay`
  deployment profiles; `scripts/taskplanner up` defaults to `live`, the actual
  external-integration profile. Live keeps its required read-only Debug
  observation sidecar running, while LLM Surgeon and Replay do not start camera
  observation by default. Provider catalogs start without loading generative
  model weights.
- Explicit voice tool requests remain operational when the VLM is absent or
  unhealthy. VLM-dependent phase inference, next-tool prediction, and
  mid-procedure Mayo classification remain fail-closed.
- Spoken commands use an open-natural-language / closed-typed-intent contract:
  the resolver may understand Korean paraphrases, but Digital Twin and BT own
  validation and execution. See
  [`docs/VOICE_COMMAND_CONTRACT.md`](docs/VOICE_COMMAND_CONTRACT.md).
- The LLM surgeon actor starts only in the `llm-surgeon` validation profile and
  generates public test stimuli such as speech, hand extension, Mayo placement,
  and procedure progress.
- The VLM must not receive hidden actor ground truth. It receives only public
  evidence: synthetic field image, visible Mayo tools, hand cue text, public
  voice transcript, digital-twin events, skill status, and BT context.
- Procedure bundles are driven by compact `vlm_procedure_prompt.yaml` files.
- Normal recovery is Mayo-stand based: surgeon hand -> Mayo stand -> robot left
  hand -> cleaner -> rack.
- A tool on Mayo can be requested again by voice or hand cue. The BT dispatches
  `pick_up_from_mayo_and_handover` through the right arm, so Mayo is a reusable
  surgeon-side pool rather than a recovery-only endpoint.
- The dashboard shows one Mayo stand. Each tool tag carries the latest
  VLM-derived reuse probability instead of separate recovery/reuse columns.
- `retrieve_from_hand` remains only as a legacy/manual path.
- Bleeding/hemostasis is modeled as an interrupt event, not as a normal
  sequential phase.
- Bed-mounted robot integration is retraction-only. Thyroidectomy tool changes
  use a blocking Service, nephrectomy fine adjustment uses a cancellable Action,
  and controller-owned arm state arrives as one documented status array.

## Repository Layout

- `src/surgical_msgs`: shared ROS messages, services, and action definitions.
- `src/model_provider_registry`: concurrent LM Studio, Unsloth Studio, vLLM,
  and NInfer discovery and lifecycle control with provider-scoped authentication.
- `src/procedure_spec`: procedure prompt loader, prior builder, and bundled
  procedure YAMLs.
- `src/or_digital_twin`: authoritative runtime reducer and world-state publisher.
- `src/simulation_runtime`: simulation manager plus rule-based and LLM surgeon
  actors.
- `src/vlm_node`: real VLM client, schema parsing, no-image camera, snapshot
  bridge, and legacy mock VLM.
- `src/taskplanner_bt_nodes`: C++ BehaviorTree.CPP custom nodes.
- `src/taskplanner_bt_trees`: Behavior Tree XML.
- `src/skill_execution`: mock skill action server and `/bt/skill_command` bridge.
- `src/surgical_interop_msgs`: external tool-handover and retraction-only ROS 2
  Service/Action/Topic definitions.
- `src/surgical_interop_execution`: fail-closed bridge and fault emulator for
  the external controller contracts.
- `src/surgical_interop_gateway`: read-only projection of accepted Taskplanner
  state for partner systems.
- `src/bringup`: launch files, smoke tests, edge probes, and multi-bundle probes.
- `webapp`: Vite/React operator dashboard.
- `reports`: generated validation reports. JSON/image outputs are ignored by Git.

## Procedure Bundles

Procedure bundles live under:

```text
src/procedure_spec/procedure_spec/specs/<procedure>/vlm_procedure_prompt.yaml
```

Available procedures:

- `thyroidectomy`: thyroidectomy partial scenario from the physician document.
- `thyroidectomy_demo`: public-demonstration thyroidectomy procedure used by
  timestamped Shadow replay cases.
- `nephrectomy`: open nephrectomy reference scenario.
- `inguinal_hernia_repair`: inguinal hernia repair reference scenario.

To add another surgery, create a new directory with one
`vlm_procedure_prompt.yaml`, then add it to
`src/procedure_spec/procedure_spec/specs/display_catalog.yaml`. The dashboard
and runtime bundle switch use this YAML-driven catalog.

### Bed-Mounted Retraction Arm Service

All retractor-arm requests use the single
`/surgery/retraction/command` (`ExecuteRetractionCommand`) Service. Its Request
contains `protocol_version`, `source_id`, `command_id`, `command`,
`target_side`, and `distance_m`; the six command constants cover direct-teach
start/finish, retraction start/adjust/stop, and tool change. A 5 cm adjustment,
for example, is `COMMAND_ADJUST_RETRACTION`, `TARGET_LEFT` or `TARGET_RIGHT`,
and `distance_m=0.050`.

The Response (`request_accepted`, `result_code`, `command_id`, `message`) is an
admission response only. It does not indicate physical completion, progress,
controller state, or tool attachment. The downstream controller owns those
implementation details as well as coordinate conversion, collision and force
control, E-stop, and distance-limit handling.
Controller state is consumed from
`/external/bed_robot_arms/status` (`BedRobotArmStateArray`): `stamp`,
`revision`, `procedure_type`, and an `arms` array whose entries contain only
`arm_id`, `role`, `role_instance_id`, `state`, `direct_teach_active`, and
`reason_code`.

There is no bed-mounted suction robot-arm control path. The surgical suction
instrument and public surgeon speech about suction remain part of the normal
clinical/tool evidence model.

The operator dashboard shows request-correlated speech, VLM interpretation, BT
validation, retraction Service admission, and controller-owned retraction-arm
status at <http://127.0.0.1:4173/> locally and
<http://192.168.1.4:4173/> on the reviewed wired LAN.

## External Dependency

This repository intentionally does not vendor `btops_ws`. Docker builds use a
local BT Ops checkout as a named build context.

```text
https://github.com/Radon213/btops_ws.git
```

Create `.env` from the example:

```bash
cp .env.example .env
```

Typical local settings:

```bash
BTOPS_LOCAL_CONTEXT=../btops_ws
BTOPS_REF=cda8abb706d1c5c4132f7661d83a33c4c5b65e9c
AUTO_APMS_REPO_URL=https://github.com/AutoAPMS/auto-apms.git
AUTO_APMS_REF=19ac8d558e35f657b8464694c5ddc524c6c31861
VLM_BASE_URL=http://127.0.0.1:8001
VLM_PROVIDER_ID=vllm
VLM_API_KEY=
VLM_MODEL_ID=unsloth/gemma-4-E4B-it-NVFP4
ACTOR_BASE_URL=http://127.0.0.1:1234
ACTOR_PROVIDER_ID=lmstudio
ACTOR_API_KEY=
ACTOR_MODEL_ID=google/gemma-4-12b-qat
LMSTUDIO_BASE_URL=http://127.0.0.1:1234
LMSTUDIO_PROVIDER_MANAGED=true
UNSLOTH_BASE_URL=http://127.0.0.1:8888
UNSLOTH_API_KEY=
UNSLOTH_PROVIDER_MANAGED=true
VLLM_BASE_URL=http://127.0.0.1:8001
VLLM_API_KEY=
VLLM_PROVIDER_MANAGED=true
NINFER_BASE_URL=http://127.0.0.1:8080
NINFER_API_KEY=
```

The runtime registers LM Studio, Unsloth Studio, vLLM, and NInfer as independent
model providers and queries them concurrently. LM Studio discovery prefers
`/api/v1/models`, so the dashboard can distinguish loaded and unloaded models;
it falls back to `/v1/models` on older servers. A failed or stopped provider is
reported separately and does not hide models from the other providers. The
dashboard keeps duplicate model IDs distinct by the `provider_id + model_id`
pair and switches endpoint, credential, and model atomically through ROS.

Set provider-scoped keys such as `UNSLOTH_API_KEY` or `VLLM_API_KEY` when that
server requires Bearer authentication. `VLM_API_KEY` and `ACTOR_API_KEY` remain
compatibility fallbacks for the initially selected endpoint. API keys stay
inside the ROS runtime and are never returned by the model-catalog services or
sent to the browser. On a VRAM-constrained host, both roles may select the same
provider and model to share one loaded set of weights.

NInfer is exposed through the Compose-managed `ninfer-manager` control plane.
Its local artifact catalog remains visible while no worker is loaded; selecting
a valid `.ninfer` artifact starts the compatible NInfer worker on demand.
Taskplanner maps its no-reasoning policy to NInfer's `enable_thinking=false`
extension and relies on prompt-level JSON instructions because NInfer does not
enforce client JSON Schema. The control plane is a normal runtime dependency,
not a host user service, so it is available from both `scripts/taskplanner up`
and profile-aware `docker compose up` commands without loading model weights.

The web UI refreshes both provider-aware model selectors every five seconds.
`/real_vlm_node/list_model_catalog` and
`/surgeon_actor/list_model_catalog` expose provider health, load metadata when
available, selectable models, and the lifecycle actions supported by each
provider. Selecting an unloaded managed model starts loading it in the
background. The adjacent icon controls can also load or unload LM Studio and
Unsloth Studio models directly. Managed vLLM models additionally support sleep
and wake. Runtime states include `loaded`, `loading`, `sleeping`, `waking`,
`unloading`, and `error`. The older `list_models` services remain for
compatibility.

Taskplanner uses each provider's native lifecycle API:

- LM Studio: `POST /api/v1/models/load` and
  `POST /api/v1/models/unload`.
- Unsloth Studio: `POST /v1/load` and `POST /v1/unload`; a configured
  `repository:GGUF_VARIANT` model ID is split into `model_path` and
  `gguf_variant`.
- vLLM: the Taskplanner manager's `/manager/load`, `/manager/unload`,
  `/manager/sleep`, and `/manager/wake` endpoints.

Set `LMSTUDIO_PROVIDER_MANAGED=false` or
`UNSLOTH_PROVIDER_MANAGED=false` to keep discovery enabled while disabling
Taskplanner lifecycle control for that application.

### vLLM manager

The common `vllm-manager` service keeps a small API and proxy online at
`127.0.0.1:8001` while the heavyweight vLLM worker is stopped. Selecting an
unloaded managed vLLM model starts the worker asynchronously; selecting a
sleeping model wakes it. The dashboard can also load, sleep, wake, or unload the
selected vLLM model through ROS without exposing `VLLM_API_KEY` to the browser.
The manager advertises every locally cached model in
`docker/vllm-manager/models.json`, but runs at most one worker at a time. A
different selection performs a controlled unload/load switch with that model's
own context, memory, multimodal, and reasoning-parser settings.

Start the manager directly, without loading model weights:

```bash
docker compose up -d vllm-manager
```

Inspect its lightweight health endpoint:

```bash
curl http://127.0.0.1:8001/health
```

The default worker listens only on `127.0.0.1:8002`; OpenAI-compatible clients
continue to use the manager at `VLLM_BASE_URL=http://127.0.0.1:8001`.
`VLLM_MANAGER_AUTO_START=false` keeps GPU memory free after boot. On a
VRAM-constrained machine, unload or sleep models running in other providers
before loading a vLLM worker. Catalog entries marked `local_only` remain visible
but cannot be selected until their weights exist in the mounted Hugging Face
cache. `VLLM_CACHE_DIR` is mounted separately so compiled kernels and autotuning
results survive manager-container recreation.

### NInfer manager

The common `ninfer-manager` service keeps its catalog and lifecycle API online
at `127.0.0.1:8080` while every NInfer worker is stopped. Configure the
host-managed NInfer installation and CUDA toolkit in `.env`:

```text
NINFER_RUNTIME_ROOT=/absolute/path/to/ninfer
NINFER_CUDA_ROOT=/usr/local/cuda-13.1
NINFER_35B_ARTIFACT_REL=models/qwen3_6_35b_a3b.ninfer
```

The manager only advertises artifact files that actually exist beneath
`NINFER_RUNTIME_ROOT`; it never downloads them and it never auto-loads a model.
Selecting an available model from either dashboard selector calls its lifecycle
API and starts a single worker on `127.0.0.1:8082`. The worker is configured
with thinking disabled. Starting or switching a Taskplanner profile recreates
the manager with no worker, returning GPU memory to the host until an operator
explicitly selects a model.

Start the control plane directly, without loading model weights:

```bash
docker compose --profile live up -d ninfer-manager
curl http://127.0.0.1:8080/health
```

For the default real-mode demo, LM Studio should expose an OpenAI-compatible
server at `http://127.0.0.1:1234`.

## Deployment Quickstart

Copy the environment template, then start exactly one runtime profile:

```bash
cp .env.example .env
scripts/taskplanner up live
scripts/taskplanner up llm-surgeon
SHADOW_CASE_ID=0704_6 scripts/taskplanner up replay
scripts/taskplanner up debug --build
```

Use `--build` for a first deployment or after dependency/interface changes:

```bash
scripts/taskplanner up live --build
```

Inspect or stop the complete stack with:

```bash
scripts/taskplanner status
scripts/taskplanner down
```

Open:

```text
http://127.0.0.1:4173
http://192.168.1.4:4173
```

외부 기관과 전체 플래너 없이 ROS 입출력, 수동 Action/Service, 리트랙터
조그, 더미 공개 토픽, 텍스트·마이크 문장을 확인할 때는 통합 **디버그
모드**를 사용한다. 같은 UI를 `http://127.0.0.1:4173` 또는 유선 LAN의
`http://192.168.1.4:4173`에서 사용하며, 전용
ROSBridge는 `ws://127.0.0.1:9091`에서 열린다. 종단 목록, 안전 동작 및
상대 기관 확인 절차는
[`docs/INTEGRATION_DEBUG_MODE.md`](docs/INTEGRATION_DEBUG_MODE.md)를 따른다.

When the dashboard is opened through another hostname, it connects to
rosbridge on that same hostname and the selected mode's configured port. A
Tailscale IPv4 browser uses the reviewed path routes `/live`, `/llm`, and
`/shadow` on the existing permitted port; it does not need direct access to
the replay bridge port. Set `VITE_ROSBRIDGE_URL` only when an explicit
websocket endpoint is required.

After any normal `scripts/taskplanner up <mode>` start, the dashboard's
**Runtime mode** selector can switch among Live, LLM surgeon, Replay, and
Debug without another terminal command. The selector asks a transient,
loopback-only, token-gated host service to run the same allowlisted launcher
profiles documented below, shows startup/retry state, then reconnects ROS
automatically. A mode switch stops the previous profile before starting the
next one; it does not send a robot Action goal.

The selected profile launches the appropriate subset of:

- `real_vlm`
- optional `llm_surgeon_actor`
- `no_image_camera`
- `or_digital_twin`
- BT executor and decision bridge
- mock or external skill action bridge
- rosbridge websocket

Use `VLM_MODE=voice_only` for an explicit voice-only deployment. A real-mode
runtime also continues accepting explicit voice requests while VLM evidence is
temporarily unavailable.

### Sentence-only operation

An external system publishes one completed surgeon sentence as
`std_msgs/msg/String` on `/sensors/surgeon/sentence`. The sentence adapter trims
the message, suppresses short-window duplicates, and republishes admitted text
on the internal compatibility topic `/surgery/audio/request_text`. The digital twin resolves
explicit tool requests against the active procedure YAML and passes the
canonical tool id to the BT. An exact retraction-arm cue from the YAML is routed
to the retraction control lane instead of being mistaken for a handover. A
suction utterance remains clinical/tool evidence and is never converted into a
bed-mounted arm command.

When VLM health is unavailable, a sentence-backed request may bypass only the
`vlm_unhealthy` and phase-uncertain inference gates. Tool existence, active
bundle membership, contamination, ownership, robot/cleaner occupancy, surgeon
two-tool capacity, and handover readiness remain mandatory. Autonomous phase
updates, predicted-tool preparation, and probabilistic Mayo recovery do not run
without VLM evidence. Set `VLM_MODE=voice_only` to avoid launching a VLM node;
the same sentence-only behavior is entered automatically while `VLM_MODE=real` is
temporarily unhealthy.

For external sentence/camera/robot integration over a wired LAN, use
`taskplanner_live.launch.py` or `INPUT_PROFILE=external` with Compose, then follow
[`docs/EXTERNAL_INPUT_CONTRACT.md`](docs/EXTERNAL_INPUT_CONTRACT.md).

## Local Build

Inside the container or a host with ROS 2 Jazzy and BT Ops sourced:

```bash
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
colcon build --symlink-install --cmake-args -DBUILD_TESTING=OFF
source install/setup.bash
```

Webapp build:

```bash
cd webapp
npm install
npm run build
```

## Launch

```bash
ros2 launch bringup taskplanner_mock.launch.py
```

Fail-closed external integration:

```bash
ros2 launch bringup taskplanner_live.launch.py
```

Important launch arguments:

- `input_profile`: `simulation` or `external`; default `simulation`.
- `execution_backend`: `mock` or `external`; default `mock`.
- `speech_input_mode`: `utterance` for validation/Shadow or `sentence_text`
  for external integration.
- `sentence_input_topic`: default `/sensors/surgeon/sentence`.
- `vlm_mode`: `real`, `mock`, `dual`, or `voice_only`; default `real`.
- `vlm_base_url`: OpenAI-compatible VLM server URL.
- `vlm_model_id`: model selected for VLM inference.
- `vlm_response_format`: default `json_schema`.
- `surgeon_actor_mode`: `llm` or `rule`; default `llm`.
- `actor_base_url`: OpenAI-compatible actor LLM server URL.
- `actor_model_id`: model selected for the surgeon actor.
- `enable_no_image_camera`: default `true`.
- `enable_synthetic_scene_camera`: default `false`.
- `spec_dir`: procedure bundle directory; default thyroidectomy.

## Validation Commands

The supported release entry point is:

```bash
scripts/taskplanner verify-release --tier quick
scripts/taskplanner verify-release --tier rc
scripts/taskplanner verify-release --tier full
```

The command always writes an auditable JSON/CSV/Markdown/SVG report bundle.
The `full` defaults are 100 restart cycles and a 24-hour soak. Recorded-surgery
evaluation can be attached to the same command with explicit, read-only
dataset and annotation roots; models are never loaded automatically. See
[`docs/RELEASE_VERIFICATION.md`](docs/RELEASE_VERIFICATION.md) for the gates,
thresholds, external-asset handling, and the separate physical-site approval
boundary.

Focused developer probes remain available after sourcing the workspace:

```bash
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
source install/setup.bash

ros2 run bringup taskplanner_edge_probe
ros2 run bringup taskplanner_smoke_test --spec-name thyroidectomy
ros2 run bringup taskplanner_smoke_test --spec-name nephrectomy
ros2 run bringup taskplanner_smoke_test --spec-name inguinal_hernia_repair
ros2 run bringup taskplanner_multi_bundle_runtime_probe --duration-sec 60
```

The historical `0.1.0` candidate was validated with:

- ROS workspace build for modified packages.
- `webapp` production build.
- 60-second runtime probe across thyroidectomy, nephrectomy, and inguinal
  hernia repair.
- Overlay leak check confirming hidden event-tool hints are not shown to VLM.

Those checks are retained as a historical baseline; they do not replace the
current release harness.

## Release

Current release:

- tag: `v0.1.0`
- release notes: `RELEASE_NOTES.md`
- default branch: `main`

Do not move the release tag for routine documentation edits. Use a new commit on
`main`; create a new tag only for the next functional release.
