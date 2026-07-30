# Taskplanner Workspace

Version: `0.1.0`

ROS 2 Jazzy workspace for a surgical tool-handover task planner. The current
baseline combines a real VLM path, an OR digital twin, Behavior Tree decisions,
mock robot skill execution, an LLM surgeon actor for validation, and a React
operator dashboard.

## Current Baseline

- Runtime startup is explicit through the `live`, `llm-surgeon`, and `replay`
  deployment profiles. Provider catalogs start without loading generative model
  weights.
- Explicit voice tool requests remain operational when the VLM is absent or
  unhealthy. VLM-dependent phase inference, next-tool prediction, and
  mid-procedure Mayo classification remain fail-closed.
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
- Bed-mounted arms are exposed only as the logical `suction` and `retraction`
  groups; the planner never selects or counts physical member arms.

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

### Bed Robot-Arm Group Contract

Retraction requests use exactly one group Action with one of `UP`, `DOWN`,
`LEFT`, `RIGHT`, `LEFT_RIGHT`, or `UP_DOWN`. Explicit distances are converted
to millimetres without planner-side clamping; qualitative distances are limited
to 1–30 mm and a request without a distance defaults to 10 mm. The downstream
group controller remains responsible for physical arm selection, collision and
force control, and final safety-limit rejection.

The operator dashboard shows request-correlated speech, VLM interpretation, BT
validation, group Action, and group status at <http://127.0.0.1:4173/>.

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

NInfer is exposed through a lightweight host control plane. Its configured
catalog remains visible while no worker is loaded; selecting a valid `.ninfer`
artifact starts the worker on demand. Taskplanner maps its no-reasoning policy
to NInfer's `enable_thinking=false` extension and relies on prompt-level JSON
instructions because NInfer does not enforce client JSON Schema.

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

For the default real-mode demo, LM Studio should expose an OpenAI-compatible
server at `http://127.0.0.1:1234`.

## Deployment Quickstart

Copy the environment template, then start exactly one runtime profile:

```bash
cp .env.example .env
scripts/taskplanner up live
scripts/taskplanner up llm-surgeon
SHADOW_CASE_ID=0704_6 scripts/taskplanner up replay
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
```

When the dashboard is opened through another hostname, such as a Tailscale
IPv4 address or MagicDNS name, it connects to rosbridge on that same hostname
and `ROSBRIDGE_PORT`. Set `VITE_ROSBRIDGE_URL` only when an explicit websocket
endpoint is required.

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
canonical tool id to the BT. An exact bed-arm cue from the YAML is routed to the
suction/retraction group instead of being mistaken for a handover.

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

The release candidate for `0.1.0` was validated with:

- ROS workspace build for modified packages.
- `webapp` production build.
- 60-second runtime probe across thyroidectomy, nephrectomy, and inguinal
  hernia repair.
- Overlay leak check confirming hidden event-tool hints are not shown to VLM.

## Release

Current release:

- tag: `v0.1.0`
- release notes: `RELEASE_NOTES.md`
- default branch: `main`

Do not move the release tag for routine documentation edits. Use a new commit on
`main`; create a new tag only for the next functional release.
