# Taskplanner 통합 디버그 모드

통합 디버그 모드는 전체 시나리오, BT, Digital Twin, VLM, Surgeon Actor를 실행하지 않고 외부 기관과 ROS 2 입출력 및 개별 로봇 기능만 확인하는 운영 모드다. 조그는 디버그 모드의 리트랙터 수동 기능으로 포함된다.

## 실행

```bash
scripts/taskplanner up debug --build
```

- UI: `http://127.0.0.1:4174`
- Debug ROSBridge: `ws://127.0.0.1:9091`
- 기본 포트와 ROS 도메인은 `docker/orchestration/debug.env`와 로컬 `.env`에서 바꿀 수 있다.
- 같은 Compose 프로젝트에서 `taskplanner-runtime` 또는 `shadow-runner`가 실행 중이면 디버그 모드 시작을 거부한다.
- Debug 프로파일은 `webapp`과 `integration-debug`만 시작하며 GPU·모델·perception 서비스를 시작하거나 중지하지 않는다.

기존 Taskplanner UI의 상단 **디버그 모드** 버튼 또는 실행 직후 기본 Debug 화면에서 진입한다. 나갈 때 연속 더미 발행을 정지하고 수동 제어를 해제한다.

## LAN 및 DDS Discovery 설정

**연결·입력** 화면 상단의 **LAN 연결과 Discovery** 패널에서 현재 기본 네트워크 인터페이스, 로컬 IPv4/CIDR, 게이트웨이, multicast 지원 여부, RMW 구현을 확인할 수 있다. 여러 물리 네트워크가 활성화되어 있으면 보조 IPv4 주소도 펼쳐서 볼 수 있다.

- **이 컴퓨터만 (`LOCALHOST`)**: DDS participant discovery를 로컬 호스트로 제한한다.
- **같은 LAN (`SUBNET`)**: 같은 LAN의 다른 컴퓨터를 DDS discovery 대상으로 허용한다.
- **ROS Domain ID**: `0`–`232` 사이의 정수를 직접 입력한다. 상대 기관 컴퓨터와 반드시 같은 값을 사용한다.
- **상대 컴퓨터 핑 테스트**: 상대 IPv4 주소에 ICMP Echo를 3회 보내 응답 수, 손실률, 평균 RTT를 표시한다. 핑은 DDS 설정을 변경하지 않는다.

DDS 설정은 이미 시작된 ROS 2 프로세스에서 안전하게 교체할 수 없으므로, **적용하고 재연결**을 누르면 설정을 저장한 뒤 `integration-debug` 런타임 컨테이너만 재시작한다. 웹 UI는 유지되며 ROSBridge에 자동 재연결한다. 수동 제어가 활성화되어 있거나 Action이 실행 중이거나 연속 더미 토픽을 발행 중일 때에는 설정 변경을 거부한다.

설정은 `${TASKPLANNER_RUN_ROOT}/debug/network-settings.json`에 보존되어 다음 디버그 모드 시작에도 적용된다. 전체 Taskplanner 런타임이나 다른 Compose 프로젝트는 재시작하지 않는다. LAN 연결 확인에는 `SUBNET`, 동일 Domain ID, 양측 multicast 허용, 호스트 방화벽의 DDS/RTPS 트래픽 허용이 모두 필요하며, 핑 성공만으로 DDS discovery 성공을 의미하지는 않는다.

`TASKPLANNER_DEBUG_NETWORK_INTERFACE`를 지정하면 Debug UI의 `LOCAL IP`는 해당
인터페이스만 주 주소로 사용한다. 이 PC의 기본값은 유선 5GbE 포트
`enp13s0`이다. 케이블이나 IPv4 주소가 없을 때 Wi-Fi 주소로 대체하지 않고
`유선 IP 없음`을 표시하므로, 상대 기관 LAN을 연결한 뒤 유선 주소가 실제로
할당됐는지 확인할 수 있다.

## 외부에서 들어오는 입력

| 토픽 | 타입 | 기본 QoS | 화면에서 확인하는 값 |
|---|---|---|---|
| `/sensors/surgeon/sentence` | `std_msgs/msg/String` | reliable / volatile | publisher, 실측 Hz, 최근 문장, freshness |
| `/surgery/images/flir/compressed` | `sensor_msgs/msg/CompressedImage` | sensor data | publisher, 실측 Hz, bandwidth, freshness |
| `/surgery/images/cam4/compressed` | `sensor_msgs/msg/CompressedImage` | sensor data | publisher, 실측 Hz, bandwidth, freshness |

상태 화면은 발견된 publisher 노드, 실제 타입, 실제 QoS, 누적 메시지 수, 5초 rolling Hz, 대역폭, 마지막 수신 경과 시간을 보여준다. 타입 불일치, 저주기, stale, publisher 없음은 서로 다른 상태로 표시된다.

## 외부 로봇으로 보내는 명령

