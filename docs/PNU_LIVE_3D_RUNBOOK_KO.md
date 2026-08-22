# PNU CAM3/CAM4 Live 3D 통합 및 운용 절차

기준: 2026-08-22 KST. 이 문서는
`hanwae-py/hand-blood-tools@0f9e93115b8cc1d470398c92e010e3fc6ef1de5d`
와 VIPLab CAM4 D455 serial `146222251000`을 대상으로 한다.

## 현재 판정

- VIPLab 운영 프로필은 CAM3/CAM4의 RGB, native depth, 두 CameraInfo와 각
  `depth_to_color` extrinsics를 발급한다. 1.7의 등록기는 카메라별 factory
  extrinsics로 native depth를 RGB grid에 투영하므로 별도 aligned-depth 영상
  토픽을 LAN으로 보내지 않는다. CAM1/CAM2/FLIR는 RGB-only 관제 입력이다.
- CAM3는 Mayo 쪽 Tool/planar pose, CAM4는 Tray 쪽 Tool/planar pose와 Hand/Blood를
  동시에 수행한다. 1.7의 단일 ingress만 full-rate `/synced` RGB-D를 구독하고
  각 worker에는 `/perception/ingress/cam_{3,4}`로 로컬 fan-out한다.
- Taskplanner의 PNU worker와 ROS bridge는 같은 PC 또는 LAN의 다른 PC에
  놓을 수 있다. 위치를 바꿔도 ROS 출력 계약은 같다. 자동 fallback은 없다.
- RGB-D 입력 게이트와 Tool pose 제어 게이트는 분리한다. `metric_3d_ready=true`는
  RGB grid의 metric depth를 쓸 수 있다는 뜻이지 로봇 pose 승인이라는 뜻이 아니다.
- CAM4 blue-tray support plane은 30개 exact RGB-D frame(210,000 points)으로
  보정했고 artifact SHA256과 version을 고정했다. fit inlier ratio는
  `0.854928571429`, residual p95는 `0.014164174501 m`이며 artifact는
  `2026-09-20T04:51:35Z`까지 유효하다. 매 frame runtime drift gate까지 통과한
  경우에만 camera-frame planar 4DoF orientation을 내보내며, 실패하면 즉시
  position-only로 강등한다. 이것은 자유 6DoF/robot-world/TCP 보정이 아니고
  Taskplanner 제어 입력으로 소비하지 않는다.
- Tool, Blood, Hand 자산은 모두 설치돼 있고 manifest의 size/SHA256 검사를
  통과한다. Tool checkpoint는 `133,941,485 bytes`, SHA256
  `253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430`으로
  고정한다. 세 자산 중 하나라도 없거나 digest가 다르면 worker health와
  required-perception startup gate는 fail-closed로 실패한다.

## 데이터 흐름

```text
VIPLab CAM3/CAM4
  RGB JPEG + native 16UC1 compressedDepth
  color/depth CameraInfo + retained depth_to_color extrinsics
        |
        | ROS 2 DDS, BEST_EFFORT latest-frame image
        v
1.7 perception_ingress (camera별 외부 구독 1개)
        |
        | 로컬 fan-out, exact source stamp
        v
CAM3 Tool + CAM4 Tool/Hand/Blood concurrent workers
        |
        | typed result + planar 6DoF TF (roll=pitch=0)
        v
1.7 final_overlay_compositor
        |
        | 2-up JPEG 한 장 + 작은 retained status
        v
Taskplanner Debug UI
```

최종 overlay는 여러 JPEG를 차분하거나 브라우저에서 겹치지 않는다. 1.7이 원본
한 프레임에 구조화된 Tool/Pose/Hand/Blood 결과를 직접 그리며, stale 또는 stamp
mismatch인 레이어만 제외한다.

## 입력 토픽

대용량 영상은 `BEST_EFFORT / VOLATILE / KEEP_LAST(1)`, CameraInfo는
`RELIABLE / VOLATILE / KEEP_LAST(20)`, extrinsics는
`RELIABLE / TRANSIENT_LOCAL / KEEP_LAST(1)`이다.

