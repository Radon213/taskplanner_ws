# Taskplanner 통합 디버그 모드

통합 디버그 모드는 전체 시나리오, BT, Digital Twin, VLM, Surgeon Actor를 실행하지 않고 외부 기관과 ROS 2 입출력 및 개별 로봇 기능만 확인하는 운영 모드다. 조그는 디버그 모드의 리트랙터 수동 기능으로 포함된다.

## 실행

시연용 운영 런타임과 통합해서 사용할 때는 평소와 같이 운영 모드를 시작한다.

```bash
scripts/taskplanner up live --build
```

이 명령은 운영 런타임, 단일 Web UI와 함께 같은 `taskplanner_ws` Compose
프로젝트의 `integration-debug` 및 유선 LAN 프록시를 시작한다. UI 상단의
**디버그 모드** 버튼으로 화면만 전환하며 별도 4174 UI를 실행하지 않는다.
Debug 화면은 운영 시나리오의 실행 여부와 관계없이 입력·토픽·Action 상태를
계속 모니터링한다. 수동 제어와 쓰기 명령은 `/simulation/state`에서 최신
안전 정지 상태(`idle`, `halted`, `completed`, `terminated`)를 확인한 동안에만
허용하고, 시나리오가 실행 중이거나
상태가 없거나 오래되었으면 fail-closed로 거부한다. 따라서 운영 컨테이너를
내릴 필요 없이 운영 화면에서 시나리오를 정지한 뒤 같은 UI에서 시험할 수 있다.

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
**음성 로그 → 장치 새로고침**을 누르면 되고, USB hotplug 때문에 컨테이너를
다시 생성할 필요는 없다.

- 통합/standalone 공용 UI: `http://127.0.0.1:4173`
- 운영 ROSBridge: `ws://127.0.0.1:9090`
- Debug ROSBridge: `ws://127.0.0.1:9091`
- 기본 포트와 ROS 도메인은 `docker/orchestration/debug.env`와 로컬 `.env`에서 바꿀 수 있다.
- standalone Debug 프로파일은 `webapp`과 `integration-debug`만 시작하며 GPU·모델·perception 서비스를 시작하거나 중지하지 않는다.
- `live`와 `llm-surgeon` 시작 시 Debug sidecar도 같은 Compose 프로젝트에서 자동으로 유지된다.

기존 Taskplanner UI의 상단 **디버그 모드** 버튼 또는 실행 직후 기본 Debug 화면에서 진입한다. 나갈 때 연속 더미 발행을 정지하고 수동 제어를 해제한다.

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
`CYCLONEDDS_URI` 프로파일을 사용한다. 프로파일의 `FragmentSize=1344B`는
1500-byte Ethernet MTU에서 RTPS/UDP/IP 헤더까지 포함한 datagram이 MTU 아래에
머물도록 해 대용량 카메라 sample의 IP fragmentation을 피한다. 설정한 NIC가
호스트에 없으면 UI만 열린 채 DDS가 다른 NIC로 빠지는 상태를 허용하지 않고
Debug ROS 런타임 시작을 거부한다.

Debug 프로파일은 localhost 전용 UI·ROSBridge를 그대로 유지하면서
`integration-debug-lan-proxy`를 함께 시작한다. 프록시는
`TASKPLANNER_DEBUG_NETWORK_INTERFACE`에 현재 할당된 IPv4에만 같은 포트를
열고, 해당 인터페이스의 직접 연결 서브넷에서 들어온 TCP 연결만 localhost
종단으로 전달한다. 따라서 다른 PC에서는 화면에 표시된 주소를 사용해
`http://<유선-IP>:<WEBAPP_PORT>/`로 접속할 수 있고, 브라우저는 같은 호스트의
`ROSBRIDGE_DEBUG_PORT`로 자동 연결한다. Wi-Fi 및 Tailscale 주소에는 별도
리스너를 만들지 않으며, 유선 IPv4가 바뀌면 리스너도 자동으로 다시 바인딩한다.

이 프록시는 사용자 인증을 추가하지 않는다. 같은 유선 서브넷의 사용자는
Debug 화면을 열 수 있으므로, 신뢰된 통합 시험망에서만 실행하고 수동 제어
활성화 및 원격 로봇 안전 확인 절차를 유지해야 한다.

