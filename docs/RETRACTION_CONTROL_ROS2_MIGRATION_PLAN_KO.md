# 리트랙션 로봇암 제어 ROS 2 패키지 이식 구현 계획

## 1. 문서 목적

이 문서는 `192.168.1.37`의 Windows/WSL/Jupyter 기반 리트랙션 로봇암 제어 코드를 문제점을 수정하면서 EIR-NUC(`192.168.1.2`)의 정식 ROS 2 패키지로 이식하기 위한 구현 계획이다.

목표는 단순한 파일 복사가 아니다. 현재 노트북에 들어 있는 로봇 제어 알고리즘, 힘·토크 센서 처리, 교시 데이터 기록, 리트랙션 및 조그 동작을 테스트 가능한 모듈로 분리하고 다음 실행 경로로 교체한다.

```text
현재
Taskplanner
  -> 1.37 WSL ROS 2 Service/Topic bridge
  -> rosbridge
  -> Windows Jupyter kernel
  -> IndyDCP3 + AFT200

목표
Taskplanner
  -> /surgery/retraction/command
  -> EIR-NUC의 retraction_control ROS 2 package
  -> IndyDCP3 + AFT200
  -> STEP2/EIR controller (192.168.1.137)
```

목표 구성에서는 Jupyter Notebook, `roslibpy`, rosbridge, Windows Python kernel을 실행 의존성으로 사용하지 않는다.

---

## 2. 구현 원칙

1. `1.37`은 구현 기간 중 계속 변경되는 upstream으로 취급한다.
2. 파일 수정 시각이 가장 최신이라는 이유만으로 배포 기준본으로 채택하지 않는다.
3. upstream 스냅샷, Python 환경, 실행 셀, 로봇/센서 설정이 함께 일관된 경우에만 기준본으로 승인한다.
4. 노트북 코드를 그대로 한 파일로 옮기지 않고 제어 코어, 하드웨어 어댑터, 상태머신, ROS 인터페이스로 분리한다.
5. 기존 공개 Service IDL은 유지한다.
6. Service 응답은 요청 접수 결과이고, 물리 실행 완료를 의미하지 않는다.
7. 실제 실행 상태와 오류는 로봇 상태 Topic 및 진단 정보로 별도 보고한다.
8. 로봇 SDK와 CAN 장치는 하나의 제어 프로세스만 소유한다.
9. 기존 1.37 환경은 최종 전환 완료 전까지 롤백 수단으로 보존한다.
10. SDK 라이선스, 암호, 토큰은 소스 코드와 스냅샷에 포함하지 않는다.

---

## 3. 현재 기준선과 변경 추적

### 3.1 계획 수립 기준선

- 캡처 시각: `2026-08-20 11:22 KST`
- 이 기준선은 구현 동결본이 아니라 변경 추적을 시작하기 위한 관찰 스냅샷이다.
- 실제 구현 시작 전과 최종 전환 직전에 새 스냅샷을 다시 생성해야 한다.

| 파일 | 수정 시각 | SHA-256 |
|---|---|---|
| `retraction_command_server_node.cpp` | 2026-08-20 10:12:26 KST | `9adfd52fd053b9f698674babd495e79a05b30285ab2e9731be111be6804dbcf6` |
| `retraction_command_bridge_node.cpp` | 2026-08-20 10:21:31 KST | `9cb134721881ad93084bdd866d82557fb2fdf56d93d3c9515b5225b674b3ebb2` |
| `retraction_server/CMakeLists.txt` | 2026-08-20 10:19:11 KST | `d878c86b1cade1fdbfc6d3f8dbeb130e53d84a4eeb0664576b6cee48a8698321` |
| `retraction_server/package.xml` | 2026-08-19 21:10:34 KST | `b9a1bd3d626fff6ee8f2e5ea8631cf6f54257ea21af801dbb174fd4dd286cf8a` |
| `ETRI_throat_control_fin.ipynb` | 2026-08-20 10:58:49 KST | `b53cbe05feca4d476b0ac8d68e6b03d33a5f7202206951153275d673f15fb5e0` |
| `ETRI_hernia_control_both.ipynb` | 2026-08-19 22:08:11 KST | `1f2e06e4b43089e180f8d95210cec1aef399e8d855ae46048eb85b0a45fc08a0` |
| `sensor_logger_fin.py` | 2026-08-10 23:04:32 KST | `a110c8720de4a34c18aaab733d1ae7b9665064f78925870a61a522377dec4713` |
| `sensor_logger_test.py` | 2026-08-11 22:42:35 KST | `42bc49619c9d29e337e21fe0bd27c069c202fa57e1b378ec360ae33758c98a1e` |
| `set_control_gain.py` | 2026-08-14 16:47:29 KST | `e1bb14cfcf9ddb278609420a33f660be458f88abfd6b5f205c334563c01731d5` |

