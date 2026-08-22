# Taskplanner 통합 디버그 모드

통합 디버그 모드는 전체 시나리오, BT, Digital Twin, 시각 schema-v4 VLM,
Surgeon Actor를 실행하지 않고 외부 기관과 ROS 2 입출력 및 개별 로봇 기능을
확인하는 운영 모드다. 단, 리트랙터 음성 명령용 text-only VLM은 독립
micro-test와 음성 통합 시험에서 사용할 수 있다. 조그는 디버그 모드의
리트랙터 수동 기능으로 포함된다.

## 실행

기본 실행은 실제 외부 통합을 위한 Live 모드다. 인자를 생략해도 같은 모드가
시작된다.

```bash
scripts/taskplanner up --build       # 최초 build가 필요한 경우
scripts/taskplanner up               # 이후 기본 Live 기동
```

이 명령은 운영 런타임, 단일 Web UI와 함께 같은 `taskplanner_ws` Compose
프로젝트의 `integration-debug` 관측 sidecar와 유선 LAN 경로 라우터를 반드시
시작한다. PipeWire 입력 또는 수술기록 API key 파일이 없으면, 관측 plane 없이
Live를 조용히 시작하지 않고 기동 전에 실패한다. UI 상단의 **통합 관측**은 이
sidecar의 Debug 작업공간을 열 뿐 운영 런타임을 교체하지 않는다. 별도 4174 UI도
실행하지 않는다. Debug 화면은 운영 시나리오의 실행 여부와 관계없이
입력·토픽·Action 상태를 계속 모니터링한다. 수동 제어와 쓰기 명령은 `/simulation/state`에서 최신
안전 정지 상태(`idle`, `halted`, `completed`, `terminated`)를 확인한 동안에만
허용하고, 시나리오가 실행 중이거나
상태가 없거나 오래되었으면 fail-closed로 거부한다. 따라서 운영 컨테이너를
내릴 필요 없이 운영 화면에서 시나리오를 정지한 뒤 같은 UI에서 시험할 수 있다.

**독립 Debug** 버튼은 통합 관측과 다른 엔지니어링 런타임 전환이다. 이 버튼은
현재 미션 실행이 완전히 정지된 상태에서만 확인 대화상자를 거쳐 Live/LLM/Replay
core를 standalone `debug` 프로파일로 교체한다. 반대로 **통합 관측**에서
**운영 화면**으로 돌아오면 core·카메라·인식 worker·ASR은 그대로 유지된다.

전체 운영 런타임을 실행하지 않는 개별 기능 시험은 다음 standalone 명령을
사용한다. 같은 4173 UI를 사용하며, 이미 `taskplanner-runtime` 또는
`shadow-runner`가 실행 중이면 시작을 거부한다.

```bash
scripts/taskplanner up debug --build
```

Debug Mode의 마이크 캡처는 Ubuntu가 현재 선택한 PipeWire 입력을 따른다.
시작 시 `${XDG_RUNTIME_DIR}/pipewire-0`을 확인하고, UI에는 해당 논리 입력만
표시한다(예: `Analog Input - Shure MVX2U GEN 2`). Raw ALSA endpoint는 숨기며
명령 서비스로 직접 선택할 수도 없다. Ubuntu에서 기본 입력을 변경한 뒤에는
**STT 입력·USB 캡처 → 장치 새로고침**을 누르면 되고, USB hotplug 때문에 컨테이너를
다시 생성할 필요는 없다.

- 통합/standalone 공용 UI: 로컬 `http://127.0.0.1:4173`, 유선 LAN `http://192.168.1.4:4173`
- 운영 ROSBridge: `ws://127.0.0.1:9090`
- Debug ROSBridge: `ws://127.0.0.1:9091`
- 기본 포트와 ROS 도메인은 `docker/orchestration/debug.env`와 로컬 `.env`에서 바꿀 수 있다.
- standalone Debug 프로파일은 `webapp`, `integration-debug`, multicam observer와
  로컬 NInfer manager/control plane을 시작한다. 모델을 자동 load/unload하지
  않으며 이미 loaded인 worker를 보존한다. 화면의 **구성 모델 로드**를 명시적으로
  누른 경우에만 launch에 고정된 managed model의 load를 요청한다.
