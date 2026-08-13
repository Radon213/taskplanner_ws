# 외부 컴퓨터비전(CV) 인터페이스 사전 이식

이 문서는 `SARAM-H_ROS2_인터페이스_계약_컴퓨터비전 연구실.xlsm`의 계약을
실제 CV 패키지가 전달되기 전에 Taskplanner에 안전하게 준비한 범위를 정의한다.
현재 카메라 송신 PC가 꺼져 있으면 입력 토픽은 없으며, 이는
`/integration/cv_contract/status`에서 `WAITING_FOR_PUBLISHER`로 정상 표시된다.
더미 이미지·더미 CV 결과·임시 custom `.msg`는 만들지 않는다.

## 현재 이식된 경계

`simulation_runtime/cv_contract_monitor`는 아래의 표준 ROS 메시지를 실제로
구독하고 구조만 검증한다.

| 논리 입력 | 기본 토픽 | 타입 | 현재 처리 |
| --- | --- | --- | --- |
| CAM4 RGB (canonical) | `/camera/cam_4/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | payload, stamp, frame_id 관찰 |
| CAM4 RGB 공개 alias | `/surgery/images/cam4/compressed` | `sensor_msgs/msg/CompressedImage` | CAM4 canonical과 동일한 물리 source로 표시만 함; 이중 집계하지 않음 |
| CAM4 CameraInfo | `/surgery/cameras/cam4/color/camera_info` | `sensor_msgs/msg/CameraInfo` | 해상도·행렬 shape·유한수 관찰 |
| CAM4 aligned depth | `/surgery/cameras/cam4/aligned_depth` | `sensor_msgs/msg/Image` | encoding·step·payload 크기 관찰 |
| 도구전달 tray RGB | `/surgery/images/tray/compressed` | `sensor_msgs/msg/CompressedImage` | 선택 입력으로 관찰 |
| 도구전달 tray CameraInfo/depth | `/surgery/cameras/tray/{color/camera_info,aligned_depth}` | `CameraInfo` / `Image` | 선택 입력으로 관찰 |

여기서 **tray는 Mayo가 아니라 도구전달 tray**다. 기존 Mayo placement
tracker와 이 경로를 연결하지 않는다.

아래의 CV 팀 출력은 동일한 이름·예상 타입·QoS·소유자 정보로 ROS graph에서
점검한다. custom IDL은 문자열 계약으로만 보관하며, 패키지를 받기 전에는
import하거나 재구현하지 않는다.

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

## 백엔드 소유권 전환

`PERCEPTION_BACKEND`는 다음 셋 중 하나만 쓴다.

| 값 | 동작 | 안전성 |
| --- | --- | --- |
| `local` (기본) | 현재 Taskplanner RF-DETR만 실행 | 기존 동작 보존 |
| `external` | 로컬 RF-DETR를 실행하지 않고 외부 CV 토픽 소유권을 비워 둠 | 현 단계에서는 evidence adapter가 없으므로 execution에는 사용하지 않음 |
| `disabled` | 어떤 perception backend도 실행하지 않음 | 카메라/CV evidence 미사용 |

따라서 `external`에서 현재의 local `rfdetr_perception_bridge`와 외부 CV 노드가
같은 overlay/diagnostics/health 이름을 동시에 발행하는 일이 없다. launcher도
`PERCEPTION_BACKEND=external`일 때 `object-perception` 컨테이너를 시작하지
않는다.

기본은 다음과 같다.

```dotenv
PERCEPTION_BACKEND=local
REQUIRE_PERCEPTION_ON_START=false
```

`REQUIRE_PERCEPTION_ON_START=true`와 `PERCEPTION_BACKEND=external`을 함께
설정하면, 실제 IDL·검증 adapter·타이밍·calibration 정책이 완성될 때까지
preflight는 의도적으로 실패한다. 토픽 이름만 맞는다는 이유로 수술 실행에
외부 evidence를 쓰지 않는다.

## 아직 보류한 항목

다음은 제공자가 값/패키지를 전달할 때까지 모두 `PENDING`이며, 숫자나 좌표계를
임의로 추정하지 않는다.

- `surgical_perception_msgs`, `hand_keypoint_interfaces`의 실제 source package,
  tag/commit, ROS distribution 및 type support
- CameraInfo 및 aligned depth의 실제 publisher, QoS, encoding, 단위, invalid
  sentinel
- RGB-depth 허용 skew, source stale timeout, late-joiner behavior
- 고정 frame/TF tree, calibration authority/버전, depth-to-robot transform
- canonical class ID와 Taskplanner T01~Txx 도구 매핑의 ontology version
- `ToolPoseArray` 및 hand target pose의 validity/confidence/maximum-age 계약

그러므로 monitor는 receipt age와 source timestamp 차이를 관찰하지만, 아직
3-D pose fusion·TF transform·robot command·Mayo mapping을 하지 않는다. hand
target pose는 나중에도 direct control 입력이 아니라 monitor-only evidence로
시작한다.

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
  "inputs": {
    "cam4_rgb": {"state": "WAITING_FOR_PUBLISHER"}
  },
  "adapter_state": "NOT_IMPLEMENTED_PENDING_EXTERNAL_IDL"
}
```

이는 “카메라 연결 대기”이며, planner fault나 mock frame을 뜻하지 않는다.

## 현장 패키지 수령 후 순서

1. CV 팀과 같은 commit/digest의 `surgical_perception_msgs`와
   `hand_keypoint_interfaces`를 양쪽 컨테이너에 설치한다. Excel의 필드 설명만
   보고 local `.msg`를 재작성하지 않는다.
2. wired LAN에서 Domain, RMW, discovery, type/QoS, publisher ownership을
   `ros2 topic list -t`와 `ros2 topic info --verbose`로 확인한다.
3. `PERCEPTION_BACKEND=external`로 전환한다. local RF-DETR publisher가 0개,
   external CV publisher가 endpoint당 정확히 1개인 것을 확인한다.
4. real CV output을 strict type/schema/stamp/frame/finiteness/freshness
   validator로 받는 adapter를 추가한다. calibration/TF/ontology가 불완전하면
   pose/robot evidence는 fail-closed 한다.
5. 실제 이미지 1개, no-detection empty array, CV kill/restart, stale input,
   CameraInfo late joiner, RGB-depth skew, TF 불가 상황을 포함한 E2E test를
   통과시킨 뒤에만 `REQUIRE_PERCEPTION_ON_START=true`를 고려한다.

## 네트워크와 공개 UI의 구분

이 계약 monitor는 native DDS의 신뢰된 제어망 안에서만 동작한다. UI 전용
컴퓨터는 native DDS에 넣지 않고 기존 read-only 9092 public bridge를 사용한다.
CV 상세 토픽은 현재 9092에 추가하지 않는다. 수신된 external CV 결과는 장차
Taskplanner adapter가 구조화·검증·비식별된 aggregate로 투영할 때만 공개한다.