## 외부에서 들어오는 입력

| 토픽 | 타입 | 기본 QoS | 화면에서 확인하는 값 |
|---|---|---|---|
| `/sensors/surgeon/sentence` | `std_msgs/msg/String` | reliable / volatile | publisher, 실측 Hz, 최근 문장, freshness |
| `/synced/cam_1/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | reliable / volatile / depth 20 | publisher, 실측 Hz, bandwidth, freshness |
| `/synced/cam_2/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | reliable / volatile / depth 20 | publisher, 실측 Hz, bandwidth, freshness |
| `/synced/cam_3/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | reliable / volatile / depth 20 | publisher, 실측 Hz, bandwidth, freshness |
| `/synced/cam_4/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | reliable / volatile / depth 20 | publisher, 실측 Hz, bandwidth, freshness |
| `/synced/flir/color/image_raw/compressed` | `sensor_msgs/msg/CompressedImage` | reliable / volatile / depth 20 | publisher, 실측 Hz, bandwidth, freshness |

상태 화면은 발견된 publisher 노드, 실제 타입, 실제 QoS, 누적 메시지 수, 5초 rolling Hz, 대역폭, 마지막 수신 경과 시간을 보여준다. 타입 불일치, 저주기, stale, publisher 없음은 서로 다른 상태로 표시된다.

## 외부 로봇으로 보내는 명령

| 종단 | 타입 | 디버그 기능 |
|---|---|---|
| `/surgery/tool_handover` | `surgical_interop_msgs/action/ExecuteToolHandover` | 도구명·인스턴스·허용된 source/target을 직접 입력해 Goal 실행 |
| `/surgery/tool_change/request` | `surgical_interop_msgs/srv/RequestToolChange` | 물리 로봇팔과 목표 retractor를 지정하고 완료 대기형 툴 교체 결과 확인 |
| `/surgery/retraction/adjust` | `surgical_interop_msgs/action/ExecuteRetractionAdjustment` | 단일 Malleable의 방향 조절 또는 양측 Malleable의 축 방향 조절과 Cancel 확인 |
| `/external/bed_robot_arms/status` | `surgical_interop_msgs/msg/BedRobotArmStateArray` | retraction arm 배열의 revision, 역할, 상태, 직접교시 여부와 reason code 확인 |

리트랙터 조그 버튼은 연속 속도 명령을 내지 않는다. 버튼을 한 번 누를
때 문서의 `single` Goal 하나만 발행하고, 양측 조절은 별도의 `multi` Goal로
발행한다. Action 화면은 command id, `adjusting`/`recovering` 상태, elapsed
time, reason code와 Cancel 결과를 표시한다. 문서에 없는 progress, 자세,
속도 또는 상세 제어 상태는 Taskplanner가 만들지 않는다.

석션 로봇암 제어와 상태 UI는 제공하지 않는다. 임상 도구인 석션과 석션
관련 음성은 도구 전달 및 공개 증거 경로에서 계속 사용할 수 있지만,
bed-mounted robot-arm 또는 의료기기 흡입 제어 명령으로 변환하지 않는다.

Tool Change 폼은 `command_id`, `arm_id`, `target_tool_id`만 송신하고
`success`, `result`, `reason_code`만 표시한다. Retraction Adjustment 폼은
`command_id`, `adjustment_mode`, `target_retractor_id`, `direction_frame`,
`direction`, `axis`, `distance_mm`만 송신하고, Result의 `success`,
`final_state`, `reason_code`와 Feedback의 `state`만 표시한다. 상태 화면은
`stamp`, `revision`, `procedure_type` 및 각 arm의 `arm_id`, `role`,
`role_instance_id`, `state`, `direct_teach_active`, `reason_code`만 사용한다.

수동 명령 전에는 **수동 제어 활성화**가 필요하다. 활성 상태는 UI heartbeat가 끊기면 6초 안에 자동 해제된다. 동시에 하나의 명령만 허용한다.

실행 중인 명령은 Action 서버 소실(기본 유예 5초), Goal 응답 지연(10초), Feedback·Cancel·Result 갱신 정지(30초), 최대 관찰 시간 초과(300초)를 감시한다. 이 조건이나 Cancel 거부가 발생하면 원격 동작을 임의로 종료됐다고 가정하지 않고 `remote_state_unknown`/`FAULT_LOCKED`로 전환해 새 명령을 막는다. 늦게 정상 Result가 도착하면 해당 Command ID를 자동으로 종결 상태에 반영한다.

