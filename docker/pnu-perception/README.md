# PNU Hand / Tool / Blood worker

This opt-in worker keeps Taskplanner's PyTorch 2.8 / CUDA 12.9 / RF-DETR 1.9
environment. It imports algorithm code from a pinned, **read-only**
`hand-blood-tools` checkout and reads all three model assets from a separate
**read-only** model mount. No checkpoint or third-party source is baked into
the image.

Startup requires both the pinned HEAD and SHA-256 matches for every Python file
the worker can load plus the Tool ontology. This standard-library manifest check
does not require `git` in the runtime image. Modified/missing source, extra
Python/native-extension artifacts, and ignored `__pycache__`/`*.pyc` files in
the executable source roots are rejected before any upstream import.

The Taskplanner Compose profile uses the provider artifact filenames below:

- `cam4_rfdetr_seg_small_regular_resume_e13_best.pth`
- `blood_detection.pth`
- `hand_landmarker.task`

The standalone worker's generic defaults remain `tool.pth` and `blood.pth`.
The `docker run` examples therefore set the two checkpoint paths explicitly so
the local and remote deployment contracts use the same verified artifacts as
Compose.

Build after the unified compatibility image:

```bash
docker build \
  --file docker/pnu-perception/Dockerfile \
  --tag taskplanner-pnu-perception:0.1 \
  .
```

Local, loopback-only run:

```bash
docker run --rm --gpus all \
  --publish 127.0.0.1:8020:8020 \
  --user "$(id -u):$(id -g)" \
  --env PNU_API_TOKEN_FILE=/run/secrets/pnu_api_token \
  --env PNU_TOOL_CHECKPOINT=/models/cam4_rfdetr_seg_small_regular_resume_e13_best.pth \
  --env PNU_BLOOD_CHECKPOINT=/models/blood_detection.pth \
  --volume /path/to/token:/run/secrets/pnu_api_token:ro \
  --volume /path/to/hand-blood-tools:/opt/hand-blood-tools:ro \
  --volume /path/to/pnu-models:/models:ro \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  taskplanner-pnu-perception:0.1
```

The image binds `0.0.0.0` inside its network namespace, so it requires a bearer
token even when Docker publishes the port only on host loopback. A native worker
may omit the token only when its actual Uvicorn bind is `127.0.0.1`, `::1`, or
`localhost`; wildcard and LAN binds fail startup without `PNU_API_TOKEN_FILE`.

This worker does not terminate TLS. The default remote Taskplanner policy
therefore publishes the worker only on host loopback and puts a reviewed TLS
reverse proxy on the wired-LAN interface. The proxy forwards to
`127.0.0.1:8020`; its certificate SAN must match the Taskplanner endpoint DNS
name and its chain must be trusted by the bridge container. Mount the same
Bearer token on both sides. Run with the model-file owner's UID/GID because the
reviewed assets are mode `0600`.

```bash
docker run --rm --gpus all \
  --publish 127.0.0.1:8020:8020 \
  --user "$(id -u):$(id -g)" \
  --env PNU_API_TOKEN_FILE=/run/secrets/pnu_api_token \
  --env PNU_TOOL_CHECKPOINT=/models/cam4_rfdetr_seg_small_regular_resume_e13_best.pth \
  --env PNU_BLOOD_CHECKPOINT=/models/blood_detection.pth \
  --volume /path/to/token:/run/secrets/pnu_api_token:ro \
  --volume /path/to/hand-blood-tools:/opt/hand-blood-tools:ro \
  --volume /path/to/pnu-models:/models:ro \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --security-opt no-new-privileges --cap-drop ALL \
  taskplanner-pnu-perception:0.1
```

Before selecting this endpoint on Taskplanner, verify that both hosts are time
synchronized (chrony/NTP), that the wired route is selected, and that only the
Taskplanner host can reach the TLS proxy port. Then require actual semantic
readiness:

```bash
python3 tools/pnu_live_preflight.py worker \
  --location remote \
  --endpoint https://PNU_WORKER_DNS_NAME:8443 \
  --api-token-file /path/to/token

PERCEPTION_PROVIDER=pnu_hand_blood \
PERCEPTION_LOCATION=remote \
PERCEPTION_ENDPOINT=https://PNU_WORKER_DNS_NAME:8443 \
PNU_SERVICE_URL=https://PNU_WORKER_DNS_NAME:8443 \
PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
PNU_ALLOW_INSECURE_REMOTE_HTTP=false \
PNU_ALLOW_UNAUTHENTICATED_REMOTE=false \
REQUIRE_PERCEPTION_ON_START=true \
scripts/taskplanner up live --build
```

