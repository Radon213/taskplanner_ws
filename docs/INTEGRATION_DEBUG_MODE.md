# Taskplanner 독립 Debug 모드와 선택적 통합 관측

> **Current-policy note (2026-08-27).** Debug observation is graph-wide and
> always read-only. Debug intervention requires authoritative paused/stopped
> state; a physical intervention also requires an explicit arm and a single
> in-flight command. Older prose below that adds active-task, robot/cleaner-idle,
> duplicate-preflight, or static read allowlist gates is historical and must
> not be copied into new work. See
> [`taskplanner_principles.toml`](../taskplanner_principles.toml).

Debug은 단일 all-in-one runtime이 아니라 독립 capability owner들의 조합이다.
`debug-observer`는 현재 ROS graph와 화면 evidence를 읽기만 하며, `debug-control`
만 typed write를 보낸다. `debug-virtual`은 외부 endpoint와 충돌하지 않는 로컬
Action/Service emulator다. ASR, record, VLM, network capability는 observer에
포함되지 않으며 필요한 경우에만 각각 별도로 시작한다.

## 실행

Standalone Debug는 `scripts/taskplanner up debug`로 시작한다. 같은 4173 UI를
사용하며, current-state observation은 running scenario와 분리되어 read-only로
가능하다. 기존 mode에 optional observer capability를 붙인 경우에도 observer는
그 mode의 ASR, VLM, record, perception, 또는 robot owner를 시작하거나 재시작하지
않는다.

`debug-control`의 write는 authoritative state가 paused 또는 stopped일 때만
가능하다. physical Action/Service에는 explicit arm과 대상 resource의 single-flight
보호가 추가된다. 상태 없음·stale·실행 중에는 새 write만 거부하고 observer는 계속
동작한다. Controller E-stop, type/range validation, idempotency, feedback/result,
cancel은 endpoint/controller에 남는다.

## Typed dispatch 경계

Debug gateway의 새 개입 API는 `operation=dispatch`와 아래 한 가지 JSON shape만
사용한다.

```json
{
  "kind": "service",
  "endpoint": "retraction_service",
  "type": "surgical_interop_msgs/srv/ExecuteRetractionCommand",
  "payload": {
    "command": "start_retraction",
    "target_side": "none",
    "distance_m": 0.0
  },
  "timeout_sec": 120.0
}
```

`endpoint`는 임의 ROS 이름이 아니라
[`integration_debug.yaml`](../src/integration_debug/config/integration_debug.yaml)의
선언된 alias 또는 현재 선택된 exact ROS endpoint여야 한다. `kind`, ROS type,
payload codec, physical 여부, single-flight, timeout 상한도 같은 선언에서 확인한다.
따라서 기존 타입의 새 Debug endpoint는 YAML policy와 해당 typed adapter만 추가하면
되고, browser operation switch·receipt·scenario allowlist를 늘리지 않는다.

- 관측은 항상 읽기 전용이며 arm이 필요 없다.
- 모든 Topic/Service/Action write는 integrated profile에서 authoritative
  `/simulation/state`의 fresh paused 또는 stopped 상태를 요구한다.
- physical Action/Service는 여기에 explicit arm과 single-flight를
  추가한다. controller의 E-stop, range/type validation, idempotency, Action
  cancel/result는 그대로 endpoint server가 소유한다.
- Debug는 `/surgery/audio/observed_utterance`를 읽을 수 있지만 음성 명령을
  재실행하지 않는다. `CommandRouter`만 `/surgery/audio/admitted_utterance`를
  소비해 catalog dispatch를 수행한다.
- Debug의 local retraction state는 상태 표시용이다. command 순서 허용 여부는
  controller Service가 판단하며 Debug는 두 번째 retraction state-machine gate를
  만들지 않는다.

- 통합/standalone 공용 UI: 로컬 `http://127.0.0.1:4173`, 유선 LAN
  `http://192.168.1.4:4173`
- Debug ROSBridge: `ws://127.0.0.1:9091`
- observer에는 PipeWire socket, ASR endpoint, record API key, PNU/VLM credential,
  또는 perception-worker mount가 없다.
- Debug capability의 재시작은 해당 owner만 대상으로 한다. owner/profile 목록과
  정확한 명령은 [`RUNTIME_OWNER_CONTROL.md`](RUNTIME_OWNER_CONTROL.md)를 따른다.
- Debug는 이미 실행 중인 외부 perception worker를 읽기만 하며 로컬 PNU/RF-DETR
  worker를 중복 시작하지 않는다.

### 외부 인식 overlay

Debug observer는 PNU, RF-DETR, VLM, 카메라 worker를 시작하지 않는다. 이미 graph에
발행되는 CAM3/CAM4 overlay·pose·health 토픽을 read-only로 구독해 “파이프라인이
멈춤”과 “현재 기물이 검출되지 않음”을 구분한다. 새 관측 토픽은 secure bridge의
read-only 구독으로 확인하며, 인식을 바꾸거나 worker를 추가로 띄우지 않는다.