Result를 끝내 받을 수 없으면 화면의 **Action 복구 필요** 카드에서 상대 로봇 정지 또는 동일 Command ID의 원격 종료 상태를 직접 확인한 뒤 **확인 후 클라이언트 복구**를 누른다. 확인 체크와 화면에 표시된 정확한 Command ID가 모두 일치해야 로컬 클라이언트 상태를 비우며, 복구 후 수동 제어는 자동 재활성화되지 않는다. 감지와 복구 내역은 `action_recovery_required`, `action_late_result_reconciled`, `action_client_recovered` 이벤트로 남는다. 이 버튼은 상대 로봇을 정지시키는 기능이 아니다.

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
| `/surgery/context` | `surgical_interop_msgs/msg/SurgeryContext` | reliable / transient local |
| `/surgery/instruments` | `surgical_interop_msgs/msg/InstrumentStateArray` | reliable / transient local |
| `/surgery/robots` | `surgical_interop_msgs/msg/RobotStateArray` | reliable / transient local |
| `/surgery/events` | `surgical_interop_msgs/msg/SurgeryEvent` | reliable / volatile |
| `/surgery/clinical_observations` | `surgical_interop_msgs/msg/ClinicalObservationArray` | reliable / transient local |
| `/surgery/health` | `surgical_interop_msgs/msg/SurgeryHealth` | reliable / transient local |

각 토픽은 수동 제어가 활성화된 동안 1회 발행 또는 0.1–10 Hz 연속
발행을 지원한다. 이미 시작된 연속 발행의 개별 정지와 전체 정지는
수동 제어가 해제되어도 항상 허용한다. 더미 메시지는 임상·수술 상태로
오인되지 않도록 `DEBUG_DUMMY_DATA`, `UNKNOWN`, `integration_debug`
값을 명시하며 확인되지 않은 관찰을 만들지 않는다. 같은 토픽에 다른
publisher가 발견되면 디버그 publisher의 발행을 거부한다. 화면의
Subscriber 수는 DDS discovery 확인값이며, 상대 기관의 실제 callback
처리는 상대 측 echo 또는 로그로 함께 확인해야 한다.

## 문장·USB 마이크 입력

**음성·로그** 탭에서 완성 문장을 직접 입력해
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

Debug 컨테이너는 raw `/dev/snd` 대신 호스트 PipeWire socket을 사용하고 Ubuntu의 현재 기본 입력 한 개만 노출한다. 장치 새로고침 후 시작하며, 화면에서 캡처 레벨, WebSocket 연결, 전송·응답·드롭 수, 최근 확정 문장을 확인한다. PipeWire가 16 kHz mono를 직접 제공하지 않으면 런타임이 사용 가능한 포맷으로 캡처해 16 kHz mono signed PCM으로 변환한다. 종료 시 남은 PCM은 서버의 8,192-byte 프레임 계약에 맞춰 silence padding한 뒤 EOF를 보낸다.

각 확정 문장의 `response_latency_ms`는 마지막 PCM 청크 송신이 완료된 monotonic
시각부터 해당 `is_final` JSON 응답을 수신한 시각까지의 클라이언트 관측
간격이다. `latency_basis=latest_pcm_send_complete_to_final_receive`,
`latency_correlated=false`를 함께 제공한다. 서버 내부 처리시간만을 뜻하지 않으며
지속 스트리밍 중 마지막 무음 청크가 기준이 될 수도 있다. WebSocket 계약에
발화·요청 ID가 없으므로 발화 단위 서버 처리시간으로 해석하지 않는다.

세션 종료 후 WAV와 확정 문장 TXT가 `${TASKPLANNER_RUN_ROOT}/debug/<session-id>/asr/`에 저장된다. 이는 음성 개인정보가 될 수 있으므로 실제 임상망이 아니라 비식별 통합 시험에서만 사용하고, 세션 산출물의 접근·보존 정책을 별도로 적용한다. 현재 ASR WebSocket 계약에는 별도 애플리케이션 인증이 정의되어 있지 않으며 URL query/userinfo에 자격증명을 넣는 방식은 거부한다.