| 용도 | 토픽 | 타입 |
|---|---|---|
| CAM3/4 RGB | `/synced/cam_{3,4}/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` |
| CAM3/4 native depth | `/synced/cam_{3,4}/depth/image_rect_raw/compressedDepth` | `sensor_msgs/msg/CompressedImage` |
| RGB CameraInfo | `/synced/cam_{3,4}/color/camera_info` | `sensor_msgs/msg/CameraInfo` |
| Depth CameraInfo | `/synced/cam_{3,4}/depth/camera_info` | `sensor_msgs/msg/CameraInfo` |
| 카메라별 depth→color extrinsics | `/synced/cam_{3,4}/extrinsics/depth_to_color` | `realsense2_camera_msgs/msg/Extrinsics` |

검증된 live provenance ID는 다음과 같다.

```text
viplab-cam4-rgbd-align-v1-6af70cfd906e807f
```

이 ID는 D455 `146222251000`, firmware `5.15.0.2`, 1280x720@15,
realsense-ros `4.57.6` commit
`e11d3e154ce3817c8be73f36b87a75f287e080e5`, librealsense `2.57.7`,
depth scale `0.001 m/unit` 조합에만 유효하다. 카메라, firmware, profile 또는
정렬 설정이 바뀌면 `PNU_DEPTH_ALIGNMENT_VALIDATED=false`로 되돌리고 재검증한다.

## 출력 토픽

1.7의 현재 native 경로는 카메라별 다음 namespace를 사용한다. CAM3는
Tool/Pose만, CAM4는 Tool/Pose/Hand/Blood를 발급한다.

```text
/perception/cam_3/tool/observations
/perception/cam_3/tool/poses
/perception/cam_4/tool/observations
/perception/cam_4/tool/poses
/perception/cam_4/hand/keypoints
/perception/cam_4/blood/mask
/tf
```

도구 TF child frame은 관측 위치와 class를 포함한
`mayo_<tool>#<left_to_right_id>` 또는 `tray_<tool>#<left_to_right_id>` 형식이며,
카메라 optical frame 기준 translation과 quaternion을 6DoF TransformStamped로
표현한다. 위치 3축과 평면 내 yaw만 직접 관측하고, 나머지 orientation 성분은
보정된 작업평면 normal로 완성한다. 즉 roll/pitch를 독립 측정한 자유 6DoF pose가
아니며 robot/world/TCP pose나 모션 권한도 아니다.

| 용도 | 토픽 | 타입 / QoS |
|---|---|---|
| 기존 CAM4 semantics, RealVLM 입력 | `/surgery/perception/cam4/semantics/json` | `std_msgs/msg/String`, RELIABLE/VOLATILE/10 |
| 기존 Mayo 관찰, Digital Twin 입력 | `/surgery/perception/cam4/mayo_tool_observations` | `surgical_msgs/msg/ToolObservation`, RELIABLE/VOLATILE/30 |
| Tool 2D 관찰 | `/surgery/perception/cam4/observations` | `surgical_perception_msgs/msg/ToolObservation2DArray`, RELIABLE/VOLATILE/10 |
| Tool metric pose | `/surgery/perception/cam4/tool_poses` | `surgical_perception_msgs/msg/ToolPoseArray`, RELIABLE/VOLATILE/10 |
| Hand 2D/3D keypoints와 palm | `/surgery/perception/cam4/hand_keypoints` | `hand_keypoint_interfaces/msg/HandKeypoints`, RELIABLE/VOLATILE/10 |
| Blood mask/centroid depth 요약 | `/surgery/perception/cam4/blood_semantics/json` | `std_msgs/msg/String`, RELIABLE/VOLATILE/10 |
| CAM4 디버그 overlay | `/surgery/images/cam4/detection_overlay/compressed` | `sensor_msgs/msg/CompressedImage`, BEST_EFFORT/VOLATILE/2 |
| Tool 자세축 overlay | `/surgery/images/cam4/pose_overlay/compressed` | `sensor_msgs/msg/CompressedImage`, BEST_EFFORT/VOLATILE/2 |
| worker/요청 진단 | `/surgery/perception/rfdetr/diagnostics/json` | `std_msgs/msg/String`, BEST_EFFORT/VOLATILE/10 |
| planner startup health | `/surgery/perception/rfdetr/health` | `std_msgs/msg/String`, RELIABLE/TRANSIENT_LOCAL/1 |
| CAM3+CAM4 최종 Debug raster | `/perception/debug/final_overlay/compressed` | `sensor_msgs/msg/CompressedImage`, BEST_EFFORT/VOLATILE/1 |
| 최종 raster/레이어 상태 | `/perception/debug/final_overlay/status` | `std_msgs/msg/String`, RELIABLE/TRANSIENT_LOCAL/1 |