browser write는 선언된 typed-dispatch endpoint로만 가능하며,
`/integration/debug/command`의 paused/stopped 경계를 통과한다. physical endpoint에는
explicit arm과 resource single-flight도 추가로 필요하다.

| 화면 증거 | ROS 토픽 |
|---|---|
| CAM4 PNU overlay | `/surgery/images/cam4/detection_overlay/compressed` |
| Tool 자세축 overlay | `/surgery/images/cam4/pose_overlay/compressed` |
| Tool별 위치·quaternion·유효성 | `/surgery/perception/cam4/tool_poses` |
| 모델·요청·latency 진단 | `/surgery/perception/rfdetr/diagnostics/json` |
| Tool/Blood 모델 준비, 최근 frame 상태 | `/surgery/perception/rfdetr/health` |
| Blood 요약 | `/surgery/perception/cam4/blood_semantics/json` |

이 예시 토픽들은 Debug browser가 변경할 수 없는 외부 관측이다. 인식 worker의
배포·인증·모델 선택은 해당 인식 owner의 책임이며 Debug owner의 환경이나 재시작
범위에 들어가지 않는다.

## LLM Surgeon·Replay와 LAN 경로

`scripts/taskplanner up llm-surgeon`과 `scripts/taskplanner up replay`는
데모/재생용 core만 시작한다. 두 모드에는 Live의 외부 카메라 observer,
`debug-observer`, `debug-control`, 운영 ASR가 포함되지 않는다. 따라서 실제
VIPLab 입력과 CAM4 인식 overlay를 확인하려면 Live를 사용해야 한다.

모든 모드의 브라우저와 ROSBridge 원격 접속은 같은 LAN/Tailnet 경로 라우터를
사용한다. 이 공유 router는 Debug node가 아니며, LLM Surgeon·Replay에서도
각 모드 자신의 bridge로만 요청을 전달한다.

## LAN 및 DDS Discovery 설정

**연결·입력** 화면 상단의 **LAN 연결과 Discovery** 패널에서 현재 기본 네트워크 인터페이스, 로컬 IPv4/CIDR, 게이트웨이, multicast 지원 여부, RMW 구현을 확인할 수 있다. 여러 물리 네트워크가 활성화되어 있으면 보조 IPv4 주소도 펼쳐서 볼 수 있다.

- **이 컴퓨터만 (`LOCALHOST`)**: DDS participant discovery를 로컬 호스트로 제한한다.
- **같은 LAN (`SUBNET`)**: 같은 LAN의 다른 컴퓨터를 DDS discovery 대상으로 허용한다.
- **ROS Domain ID**: `0`–`232` 사이의 정수를 직접 입력한다. 상대 기관 컴퓨터와 반드시 같은 값을 사용한다.
- **상대 컴퓨터 핑 테스트**: 상대 IPv4 주소에 ICMP Echo를 3회 보내 응답 수, 손실률, 평균 RTT를 표시한다. 핑은 DDS 설정을 변경하지 않는다.

DDS 설정은 이미 시작된 ROS 2 프로세스에서 안전하게 교체할 수 없다. 네트워크
capability owner가 배포된 경우에만 **적용하고 재연결**은 그 owner만 재시작한다.
`debug-observer`와 `debug-control`은 네트워크 설정을 바꾸지 않으며, 웹 UI는
ROSBridge에 자동 재연결한다.

설정은 `${TASKPLANNER_RUN_ROOT}/debug/network-settings.json`에 보존되어 다음
standalone 디버그 모드 시작에도 적용된다. 운영 런타임에 통합된 sidecar는 과거에
저장한 Domain/discovery 값을 의도적으로 무시하고 운영 런타임의
`ROS_DOMAIN_ID`와 discovery 범위에 잠긴다. 따라서 과거 D97 설정 등으로 핵심
운영 노드 감지를 우회해 수동 제어가 열리지 않는다. 네트워크 편집은 standalone
Debug에서만 수행한다. 전체 Taskplanner 런타임이나 다른 Compose 프로젝트는
재시작하지 않는다. LAN 연결 확인에는 `SUBNET`, 동일 Domain ID, 양측 multicast
허용, 호스트 방화벽의 DDS/RTPS 트래픽 허용이 모두 필요하며, 핑 성공만으로 DDS
discovery 성공을 의미하지는 않는다.

이 Domain 0/SUBNET 유선 계약은 live 런타임, 운영 ASR, public bridge와 통합
Debug sidecar에 적용된다. 데이터셋 replay/shadow는 외부 통합 참가자가 아니므로
회귀 재생을 운영 graph에서 격리하기 위해 D71/LOCALHOST를 유지하고 유선
`CYCLONEDDS_URI`를 전달받지 않는다.

