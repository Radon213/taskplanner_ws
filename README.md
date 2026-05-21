# Taskplanner Workspace

ROS 2 Jazzy workspace for task-planning, digital-twin state management, mock/real VLM integration, Behavior Tree execution, and the React monitoring UI for surgical-assistance scenarios.

## Repository Layout

- `src/surgical_msgs`: ROS messages, services, and actions used across the system.
- `src/procedure_spec`: Procedure bundles such as `thyroidectomy` and `nephrectomy`.
- `src/or_digital_twin`: Authoritative runtime state model and reducer logic.
- `src/simulation_runtime`: Simulation manager, surgeon actor, and mock surgeon runtime.
- `src/vlm_node`: Mock VLM, real VLM, synthetic camera, and snapshot bridge.
- `src/taskplanner_bt_nodes`: C++ BehaviorTree.CPP custom nodes.
- `src/taskplanner_bt_trees`: Behavior tree XML resources.
- `src/skill_execution`: Mock skill execution and action bridge.
- `src/bringup`: Launch files, config, smoke tests, manual probes, and BT audit.
- `webapp`: Vite/React operator UI.
- `reports`: Human-readable validation reports and selected audit summaries.

Generated directories such as `build/`, `install/`, `log/`, `webapp/node_modules/`, and `reports/taskplanner_validation_assets/` are intentionally excluded from Git.

## External Dependency

This repository intentionally does not vendor `btops_ws`. Docker builds require a separate BT Ops repository URL.

Create `.env` from the example:

```bash
cp .env.example .env
```

Set:

```bash
BTOPS_REPO_URL=<git url for btops_ws source repository>
BTOPS_REF=main
```

If the BT Ops repository is private, ensure Docker has access to the repository credentials before building.

## Docker Quickstart

Build the development image:

```bash
docker compose build --build-arg BTOPS_REPO_URL=<btops_repo_url>
```

Build inside the container:

```bash
docker compose run --rm taskplanner-dev bash
colcon build --symlink-install
cd webapp && npm ci && npm run build
```

Run the mock runtime and web UI:

```bash
docker compose up taskplanner-runtime webapp
```

Open:

```text
http://127.0.0.1:4173
```

The default runtime uses `vlm_mode=mock`. Real VLM/LM Studio integration can be enabled later through environment variables and launch arguments.

## Local ROS Usage

If ROS 2 Jazzy and BT Ops are already installed locally:

```bash
source /opt/ros/jazzy/setup.bash
source /home/arl/btops_ws/install/setup.bash
cd /home/arl/taskplanner_ws
colcon build --symlink-install
source install/setup.bash
ros2 launch bringup taskplanner_mock.launch.py
```

Run the UI separately:

```bash
cd /home/arl/taskplanner_ws/webapp
npm install
npm run dev -- --host 127.0.0.1 --port 4173
```

## Validation Commands

```bash
source /opt/ros/jazzy/setup.bash
source /home/arl/btops_ws/install/setup.bash
source /home/arl/taskplanner_ws/install/setup.bash

ros2 run bringup taskplanner_smoke_test --spec-name thyroidectomy
ros2 run bringup taskplanner_smoke_test --spec-name nephrectomy
ros2 run bringup taskplanner_manual_probe --spec-name thyroidectomy
ros2 run bringup taskplanner_manual_probe --spec-name nephrectomy
ros2 run bringup taskplanner_bt_audit --spec-name thyroidectomy
ros2 run bringup taskplanner_bt_audit --spec-name nephrectomy

cd /home/arl/taskplanner_ws/webapp
npm run build
```

## Collaboration Workflow

- Default branch: `main`
- Feature branches: `feature/<topic>`
- Fix branches: `fix/<topic>`
- Docs branches: `docs/<topic>`

Pull requests should pass:

- `colcon build --symlink-install`
- `cd webapp && npm run build`
- `ros2 run bringup taskplanner_smoke_test --spec-name thyroidectomy`
- `ros2 run bringup taskplanner_smoke_test --spec-name nephrectomy`

## Note on `src/rosbridge_suite`

`src/rosbridge_suite` is currently present in the workspace as a vendored source tree. If it still contains its own `.git` directory, convert it to normal vendored source before the first repository commit, or intentionally register it as a submodule. For this repository plan, the intended default is normal vendored source.