- `live`는 `integration-debug`와 multicam observer를 필수 관측 sidecar로
  시작하고, Debug ROSBridge readiness까지 확인한 뒤에만 Live를 ready로 기록한다.
  Live의 운영 PNU worker가 인식과 overlay를 소유하므로 Debug 쪽 PNU worker는
  중복 기동하지 않는다.
- `llm-surgeon`과 `replay`는 외부 카메라 observer와 `integration-debug`를
  자동 기동하지 않는다. 두 모드는 자체 ROSBridge를 LAN/Tailnet 경로 라우터로
  계속 제공하지만, 그것은 Debug 관측 runtime을 의미하지 않는다.

Live UI의 **통합 관측**에서 Debug 작업공간으로 진입한다. standalone Debug는
런처로 명시적으로 시작하거나 상단 **독립 Debug** 전환을 사용한다. Debug
작업공간을 나갈 때 연속 더미 발행을 정지하고 수동 제어를 해제한다.

### PNU CAM4 인식 오버레이

standalone `debug` 프로파일은 Taskplanner/BT/DT/로봇 실행 계층 없이
`debug_pnu_perception_bridge` 하나를 추가로 실행한다. 기본값은 이 PC에서 파일과
SHA-256을 검증한 local worker의 `tool,blood,hand` 전체를 요청하며, Docker readiness도
같은 세 모델 모두 `ready=true`여야 통과한다. worker의 원본 `/v1/health`가 전역
`status=ready`가 아니면 overlay health는 준비 상태로 승격되지 않는다.

입력은 VIPLab CAM4의 실제 RGB, color CameraInfo, depth-to-color 정렬
`compressedDepth`, 정렬 CameraInfo 네 토픽이다. 검증된 현재 D455 계약에 한해
`0.001 m/unit`과 alignment provenance를 사용한다. 결과가 0건이어도 worker가 해당
알고리즘을 실제 실행했다면 같은 source stamp의 투명 overlay frame을 발행한다.
Debug UI는 health/diagnostics의 실행 모델과 0건 상태를 함께 표시하므로 “파이프라인이
멈춤”과 “현재 기물이 검출되지 않음”을 구분할 수 있다.

Debug 브라우저가 구독할 수 있는 인식 토픽은 exact read-only allowlist다.

| 화면 증거 | ROS 토픽 |
|---|---|
| CAM4 PNU overlay | `/surgery/images/cam4/detection_overlay/compressed` |
| Tool 자세축 overlay | `/surgery/images/cam4/pose_overlay/compressed` |
| Tool별 위치·quaternion·유효성 | `/surgery/perception/cam4/tool_poses` |
| 모델·요청·latency 진단 | `/surgery/perception/rfdetr/diagnostics/json` |
| 세 모델 준비, 최근 frame 상태 | `/surgery/perception/rfdetr/health` |
| Blood 요약 | `/surgery/perception/cam4/blood_semantics/json` |
| Hand keypoints | `/surgery/perception/cam4/hand_keypoints` |

이 토픽들은 publish allowlist에 들어가지 않으며 Debug 브라우저는 인식 결과를
변조할 수 없다. PNU adapter도 카메라를 구독하고 관찰 결과만 발행하며 planner,
Action, Service 또는 물리 로봇 명령을 시작하지 않는다.

외부 PC worker를 쓸 때는 Taskplanner PC에서 다음처럼 명시한다. remote 선택은
local `pnu-perception` 컨테이너를 중지한 채 시작하지 않으며, endpoint 실패 시
local로 자동 fallback하지 않는다.