`TASKPLANNER_DEBUG_NETWORK_INTERFACE`를 지정하면 Debug UI의 `LOCAL IP`는 해당
인터페이스만 주 주소로 사용한다. 이 PC의 기본값은 유선 5GbE 포트
`enp13s0`이다. 케이블이나 IPv4 주소가 없을 때 Wi-Fi 주소로 대체하지 않고
`유선 IP 없음`을 표시하므로, 상대 기관 LAN을 연결한 뒤 유선 주소가 실제로
할당됐는지 확인할 수 있다. `SUBNET` 배포는 같은 인터페이스를 지정한
`CYCLONEDDS_URI` 프로파일을 사용한다. 프로파일은 DDSI fragment를 `1344B`로
나누고, 여러 fragment를 한 UDP payload로 다시 묶는 상한도
`MaxMessageSize=1450B`와 `MaxRexmitMessageSize=1450B`로 제한한다. 따라서
1500-byte Ethernet MTU에서 IPv4/UDP header 여유를 남기며 대용량 카메라 sample의
IP fragmentation을 피한다. 이 효과는 writer가 같은 profile을 사용할 때
보장되므로 VIPLab에도 동일 payload 상한을 적용해야 한다. 설정한 NIC가 호스트에
없으면 UI만 열린 채 DDS가 다른 NIC로 빠지는 상태를 허용하지 않고 Debug ROS
런타임 시작을 거부한다.

`webapp`은 Vite를 localhost에만 유지하면서 프로필 비종속
`webapp-lan-proxy`를 필수 의존성으로 시작한다. 프록시는 기본적으로
`192.168.1.4:4173`만 열고 `192.168.1.0/24`에서 들어온 TCP 연결만 localhost
종단으로 전달한다. 따라서 `webapp`을 직접 시작하거나 Live, LLM Surgeon,
Replay, Debug 모드 사이를 전환해도 LAN UI 주소는 유지된다. 주소와 허용
서브넷은 각각 `TASKPLANNER_WEBAPP_LAN_ADDRESS`,
`TASKPLANNER_WEBAPP_LAN_NETWORK`로만 명시적으로 변경할 수 있다.

Live/LLM/Replay의 LAN proxy와 standalone Debug bridge는 분리한다. standalone
Debug는 observer가 제공하는 `ROSBRIDGE_DEBUG_PORT`(기본 9091)를 직접 사용하며,
Live/LLM/Replay의 proxy restart를 요구하지 않는다. 유선 IPv4와 proxy 설정은
network capability owner의 범위이며 observer/control owner가 바꾸지 않는다.

Tailnet에서 허용된 TCP는 호스트의 loopback으로 전달되므로, Tailscale IPv4
접속도 같은 `ROSBRIDGE_DEBUG_PORT`의 loopback 경로 라우터를 통과한다. `/`는
격리된 Debug upstream으로, `/live`와 `/llm`은 운영 ROSBridge로, `/shadow`는
리플레이 ROSBridge로만 전달된다. 라우터는 Tailnet CGNAT 대역과 로컬 프록시만
허용하며 새 외부 포트를 열지 않는다. 대시보드의 실행 모드 선택은 이 경로를
사용해 선택한 프로파일을 기동하고 ROS 재연결을 기다린다.

이 프록시는 사용자 인증을 추가하지 않는다. 같은 유선 서브넷의 사용자는
Debug 화면을 열 수 있으므로, 신뢰된 통합 시험망에서만 실행하고 수동 제어
활성화 및 원격 로봇 안전 확인 절차를 유지해야 한다.

## 외부에서 들어오는 입력

| 토픽 | 타입 | 기본 QoS | 화면에서 확인하는 값 |
|---|---|---|---|
| `/sensors/surgeon/utterance` | `surgical_msgs/msg/SpeechUtterance` | reliable / volatile / depth 20 | typed ASR source, publisher, freshness |
| `/surgery/audio/observed_utterance` | `surgical_msgs/msg/SpeechUtterance` | reliable / volatile / depth 20 | CommandRouter의 read-only relay, 최근 admitted utterance |
| `/integration/cv_contract/status` | `std_msgs/msg/String` | reliable / transient local / depth 1 | CV 계약 상태의 publisher, 실측 Hz, 최근 JSON, freshness |
| `/synced/cam_1/status` | `std_msgs/msg/String` | reliable / transient local / depth 1 | 원본 RGB publisher, 소스 실측 Hz·payload·누적 발행/드롭, 원본 QoS, freshness |
| `/synced/cam_2/status` | `std_msgs/msg/String` | reliable / transient local / depth 1 | 원본 RGB publisher, 소스 실측 Hz·payload·누적 발행/드롭, 원본 QoS, freshness |
| `/synced/cam_3/status` | `std_msgs/msg/String` | reliable / transient local / depth 1 | 원본 RGB publisher, 소스 실측 Hz·payload·누적 발행/드롭, 원본 QoS, freshness |
| `/synced/cam_4/status` | `std_msgs/msg/String` | reliable / transient local / depth 1 | 원본 RGB publisher, 소스 실측 Hz·payload·누적 발행/드롭, 원본 QoS, freshness |
| `/synced/flir/status` | `std_msgs/msg/String` | reliable / transient local / depth 1 | 원본 RGB publisher, 소스 실측 Hz·payload·누적 발행/드롭, 원본 QoS, freshness |
| `/simulation/state` | `surgical_msgs/msg/SimulationState` | reliable / volatile / depth 5 | 운영 런타임 정지 interlock과 freshness |
| `/external/bed_robot_arms/status` | `surgical_interop_msgs/msg/BedRobotArmStateArray` | reliable / volatile / depth 50 | revision, procedure type, 역할별 arm 상태와 freshness |
| `/integration/debug/virtual/bed_robot_arms/status` | `surgical_interop_msgs/msg/BedRobotArmStateArray` | reliable / volatile / depth 50 | 내장 가상 로봇을 선택했을 때 동일한 상태 계약과 freshness |