Worker v1은 overlay binary를 반환하지 않는다. 대신 ROS bridge가 엄격히 검증한
구조화 결과만 원본 CAM4 JPEG 위에 합성해 동일 source stamp의 overlay를 만든다.
따라서 local/remote worker 배치와 무관하게 browser 출력 계약이 같고, 검증되지
않은 worker 응답이나 가짜 검출은 그리지 않는다. 0건 검출도 해당 모델의
`ready=true`, `executed=true`, 동일 request/source stamp가 확인돼야 정상이며,
그 상태는 짝이 맞는 diagnostics와 typed 결과에서 확인한다.

자세축 layer도 bridge가 typed ToolPoseArray와 RGB CameraInfo로 직접 만든다.
`orientation_valid=true`인 pose만 X=red, Y=green, Z=blue 축을 표시하고,
position만 유효한 pose는 amber `position-only` 마커로 표시한다. 0건이면 투명
frame을 발행해 이전 자세축을 지운다. 따라서 지지면 검증 실패를 그럴듯한 축으로
대체하지 않는다.

## standalone Debug에서 확인

Debug UI는 1.7이 만든 2-up final raster 한 장만 영상으로 구독한다. CAM3/CAM4의
Tool/Pose/Hand/Blood layer별 `live/stale/missing/disabled`, 결과 수와 drop 수는
별도 작은 status에서 표시한다. 기존 Taskplanner PNU health·diagnostics·typed
결과는 scalar 증거 검토용으로 유지하되 원본/개별 overlay JPEG를 추가 구독하지
않는다. 검출 0건은 transport 실패가 아니며 실행 상태와 결과 count로 구분한다.

Hand 카드는 typed `HandKeypoints`의 handedness, 21개 joint 2D/3D, joint별 depth
validity, palm translation/quaternion/3x3 rotation matrix와 `depth_source`를 표시한다.
Blood 카드는 instance/combined centroid의 pixel 좌표와 metric depth validity를
표시한다. 두 결과는 다음 frame 메시지가 먼저 도착해도 stamp별 bounded buffer에
보관했다가 같은 검출 overlay가 도착했을 때만 승격한다. overlay rate-limit frame은
의도적으로 `대기`로 표시하며 이전 frame의 수치를 현재 frame인 것처럼 유지하지
않는다.

같은 화면의 support-plane 진단은 보정 당시의 고정 fit 품질과 현재 frame의
runtime drift 측정을 분리해 표시한다. artifact/version pin, static reason,
현재 inlier ratio와 residual median/p95가 보이지 않거나 runtime gate가 실패하면
자세축을 신뢰하지 않는다. 또한 worker가 local loopback인지 remote HTTPS인지,
인증이 적용됐는지도 diagnostics에서 함께 확인한다.

```bash
scripts/taskplanner up debug --build
```

이 경로는 Taskplanner planner/BT/DT를 시작하지 않는다. CAM4 RGB-D 입력과 PNU
관찰 출력만 연결하고, Debug secure rosbridge는 overlay, health, diagnostics 및
정확히 열거한 CAM4 인식 결과를 subscribe-only로 노출한다. 요청 subset을 바꿀
때는 다음 두 값을 함께 바꿔 bridge 실행 subset과 local worker health subset을
항상 일치시킨다.