```bash
ENABLE_PNU_DEBUG_PERCEPTION=true \
PERCEPTION_PROVIDER=pnu_hand_blood \
PERCEPTION_LOCATION=remote \
PERCEPTION_ENDPOINT=https://PNU_WORKER_DNS_NAME:8443 \
PNU_SECRET_ROOT=/absolute/path/to/perception-secret \
PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token \
scripts/taskplanner up debug --build
```

원격 endpoint는 기본적으로 HTTPS여야 한다. TLS proxy 전의 격리된 신뢰 유선
LAN 시험에서만 `http://WORKER_LAN_IP:8020`과
`PNU_ALLOW_INSECURE_REMOTE_HTTP=true`를 함께 사용한다. 이 전송 예외는 별도
bearer 인증 요구를 해제하지 않는다.

세 모델 경로나 digest를 바꿀 때는 worker readiness와 bridge의
`PNU_EXPECTED_MODEL_DIGESTS_JSON`을 함께 검토한다. 일시적으로 인식 계층을 완전히
빼려면 `ENABLE_PNU_DEBUG_PERCEPTION=false scripts/taskplanner up debug`를 사용한다.

## LLM Surgeon·Replay와 LAN 경로

`scripts/taskplanner up llm-surgeon`과 `scripts/taskplanner up replay`는
데모/재생용 core만 시작한다. 두 모드에는 Live의 외부 카메라 observer,
`integration-debug` 관측 sidecar, 운영 ASR가 포함되지 않는다. 따라서 실제
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

DDS 설정은 이미 시작된 ROS 2 프로세스에서 안전하게 교체할 수 없으므로, **적용하고 재연결**을 누르면 설정을 저장한 뒤 `integration-debug` 런타임 컨테이너만 재시작한다. 웹 UI는 유지되며 ROSBridge에 자동 재연결한다. 수동 제어가 활성화되어 있거나 Action이 실행 중이거나 연속 더미 토픽을 발행 중일 때에는 설정 변경을 거부한다.

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

`integration-debug-lan-proxy`는 Live, LLM Surgeon, Replay, standalone Debug와
독립적으로 선택한 유선 인터페이스의 `ROSBRIDGE_DEBUG_PORT` 경로만 노출한다.
유선 IPv4가 바뀌면 이 ROS
리스너는 자동으로 다시 바인딩되며, 재시작되어도 4173 UI 연결에는 영향을 주지
않는다.

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
| `/sensors/surgeon/sentence` | `std_msgs/msg/String` | reliable / volatile / depth 20 | publisher, 실측 Hz, 최근 문장, freshness |
| `/surgery/audio/request_text` | `std_msgs/msg/String` | reliable / volatile / depth 20 | speech adapter가 입장을 허용한 정규화 문장, publisher, 수신 횟수 |
| `/input/speech/status` | `surgical_msgs/msg/InputSourceStatus` | reliable / volatile / depth 10 | speech adapter 상태, source topic, 최근 수신/허용/거부 횟수와 사유 |
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

멀티캠 화면 자체는 `/preview/cam_*`와 `/preview/flir`의 5 Hz pass-through
CompressedImage만 구독한다. 인식 화면은 1.7이 합성한
`/perception/debug/final_overlay/compressed` 한 장과
`/perception/debug/final_overlay/status`만 구독하며, 브라우저에서 여러 JPEG
레이어를 exact-stamp로 다시 합성하지 않는다.

`cv_contract_monitor`와 Debug gateway는 reliable / transient local / depth 1로
snapshot QoS를 맞춘다. 따라서 gateway가 늦게 시작해도 마지막 CV 계약 상태를 받을
수 있다. 반면 surgeon sentence와 admitted text 같은 실시간 문자열은 reliable /
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
`distance_m`만 포함하며, 5 cm 조절은 `distance_m=0.050`이다. Service 화면은
`request_accepted`, `result_code`, 응답 `command_id`, `message`만 표시한다.
이는 Request admission 확인일 뿐, 물리 동작의 완료·진행률·상태·Tool 부착을
의미하지 않는다. 문서에 없는 자세, 속도 또는 상세 제어 상태는 Taskplanner가
만들지 않는다.