상태 화면은 작은 status 토픽의 publisher와 monitor 수신률뿐 아니라, status 안의
원본 publisher 실측 Hz, payload 크기, 누적 발행/드롭 및 원본 QoS를 구분해
표시한다. 따라서 Debug gateway가 15 Hz JPEG 다섯 개를 다시 구독하지 않고도
타입 불일치, 저주기, stale, publisher 없음 상태를 서로 다르게 판정한다.

멀티캠 화면 자체는 `/synced/cam_*`와 `/synced/flir`의
timestamp-preserving CompressedImage만 구독하며 `/camera/*` 또는
`/preview/*`로 fallback하지 않는다. 인식 화면은 서버가 합성한
`/perception/debug/final_overlay/compressed` 한 장과
`/perception/debug/final_overlay/status`만 구독하며, 브라우저에서 여러 JPEG
레이어를 exact-stamp로 다시 합성하지 않는다.

`cv_contract_monitor`와 Debug gateway는 reliable / transient local / depth 1로
snapshot QoS를 맞춘다. 따라서 gateway가 늦게 시작해도 마지막 CV 계약 상태를 받을
수 있다. 반면 ASR source와 observed utterance 같은 실시간 메시지는 reliable /
volatile / depth 20을 유지해 과거 발화를 새 세션 명령으로 재생하지 않는다.

## 선택 가능한 로봇 종단

| 선택 | Tool Action | Retraction Service | controller status | 용도 |
|---|---|---|---|---|
| `external` | `/surgery/tool_handover` | `/surgery/retraction/command` | `/external/bed_robot_arms/status` | 상대 제어기와의 실제 wire contract 시험 |
| `virtual` | `/integration/debug/virtual/tool_handover` | `/integration/debug/virtual/retraction/command` | `/integration/debug/virtual/bed_robot_arms/status` | 물리 로봇과 상대 Service로 송신하지 않는 로컬 계약 시험 |

두 선택 모두 Action 타입은
`surgical_interop_msgs/action/ExecuteToolHandover`, Service 타입은
`surgical_interop_msgs/srv/ExecuteRetractionCommand`, 상태 타입은
`surgical_interop_msgs/msg/BedRobotArmStateArray`로 동일하다. Debug launch는 전용
가상 emulator를 별도 이름으로 띄우므로 external 종단을 가로채지 않는다. 브라우저는
disarmed이고 active command가 없을 때만 `/integration/debug/command`의
`configure_robot_endpoint_source` op로 선택을 바꿀 수 있다. 전환 시 local
retraction admission state는 `idle`로 초기화되고 음성 자동 송신은 꺼진다.

리트랙터 제어는 단일 Service Request로만 발행한다. Request는
`protocol_version`, `source_id`, `command_id`, `command`, `target_side`,
`distance_m`만 포함한다. 직접 교시 종료는 Debug 입력에서
`target_side=none|left|right|both`를 허용하되, 배포된 로봇 peer와의 호환을
위해 Service wire에서는 세션 단위 종료를 뜻하는 `TARGET_NONE`으로 정규화한다.
조정은 `left|right|both`를 허용하며 `both`는 Service wire의
`TARGET_BOTH=3`으로 직렬화되고 각 팔에 같은 거리를 적용한다.
5 cm 조절은 `distance_m=0.050`이다. Service 화면은
`request_accepted`, `result_code`, 응답 `command_id`, `message`만 표시한다.
이는 Request admission 확인일 뿐, 물리 동작의 완료·진행률·상태·Tool 부착을
의미하지 않는다. 문서에 없는 자세, 속도 또는 상세 제어 상태는 Taskplanner가
만들지 않는다.

`suction`은 Debug-specific parser가 아니라 CommandRouter catalog의 typed Service
command다. catalog는 `protocol_version: 1`, `command: 7`, `target_side: 0`,
`distance_m: 0.0`을 execution-owner proxy로 보낸다. selected external or virtual
endpoint가 command 7을 지원하는지와 실제 physical behavior는 Taskplanner가
추정하지 않는다.

기존 Tool Change와 Retraction Adjustment의 별도 form/Action Cancel은 더 이상
제공하지 않는다. 상태 화면은 `stamp`, `revision`, `procedure_type` 및 각 arm의
`arm_id`, `role`, `role_instance_id`, `state`, `direct_teach_active`,
`reason_code`만 사용한다.

수동 명령 전에는 **수동 제어 활성화**가 필요하다. 활성 상태는 UI heartbeat가 끊기면 6초 안에 자동 해제된다. 동시에 하나의 명령만 허용한다.