### 3.2 확인된 upstream 드리프트

계획 검토 중 `ETRI_throat_control_fin.ipynb`가 약 54 KB 버전에서 59,631 byte 버전으로 다시 수정되었다. 또한 11:22 KST 기준으로 다음 불일치가 존재한다.

- 최신 throat 노트북은 `/surgery/retraction/local_command` Topic을 구독한다.
- 최신 C++ `retraction_command_bridge_node`는 같은 이름의 `ExecuteLocalCommand` Service를 호출한다.
- 따라서 각 파일의 최신 버전을 단순히 묶은 결과는 하나의 일관된 실행 경로가 아니다.
- Jupyter에서 현재 실행 중인 셀의 상태는 저장된 `.ipynb` 내용과 다를 수 있다.

이 때문에 구현 전에 반드시 “파일 스냅샷”과 “실행 기준본”을 구분해야 한다.

### 3.3 스냅샷 절차

구현 저장소에는 다음 기능을 가진 읽기 전용 upstream 캡처 도구를 추가한다.

1. 지정한 파일만 수집하고 C: 루트 전체를 탐색하지 않는다.
2. 파일 경로, 크기, 수정 시각, SHA-256을 `manifest.json`에 기록한다.
3. Notebook 원본과 실행하지 않은 Python export를 함께 보존한다.
4. `pip freeze`, Python 버전, Neuromeka SDK 버전, ROS 배포판을 기록한다.
5. 로봇 IP, CAN 채널, 팔/센서 매핑, waypoint 이름은 기록하되 라이선스 키는 마스킹한다.
6. 이전 스냅샷과 함수·상수·설정값의 차이를 자동 보고한다.
7. 새 변경을 다음 범주로 분류한다.
   - 제어 알고리즘 변경
   - waypoint/게인/센서 보정 변경
   - ROS 연결 방식 변경
   - Notebook 실험·시각화 변경
   - 실행과 무관한 출력 셀 변경
8. 상대 팀이 하나의 동작 가능한 조합으로 확인한 스냅샷만 `accepted-upstream` 태그로 승인한다.

최종 코드 동결 시에는 승인된 스냅샷 이후 발생한 모든 차이를 다시 검토하고 누락된 제어 변경이 없음을 양측이 확인한다.

---

## 4. 목표 배포 환경

### 4.1 권장 대상

- 호스트: EIR-NUC `192.168.1.2`
- OS: Ubuntu 24.04
- ROS 2: Jazzy
- RMW: Cyclone DDS
- ROS domain: 리트랙션은 `ROS_DOMAIN_ID=0`
- 로봇 컨트롤러: STEP2 `192.168.1.137`
- Python: 패키지 전용 Python 3.11 또는 검증된 동일 환경
- Neuromeka SDK: `3.5.0.7` 고정

EIR-NUC에는 Linux용 `neuromeka 3.5.0.7` wheel과 필요한 DCP3 API가 이미 존재한다. 하지만 기존 tool-handover 환경과 리트랙션 환경은 분리한다.

### 4.2 기존 EIR tool-handover와의 격리

EIR의 기존 `arpa-h-handover` workspace는 Taskplanner와 다른 버전의 `surgical_interop_msgs`를 사용한다. 따라서 해당 workspace를 직접 덮어쓰거나 같은 shell에서 overlay하지 않는다.