석션 로봇암 제어와 상태 UI는 제공하지 않는다. 임상 도구인 석션과 석션
관련 음성은 도구 전달 및 공개 증거 경로에서 계속 사용할 수 있지만,
bed-mounted robot-arm 또는 의료기기 흡입 제어 명령으로 변환하지 않는다.

기존 Tool Change와 Retraction Adjustment의 별도 form/Action Cancel은 더 이상
제공하지 않는다. 상태 화면은 `stamp`, `revision`, `procedure_type` 및 각 arm의
`arm_id`, `role`, `role_instance_id`, `state`, `direct_teach_active`,
`reason_code`만 사용한다.

수동 명령 전에는 **수동 제어 활성화**가 필요하다. 활성 상태는 UI heartbeat가 끊기면 6초 안에 자동 해제된다. 동시에 하나의 명령만 허용한다.

`/surgery/retraction/command`는 Response가 돌아오면 admission 조회를 끝낸다.
그 뒤의 원격 동작을 Debug Mode가 완료·취소·복구됐다고 추정하지 않는다. Action
watchdog, Cancel 및 복구 카드는 계속 Action인 `/surgery/tool_handover`에만
적용된다.

운영 프로파일의 Debug sidecar는 `/simulation/state`를 안전 interlock으로
사용한다. 최신 `execution_state`가 `idle`, `halted`, `completed`,
`terminated` 중 하나이고 `running=false`, 활성 로봇 task 없음, 로봇·cleaner
비활성까지 모두 확인될 때만 `operational_runtime_stopped=true`가 된다.
여기에 Fault가 없고 진행 중 Action도 없어야 `manual_control_available=true`가
되어 수동 제어를 활성화할 수 있다. 실행 중, 상태 미수신, freshness 만료 또는 안전을
확정할 수 없는 값은 모두 **운영 시나리오 실행/상태 불명**으로 처리한다.
시나리오 정지가 확인되어도 Fault나 진행 중 Action 등 다른 안전 조건이 남아
있으면 별도의 **수동 잠금** 상태를 표시한다. 수동 Action·Service·더미
토픽·문장·마이크 명령은 운영 화면에서 시나리오를 정지하고 Debug 화면에
**운영 시나리오 정지 확인**과 **수동 활성화 가능**이 모두 표시된 뒤 시험한다.
운영 컨테이너 자체를 종료할 필요는 없다.

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

**USB 음성·로그** 탭에서 완성 문장을 직접 입력해
`/sensors/surgeon/sentence`로 발행할 수 있다. standalone Debug에서는 호스트에
연결한 USB 마이크로 Puzzle AI WebSocket ASR도 시험할 수 있다. live 운영
런타임에 포함된 통합 Debug sidecar에서는 `asr_start`를 거부하며, 같은 UI의
운영 화면 **수술실 음성 입력**에서 USB ASR을 시작해야 한다. 통합 Debug의 장치
새로고침과 중지는 허용한다. 이 구분은 Debug publisher가 운영 preflight를 대신
만족한 직후 시나리오 interlock으로 사라지는 경로를 막는다. 브라우저는
이 토픽을 직접 advertise하지 않고 `publish_voice_command` 백엔드
명령을 통해 수동 제어·Fault·운영 시나리오 interlock을 다시 확인한 후
발행한다. 마이크 세션은 사용자가 명시적으로 시작할 때만 열리고,
서버가 `is_final`로 확정한 비어 있지 않은 문장만 같은 ROS 토픽으로
발행한다. 중간 인식 문장은 화면 진단용으로만 표시하며 ROS에
발행하지 않는다.