The health response must have global `ready=true`; a reachable server or
Blood/Hand-only `partial_ready` result is not sufficient.

For a bounded test on an isolated trusted wired LAN before the TLS proxy is
available, plain HTTP requires the independent development opt-in below. It
does not disable Bearer authentication:

```bash
python3 tools/pnu_live_preflight.py worker \
  --location remote \
  --endpoint http://WORKER_LAN_IP:8020 \
  --api-token-file /path/to/token \
  --allow-insecure-remote-http

PERCEPTION_ENDPOINT=http://WORKER_LAN_IP:8020 \
PNU_SERVICE_URL=http://WORKER_LAN_IP:8020 \
PNU_ALLOW_INSECURE_REMOTE_HTTP=true \
PNU_ALLOW_UNAUTHENTICATED_REMOTE=false \
scripts/taskplanner up live --build
```

Publish `WORKER_LAN_IP:8020:8020` only for that bounded test and restrict the
source to the Taskplanner wired IP with the host firewall. Never use this
exception on a routed or shared network.

Configuration contract:

| Variable | Default | Meaning |
|---|---|---|
| `PNU_UPSTREAM_ROOT` | `/opt/hand-blood-tools` | read-only pinned source checkout |
| `PNU_EXPECTED_UPSTREAM_COMMIT` | `0f9e93115b8cc1d470398c92e010e3fc6ef1de5d` | exact accepted source revision |
| `PNU_MODEL_ROOT` | `/models` | read-only model root |
| `PNU_TOOL_CHECKPOINT` | `$PNU_MODEL_ROOT/tool.pth` | Tool RF-DETR checkpoint |
| `PNU_BLOOD_CHECKPOINT` | `$PNU_MODEL_ROOT/blood.pth` | Blood RF-DETR checkpoint |
| `PNU_HAND_MODEL` | `$PNU_MODEL_ROOT/hand_landmarker.task` | MediaPipe task asset |
| `PNU_HOST` / `PNU_PORT` | `0.0.0.0` / `8020` in image | listen address |
| `PNU_API_TOKEN_FILE` | unset | read-only bearer-token file; mandatory for wildcard/LAN binds |
| `PNU_DEVICE_POLICY` | `cuda_required` | fail load rather than silently using RF-DETR CPU |
| `PNU_MAX_DECODED_RGB_BYTES` | `16777216` | maximum PNG/JPEG decoded allocation accepted by header preflight |
| `PNU_DEPTH_MIN_M` / `PNU_DEPTH_MAX_M` | `0.05` / `10.0` | accepted metric depth range after validated scale conversion |
| `PNU_TOOL_SUPPORT_PLANE_NORMAL` | `0.049679...,0.060100...,-0.996955...` | comma-separated CAM4-frame plane normal; reference value is provisional |
| `PNU_TOOL_SUPPORT_PLANE_OFFSET_M` | `0.7951867203` | support-plane offset in `normal @ point + offset = 0` |
| `PNU_TOOL_SUPPORT_PLANE_CONFIG_VERSION` | `reference_mcap_first_frame_blue_plane_provisional` | provenance identifier emitted with Tool pose evidence |
| `PNU_TOOL_SUPPORT_PLANE_INLIER_RATIO` | `0.7407333333` | support-plane fit evidence |
| `PNU_TOOL_SUPPORT_PLANE_RESIDUAL_P95_M` | `0.0054806478` | support-plane fit residual evidence |
| `PNU_TOOL_SUPPORT_PLANE_VALIDATED` | `false` | when false, Tool orientation is marked invalid/degraded even though depth-backed position can be reported |

API:

- `GET /v1/health`: readiness and per-model load errors. Missing models are
  `degraded`, never represented as an empty detection result.
- `GET /v1/capabilities`: schemas, digests, limits, transport, queue policy.
  When a bearer token is configured this read-only endpoint also requires it,
  so preflight can prove the client/worker token match without sending images.
- `POST /v1/infer`: multipart fields `metadata` (application/json), `rgb`
  (JPEG/PNG bytes), and optional `depth` (`16UC1; compressedDepth png` bytes).

Minimal RGB-only request metadata:

```json
{
  "schema": "taskplanner.pnu_perception.request.v1",
  "request_id": "cam4-000001",
  "source": {
    "rgb": {
      "stamp_ns": 1725000123456789000,
      "frame_id": "cam_4_color_optical_frame",
      "format": "jpeg"
    }
  },
  "requested_algorithms": ["tool", "blood", "hand"],
  "deadline_unix_ms": 1787242400000
}
```