`/surgery/retraction/command`는 Response가 돌아오면 admission 조회를 끝낸다.
그 뒤의 원격 동작을 Debug Mode가 완료·취소·복구됐다고 추정하지 않는다. Action
watchdog, Cancel 및 복구 카드는 계속 Action인 `/surgery/tool_handover`에만
적용된다.

`debug-control` owner는 쓰기 개입에만 `/simulation/state`의 최신 paused 또는
stopped 상태를 확인한다. physical Action/Service에는 explicit arm과 대상 resource의
single-flight가 더해진다. 카메라·ASR·VLM·record health, UI 상태, 시나리오의
별도 policy는 이 admission을 막지 않는다. 실행 중·상태 미수신·freshness 만료는
`operational_intervention_block_reason`으로 표시하고 새 write만 거부한다. observer
관측과 VLM/ASR/record capability의 상태 표시는 계속 가능하다.

VLM capability가 별도로 실행 중이면 observer는 그 상태를 읽을 수 있지만,
VLM은 음성 명령 경로에 포함되지 않는다. 모델을 실제로 적재하거나 교체하는
작업은 resource state를 바꾸는 개입이므로 paused/stopped와 arm 경계를 따른다.

운영 프로파일은 추가로 `TASKPLANNER_DEBUG_ALLOW_PLANNER_COEXISTENCE=false`와
runtime network lock을 적용한다. UI 확인만으로 불명확한 상태를 승인하거나
DDS 설정을 바꿔 interlock을 우회할 수 없다. 실행 중 상태로 바뀌거나 상태가
stale해지면 수동 제어와 쓰기 publisher를 자동 해제한다. Debug 기능은 상대
플래너나 원격 로봇을 정지시키지 않으며 그 안전 상태를 대신 보증하지 않는다.

## 외부로 발행하는 공개 토픽

| 토픽 | 타입 | QoS |
|---|---|---|
| `/surgery/context` | `surgical_interop_msgs/msg/SurgeryContext` | reliable / transient local / depth 1 |
| `/surgery/instruments` | `surgical_interop_msgs/msg/InstrumentStateArray` | reliable / transient local / depth 1 |
| `/surgery/robots` | `surgical_interop_msgs/msg/RobotStateArray` | reliable / transient local / depth 1 |
| `/surgery/events` | `surgical_interop_msgs/msg/SurgeryEvent` | reliable / volatile / depth 50 |
| `/surgery/clinical_observations` | `surgical_interop_msgs/msg/ClinicalObservationArray` | reliable / transient local / depth 1 |
| `/surgery/health` | `surgical_interop_msgs/msg/SurgeryHealth` | reliable / transient local / depth 1 |

각 토픽은 수동 제어가 활성화된 동안 1회 발행 또는 0.1–10 Hz 연속
발행을 지원한다. 이미 시작된 연속 발행의 개별 정지와 전체 정지는
수동 제어가 해제되어도 항상 허용한다. 더미 메시지는 임상·수술 상태로
오인되지 않도록 `DEBUG_DUMMY_DATA`, `UNKNOWN`, `integration_debug`
값을 명시하며 확인되지 않은 관찰을 만들지 않는다. 같은 토픽에 다른
publisher가 발견되면 디버그 publisher의 발행을 거부한다. 화면의
Subscriber 수는 DDS discovery 확인값이며, 상대 기관의 실제 callback
처리는 상대 측 echo 또는 로그로 함께 확인해야 한다.

## 문장·USB 마이크 입력

`debug-observer`는 USB 장치·PipeWire·ASR WebSocket을 열지 않는다. 화면은
typed source `/sensors/surgeon/utterance`와 CommandRouter의 read-only
`/surgery/audio/observed_utterance`를 관찰할 수 있다. USB capture와 final
발행은 명시적으로 시작한 `asr` capability owner에만 있다. observer container에는
PipeWire socket이나 ASR endpoint 환경변수가 없다.

ASR owner가 있을 때만 화면의 **ASR 시작**을 표시한다. `LISTENING`은 실제
WebSocket 연결을 뜻하며, observer가 `WAITING_PUBLISHER`인 것은 관찰 기능의
실패가 아니다. LAN 프록시는 인증 경계가 아니므로 마이크와 전사 원문은 신뢰된
격리 시험망에서만 사용한다.

ASR 세션이 멈춰 있을 때만 화면의 **클라우드** 또는 **LAN 192.168.1.5** route를
선택할 수 있다. 이 선택은 `cloud`/`lan` 식별자만 Debug backend로 전달하며, 브라우저가
임의 `ws://`·`wss://` URL을 전달하는 것은 허용하지 않는다. 기본 route와 각 route의
배포 주소는 `PUZZLE_ASR_ENDPOINT`, `PUZZLE_ASR_URL`, `PUZZLE_ASR_LAN_URL`로
시작 시 설정하고, 현재 선택은 status의 `asr.endpoint_id`로 확인한다. LAN route는
평문 `ws://`이므로 신뢰된 유선 시험망에서만 사용한다.