```bash
PNU_DEBUG_REQUESTED_ALGORITHMS=tool,blood,hand
PNU_WORKER_REQUIRED_ALGORITHMS=tool,blood,hand
```

## Taskplanner 연결 경계와 ontology

PNU의 typed Tool 출력은 upstream ontology와 원래 `class_name`을 그대로 보존한다.
기존 RealVLM/Digital Twin 호환 출력에만 다음 exact ID/name pair를 변환한다.

| PNU Tool | 기존 Taskplanner 이름 |
|---|---|
| `1 / Scalpel` | `#15 Scalpel` |
| `2 / Allis Forceps` | `Allis clamp forceps` |
| `3 / Mosquito` | `Mosquito forceps` |
| `4 / Adson Forceps` | `Adson forceps` |
| `5 / Bipolar Forceps` | `Bipolar cautery` |
| `6 / Bovie` | `Bovie surgical cautery` |
| `7 / Army-Navy Retractor` | `Army navy retractor` |
| `8 / Thyroid Retractor` | `Thyroid retractor` |

ID/name pair가 다르거나 알려지지 않은 label은 semantics/Mayo 호환 경로에서
버린다. 실제 procedure spec으로 `thyroidectomy_demo`는 8/8이 resolve되고,
일반 `thyroidectomy`는 catalog에 T11이 없으므로 Thyroid Retractor만 생성하지
않고 7/8이 resolve된다.

현재 planner가 실제로 구독하는 호환 경로는 Tool semantics→RealVLM과 안정화된
Mayo observation→Digital Twin이다. 새 ToolPose/Observation, HandKeypoints,
Blood semantics 토픽은 아직 monitor-only이며 직접 구독하는 BT/control node가
없다. 특히 `metric_3d_ready`만으로 로봇 명령을 만들지 않는다. ToolPose의
`validity`, `orientation_valid`, support-plane 승인, robot-frame TF와 age gate가
모두 추가되기 전까지 motion authority는 비활성이다.

## 3D fail-closed 조건

1.7 ingress와 native-depth pose runtime이 다음을 다시 확인한다.

1. RGB/depth source stamp가 허용 skew 안에 있고 stale하지 않다.
2. 두 payload가 실제 1280x720이며 depth PNG가 `uint16` 단일 채널이다.
3. color/depth CameraInfo와 해당 카메라의 retained
   `depth_to_color` extrinsics가 모두 존재한다.
4. 두 CameraInfo의 dimensions와 intrinsic/distortion 값이 payload profile과
   일치하고, native depth를 color grid로 투영할 수 있다.
5. depth scale은 양수이고 startup provenance로 검증됐다.
6. registration validation flag와 non-empty provenance ID가 함께 있다.

하나라도 실패하면 요청 자체를 위조하지 않고 2D fallback 또는 malformed
input 오류로 처리한다. Tool 제어 pose는 여기에 support-plane version/validation,
`validity`, `orientation_valid`, age와 향후 robot-frame TF gate가 추가로 필요하다.
현재 typed 3D 출력은 monitor-only다.

## 배포 상태

2026-08-22 운영 cutover를 적용했다.

- VIPLab의 `arpa-multicam-operations.service`가 CAM1/2/3/4/FLIR RGB, CAM3/4
  native depth, CameraInfo/extrinsics와 5 Hz preview를 발행한다.
- 1.7의 enabled `taskplanner-perception-stack.target`이 ingress, CAM3 Tool,
  CAM4 Tool/Hand/Blood, final compositor 네 service를 소유한다. child service가
  개별적으로 `disabled`인 것은 target-owned 설치 계약상 정상이다.
- Taskplanner Live/Debug는 CAM1/2/3/4/FLIR 관제와 VLM에 `/preview`만 사용한다.
  full `/synced` RGB-D의 Taskplanner 직접 구독자는 0이고, 1.7 ingress만 LAN
  구독한다. VIPLab-local `world_anchor_node`의 CAM3/4 RGB 구독은 예외다.
- Debug endpoint는 `MONITOR_ONLY`, `armed=false`이며 final overlay와 scalar/TF
  증거만 노출한다. 이 cutover는 로봇 motion authority를 추가하지 않는다.

