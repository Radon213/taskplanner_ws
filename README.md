# Taskplanner Workspace

Version: `0.1.0`

ROS 2 Jazzy workspace for a surgical tool-handover task planner. The current
baseline combines a real VLM path, an OR digital twin, Behavior Tree decisions,
mock robot skill execution, an LLM surgeon actor for validation, and a React
operator dashboard.

## Current Baseline

- Default VLM mode is `real`, targeting an OpenAI-compatible local LM Studio
  endpoint.
- Default surgeon actor mode is `llm`, used only to generate public test
  stimuli such as speech, hand extension, Mayo placement, and procedure progress.
- The VLM must not receive hidden actor ground truth. It receives only public
  evidence: synthetic field image, visible Mayo tools, hand cue text, public
  voice transcript, digital-twin events, skill status, and BT context.
- Procedure bundles are driven by compact `vlm_procedure_prompt.yaml` files.
- Normal recovery is Mayo-stand based: surgeon hand -> Mayo stand -> robot left
  hand -> cleaner -> rack.
- `retrieve_from_hand` remains only as a legacy/manual path.
- Bleeding/hemostasis is modeled as an interrupt event, not as a normal
  sequential phase.

## Repository Layout

- `src/surgical_msgs`: shared ROS messages, services, and action definitions.
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
- `nephrectomy`: open nephrectomy reference scenario.
- `inguinal_hernia_repair`: inguinal hernia repair reference scenario.

To add another surgery, create a new directory with one
`vlm_procedure_prompt.yaml`, then add it to
`src/procedure_spec/procedure_spec/specs/display_catalog.yaml`. The dashboard
and runtime bundle switch use this YAML-driven catalog.

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
BTOPS_REF=main
AUTO_APMS_REPO_URL=https://github.com/AutoAPMS/auto-apms.git
AUTO_APMS_REF=1.5.1
VLM_BASE_URL=http://127.0.0.1:1234
VLM_MODEL_ID=qwen3.6-35b-a3b-mtp@q2_k_xl
ACTOR_BASE_URL=http://127.0.0.1:1234
ACTOR_MODEL_ID=google/gemma-4-12b-qat
```

For the default real-mode demo, LM Studio should expose an OpenAI-compatible
server at `http://127.0.0.1:1234`.

## Docker Quickstart

Build the image:

```bash
docker compose build
```

Run the ROS runtime and dashboard:

```bash
docker compose up taskplanner-runtime webapp
```

Open:

```text
http://127.0.0.1:4173
```

The runtime launches:

- `real_vlm`
- `llm_surgeon_actor`
- `no_image_camera`
- `or_digital_twin`
- BT executor and decision bridge
- mock skill action server
- rosbridge websocket

To disable the LLM actor or real VLM for focused debugging, override environment
variables such as `SURGEON_ACTOR_MODE=rule` or `VLM_MODE=mock`.

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

Important launch arguments:

- `vlm_mode`: `real`, `mock`, or `dual`; default `real`.
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