Debug 컨테이너는 raw `/dev/snd` 대신 호스트 PipeWire socket을 사용하고 Ubuntu의 현재 기본 입력 한 개만 노출한다. 장치 새로고침 후 시작하며, 화면에서 캡처 레벨, WebSocket 연결, 전송·응답·드롭 수, 최근 확정 문장을 확인한다. PipeWire가 16 kHz mono를 직접 제공하지 않으면 런타임이 사용 가능한 포맷으로 캡처해 16 kHz mono signed PCM으로 변환한다. 종료 시 남은 PCM은 서버의 8,192-byte 프레임 계약에 맞춰 silence padding한 뒤 EOF를 보낸다.

각 확정 문장의 `response_latency_ms`는 마지막 PCM 청크 송신이 완료된 monotonic
시각부터 해당 `is_final` JSON 응답을 수신한 시각까지의 클라이언트 관측
간격이다. `latency_basis=latest_pcm_send_complete_to_final_receive`,
`latency_correlated=false`를 함께 제공한다. 서버 내부 처리시간만을 뜻하지 않으며
지속 스트리밍 중 마지막 무음 청크가 기준이 될 수도 있다. WebSocket 계약에
발화·요청 ID가 없으므로 발화 단위 서버 처리시간으로 해석하지 않는다.

세션 종료 후 WAV와 확정 문장 TXT가 `${TASKPLANNER_RUN_ROOT}/debug/<session-id>/asr/`에 저장된다. 이는 음성 개인정보가 될 수 있으므로 실제 임상망이 아니라 비식별 통합 시험에서만 사용하고, 세션 산출물의 접근·보존 정책을 별도로 적용한다. 현재 ASR WebSocket 계약에는 별도 애플리케이션 인증이 정의되어 있지 않으며 URL query/userinfo에 자격증명을 넣는 방식은 거부한다.

Debug에서 들어온 final speech도 `speech_input_adapter`를 거쳐 typed
`/surgery/audio/admitted_utterance`가 된 뒤에만 처리한다. `CommandRouter`가
catalog match를 정확히 한 번 dispatch하고 observer는
`/surgery/audio/observed_utterance`만 읽는다. VLM text interpretation, local
voice state machine, and an additional retraction voice lane are not Debug
command paths. Manual typed dispatch remains a separate `debug-control`
operation and cannot replay an observed utterance. Service response is request
admission only, not physical execution or completion.

## 수술기록 생성 API 시험

이 절의 수동 TXT 전송 시험과 Live 자동 수술기록 owner는 서로 다른 경계다.
Live에서는 전용 `surgery-record` owner가 항상 시작되며, 음성·버튼이 공통으로
호출하는 `/simulation/control`의 성공 결과를 SimulationManager가 실행기 정착까지
확인한 뒤 `/simulation/lifecycle_terminal`로 투영한다. 같은
`procedure_run_id`의 `stop/halted` 또는 `completed/completed`만 로컬 0600 파일 저장과
HTTPS POST를 승인한다. `pause`는 현재 timeline을 유지하며 저장·POST하지 않고,
`reset/idle`, 실패한 Stop, raw `halted` 프레임도 승인 근거가 아니다. 이 owner는
Debug observer나 Debug 수동 record capability 안에서 중복 생성되지 않는다.

**수술기록 API** 탭은 `record` capability owner가 명시적으로 시작된 경우에만
전송 제어를 표시한다. `debug-observer`에는 record 입력 마운트·API key·HTTP client가
없다. record owner는 CommandRouter의 read-only
`/surgery/audio/observed_utterance`를 timeline 관측값으로만 읽고, 권위 있는
`0704_6`–`0704_17` UTF-8 전달용 TXT 12개를
read-only로 표시하고, 선택한 파일 전체를 JSON의 `text` 필드에 넣어 단일 `POST`로
전송한다. `roomName` 기본값은 전임상센터의 영문명인 `Preclinical Center`이며
`surgeryCode`, `date`, HTTPS endpoint는 제출마다 확인한다. `X-API-Key`는 호스트의
mode-0600 비밀 파일에서 백엔드만 읽고 read-only로 마운트한다. 키 값·길이·해시·파일
경로는 browser payload, status snapshot, 이벤트 로그와 요청 이력에 포함하지 않으며
화면에는 설정 여부만 표시한다.

현재 수술기록 canonical endpoint는 `https://192.168.1.5:6627/api/v1/surgery/img_texts`다. 서버는 이 endpoint만 allowlist하며, 다른 호스트·경로와 HTTP redirect로 TXT가 전송되는 것을 거부한다. 성공 `201`은 접수 ID와 수신 시각을 확인하는 것이며, 문서에는 생성 결과 조회·다운로드 endpoint가 없다. 따라서 화면은 실제 응답에 결과 본문이 포함되지 않은 이상 “기록 생성 완료”라고 표시하지 않는다. 30초 전후 timeout은 서버가 이미 접수했을 가능성이 있어 자동 재전송하지 않는다.