지원 launcher로 동일 배치를 다시 구성할 수 있다.

```bash
scripts/taskplanner up live --build
```

최종 60초 단일-counter soak에서 CAM3 ingress RGB/depth는 `895/661`, CAM4는
`889/644`, final JPEG는 `599`(약 9.98 Hz), status는 `121`(약 2.02 Hz)이었다.
각 10초 창의 0-frame 구간은 없었고 네 perception service의 `NRestarts=0`, 관련
journal 예외도 0건이었다. 추가 `/synced` BEST_EFFORT reader는 불연속일 수 있으므로
운영 계약은 계속 camera별 ingress 하나만 허용한다.

실제 브라우저는 1920x540 단일 raster를 LIVE로 표시했다. 5초 동안 100 ms 간격
50회 검사에서 image element는 항상 1개였고, source stamp는 33회 전진했으며 blank
전환은 0회였다. browser console error/warning도 0건이었다.

세 호스트는 `FragmentSize=1344B`, `MaxMessageSize=1450B`,
`MaxRexmitMessageSize=1450B`의 MTU-safe LAN profile을 사용한다. Taskplanner와
1.7은 대형 BEST_EFFORT frame의 동시 재조립 한도를
`DefragUnreliableMaxSamples=64`로 올렸고, 1.7 delivery queue는 512 sample로
유한하게 제한했다. 운영 중 15초 NIC aggregate는 Taskplanner RX/TX
`101.159/7.725 Mbps`, VIPLab `4.089/232.662 Mbps`, 1.7
`235.362/3.729 Mbps`였다. 이어진 15초 동안 세 호스트 모두 IP reassembly failure와
UDP receive-buffer error 증가분이 0이었다.

이번 장애의 직접 원인은 CAM2 hardware reset 중 공유
`component_container_mt`가 SIGSEGV(-11)로 종료됐지만 상위 launch가 살아 있어
선언된 publisher만 남은 것이었다. VIPLab launch는 이제 shared container 종료를
전체 launch shutdown으로 전파하고, operations systemd unit은 전체 rig를 다시
구성한다. source stamp와 `measured_hz`를 함께 확인하며 publisher count만으로
정상을 판정하지 않는다.

## 로컬 worker 선택

`.env` override 예다. JSON은 일부 모델만 넣거나 빈 `{}`로 두면 안 된다.

```bash
PERCEPTION_PROVIDER=pnu_hand_blood
PERCEPTION_LOCATION=local
PERCEPTION_ENDPOINT=http://127.0.0.1:8020
PNU_SERVICE_URL=http://127.0.0.1:8020
PNU_EXPECTED_MODEL_DIGESTS_JSON={"tool":"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430","blood":"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75","hand":"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"}
PNU_EXPECTED_TOOL_SUPPORT_PLANE_CONFIG_VERSION=viplab_cam4_146222251000_support_plane_v1_sha256_b683ecd5a5382a4f
REQUIRE_PERCEPTION_ON_START=true
PNU_REQUIRE_METRIC_3D_ON_START=true
```

지원 launcher는 이 경우에만 `pnu-perception` 컨테이너를 시작한다. worker를
시작하는 shell에서는 보정값 일부를 손으로 복사하지 말고 검토된 환경 파일 전체를
export한다. 표준 Compose의
`./config/pnu_perception:/config/pnu_perception:ro` mount도 제거하지 않는다.

```bash
set -a
. config/pnu_perception/cam4_support_plane.env
set +a

python3 tools/pnu_live_preflight.py artifacts
scripts/taskplanner up live --build
```

Artifact preflight가 exit `3`을 반환하거나 worker health가 `degraded`이면 모델
실행을 시작하지 않는다. 기존 기본 provider는 계속 `builtin_rfdetr`이며, PNU를
기본값으로 자동 전환하지 않았다.

## LAN의 다른 PC에 worker 배치