standalone Debug 마이크 시작 전에는 화면 최상단의 **수동 제어 활성화**가 필요하다. 이 전역
제어는 조그 탭 안에 중복 표시하지 않는다. 수동 제어 해제, UI
heartbeat timeout, Fault 또는 런타임 종료 시 마이크를 자동 중지한다. 자체 ASR
publisher가 존재한다는 이유만으로 입력 준비 완료로 보지 않으며, WebSocket이
실제로 연결된 `LISTENING` 상태일 때만 readiness를 만족한다. LAN 프록시는
인증 경계가 아니므로 마이크와 전사 원문은 신뢰된 격리 시험망에서만 사용한다.

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

도구 전달용 **음성 즉시 실행**과 리트랙터용 **음성 + 버튼**은 서로 다른
게이트다. 전자는 설정된 도구 별칭만 결정적으로 변환한다. 후자는 조그·수동
실행 탭에서 별도로 켜며, USB ASR 탭이 이미 발행한 final 문장을 text-only
VLM에 비동기로 전달한다. 리트랙터 음성 게이트는 별도 마이크나 ASR 세션을
열지 않으며, standalone Debug의 마이크 캡처 소유자는 **USB 음성·로그** 탭
하나뿐이다. 같은 탭의 수동 문장 입력도 동일한 final 문장 토픽을 사용하므로
해당 게이트가 켜져 있으면 리트랙터 음성 입력으로 처리된다. VLM 응답은 6개
명령의 폐쇄형 스키마, 현재 Debug 내부 상태, 원문
근거를 다시 검증한 뒤에만 단일 `/surgery/retraction/command` Service 요청이
된다. 모델이 없거나 timeout·형식 오류가 나면 동일한 공용 결정론 정규화기로
폴백하고, 화면과 이벤트 로그에 `interpreter_source`와 `vlm_invoked`를 표시한다.
Service 응답은 요청 접수 여부일 뿐 물리 실행·완료를 뜻하지 않는다.

## 수술기록 생성 API 시험

**수술기록 API** 탭은 권위 있는 `0704_6`–`0704_17` UTF-8 전달용 TXT 12개를 read-only로 표시하고, 선택한 파일 전체를 JSON의 `text` 필드에 넣어 단일 `POST`로 전송한다. `roomName` 기본값은 전임상센터의 영문명인 `Preclinical Center`이며 `surgeryCode`, `date`, HTTPS endpoint는 제출마다 확인한다. `X-API-Key`는 호스트의 mode-0600 비밀 파일에서 백엔드만 읽고 read-only로 마운트한다. 키 값·길이·해시·파일 경로는 브라우저 payload, 상태 snapshot, 이벤트 로그와 요청 이력에 포함하지 않으며 화면에는 설정 여부만 표시한다. 파일 문자 수·바이트 수·SHA-256과 API 제한(65,535자, JSON body 1 MB)을 전송 전에 표시한다.

통합 수신 시험에서 `https://dev.puzzle-ai.com:6627/api/v1/surgery/img_texts`가 실제 요청을 수신하는 주소로 확인되어 현재 canonical endpoint로 사용한다. 서버도 이 endpoint만 allowlist하며, 다른 호스트·경로와 HTTP redirect로 TXT가 전송되는 것을 거부한다. 성공 `201`은 접수 ID와 수신 시각을 확인하는 것이며, 문서에는 생성 결과 조회·다운로드 endpoint가 없다. 따라서 화면은 실제 응답에 결과 본문이 포함되지 않은 이상 “기록 생성 완료”라고 표시하지 않는다. 30초 전후 timeout은 서버가 이미 접수했을 가능성이 있어 자동 재전송하지 않는다.

현재 LAN UI/ROSBridge는 사용자 인증과 TLS를 추가하지 않으므로 신뢰된 격리 시험망에서 비식별 TXT만 전송한다. 외부 운영 전에는 canonical endpoint, 중복 키, timeout 후 조회/reconcile, 결과 schema, API 키 회전·폐기 정책을 Puzzle AI 측과 확정해야 한다.

## 내부 진단 인터페이스