| 종단 | 타입 | 디버그 기능 |
|---|---|---|
| `/surgery/tool_handover` | `surgical_interop_msgs/action/ExecuteToolHandover` | 도구명·인스턴스·허용된 source/target을 직접 입력해 Goal 실행 |
| `/surgery/retraction` | `surgical_interop_msgs/action/ExecuteRetraction` | 방향별 단일 `MOVE` Goal, `RELEASE`, `CHANGE_END_EFFECTOR` |
| `/surgery/suction/set` | `surgical_interop_msgs/srv/SetSuction` | 명시적 ON/OFF 요청 및 응답 확인 |

리트랙터 조그 버튼은 연속 속도 명령을 내지 않는다. 버튼을 한 번 누를 때 최대 30 mm의 독립된 `MOVE` Goal 하나만 발행한다. Action 화면은 command id, 상태, progress, elapsed time, reason code와 Cancel 결과를 표시한다.

수동 명령 전에는 **수동 제어 활성화**가 필요하다. 활성 상태는 UI heartbeat가 끊기면 6초 안에 자동 해제된다. 동시에 하나의 명령만 허용하고, Cancel recovery가 실패하거나 Cancel이 거부되면 fault lock으로 전환한다. 전체 Taskplanner 핵심 노드가 같은 ROS 도메인에서 발견되면 활성화와 명령을 거부한다.

## 외부로 발행하는 공개 토픽

| 토픽 | 타입 | QoS |
|---|---|---|
| `/surgery/context` | `surgical_interop_msgs/msg/SurgeryContext` | reliable / transient local |
| `/surgery/instruments` | `surgical_interop_msgs/msg/InstrumentStateArray` | reliable / transient local |
| `/surgery/robots` | `surgical_interop_msgs/msg/RobotStateArray` | reliable / transient local |
| `/surgery/events` | `surgical_interop_msgs/msg/SurgeryEvent` | reliable / volatile |
| `/surgery/clinical_observations` | `surgical_interop_msgs/msg/ClinicalObservationArray` | reliable / transient local |
| `/surgery/health` | `surgical_interop_msgs/msg/SurgeryHealth` | reliable / transient local |

각 토픽은 1회 발행 또는 0.1–10 Hz 연속 발행을 지원한다. 더미 메시지는 임상·수술 상태로 오인되지 않도록 `DEBUG_DUMMY_DATA`, `UNKNOWN`, `integration_debug` 값을 명시하며 확인되지 않은 관찰을 만들지 않는다. 같은 토픽에 다른 publisher가 발견되면 디버그 publisher의 발행을 거부한다. 화면의 Subscriber 수는 DDS discovery 확인값이며, 상대 기관의 실제 callback 처리는 상대 측 echo 또는 로그로 함께 확인해야 한다.

## 문장·마이크 입력

**음성·로그** 탭에서 완성 문장을 직접 입력해 `/sensors/surgeon/sentence`로 발행할 수 있다. 브라우저가 Web Speech API를 제공하면 한국어 마이크 입력도 같은 문장 토픽으로 발행한다. 마이크를 지원하지 않는 브라우저에서도 텍스트 입력은 계속 사용할 수 있다.

선택적으로 **음성 즉시 실행**을 활성화할 수 있다. 이 경로는 VLM이나 BT를 사용하지 않고 설정된 한국어·영어 도구 별칭, 리트랙터 방향·거리, 석션 ON/OFF 문법만 결정적으로 변환한다. 모호하거나 불완전한 문장은 fail-closed로 기록하고 실행하지 않는다.

## 내부 진단 인터페이스

- `/integration/debug/status` (`std_msgs/msg/String`): UI용 JSON 상태
- `/integration/debug/events` (`std_msgs/msg/String`): 검증 이벤트 JSON
- `/integration/debug/command` (`surgical_msgs/srv/IntegrationDebugCommand`): UI 명령 게이트웨이
- `/integration/readiness` (`std_msgs/msg/String`): sentence publisher 및 로봇 종단 준비 상태
- `/integration/check_readiness` (`std_srvs/srv/Trigger`): 현재 readiness 질의

세션 이벤트는 `${TASKPLANNER_RUN_ROOT}/debug/<session-id>/events.jsonl`에 JSONL로 남는다.

## 상대 기관과의 확인 순서

1. 양측 `ROS_DOMAIN_ID`, discovery 범위, 네트워크 multicast/participant discovery를 맞춘다.
2. **연결·입력**에서 상대 publisher, 타입, QoS, 실측 Hz, freshness를 확인한다.
3. **출력 검증**에서 해당 토픽을 1회 발행하고 상대 기관 echo/callback 로그를 확인한다.
4. Action/Service 서버가 발견되면 **수동 제어 활성화** 후 가장 작은 안전 명령부터 실행한다.
5. feedback/result/reason code와 양측 로그의 동일 command id를 대조한다.
6. 완료 후 **전체 정지**, **수동 제어 해제**, 디버그 모드 종료를 수행한다.