Worker PC에는 이 저장소의 같은 revision, pinned upstream checkout, 세 모델,
`config/pnu_perception/cam4_support_plane.env`와 그 파일이 지목하는 JSON artifact,
token 파일을 먼저 둔다. token은 `PNU_SECRET_ROOT/token`에 있고 worker가 읽을
수 있는 regular file이어야 한다. JSON artifact 디렉터리는 표준 Compose의
`/config/pnu_perception:ro` bind로만 제공하며 쓰기 mount로 바꾸지 않는다. PNU
image의 기본 base는 registry image가 아니므로, 새 PC에서는 unified compatibility
base를 먼저 같은 tag로 만든다.

원격 endpoint의 기본 계약은 HTTPS다. PNU worker 자체는 TLS를 종료하지 않으므로
worker port는 loopback에만 열고, 같은 PC의 검토된 TLS reverse proxy가 유선 LAN
주소에서 HTTPS를 종료해 `127.0.0.1:8020`으로 전달한다. proxy 인증서의 SAN은
Taskplanner가 사용하는 DNS 이름과 일치해야 하며, 전체 인증서 체인은 bridge
컨테이너의 Python/Requests CA store에서 신뢰되어야 한다. 다음은 **worker
PC에서** worker만 준비하는 예다.

```bash
set -a
. config/pnu_perception/cam4_support_plane.env
set +a

export PNU_EXPECTED_MODEL_DIGESTS_JSON='{"tool":"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430","blood":"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75","hand":"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"}'

PERCEPTION_DOCKERFILE=docker/rfdetr-perception/Dockerfile.unified \
PERCEPTION_IMAGE=taskplanner-rfdetr-perception:unified-compat \
docker compose --profile live build object-perception

PNU_BIND_HOST=127.0.0.1 \
PNU_UPSTREAM_ROOT=/absolute/path/to/hand-blood-tools-0f9e93115b8c \
PNU_MODEL_ROOT=/absolute/path/to/pnu_hand_blood \
PNU_SECRET_ROOT=/absolute/path/to/perception-secret \
PNU_WORKER_API_TOKEN_FILE=/run/taskplanner/perception/token \
PNU_MAX_INGRESS_READ_SEC=1.0 \
docker compose --profile live up --build -d pnu-perception
```

TLS proxy는 예를 들어 `PNU_WORKER_DNS_NAME:8443`에서 듣고 worker의
`127.0.0.1:8020`으로 전달한다. Worker host firewall은 proxy의 HTTPS port
source를 Taskplanner 유선 IP 하나로 제한한다. 예를 들어 UFW를 이미 쓰는
host라면 검토 후 다음처럼 범위를 지정한다.

Proxy에도 request body를 **처음부터 끝까지 읽는 absolute deadline**을 worker의
`PNU_MAX_INGRESS_READ_SEC` 이하(현재 `1.0 s`)로 설정하고, body 크기 상한을
worker의 `PNU_MAX_REQUEST_BYTES` 이하(현재 `20 MiB`)로 설정한다. 단순 idle
`read_timeout`은 absolute deadline이 아니므로 대체할 수 없다. 사용하는 proxy가
전체 body absolute deadline을 지원하지 않으면 그 proxy 구성은 승인하지 않는다.

```bash
sudo ufw allow in on WIRED_IFACE proto tcp \
  from TASKPLANNER_LAN_IP to WORKER_LAN_IP port 8443
```

그 다음 **Taskplanner PC에서** 같은 token을
`PNU_SECRET_ROOT/token`에 두고 다음 selection을 사용한다.

```bash
set -a
. config/pnu_perception/cam4_support_plane.env
set +a

export PERCEPTION_PROVIDER=pnu_hand_blood
export PERCEPTION_LOCATION=remote
export PERCEPTION_ENDPOINT=https://PNU_WORKER_DNS_NAME:8443
export PNU_SERVICE_URL=https://PNU_WORKER_DNS_NAME:8443
export PNU_SECRET_ROOT=/absolute/path/to/taskplanner-perception-secret
export PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token
export PNU_ALLOW_INSECURE_REMOTE_HTTP=false
export PNU_ALLOW_UNAUTHENTICATED_REMOTE=false
export PNU_EXPECTED_MODEL_DIGESTS_JSON='{"tool":"253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430","blood":"f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75","hand":"fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"}'
export REQUIRE_PERCEPTION_ON_START=true
export PNU_REQUIRE_METRIC_3D_ON_START=true
```