| 방향 | 이름 | 타입 | 실제 QoS/호출 주체 | 의미 |
|---|---|---|---|---|
| gateway → browser | `/integration/debug/status` | `std_msgs/msg/String` | reliable / volatile / depth 10 | UI가 소비하는 `taskplanner.integration_debug.status.v1` 전체 snapshot |
| gateway → ROS | `/integration/debug/events` | `std_msgs/msg/String` | reliable / volatile / depth 50 | 개별 검증 이벤트 JSON; UI는 주로 status의 bounded `recent_events`를 사용 |
| gateway → browser | `/integration/debug/readiness` | `std_msgs/msg/String` | reliable / volatile / depth 10 | sentence publisher, Tool Action, retraction Service, bed-arm status 네 가지 readiness |
| browser → gateway | `/integration/debug/heartbeat` | `std_msgs/msg/String` | reliable / volatile / browser queue 1, gateway depth 5 | 현재 Debug 세션 ID를 담은 UI 생존 신호 |
| browser → gateway | `/integration/debug/command` | `surgical_msgs/srv/IntegrationDebugCommand` | 운영 통합 Debug bridge의 유일한 mutation Service; standalone bridge는 별도로 world-anchor 4개 Trigger도 허용 | arm/disarm, 수동 문장·출력, Action/Service 요청을 서버 interlock 뒤에서 중계 |
| ROS client → gateway | `/integration/debug/check_readiness` | `std_srvs/srv/Trigger` | Service; 현재 secure browser allowlist에는 없음 | 현재 Debug readiness를 즉시 질의하고 readiness topic에도 발행 |

`status` JSON의 최상위 필드는 `schema`, `stamp_sec`, `session`, `runtime`,
`inputs`, `endpoints`, `action`, `outputs`, `voice`, `vlm`, `virtual_robot`,
`asr`, `surgery_record`, `recent_events`다. 운영 interlock 진단에는
`runtime.operational_running`, `operational_active_robot_task_id`,
`operational_robot_state`, `operational_cleaner_busy`,
`operational_state_publishers`, `operational_state_expected_publisher`,
`operational_state_publisher_trusted`, `operational_state_age_sec`,
`operational_state_fresh`, `operational_runtime_stopped`를 함께 사용한다.
리트랙터 해석 상태는 `voice.retraction` 아래의 `mode`, `internal_state`,
`interpreter_mode`, `interpreter_pending`, `interpreter_pending_age_sec`,
`allowed_commands`, `service_ready`, `in_flight`, `last_interpretation`,
`last_rejection_reason`으로 전달된다. Service 응답은 `action`의
`response_semantics=admission`, `request_accepted`, `result_code`,
`response_message`로 구분한다.

리트랙터 패널의 `force_retraction_idle`은 Debug 내부 admission 상태만
`idle`로 초기화하는 복구용 UI 명령이다. 진행 중 Action/Service 요청이 없어야
하며 `remote_motion_stopped_confirmed=true` 확인이 필요하다. 실행 시 수동 제어와
리트랙터 음성 전송 권한을 해제하고, 외부 Service 또는 로봇에는 어떤 명령도
보내지 않는다. 따라서 상대 로봇의 정지나 물리 상태를 증명하는 기능이 아니다.