The deadline must be regenerated for each request. A direct diagnostic call
is binary multipart (never base64):

```bash
curl --fail-with-body http://127.0.0.1:8020/v1/infer \
  --header 'Authorization: Bearer REPLACE_WITH_TOKEN' \
  --form 'metadata=@/tmp/request.json;type=application/json' \
  --form 'rgb=@/tmp/cam4.jpg;type=image/jpeg'
```

The worker has one in-flight slot and no request queue. It returns 429 while
busy so the ROS adapter can discard superseded input and retain the latest
frame. A real inference that finds zero objects returns HTTP 200 with
`models.<name>.status=executed` and an empty result list. Missing/unloaded
requested models return HTTP 503.

Metric 3-D is enabled without changing the top-level request/response v1
envelope. It is fail-closed unless all of the following evidence is present:

- `source.depth.aligned=true` and its `frame_id` equals the RGB frame;
- `alignment={"validated":true,"id":"..."}` with a non-empty ID;
- a color `CameraInfo`, an RGB-grid depth `CameraInfo` with exactly matching
  frame/dimensions/calibration fields, and matching decoded RGB/depth dimensions;
- `depth_scale_validated=true` and a positive `depth_scale_m_per_unit`.

Already RGB-aligned depth does not require a depth-to-color extrinsics gate.
Malformed compressedDepth is rejected as `invalid_depth`; missing, native, or
unvalidated depth remains a valid 2-D request and never fabricates metric data.
The response preserves `metric_3d={ready,reasons}` and adds exact
`depth_evidence`. Algorithm payloads use `pnu.{tool,blood,hand}.rgbd.v1` only
after the input gates pass; otherwise their existing `.2d.v1` schemas remain.

Minimal aligned-depth additions to the request are:

```json
{
  "source": {
    "rgb": {
      "stamp_ns": 1725000123456789000,
      "frame_id": "cam_4_color_optical_frame",
      "format": "jpeg"
    },
    "depth": {
      "stamp_ns": 1725000123456789000,
      "frame_id": "cam_4_color_optical_frame",
      "format": "16UC1; compressedDepth png",
      "aligned": true
    }
  },
  "alignment": {"validated": true, "id": "viplab-cam4-align-v1"},
  "depth_scale_m_per_unit": 0.001,
  "depth_scale_validated": true,
  "color_camera_info": {
    "stamp_ns": 1725000123456789000,
    "frame_id": "cam_4_color_optical_frame",
    "width": 1280,
    "height": 720,
    "distortion_model": "plumb_bob",
    "d": [0, 0, 0, 0, 0],
    "k": [900, 0, 640, 0, 900, 360, 0, 0, 1],
    "r": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    "p": [900, 0, 640, 0, 0, 900, 360, 0, 0, 0, 1, 0]
  },
  "depth_camera_info": {
    "stamp_ns": 1725000123456789000,
    "frame_id": "cam_4_color_optical_frame",
    "width": 1280,
    "height": 720,
    "distortion_model": "plumb_bob",
    "d": [0, 0, 0, 0, 0],
    "k": [900, 0, 640, 0, 900, 360, 0, 0, 1],
    "r": [1, 0, 0, 0, 1, 0, 0, 0, 1],
    "p": [900, 0, 640, 0, 0, 900, 360, 0, 0, 0, 1, 0]
  }
}
```

The pinned upstream algorithms then provide constrained Tool pose evidence,
Hand 3-D keypoints/palm pose, and Blood centroid depth. Tool orientation stays
degraded until the deployed CAM4 support plane is independently validated.
`metric_3d.ready=true` means the alignment/calibration gates passed and the
frame contained at least one valid metric-depth sample. An all-zero/out-of-range
depth frame falls back to 2-D with `depth_has_no_valid_samples`; a valid-depth
frame with zero detections remains a successful RGB-D execution. Consumers must
still honor Tool `position_valid`/`orientation_valid`, Hand
`kp_valid_depth`/nullable `palm_6d`, and nullable Blood centroid depths. Blood
keeps the mathematical 2-D mask centroid and associates it with the median of
finite positive mask-internal depths, so concave/disjoint masks never sample
background at the centroid. Hand metric rays are undistorted with the supplied
CAM4 `plumb_bob` calibration before joints and palm pose are recomputed.