Compose는 upstream/model/token 디렉터리를 각각 read-only mount한다. 기본
`PNU_DEVICE_POLICY=cuda_required`이므로 RF-DETR를 실행할 호환 NVIDIA GPU/CUDA
runtime도 필요하다. 지원
launcher는 remote 선택 시 local PNU 컨테이너를 시작하지 않고 남아 있는 local
worker도 중지한다. endpoint 실패 시 local로 자동 전환하지 않는다.

```bash
python3 tools/pnu_live_preflight.py worker \
  --location remote \
  --endpoint https://PNU_WORKER_DNS_NAME:8443 \
  --api-token-file /path/to/token \
  --expected-model-digests-json "${PNU_EXPECTED_MODEL_DIGESTS_JSON}"
scripts/taskplanner up live --build
```

이 probe도 빈/부분 pin을 허용하지 않고 health와 authenticated capabilities가
각각 보고한 세 digest를 동일한 reviewed JSON과 비교한다.

현재 장비의 격리된 신뢰 유선 LAN에서 TLS proxy를 준비하기 전 연결 시험만 해야
한다면 plain HTTP를 다음처럼 **별도로** opt-in할 수 있다.

```bash
# worker PC: 이 시험 동안에만 유선 IP에 worker port를 직접 bind한다.
PNU_BIND_HOST=WORKER_LAN_IP \
docker compose --profile live up --build -d pnu-perception

# Taskplanner PC
PERCEPTION_ENDPOINT=http://WORKER_LAN_IP:8020
PNU_SERVICE_URL=http://WORKER_LAN_IP:8020
PNU_ALLOW_INSECURE_REMOTE_HTTP=true
PNU_ALLOW_UNAUTHENTICATED_REMOTE=false

python3 tools/pnu_live_preflight.py worker \
  --location remote \
  --endpoint http://WORKER_LAN_IP:8020 \
  --api-token-file /path/to/token \
  --expected-model-digests-json "${PNU_EXPECTED_MODEL_DIGESTS_JSON}" \
  --allow-insecure-remote-http
```

이 플래그는 TLS 전송 gate만 푼다. bearer token 요구를 풀지 않으며, token 없이
시험하려면 별도의 `PNU_ALLOW_UNAUTHENTICATED_REMOTE=true`까지 필요하다. 두
예외를 동시에 켜는 구성은 운영용이 아니다. 양쪽 PC의 시간 동기화, 1GbE Full
Duplex, 유선 route와 source 제한 firewall을 확인한다.

## 현재 실측 자원과 성능

- 호스트 여유 공간: 약 826 GiB(최종 점검 시 `886,617,952,256 bytes`).
- 최종 worker image tag의 `docker image inspect` logical size는
  `5,212,939,888 bytes`, Docker의 uncompressed
  virtual size 표시는 약 15.1 GB다. 최종 worker tag 자체의 unique layer는
  약 180 kB이며 unified base와 대부분 공유한다. 새 PC는 shared layer가 없으므로
  model과 별도로 약 15.1 GB image 공간 및 build 여유 공간을 잡는다. 로컬 image
  ID는 rebuild마다 달라질 수 있으므로 배포 신뢰 기준으로 쓰지 않고, source commit과
  아래 model/calibration SHA-256 pin을 사용한다.
- 현재 model root는 세 모델 포함 `276,725,062 bytes`다.
- 세 모델 Tool+Blood+Hand 현재 live 상태: worker RAM 약 `2.38 GiB`, GPU process
  `1,604 MiB`다. 최종 스냅샷 구간에서 호스트 GPU는 32 GB 중 약
  `4.3–4.6 GiB`를 사용하고 `27.5–27.8 GiB`가 비어 있었다. 호스트 총사용량은
  데스크톱 앱에 따라 변하지만 PNU process 점유량은 분리해 확인할 수 있다.
  NInfer manager에는 heavyweight model이 로드돼 있지 않았고, worker를 내리면
  PNU VRAM은 반환된다.