```text
/home/user/ws/arpa-h-handover
  - 기존 Tool Handover
  - ROS_DOMAIN_ID=97
  - 기존 interface package 유지

/home/user/ws/retraction-control
  - 신규 Retraction Control
  - ROS_DOMAIN_ID=0
  - Taskplanner와 동일한 ExecuteRetractionCommand IDL 사용
```

각 runtime은 독립 venv, 독립 ROS workspace, 독립 launch script를 사용한다.

### 4.3 우리 Ubuntu 호스트의 역할

우리 Ubuntu 26.04 호스트는 Taskplanner와 자동 테스트의 기준 환경으로 사용한다. 동일한 `retraction_control` 소스를 빌드하고 fake adapter 기반 통합 테스트까지 수행하되, 첫 실기 배포 대상은 EIR-NUC로 한다.

우리 호스트에서 직접 로봇을 제어하는 구성도 가능하지만 다음 항목이 추가로 검증되기 전에는 production 대안으로 승격하지 않는다.

- ROS 2 Lyrical의 system Python 3.14에서 Neuromeka 3.5.0.7과 모든 gRPC 의존성이 정상 동작하는지
- AFT200 USB-CAN 장치와 Linux 권한/udev 설정
- Taskplanner와 hardware worker를 같은 프로세스로 실행할지, 격리된 worker로 실행할지
- EIR-NUC와 동일한 profile 및 call trace를 재현하는지

패키지의 제어 코어와 테스트는 ROS 배포판에 종속되지 않게 작성하고, ROS node/launch 계층에서 Jazzy와 Lyrical 차이를 격리한다.

---

## 5. ROS 패키지 구조

현재 Python 기반 제어 로직과 Neuromeka SDK를 재사용하기 위해 `ament_python` 패키지로 구현한다.

```text
retraction_control/
├── package.xml
├── setup.py
├── setup.cfg
├── resource/retraction_control
├── launch/
│   └── retraction_control.launch.py
├── config/
│   ├── throat.yaml
│   ├── hernia.yaml
│   └── logging.yaml
├── retraction_control/
│   ├── __init__.py
│   ├── command_server_node.py
│   ├── command_executor.py
│   ├── state_machine.py
│   ├── command_models.py
│   ├── teaching_session.py
│   ├── profile_loader.py
│   ├── diagnostics.py
│   ├── adapters/
│   │   ├── indy_dcp3.py
│   │   ├── aft200.py
│   │   └── clock.py
│   └── algorithms/
│       ├── joint_targets.py
│       ├── impedance.py
│       ├── force_jog.py
│       └── force_analysis.py
└── test/
    ├── test_command_validation.py
    ├── test_state_machine.py
    ├── test_distance_conversion.py
    ├── test_side_mapping.py
    ├── test_teaching_session.py
    ├── test_force_jog.py
    ├── test_error_propagation.py
    ├── test_idempotence.py
    └── test_launch_contract.py
```

### 5.1 실행 프로세스

초기 구현은 하나의 ROS node와 하나의 CAN reader thread를 사용한다.

- `command_server_node`
  - `/surgery/retraction/command` Service 제공
  - 요청 검증, command ID 중복 방지, 작업 큐 관리
  - 상태머신과 executor 호출
  - `/external/bed_robot_arms/status` 상태 발행
  - `/diagnostics` 진단 발행
- `Aft200Adapter`
  - USB-CAN 장치 단독 소유
  - 수신 전용 thread에서 최신 샘플과 timestamp 관리
  - 교시 및 임피던스 기록에 동일한 버스를 재사용
- `IndyDcp3Adapter`
  - SDK 연결 단독 소유
  - SDK 호출을 명시적인 결과/예외로 변환
  - fake adapter를 주입할 수 있도록 interface 분리

ROS callback에서 장시간 motion 함수를 직접 실행하지 않는다. Service callback은 유효한 요청을 작업 큐에 접수한 뒤 반환하고, 물리 실행은 단일 worker가 순차 처리한다.

