# 외부 컴퓨터비전(CV) 인터페이스 사전 이식

> 이 문서의 native-depth/pending 구간은 초기 handoff snapshot이다. 현재
> CAM4-only aligned compressedDepth, PNU worker/bridge 3D 구현과 로컬/원격
> 운용 절차는 [`PNU_LIVE_3D_RUNBOOK_KO.md`](PNU_LIVE_3D_RUNBOOK_KO.md)를
> 기준으로 한다.

이 문서는 `SARAM-H_ROS2_인터페이스_계약_컴퓨터비전 연구실.xlsm`과
[`hand-blood-tools`](https://github.com/hanwae-py/hand-blood-tools)의 인식 계층을
Taskplanner에 안전하게 준비한 범위와 다음 이식 게이트를 정의한다. 검토 기준
upstream은 commit `0f9e93115b8cc1d470398c92e010e3fc6ef1de5d`이다.

2026-08-21에는 VIPLab PC에서 실제 ROS graph, QoS, CameraInfo와 카메라 설정을
확인한 뒤 사용자 승인에 따라 CAM4만 align filter와 compressedDepth relay를
활성화했다. CAM1~3과 FLIR 설정은 바꾸지 않았다. 입력이 없으면
`/integration/cv_contract/status`에서
`WAITING_FOR_PUBLISHER`로 표시하며, 더미 이미지·더미 CV 결과·추정 calibration·
임시 custom `.msg`는 만들지 않는다.

## 현재 이식된 경계

`simulation_runtime/cv_contract_monitor`는 아래의 표준 ROS 메시지를 실제로
구독하고 구조만 검증한다.

| 논리 입력 | 기본 토픽 | 타입 | 현재 처리 |
| --- | --- | --- | --- |
| CAM4 RGB (canonical) | `/synced/cam_4/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | payload, stamp, frame_id 관찰 |
| CAM4 RGB 공개 alias | `/surgery/images/cam4/compressed` | `sensor_msgs/msg/CompressedImage` | CAM4 canonical과 동일한 물리 source로 표시만 함; 이중 집계하지 않음 |
| CAM4 color CameraInfo | `/synced/cam_4/color/camera_info` | `sensor_msgs/msg/CameraInfo` | 해상도·행렬 shape·유한수 관찰 |
| CAM4 native depth | `/synced/cam_4/depth/image_rect_raw/compressedDepth` | `sensor_msgs/msg/CompressedImage` | depth optical frame의 native `16UC1; compressedDepth png`; aligned로 취급하지 않음 |
| CAM4 depth CameraInfo | `/synced/cam_4/depth/camera_info` | `sensor_msgs/msg/CameraInfo` | native depth intrinsics 관찰; color intrinsics와 혼용하지 않음 |
| CAM4 depth-to-color extrinsics | `/synced/cam_4/extrinsics/depth_to_color` | `realsense2_camera_msgs/msg/Extrinsics` | graph/type/QoS만 관찰; 배열 layout 검증 전 값은 소비하지 않음 |
| CAM4 aligned depth | `/synced/cam_4/aligned_depth_to_color/image_raw/compressedDepth` | `sensor_msgs/msg/CompressedImage` | color optical frame의 `16UC1; compressedDepth png`; PNU 3D 입력 |
| CAM4 aligned CameraInfo | `/synced/cam_4/aligned_depth_to_color/camera_info` | `sensor_msgs/msg/CameraInfo` | color CameraInfo와 exact calibration/frame/dimensions 검증 |
| 도구전달 tray RGB | `/surgery/images/tray/compressed` | `sensor_msgs/msg/CompressedImage` | 선택 입력으로 관찰 |
| 도구전달 tray CameraInfo/depth | `/surgery/cameras/tray/{color/camera_info,aligned_depth}` | `CameraInfo` / `Image` | 선택 입력으로 관찰 |

여기서 **tray는 Mayo가 아니라 도구전달 tray**다. 기존 Mayo placement
tracker와 이 경로를 연결하지 않는다.

실측 CAM4는 color/depth 모두 `1280x720@15 Hz`이고 CAM4에만
`align_depth.enable=True`다. aligned compressedDepth는 20초 동안 RGB와
299/299 exact stamp, 14.938 Hz, `cam_4_color_optical_frame`, `uint16` 및 약
77.77% valid depth를 보였다. payload는 약 `6.47 MB/s`다. native depth는 계속
진단용으로 유지하지만 PNU는 librealsense가 만든 별도 aligned stream만 3D에
사용한다.

Native-depth용 live extrinsics 값은 VIPLab fallback JSON과 같지만, JSON/source 설명은
row-major이고 `realsense2_camera_msgs/msg/Extrinsics` IDL 주석은 column-major다.
publisher가 transpose 없이 배열을 복사하므로 known-point projection으로
convention을 검증하기 전에는 `layout_validated=false`다. PNU live 3D는 이
ambiguous 배열로 자체 registration하지 않고 librealsense aligned output을 쓴다.

ROS parameter service의 `depth_module.depth_units` 요청은 응답 제한시간을
넘겼지만, [librealsense의 `get_depth_scale()` 안내](https://github.com/realsenseai/librealsense/issues/3473)에
따라 2.57.7 read-only sensor option API로 CAM4 serial
`146222251000`(firmware `5.15.0.2`)의 **현재값** `0.001 m/unit`을 확인했다.
option 변경과 두 번째 stream start는 하지 않았고, 조회 직후 RGB/depth 모두
약 15 Hz로 계속 발행됐다. 이 값은 현재 live 장치 근거이지 `0704_6` 또는 별도
H5의 기록 당시 scale을 소급 증명하지는 않는다.

아래 출력은 동일한 이름·예상 타입·QoS·소유자 정보로 ROS graph에서 점검한다.
Tool/Hand custom IDL은 pinned upstream의 Apache-2.0 package를 byte-identical로
설치했으며 PNU bridge가 typed message를 발행한다.

| 기능군 | 출력 계약 |
| --- | --- |
| 도구 인식 | CAM4/tray `ToolObservation2DArray`, `ToolPoseArray`, detection overlay |
| 도구 상태 | `/surgery/perception/rfdetr/{diagnostics/json,health}` |
| 손 인식 | CAM4 hand keypoints, hand target pose, hand overlay, diagnostics, health |
| 출혈/D405 | suction RGB/depth/CameraInfo, bleeding mask/overlay, diagnostics, health |

전체 endpoint 목록·Qos·pending 이유는
[`cv_contract.py`](../src/simulation_runtime/simulation_runtime/cv_contract.py)에
코드로 고정되어 있다.

ROS graph가 history/depth를 `UNKNOWN`으로만 보고하는 DDS 조합에서는
`QOS_UNVERIFIABLE_DEPTH`를 표시한다. 이는 QoS가 맞는다고 추정한 것이 아니라,
reliability/durability는 관찰했지만 depth를 현장 `ros2 topic info --verbose`와
제공자 설정으로 별도 확인해야 한다는 뜻이다.

## 구현과 실행 위치의 독립 선택

입력 모드(Live/Replay), 인식 구현, 실행 위치를 한 변수에 섞지 않는다.

```dotenv
PERCEPTION_PROVIDER=builtin_rfdetr   # builtin_rfdetr | pnu_hand_blood | disabled
PERCEPTION_LOCATION=local            # local | remote
PERCEPTION_ENDPOINT=http://127.0.0.1:8010
```

- `local` endpoint는 loopback만 허용한다.
- `remote` endpoint는 loopback과 bind-all 주소를 거부한다.
- 자동 local fallback은 없다. remote 장애 시 stale/health fault를 보고하고
  로컬 GPU worker를 몰래 시작하지 않는다.
- credentials, query, fragment가 들어간 URL은 거부한다. 인증정보는 read-only
  token mount와 bearer header로 전달한다. 원격 무인증은 명시적 dev opt-in
  없이는 거부한다.
- provider가 `disabled`면 endpoint도 비어 있어야 한다.

기존 설정은 호환 alias로만 남긴다.

| legacy `PERCEPTION_BACKEND` | 새 축으로 해석 |
| --- | --- |
| `local` | `builtin_rfdetr/local` |
| `external` | `pnu_hand_blood/remote` |
| `disabled` | `disabled/local` |

지원 실행 경로는 `scripts/taskplanner up live|replay`다. 이 launcher는
`builtin_rfdetr/local`일 때만 `object-perception`을 시작하고 remote 또는
disabled에서는 기존 로컬 worker를 중지한다. 광범위한
`docker compose --profile live up`은 이 선택 gate를 표현하지 못하므로 운영
명령으로 사용하지 않는다.

`pnu_hand_blood`는 versioned `/v1/{health,capabilities,infer}` worker API와
Taskplanner-side ROS bridge를 사용한다. 기존 RF-DETR `/v1/perceive`와 혼용하지
않으며 local/remote 모두 같은 strict binary multipart 계약을 쓴다.

## Python 환경과 저장공간 검토

upstream이 제안한 Hand와 Tool/Blood 두 Python 환경을 host에 그대로 만들지
않는다. 기존 Taskplanner RF-DETR 환경을 보존하는 opt-in 이미지
`Dockerfile.unified`를 별도 tag로 빌드했다.

| 항목 | 통합 profile |
| --- | --- |
| Python/PyTorch | 기존 Python 3.11, `torch 2.8.0+cu129`, `torchvision 0.23.0+cu129` 유지 |
| RF-DETR | 기존 `rfdetr 1.9.0` 유지 |
| Hand | `mediapipe 0.10.18`; real-depth core만 사용하고 mono-depth용 Torch 2.11은 설치하지 않음 |
| NumPy/OpenCV | `numpy 1.26.4`, 단일 `opencv-contrib-python 4.11.0.86` provider |
| 실행 방식 | 기존 image/CMD는 그대로 두고 `PERCEPTION_DOCKERFILE`과 `PERCEPTION_IMAGE`로 opt-in |

build/import/core smoke와 Tool/Blood/Hand 실제 live 실행은 통과했다. 세 모델은
size/SHA256 manifest로 고정돼 있다. Blood checkpoint의 RF-DETR 1.10-dev
metadata와 runtime 1.9 사이 정확도 parity는 별도 검증 대상이다. 상세 환경 결과는
[`compatibility-result.json`](../docker/rfdetr-perception/compatibility-result.json)과
[`UNIFIED_ENVIRONMENT.md`](../docker/rfdetr-perception/UNIFIED_ENVIRONMENT.md)에
고정한다.

2026-08-21 host 측정값:

- root filesystem: 1.9 TB 중 `893,031,346,176 bytes` 여유.
- Docker images 57.86 GB, build cache 26.5 GB. 임의 정리는 수행하지 않았다.
- PNU worker 이미지의 `docker image inspect .Size`: 5,212,781,343 bytes.
- Tool/Blood/Hand model root: 276,725,062 bytes.
- 통합 이미지: 5,212,690,932 bytes, 기존 대비 +123,539,098 bytes. 새 PC에서는
  unpacked layer까지 고려해 image runtime용 최소 20 GB를 예약한다.
- model root는 Tool 미포함 약 137 MB다. Tool manifest checkpoint는
  133,941,485 bytes, Blood는 133,788,164 bytes, Hand asset은 7,819,105 bytes다.
- 현재 `0704_6` MCAP 디렉터리는 약 2.0 GB다. image build까지 할 PC는 runtime
  20 GB와 별도로 최소 30 GB의 임시 build/cache 여유를 둔다.

## VRAM과 LAN 배치 검토

RTX 5090은 총 32,607 MiB이고 PNU 종료 후 약 29,266 MiB가 비어 있었다.
NInfer heavyweight model은 `unloaded`였다. 실제 Blood+Hand PNU process는
상주 약 1.4 GiB VRAM, 컨테이너 RAM 약 1.9 GiB였고 live 한 프레임에서 Blood
약 0.91초, Hand 약 10ms였다. Tool checkpoint가 없으므로 세 모델 동시 peak와
Tool latency는 아직 측정하지 않았다.

첫 GPU acceptance에서는 warm-up 전/후 process VRAM, 1280x720 15 Hz의 steady
state와 99th-percentile peak를 각각 기록한다. NInfer가 나중에 loaded라면
명시적으로 unload한 뒤 시작하며, 기존 builtin RF-DETR와 PNU worker를 동시에
소유하지 않는다. 측정 없이 “모델 파일 크기 = VRAM”으로 계산하지 않는다.

remote worker에는 같은 immutable image digest와 checkpoint SHA256을 사용한다.
VIPLab aligned compressedDepth만 약 6.47 MB/s(51.8 Mb/s)이고 RGB도 별도로
전송되므로 1 GbE를 사용한다. 100 MbE는 DDS와 remote HTTP 복제까지 고려하면
부적합하다.
PNU API는 기존 `/v1/perceive`의 base64 JSON을 복제하지 않고 binary multipart
또는 동등한 binary transport로 compressed payload를 재인코딩 없이 전달한다.
bounded queue/latest-frame 정책, request deadline, source stamp/frame_id,
health/capabilities schema, no-automatic-fallback을 wire contract에 포함한다.

## 갑상선절제술 Replay 범위

실제 `0704_6` MCAP은 163초, 28,841 messages이며 CAM4 RGB와 color CameraInfo가
각 1,937개다. native compressedDepth, aligned raw depth, depth CameraInfo,
extrinsics는 없다. Replay controller는 RGB를 VIPLab canonical alias에도
발행하고, 선택적 geometry 입력은 bag에 정확한 topic/type/count가 있을 때만
publisher를 만든다.

따라서 이 Replay에서 먼저 검증할 수 있는 범위는 Tool/Blood 2-D와 Hand 2-D다.
3-D 필드는 `metric_3d_ready=false`를 유지한다. 별도 원본 depth H5는 RGB와
frame 수가 다르고 units/alignment/calibration metadata도 없으므로, timestamp
pairing과 registration을 검증하기 전 MCAP에 자동 합성하지 않는다.

## 아직 보류한 항목

- Tool checkpoint 승인·SHA 검증, 실제 GPU 실행과 정확도 확인.
- Blood RF-DETR 1.10-dev checkpoint와 runtime 1.9의 output parity 및 model asset
  재배포 license 확인.
- 현재 수술면에서 support-plane 재측정·승인과 known-distance 3D 오차 시험.
- depth-to-robot TF, robot grasp/TCP 계약과 Tool pose maximum-age 제어 gate.
- 실제 별도 LAN worker에서 60초 이상 drop/timeout/clock/firewall 시험.
- 기록 당시 depth가 없는 `0704_6` Replay의 metric 3D. 이를 합성하지 않는다.

PNU bridge는 typed 3D와 기존 Mayo semantics를 발행하지만 Tool support plane이
미검증이면 orientation을 `DEGRADED`로 유지한다. 현재 3D는 monitor-only이며 robot
command 권한으로 사용하지 않는다.

## 현재 상태 확인

Taskplanner runtime 안에서 다음 명령으로 latched 상태를 한 번 받는다.

```bash
source /workspaces/taskplanner_ws/install/setup.bash
ros2 topic echo /integration/cv_contract/status --once
```

카메라 PC가 꺼져 있을 때 기대되는 CAM4 상태는 다음과 같다.

```json
{
  "perception_backend": "local",
  "perception_provider": "builtin_rfdetr",
  "perception_location": "local",
  "inputs": {
    "cam4_rgb": {"state": "WAITING_FOR_PUBLISHER"}
  },
  "adapter_state": "NOT_IMPLEMENTED_PENDING_EXTERNAL_IDL"
}
```

이는 “카메라 연결 대기”이며, planner fault나 mock frame을 뜻하지 않는다.

## 단계별 이식 순서와 승인 게이트

1. **Artifact gate**: Tool/Blood/Hand asset을 read-only model root에 두고
   SHA256/license를 기록한다. unified checker의 CPU checkpoint load를 통과한다.
2. **Worker contract**: `/health`, `/v1/capabilities`, versioned binary inference
   request/response를 구현한다. 입력 stamp/frame_id/CameraInfo와 출력 schema,
   validity, model digest를 고정한다.
3. **Taskplanner adapter**: ROS 입력을 worker에 전달하고 PNU 출력을 strict
   schema/stamp/frame/finiteness/freshness validator 뒤에만 발행한다. perception
   output은 planner evidence이며 motion command authority를 갖지 않는다.
4. **Local Replay 2-D**: NInfer/builtin ownership을 정리한 뒤 `0704_6` 고정
   프레임에서 Tool/Blood/Hand 결과, empty detection, pause/seek/reset,
   worker kill/restart와 stale suppression을 검증한다.
5. **Local GPU gate**: 15 Hz steady state, latency percentiles, dropped/coalesced
   frames, process VRAM과 host free VRAM을 artifact로 남긴다.
6. **Remote LAN gate**: 동일 image/checkpoint digest로 remote를 실행하고
   `PERCEPTION_LOCATION=remote`만 바꾼다. local worker 0개, endpoint owner 1개,
   link loss/deadline/recovery/no-fallback을 확인한다.
7. **3-D gate**: 기록 당시 depth CameraInfo, depth scale, extrinsics layout,
   RGB-depth registration 및 TF를 known target으로 검증한 새 Replay에서만
   `metric_3d_ready=true`를 허용한다.
8. 실제 이미지 1개, no-detection empty array, CameraInfo late joiner,
   RGB-depth skew, TF 불가 상황까지 통과한 뒤에만
   `REQUIRE_PERCEPTION_ON_START=true`를 고려한다.

## 네트워크와 공개 UI의 구분

이 계약 monitor는 native DDS의 신뢰된 제어망 안에서만 동작한다. UI 전용
컴퓨터는 native DDS에 넣지 않고 기존 read-only 9092 public bridge를 사용한다.
CV 상세 토픽은 현재 9092에 추가하지 않는다. 수신된 external CV 결과는 장차
Taskplanner adapter가 구조화·검증·비식별된 aggregate로 투영할 때만 공개한다.