- 실제 CAM4 RGB-D에서 Blood 1건을 검출했고 confidence `0.504395`, centroid
  `[663.942, 274.042] px`, centroid-associated depth `0.792 m`를 같은 source
  stamp의 ROS output에서 확인했다. Hand도 `executed=true`, real-depth path였지만
  현재 장면에서는 0건이었다.
- 최종 direct worker frame은 decode `8.623 ms`, Blood `23.215 ms`, Hand
  `6.580 ms`, total `38.445 ms`였고 exact response validator와 metric-3D
  requirement를 통과했다.
- 동기화 수정 후 기본 `max_rate_hz=15`의 warm 20초 soak는 입력 299,
  처리 262(`13.10 Hz`), drop 37(`1.85/s`), HTTP/timeout/error 0,
  metric 3D `262/262`, `depth_missing=0`, source-to-output latency
  p50/p95/max `28.104/30.203/33.782 ms`였다. 수정 전보다 처리량은 10.5%
  증가하고 drop은 41.3% 감소했다.
- `max_rate_hz=20`은 별도 warm 15초 시험에서 처리 `10.53 Hz`, drop
  `4.33/s`로 오히려 악화됐다. 따라서 기본값은 15로 유지한다. 15 Hz 입력을
  전부 처리하지 못하는 잔여 one-slot/DDS scheduling loss는 성능 개선 항목이며,
  오류나 가짜 3D로 숨기지 않는다.
- VIPLab align 활성화 부하: 전체 CPU 약 `+0.68%p`, RealSense process 약
  `+1% of one core`, relay 약 `3.7% of one core`.
- aligned compressedDepth: 평균 0.42 MB/frame, 6.47 MB/s, 약 51.8 Mb/s.
  원격 subscriber 수만큼 VIPLab DDS payload가 늘고, Taskplanner에서 remote
  worker로 RGB-D HTTP traffic이 한 번 더 나간다. 현재 1GbE 범위지만 remote
  실기에서 drop/timeout을 60초 이상 다시 측정한다.

Blood checkpoint는 RF-DETR `1.10.0.dev0` metadata인데 통합 runtime은 RF-DETR
`1.9.0`이다. loader의 grouped-query flat-slice warning 때문에 실행 성공은
정확도 parity 증거가 아니다. 내일 실제 기물로 output accuracy를 반드시 본다.

## 내일 현장 acceptance

1. `pnu_live_preflight.py artifacts`와 worker health/capabilities를 통과한다.
2. NInfer heavyweight model을 unload한 뒤 PNU를 local 또는 reviewed remote로
   명시 선택한다.
3. health에서 `semantic_ready=true`, `metric_3d_ready=true`, 빈 reasons와
   동일 source stamp를 확인한다.
4. Tool, 손, 혈액 모형을 차례로 배치해 각 모델의 `executed`, latency, typed
   output을 확인한다. 검출 0건 자체는 transport 실패가 아니다.
5. RealVLM semantics와 Digital Twin Mayo observation까지 같은 source stamp로
   이어지는지 확인한다.
6. 알려진 거리 타깃으로 depth position 오차를 측정하고 Debug에서 artifact fit과
   현재-frame drift gate를 함께 확인한다. 카메라/트레이 이동, firmware/profile/
   intrinsics/alignment 변경 또는 artifact 만료가 있으면 support plane을 다시
   보정·승인한다. Tool pose는 계속 camera-frame planar 4DoF monitor-only이며
   robot control에는 사용하지 않는다.

## VIPLab rollback

CAM4 align 변경 전 원본과 rollback 절차는 VIPLab에 보존돼 있다.

```text
/home/viplab/ros2_wc/src/arpa_multicam/.codex-backups/20260821T0317-cam4-align
```

전체 카메라 스택을 재시작하지 않고 CAM4 component만 되돌리는 절차는 해당
디렉터리의 `ROLLBACK.md`를 따른다.