SurgiMate용 read-only 결과는 `/surgery/record/receipt`의 `std_msgs/msg/String` JSON으로 한 번만 발행한다. schema는 `taskplanner.surgery_record.receipt.v1`이며 `SUCCEEDED`, `FAILED`, `REMOTE_STATE_UNKNOWN`의 HTTPS 종료 결과만 reliable / transient-local / depth 1로 유지한다. `record_text`에는 terminal 시점에 생성한 수술기록 원문 전체를 메모리에서 담고, `response`에는 서버가 실제로 반환한 원문(서버가 수술기록 본문을 포함한 경우 그 본문 포함)을 16 KiB 이내로 전달한다. receipt 전용 1 MiB rosbridge frame 한도는 65,535자 원문이 JSON envelope 안에서도 버려지지 않게 하며, 자격증명·endpoint·로컬 `output_path`는 제거한다. 기존 `/surgery/record/post_status`는 private operator 상태 토픽으로 유지한다.

서버가 Puzzle private CA 체인을 사용하므로 Live `surgery-record` owner는 `config/puzzle_surgery_record_root_ca.pem`만 전용 신뢰 앵커로 사용한다. 2026-08-28에 확인한 CA SHA-256 fingerprint는 `E1:0D:AD:91:50:41:81:C9:92:0C:51:36:7B:F1:B1:70:7F:86:FB:35:F8:CF:13:EA:C5:10:5C:10:C6:02:90:3E`다. TLS 및 IP SAN 검증을 끄지 않는다.

현재 LAN UI/ROSBridge는 사용자 인증과 TLS를 추가하지 않으므로 신뢰된 격리 시험망에서 비식별 TXT만 전송한다. 외부 운영 전에는 canonical endpoint, 중복 키, timeout 후 조회/reconcile, 결과 schema, API 키 회전·폐기 정책을 Puzzle AI 측과 확정해야 한다.

## 내부 진단 인터페이스

| 방향 | 이름 | 타입 | 실제 QoS/호출 주체 | 의미 |
|---|---|---|---|---|
| observer → browser | `/integration/debug/status` | `std_msgs/msg/String` | reliable / volatile / depth 10 | UI가 소비하는 public `taskplanner.integration_debug.status.v1`; observer input telemetry와 최신 control projection을 합성 |
| observer → ROS | `/integration/debug/events` | `std_msgs/msg/String` | reliable / volatile / depth 50 | observer 이벤트 JSON |
| observer → browser | `/integration/debug/readiness` | `std_msgs/msg/String` | reliable / volatile / depth 10 | read-only observer readiness |
| browser → owners | `/integration/debug/heartbeat` | `std_msgs/msg/String` | reliable / volatile / browser queue 1, owner depth 5 | 현재 Debug 세션 ID를 담은 UI 생존 신호 |
| browser → control | `/integration/debug/command` | `surgical_msgs/srv/IntegrationDebugCommand` | `debug-control`의 유일한 mutation Service | arm/disarm, 더미 출력, 직접 Action/Service 요청을 typed dispatch로 중계 |
| control → observer | `/integration/debug/control/{status,events,readiness}` | `std_msgs/msg/String` | reliable / volatile | private owner projection; browser는 이 토픽을 직접 소비하지 않음 |
| ROS client → observer/control | `/integration/debug/check_readiness`, `/integration/debug/control/check_readiness` | `std_srvs/srv/Trigger` | Service | 각 owner의 readiness를 즉시 질의 |

`status` JSON의 최상위 필드는 `schema`, `capabilities`, `stamp_sec`, `session`,
`runtime`, `inputs`, `endpoints`, `action`, `outputs`, `voice`, `vlm`,
`virtual_robot`, `asr`, `surgery_record`, `recent_events`다. `capabilities[]`의
`enabled`, `state`, `restart_scope`는 각 owner가 실제로 시작됐는지와 필요한
최소 재시작 범위를 표시한다. observer가 control private status를 3초 안에 받으면
session/action/typed endpoint projection을 public status에 합성하며, 늦거나 없으면
observer-only 상태를 정확히 표시한다. 운영 interlock 진단에는
`runtime.operational_state_fresh`, `operational_runtime_stopped`,
`operational_intervention_allowed`, `operational_intervention_block_reason`,
`manual_control_gate`를 함께 사용한다.
`voice` status는 최근 observed utterance와 CommandRouter의 read-only result
projection만 표시한다. Debug는 별도의 음성 해석기, command 순서 allowlist,
또는 local admission state machine을 보유하지 않는다. Service 응답은
`action`의 `response_semantics=admission`, `request_accepted`, `result_code`,
`response_message`로 구분한다. Controller Service는 command ordering과 physical
state를 단독으로 판단한다.

브라우저는 `/surgery/tool_handover` Action이나
`/surgery/retraction/command` Service를 직접 호출하지 않는다. 브라우저가 직접
publish하는 ROS 토픽도 heartbeat 하나뿐이다. 모든 수동 요청은
`/integration/debug/command`를 통과한 뒤 control owner가 상대 Action/Service
client가 되어 전송한다. 따라서 브라우저 통합 테스트는 아래 다섯 op만 있으면 충분하다:
status/readiness subscribe, heartbeat advertise/publish, command Service call. 상대
로봇 계약 테스트는 내장 `virtual` 종단이나 별도 ROS harness의 fake server를
사용한다.