---

## 6. 외부 ROS 계약

### 6.1 유지할 Service

- 이름: `/surgery/retraction/command`
- 타입: `surgical_interop_msgs/srv/ExecuteRetractionCommand`
- Taskplanner와 EIR에서 IDL SHA-256이 동일해야 한다.
- `protocol_version`, `source_id`, `command_id`를 실제로 검증한다.
- 같은 `command_id`의 재전송은 물리 동작을 중복 실행하지 않는다.

### 6.2 응답 의미

`request_accepted=true`는 다음만 의미한다.

- 요청 형식이 유효하다.
- 현재 상태에서 요청을 받을 수 있다.
- command ID가 중복 실행 대상이 아니다.
- 내부 실행 큐에 접수되었다.

이는 이동, 교시, tool change 또는 retraction의 물리 완료를 의미하지 않는다.

### 6.3 상태 보고

물리 실행 상태는 기존 `BedRobotArmStateArray` 기반 `/external/bed_robot_arms/status`로 보고한다. 최소 상태 정보는 다음과 같다.

- controller 연결 여부와 source timestamp
- 내부 상태와 revision
- active command ID
- 대상 팔/역할
- 진행 중 operation
- terminal outcome 또는 fault
- 마지막 오류 코드와 사람이 읽을 수 있는 메시지

상세 센서 및 프로세스 상태는 `diagnostic_msgs/DiagnosticArray`의 `/diagnostics`로 발행한다.

---

## 7. 내부 상태머신

```text
IDLE
  -> START_DIRECT_TEACH -> DIRECT_TEACHING

DIRECT_TEACHING
  -> FINISH_DIRECT_TEACH -> TAUGHT_READY

TAUGHT_READY
  -> START_RETRACTION -> RETRACTING

RETRACTING
  -> ADJUST_RETRACTION -> RETRACTING
  -> CHANGE_TOOL -> TOOL_CHANGING -> RETRACTING
  -> STOP_RETRACTION -> STOPPING -> TAUGHT_READY

어느 상태에서든 복구 불가능한 SDK/CAN 오류
  -> FAULT
```

상태 전이 원칙:

- Service 접수와 물리 상태 전이를 구분한다.
- 물리 상태는 worker가 실제 SDK/센서 결과를 확인한 뒤 변경한다.
- 직접 교시 종료는 유효한 센서 및 관절 데이터가 저장된 경우에만 `TAUGHT_READY`가 된다.
- adjust는 `RETRACTING`에서만 허용한다.
- stop은 실행 중인 작업보다 우선 처리할 수 있는 별도 경로를 가진다.
- 재시작 후 임의로 이전 상태를 복원하지 않는다. 저장된 세션의 무결성을 검증한 뒤 `TAUGHT_READY` 또는 `IDLE`로 결정한다.
- FAULT 해제는 명시적 진단 및 reset 절차를 거친다.

---

## 8. 6개 명령의 물리 의미와 구현

| Service command | 신규 구현 동작 | 성공 판정 |
|---|---|---|
| 직접 교시 시작 | 마찰 보상 설정, custom gain 비활성화, `set_direct_teaching(true)`, AFT200/관절 기록 시작 | SDK 교시 상태와 센서 기록 시작 확인 |
| 직접 교시 종료 | `set_direct_teaching(false)`, 마찰 보상 복원, 기록 종료 및 세션 검증 | 유효한 힘·관절 데이터 저장 확인 |
| Retraction 시작 | 승인된 교시 세션의 목표 관절/힘 계산, custom gain 활성화, 목표 자세 이동 및 유지 | motion 결과와 controller state 확인 |
| Retraction 좌/우 더 | `distance_mm = distance_m * 1000.0`, profile의 side→arm 매핑, 교시 힘 방향 기준 TCP 상대 이동 | 선택 팔의 motion 결과 확인 |
| Tool change | procedure profile의 승인된 waypoint 시퀀스 실행 | waypoint 완료 또는 SDK 오류 확인 |
| Retraction 종료 | motion stop/hold 정책 실행, custom gain 비활성화, 상태 및 기록 정리 | controller가 정지/대기 상태임을 확인 |