브라우저는 `/surgery/tool_handover` Action이나
`/surgery/retraction/command` Service를 직접 호출하지 않는다. 브라우저가 직접
publish하는 ROS 토픽도 heartbeat 하나뿐이다. 모든 수동 요청은
`/integration/debug/command`를 통과한 뒤 gateway가 상대 Action/Service client가
되어 전송한다. 따라서 브라우저 통합 테스트는 아래 다섯 op만 있으면 충분하다:
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
| Debug 수동 문장 | command Service의 `publish_voice_command` | `/sensors/surgeon/sentence` | final 문장 1회 및 status의 last sentence 갱신 | 아니요 |
| Debug 리트랙터 해석 단위 시험 | ROS 없는 interpreter 함수에 final text와 local state 입력 | 폐쇄형 6-command 결과 또는 rejection | side 누락·양측·범위 밖 거리 거부; Service client 호출 0회 | 아니요 |
| Debug 리트랙터 통합 시험 | `virtual` 선택 + command Service 요청 | 내장 Service가 받은 admission 결과와 Debug status | 정확한 enum/side/metres/command_id; state는 `request_accepted + RESULT_ACCEPTED`일 때만 전이 | 아니요 |
| Debug Tool Action 통합 시험 | `virtual` 선택 + command Service 요청 | 내장 Action feedback/result와 Debug status | Goal field·command_id 상관, terminal Result 및 watchdog 경계 확인 | 아니요 |
| Live STT 어댑터 단위 시험 | `/sensors/surgeon/sentence` 완성 String | `/surgery/audio/request_text`, `/input/speech/status` | 공백 정규화, 빈 문장·1초 내 동일 문장 거부, accepted count 1 증가 | 아니요 |
| Live 리트랙터 경로 통합 시험 | final sentence → `/surgery/audio/request_text`; fake controller status와 fake Service | `/surgeon/bed_robot_arm_group_request` → `/bt/bed_robot_arm_group_command` → Service Request → `/bed_robot_arm_group/status` | VLM/폴백 provenance, 6명령 매핑, 한 Service lane, admission-only state 전이 확인 | fake server만 |
| Mock 직접 계약 시험 | `fault_action_emulator`의 fake Action/Service와 bed-arm status | Live와 동일 public 종단 | 외부 하드웨어 없이 public wire contract와 실패 주입 검증 | 아니요 |

Live의 리트랙터 음성 경로에서 각 노드가 소유하는 실제 ROS 인터페이스는 다음과
같다.

| 소유 노드 | 구독/서버 입력 | 발행/client 출력 |
|---|---|---|
| `taskplanner_asr` | `/input/asr/control` (`AsrControl` Service) | `/input/asr/runtime_status` String snapshot, 연결 중에만 `/sensors/surgeon/sentence` String |
| `speech_input_adapter` | `/sensors/surgeon/sentence` String, `/simulation/control_state` String | `/surgery/audio/request_text` String, `/input/speech/status` `InputSourceStatus` |
| `bed_robot_arm_group_orchestrator` | request/proposal/group status/world/controller status/audio text/control state | `/surgeon/bed_robot_arm_group_request`, `/bt/bed_robot_arm_group_command`, `/bed_robot_arm_group/status`, `/bed_robot_arm_group/voice_normalization_status` |
| `surgical_interop_execution_bridge` | `/bt/skill_command`, `/bt/bed_robot_arm_group_command`, `/simulation/control_state`, `/external/bed_robot_arms/status` | `/skill/status`, `/skill/events`, `/bed_robot_arm_group/status`; `/surgery/tool_handover` Action client; `/surgery/retraction/command` Service client |
| 상대 또는 mock controller | `/surgery/tool_handover` Action server, `/surgery/retraction/command` Service server | `/external/bed_robot_arms/status` controller-owned 상태 |

세션 이벤트는 `${TASKPLANNER_RUN_ROOT}/debug/<session-id>/events.jsonl`에 JSONL로 남는다.

## 상대 기관과의 확인 순서

1. 양측 `ROS_DOMAIN_ID`, discovery 범위, 네트워크 multicast/participant discovery를 맞춘다.
2. **연결·입력**에서 상대 publisher, 타입, QoS, 실측 Hz, freshness를 확인한다.
3. **출력 검증**에서 해당 토픽을 1회 발행하고 상대 기관 echo/callback 로그를 확인한다.
4. Action/Service 서버가 발견되면 **수동 제어 활성화** 후 가장 작은 안전 명령부터 실행한다.
5. feedback/result/reason code와 양측 로그의 동일 command id를 대조한다.
6. 완료 후 **전체 정지**, **수동 제어 해제**, 디버그 모드 종료를 수행한다.