## 기능별 ROS I/O 시험 표

| 시험 | 시험 입력 | 관찰 출력 | 성공 기준 | 실제 로봇 종단 필요 |
|---|---|---|---|---|
| Debug 화면 연결 | `/integration/debug/status`, `/integration/debug/readiness` 구독 | heartbeat advertise 및 주기 발행 | fresh status 수신 후에만 command gate 활성 | 아니요 |
| Debug 입력 모니터 | config의 sentence/CV/camera 토픽별 단일 안전 sample | status의 `inputs[]` | 타입·publisher·QoS와 `message_count`, rate, age 갱신 | 아니요 |
| Debug 운영 interlock | `/simulation/state`의 안전 정지 sample | status의 `runtime.operational_*` | trusted publisher, fresh age, stopped 조건이 모두 분리되어 표시 | 아니요 |
| Debug bed-arm 상태 | 선택한 external 또는 virtual bed status sample | status의 `endpoints[name=bed_robot_arm_status]`, `virtual_robot` | schema/revision/role 검증 및 선택 종단 freshness 갱신 | 내장 virtual 또는 fake publisher만 |
| Debug 더미 출력 | `/integration/debug/command`의 `publish_once` 또는 `configure_output` | 선택한 `/surgery/*` 공개 토픽 | 서버가 동시 publisher를 거부하고 subscriber/callback에서 dummy marker 확인 | 아니요 |
| Debug 음성 관찰 | typed ASR sample | `/surgery/audio/observed_utterance`, status의 last utterance | observer가 command를 재실행하지 않고 변경 없는 relay를 표시 | 아니요 |
| Catalog Service command 단위 시험 | admitted `SpeechUtterance` 하나 | typed request 하나 또는 catalog miss | exact phrase는 한 번 dispatch, 비명령은 0회 dispatch | fake server만 |
| Debug 리트랙터 통합 시험 | `virtual` 선택 + command Service 요청 | 내장 Service가 받은 admission 결과와 Debug status | 정확한 enum/side/metres/command_id; state는 `request_accepted + RESULT_ACCEPTED`일 때만 전이 | 아니요 |
| Debug Tool Action 통합 시험 | `virtual` 선택 + command Service 요청 | 내장 Action feedback/result와 Debug status | Goal field·command_id 상관, terminal Result 및 watchdog 경계 확인 | 아니요 |
| ASR adapter 단위 시험 | complete `SpeechUtterance` 또는 legacy sentence compatibility input | `/surgery/audio/admitted_utterance`, `/input/speech/status` | final/source/freshness/echo/dedupe를 한 번 적용 | 아니요 |
| Voice Service 통합 시험 | admitted utterance + fake Service | execution-owner proxy → fake Service Request | catalog payload, one dispatch, endpoint admission response를 분리해 확인 | fake server만 |
| Mock 직접 계약 시험 | `fault_action_emulator`의 fake Action/Service와 bed-arm status | Live와 동일 public 종단 | 외부 하드웨어 없이 public wire contract와 실패 주입 검증 | 아니요 |

Live의 리트랙터 음성 경로에서 각 노드가 소유하는 실제 ROS 인터페이스는 다음과
같다.

| 소유 노드 | 구독/서버 입력 | 발행/client 출력 |
|---|---|---|
| `taskplanner_asr` | `/input/asr/control` (`AsrControl` Service) | `/input/asr/runtime_status` snapshot, typed `/sensors/surgeon/utterance` |
| `speech_input_adapter` | typed ASR source 또는 legacy sentence compatibility input | `/surgery/audio/admitted_utterance`, `/input/speech/status` `InputSourceStatus` |
| `CommandRouter` | `/surgery/audio/admitted_utterance`, hot-reloadable catalog | `/surgery/audio/observed_utterance`, exact typed endpoint request |
| execution owner | typed proxy request, route state, controller status | selected `/surgery/tool_handover` Action client 또는 `/surgery/retraction/command` Service client |
| 상대 또는 mock controller | `/surgery/tool_handover` Action server, `/surgery/retraction/command` Service server | `/external/bed_robot_arms/status` controller-owned 상태 |

세션 이벤트는 `${TASKPLANNER_RUN_ROOT}/debug/<session-id>/events.jsonl`에 JSONL로 남는다.

## 상대 기관과의 확인 순서

1. 양측 `ROS_DOMAIN_ID`, discovery 범위, 네트워크 multicast/participant discovery를 맞춘다.
2. **연결·입력**에서 상대 publisher, 타입, QoS, 실측 Hz, freshness를 확인한다.
3. **출력 검증**에서 해당 토픽을 1회 발행하고 상대 기관 echo/callback 로그를 확인한다.
4. Action/Service 서버가 발견되면 **수동 제어 활성화** 후 가장 작은 안전 명령부터 실행한다.
5. feedback/result/reason code와 양측 로그의 동일 command id를 대조한다.
6. 완료 후 **전체 정지**, **수동 제어 해제**, 디버그 모드 종료를 수행한다.