조직의 실제 힘이 목표와 일치하는지는 별도의 관찰 결과로 보고하며, 자세 도달만으로 힘 도달을 주장하지 않는다.

---

## 9. 현재 문제점과 수정 방법

| 현재 문제 | 수정 방향 | 필수 테스트 |
|---|---|---|
| `distance_m`를 그대로 `jog:` 값으로 전달 | SI 단위는 경계에서 한 번만 mm로 변환 | `0.050 m == 50.0 mm` |
| 응답 메시지의 cm/mm 의미 불일치 | 내부는 mm, 외부는 m으로 고정하고 UI용 문자열만 명시 변환 | 5 cm, 50 mm, 0.05 m 동치성 |
| `target_side`가 실제 팔 선택에 사용되지 않음 | profile에 `LEFT/RIGHT -> role/arm/sensor` 매핑 정의 | 좌우 fake adapter 호출 분리 |
| throat와 hernia가 서로 다른 이름/IP/축 매핑 사용 | `throat.yaml`, `hernia.yaml`로 procedure profile 분리 | 각 profile schema 및 call trace |
| 내부 실패를 Service success로 반환 가능 | adapter 결과와 예외를 structured execution outcome으로 전파 | SDK/CAN/timeout 실패 주입 |
| 직접 교시 명령이 실제 direct-teaching API 호출을 보장하지 않음 | enable/disable 호출과 controller state 확인을 명시 | 시작/종료 순서 및 rollback |
| Notebook global state에 의존 | typed state machine과 session repository로 이동 | 프로세스 재시작/손상 세션 테스트 |
| `JOG_MAX_MM`, 누적 한계가 사실상 무제한 | procedure별 검증된 제한값을 YAML로 외부화 | 경계값/초과 거부 |
| force 방향·팔·관절 slice에 TODO 존재 | 실물 확인 결과를 versioned calibration profile로 관리 | profile checksum 및 범위 검증 |
| ROS callback에서 장시간 제어가 실행될 수 있음 | worker queue와 우선 stop 경로 도입 | busy/stop/concurrency 테스트 |
| Jupyter 실행 순서에 따라 함수/상태가 달라짐 | import 가능한 모듈과 단일 entry point로 변환 | clean process cold-start 테스트 |
| rosbridge 연결이 끊겨도 notebook이 남을 수 있음 | native rclpy와 ROS graph lifecycle 사용 | DDS disconnect/reconnect 테스트 |
| SDK 라이선스가 코드에 노출됨 | runtime secret로 이동하고 snapshot에서 마스킹 | repository secret scan |
| 상대 경로 `output/...` 사용 | ROS parameter 기반 절대 data directory 사용 | 권한/디스크 부족/파일 원자성 테스트 |
| 동일 SDK에 복수 kernel이 연결될 수 있음 | process lock과 single-owner startup guard | 두 번째 프로세스 시작 거부 |

---

## 10. 설정 및 데이터 관리

### 10.1 YAML로 이동할 항목

- robot IP 및 SDK timeout
- procedure/profile 이름
- arm joint slice
- side/role/sensor mapping
- tool-change waypoint
- home/wait position
- friction compensation level
- custom control gain
- jog axis map/sign/frame
- jog 단일/누적 거리 제한
- force threshold 및 freshness timeout
- impedance target 및 tolerance
- CAN channel, bitrate, sensor ID, sample rate
- log/data directory

### 10.2 코드에 남길 항목

- Service command enum의 의미
- 상태머신 전이 규칙
- SI 단위 변환
- validation 및 error taxonomy
- adapter interface
- 기록 파일 schema

### 10.3 교시 세션 저장

각 세션은 CSV만 저장하지 않고 다음 manifest를 함께 저장한다.

- session ID와 생성 시각
- procedure/profile 버전과 checksum
- robot/controller 식별 정보
- source code 및 config revision
- sensor calibration 정보
- joint/force sample count와 시간 범위
- target pose/force 계산 결과
- 정상 종료 여부와 무결성 checksum

