# Isaac Sim 6.0.1 Host App Workflow

Run repository commands from a clone-local path:

```bash
cd /path/to/taskplanner_ws
export TASKPLANNER_WS_ROOT="$PWD"
```

## Current Decision

Use Isaac Sim as a host desktop app. Do not run Isaac Sim in Docker unless a
specific deployment test requires it.

Reason:

- Host ROS can receive `/surgery/images/field/image_raw` from Isaac at about
  60 Hz.
- The taskplanner Docker container can discover the topic, but image samples may
  fail to cross the host/container boundary unless Fast DDS transport is tuned.
- The VLM only needs low-rate compressed images, so keeping Isaac on the host is
  simpler and more reliable.

## Isaac Launch Environment

Run Isaac Sim from a terminal so ROS and NVIDIA offload settings are inherited:

```bash
export ROS_DOMAIN_ID=0
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export __NV_PRIME_RENDER_OFFLOAD=1
export __GLX_VENDOR_LIBRARY_NAME=nvidia
export __VK_LAYER_NV_optimus=NVIDIA_only

cd ~/isaacsim-6.0.1
./isaac-sim.sh
```

If Docker must consume Isaac topics directly, also set Fast DDS UDP transport in
the Isaac terminal before launching:

```bash
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export FASTRTPS_DEFAULT_PROFILES_FILE="$TASKPLANNER_WS_ROOT/config/fastdds_udp.xml"
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
```

## Isaac Action Graph Settings

Use the Isaac Sim menu-generated camera graph as the baseline:

```text
Tools > Robotics > ROS 2 OmniGraphs > Camera
```

The graph should include:

```text
On Playback Tick
Isaac Run One Simulation Frame
Isaac Create Render Product
ROS2 Context
ROS2 Camera Helper
```

Important settings:

```text
ROS2 Context.domain_id = 0
ROS2 Context.useDomainIDEnvVar = checked

Create Render Product.cameraPrim = /World/Cameras/vlm_overview_camera
Create Render Product.renderProductPrim = empty
Create Render Product.width = 1280
Create Render Product.height = 720

ROS2 Camera Helper.topicName = /surgery/images/field/image_raw
ROS2 Camera Helper.type = rgb
ROS2 Camera Helper.frameId = vlm_overview_camera
ROS2 Camera Helper.renderProductPath = Create Render Product.outputs:renderProductPath
```

Do not set `renderProductPrim` to the `RGBPublish` node. In Isaac Sim 6.0.1, an
empty `renderProductPrim` lets the node create a new render product. Setting it
to the wrong existing prim can create a publisher with no image samples.

## Host Verification

With the Isaac timeline playing:

```bash
source /opt/ros/<host_ros_distro>/setup.bash
export ROS_DOMAIN_ID=0
ros2 topic hz /surgery/images/field/image_raw
```

Expected:

```text
average rate: about 60 Hz
```

## Taskplanner Runtime

For direct Docker DDS testing:

```bash
cd "$TASKPLANNER_WS_ROOT"
unset DOCKER_HOST

ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET \
RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
FASTRTPS_DEFAULT_PROFILES_FILE=/workspaces/taskplanner_ws/config/fastdds_udp.xml \
ENABLE_NO_IMAGE_CAMERA=false \
ENABLE_SYNTHETIC_SCENE_CAMERA=false \
VLM_MODE=mock \
SURGEON_ACTOR_MODE=none \
docker compose up -d --force-recreate taskplanner-runtime
```

Check inside Docker:

```bash
docker compose exec -T taskplanner-runtime bash -lc \
'source install/setup.bash && ros2 topic info -v /surgery/images/field/image_raw'

docker compose exec -T taskplanner-runtime bash -lc \
'source install/setup.bash && timeout 10 ros2 topic hz /surgery/images/field/image_raw'
```

If Docker still discovers the publisher but receives no samples, avoid spending
more time on direct DDS for the VLM path. Use a host-side bridge to publish
compressed frames into taskplanner via rosbridge or another explicit transport.

## Recommended Isaac-to-VLM Bridge

Run this on the host ROS environment that can receive Isaac frames:

```bash
cd "$TASKPLANNER_WS_ROOT"
source /opt/ros/<host_ros_distro>/setup.bash
export ROS_DOMAIN_ID=0
python3 tools/serve_image_raw_snapshot.py
```

It serves:

```text
http://127.0.0.1:8765/snapshot.jpg
http://127.0.0.1:8765/status
```

Then start the Docker runtime with the snapshot bridge enabled:

```bash
cd "$TASKPLANNER_WS_ROOT"
unset DOCKER_HOST

FIELD_SNAPSHOT_URL=http://127.0.0.1:8765/snapshot.jpg \
ENABLE_NO_IMAGE_CAMERA=false \
ENABLE_SYNTHETIC_SCENE_CAMERA=false \
VLM_MODE=real \
VLM_PUBLISH_PERIOD_SEC=1.0 \
VLM_IMAGE_STALE_SEC=3.0 \
SURGEON_ACTOR_MODE=none \
docker compose up -d --force-recreate taskplanner-runtime
```

Increase `VLM_PUBLISH_PERIOD_SEC` for slower models when inference takes longer
than the requested cadence.

Check that Docker now publishes compressed images:

```bash
docker compose exec -T taskplanner-runtime bash -lc \
'source install/setup.bash && ros2 topic hz /surgery/images/field/compressed'
```

## VLM Input Contract

`real_vlm_node` consumes:

```text
/surgery/images/field/compressed
sensor_msgs/msg/CompressedImage
```

Isaac publishes:

```text
/surgery/images/field/image_raw
sensor_msgs/msg/Image
```

Therefore the production test path needs one bridge:

```text
Isaac image_raw -> JPEG/PNG CompressedImage -> /surgery/images/field/compressed
```

The existing `tools/image_raw_to_compressed.py` uses Pillow, which is already
installed in the runtime image. It does not require OpenCV.