선택적으로 **음성 즉시 실행**을 활성화할 수 있다. 이 경로는 VLM이나
BT를 사용하지 않고 설정된 한국어·영어 도구 별칭, retractor 툴 교체,
단일/양측 리트랙터 방향과 거리만 결정적으로 변환한다. 석션 발화는
bed-mounted robot-arm 명령으로 해석하지 않는다. 모호하거나 불완전한
문장은 fail-closed로 기록하고 실행하지 않는다.

## 수술기록 생성 API 시험

**수술기록 API** 탭은 권위 있는 `0704_6`–`0704_17` UTF-8 전달용 TXT 12개를 read-only로 표시하고, 선택한 파일 전체를 JSON의 `text` 필드에 넣어 단일 `POST`로 전송한다. `roomName` 기본값은 전임상센터의 영문명인 `Preclinical Center`이며 `surgeryCode`, `date`, HTTPS endpoint는 제출마다 확인한다. `X-API-Key`는 호스트의 mode-0600 비밀 파일에서 백엔드만 읽고 read-only로 마운트한다. 키 값·길이·해시·파일 경로는 브라우저 payload, 상태 snapshot, 이벤트 로그와 요청 이력에 포함하지 않으며 화면에는 설정 여부만 표시한다. 파일 문자 수·바이트 수·SHA-256과 API 제한(65,535자, JSON body 1 MB)을 전송 전에 표시한다.

통합 수신 시험에서 `https://dev.puzzle-ai.com:6627/api/v1/surgery/img_texts`가 실제 요청을 수신하는 주소로 확인되어 현재 canonical endpoint로 사용한다. 서버도 이 endpoint만 allowlist하며, 다른 호스트·경로와 HTTP redirect로 TXT가 전송되는 것을 거부한다. 성공 `201`은 접수 ID와 수신 시각을 확인하는 것이며, 문서에는 생성 결과 조회·다운로드 endpoint가 없다. 따라서 화면은 실제 응답에 결과 본문이 포함되지 않은 이상 “기록 생성 완료”라고 표시하지 않는다. 30초 전후 timeout은 서버가 이미 접수했을 가능성이 있어 자동 재전송하지 않는다.

현재 LAN UI/ROSBridge는 사용자 인증과 TLS를 추가하지 않으므로 신뢰된 격리 시험망에서 비식별 TXT만 전송한다. 외부 운영 전에는 canonical endpoint, 중복 키, timeout 후 조회/reconcile, 결과 schema, API 키 회전·폐기 정책을 Puzzle AI 측과 확정해야 한다.

## 내부 진단 인터페이스

- `/integration/debug/status` (`std_msgs/msg/String`): UI용 JSON 상태
- `/integration/debug/events` (`std_msgs/msg/String`): 검증 이벤트 JSON
- `/integration/debug/heartbeat` (`std_msgs/msg/String`): 현재 Debug 세션 ID를 담은 UI 생존 신호
- `/integration/debug/command` (`surgical_msgs/srv/IntegrationDebugCommand`): UI 명령 게이트웨이
- `/integration/debug/readiness` (`std_msgs/msg/String`): Debug sentence publisher 및 로봇 종단 준비 상태
- `/integration/debug/check_readiness` (`std_srvs/srv/Trigger`): 현재 Debug readiness 질의

세션 이벤트는 `${TASKPLANNER_RUN_ROOT}/debug/<session-id>/events.jsonl`에 JSONL로 남는다.

## 상대 기관과의 확인 순서

1. 양측 `ROS_DOMAIN_ID`, discovery 범위, 네트워크 multicast/participant discovery를 맞춘다.
2. **연결·입력**에서 상대 publisher, 타입, QoS, 실측 Hz, freshness를 확인한다.
3. **출력 검증**에서 해당 토픽을 1회 발행하고 상대 기관 echo/callback 로그를 확인한다.
4. Action/Service 서버가 발견되면 **수동 제어 활성화** 후 가장 작은 안전 명령부터 실행한다.
5. feedback/result/reason code와 양측 로그의 동일 command id를 대조한다.
6. 완료 후 **전체 정지**, **수동 제어 해제**, 디버그 모드 종료를 수행한다.