불완전하거나 다른 profile에서 생성된 세션은 retraction 시작에 사용할 수 없다.

---

## 11. 구현 단계

### M0. Upstream 동기화 체계

- [ ] 1.37 scoped snapshot 도구 구현
- [ ] secret redaction 검사 구현
- [ ] Notebook 비실행 export 및 함수 단위 diff 구현
- [ ] 상대 팀과 accepted-upstream 승인 절차 확정
- [ ] 6개 명령별 현재 기대 DCP/CAN call trace 확보

완료 조건: 같은 manifest로 동일한 소스 기준을 재현할 수 있다.

### M1. ROS 패키지 골격 및 fake runtime

- [ ] `retraction_control` ament_python 패키지 생성
- [ ] launch/config/package dependency 구성
- [ ] command model, validator, 상태머신 구현
- [ ] fake Indy/AFT200 adapter 구현
- [ ] 6개 명령의 admission 및 call trace 테스트

완료 조건: 로봇 없이 `colcon test`와 전체 명령 시퀀스가 통과한다.

### M2. 제어 알고리즘 이식 및 문제 수정

- [ ] joint target 합성 이식
- [ ] 교시 recorder와 session repository 구현
- [ ] impedance 계산 및 force monitor 이식
- [ ] force jog와 정확한 m→mm 변환 구현
- [ ] profile 기반 side/arm/sensor 선택 구현
- [ ] tool-change waypoint executor 구현
- [ ] 오류를 structured outcome으로 변경

완료 조건: 승인된 upstream 스냅샷과 동일한 입력에 대해 의도한 call trace를 생성하고 알려진 문제 테스트가 모두 통과한다.

### M3. ROS 및 Taskplanner 통합

- [ ] `/surgery/retraction/command` Service server 구현
- [ ] command ID idempotence ledger 구현
- [ ] worker queue, busy 처리, 우선 stop 구현
- [ ] `/external/bed_robot_arms/status` 발행
- [ ] `/diagnostics` 발행
- [ ] Cyclone DDS domain 0 통합 테스트

완료 조건: Taskplanner client가 6개 요청을 접수하고, physical completion을 과장하지 않는 상태를 수신한다.

### M4. EIR-NUC 비동작 배포 검증

- [ ] 독립 workspace/venv 생성
- [ ] Neuromeka 3.5.0.7 wheel 고정 및 checksum 기록
- [ ] AFT200 USB 장치와 Linux udev/권한 확인
- [ ] CAN 수신률, timestamp, zeroing 검증
- [ ] STEP2 DCP 포트 및 read-only 상태 조회 검증
- [ ] fake adapter를 사용한 cold boot/재시작 검증

완료 조건: 모션 API를 호출하지 않고 DDS, SDK read-only, CAN data-plane이 모두 검증된다.

### M5. Shadow 및 HIL 검증

- [ ] 실제 Service 요청을 받되 motion adapter를 record-only로 실행
- [ ] 1.37의 의도된 call trace와 신규 package call trace 비교
- [ ] 단일 명령별 HIL 절차 승인
- [ ] 직접 교시→종료→retraction→좌/우 adjust→tool change→종료 순서 검증
- [ ] SDK/CAN/DDS 단절과 재시작 복구 검증

완료 조건: 상대 팀과 현장 담당자가 명령 의미, 좌우 매핑, 거리, force 방향, stop 동작을 실물에서 승인한다.

### M6. 전환 및 롤백 준비

- [ ] accepted-upstream 최종 delta 반영
- [ ] 기존 Windows/Jupyter hardware authority 종료 절차 작성
- [ ] 신규 EIR process lock 확인 후 단독 실행
- [ ] launch 또는 systemd supervision 구성
- [ ] 롤백 시 신규 프로세스 종료 후 1.37을 복원하는 절차 검증

완료 조건: 두 제어기가 동시에 로봇 SDK/CAN 장치를 소유하지 않으며 재부팅 후에도 단일 명령으로 정상 기동된다.

---

## 12. 테스트 매트릭스

### 12.1 하드웨어 없는 자동 테스트

- request protocol/version/parameter 검증
- 6개 command 상태 전이
- command ID 중복 및 재전송
- `0.050 m -> 50.0 mm`
- left/right mapping
- SDK exception, timeout, rejection
- CAN stale/missing/invalid sample
- session file 손상 및 profile mismatch
- busy 상태와 stop 우선 처리
- shutdown 중 gain/direct-teach 정리 순서
- launch parameter와 interface type 확인

### 12.2 비동작 장치 테스트

- Neuromeka SDK import 및 버전
- STEP2 연결과 상태 조회만 수행
- AFT200 enumeration, 두 채널 또는 procedure별 채널 확인
- 기대 sample rate와 freshness
- DDS Service discovery와 type hash
- 상태 Topic timestamp/revision

### 12.3 실기 테스트

실기 테스트는 한 번에 하나의 물리 명령만 허용하고 다음 증거를 남긴다.

- 요청과 command ID
- 상태 전이
- 실제 DCP 호출과 결과
- 관절/pose 변화
- 힘·토크 그래프
- 선택된 side/arm
- operator 관찰 결과
- stop 및 fault recovery 결과

---

## 13. 최종 인수 기준

다음 조건을 모두 만족해야 이식 완료로 판단한다.

- [ ] Windows, WSL, Jupyter, rosbridge 없이 cold boot 가능
- [ ] 하나의 ROS launch로 Service, SDK, CAN, 상태 발행 기동
- [ ] Taskplanner와 Service IDL/type hash 일치
- [ ] 6개 명령이 지정된 상태와 물리 의미로 실행
- [ ] 5 cm 요청이 정확히 50 mm로 실행
- [ ] left/right가 서로 다른 승인된 팔/role로 전달
- [ ] 내부 실패가 success로 보고되지 않음
- [ ] 접수 결과와 물리 완료 상태가 분리됨
- [ ] 교시 세션이 재시작 후에도 검증 가능
- [ ] SDK/CAN/DDS 단절 시 FAULT 또는 명시적 거부
- [ ] 기존 tool-handover domain 97 환경에 영향 없음
- [ ] 기존 1.37을 보존한 롤백 절차 검증
- [ ] 라이선스와 기타 secret이 저장소에 없음
- [ ] 상대 팀의 최종 upstream 변경분이 누락 없이 반영됨

---

## 14. 구현 시작 전에 확정할 상대 팀 입력

다음 정보는 추측하지 않고 상대 팀의 승인값을 받는다.

1. throat와 hernia 중 시연 대상 profile 및 둘 다 필요한지 여부
2. `TARGET_LEFT/RIGHT`의 실제 arm, joint slice, CAN channel 대응
3. 5 cm 추가 견인의 정확한 물리 방향과 좌표계
4. direct teach 시작/종료 시 필요한 SDK 호출 순서
5. Retraction 종료의 물리 의미: hold, gain off, stop motion, wait pose 중 무엇인지
6. Tool change waypoint와 완료 판정
7. 검증된 custom gain, friction compensation, force threshold
8. SDK 라이선스의 EIR-NUC 사용 승인 및 제공 방식
9. 승인된 최신 upstream snapshot과 테스트 결과

위 값은 YAML profile 또는 secret으로 반영하고 코드에 하드코딩하지 않는다.

---

## 15. 권장 구현 순서 요약

1. 1.37 변경 추적과 동결 절차부터 만든다.
2. EIR-NUC의 독립 workspace에서 fake adapter 기반 ROS 패키지를 완성한다.
3. 최신 승인본의 알고리즘과 보정값을 모듈 단위로 이식한다.
4. 현재 확인된 단위, 좌우, 상태, 성공 응답 문제를 테스트와 함께 수정한다.
5. CAN과 DCP를 각각 비동작 방식으로 검증한다.
6. shadow call trace 비교 후 제한된 HIL로 전환한다.
7. 신규 EIR 프로세스가 유일한 hardware authority가 된 뒤 Windows/Jupyter 경로를 종료한다.
