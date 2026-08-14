# 공개 토픽 빠른 참조 — v0.3.0

기준: 로컬 `main` 워크트리 / `surgical_interop_msgs` 0.3.0

공개 상태 Gateway는 기본 활성화되어 있다. 시나리오가 없거나
`/twin/world_state`가 stale이면 이전 데이터를 유지하지 않고 동적
snapshot을 빈 값 또는 `UNKNOWN`으로 덮어쓴다.

자유문장 공개는 별도 경계다. 기본값 `PUBLISH_SHARED_FREE_TEXT=false`에서는
음성 `text`와 VLM `summary`가 비어 있지만, sequence·상태·latency·구조화된
ID/confidence는 계속 제공된다. 이 Gateway는 자유문장을 비식별화하지 않으므로
기관 간 PHI 처리 합의가 끝난 배포만 명시적으로 `true`를 사용한다.

- 상태 snapshot 10개: Reliable / Transient Local / Keep Last 1 / 기본 약 1 Hz
- Event 1개: Reliable / Volatile / Keep Last 50 / 발생 즉시
- 카메라 alias 2개: Best Effort / Volatile / Keep Last 5 / 활성 시나리오에서만 전달
- wire 계약의 최종 원본: `src/surgical_interop_msgs/msg`, `action`, `srv`

## UI 권장 초기 구독 순서

1. `/surgery/gateway_info`에서 Gateway 생존·버전·run ID를 확인한다.
2. `/surgery/catalog`에서 단계와 도구의 한글·영문 표시명을 만든다.
3. `/surgery/health`에서 source별 unavailable/stale를 확인한다.
4. 필요한 동적 snapshot과 `/surgery/events`를 구독한다.
5. `gateway_instance_id`, `catalog_version`, `procedure_run_id`가 바뀌면 관련 캐시를 초기화한다.
6. Event는 메시지 자체의 `(gateway_instance_id, procedure_run_id)`로 묶고 `sequence`로 정렬한다.

## 공개 상태 토픽

| 토픽 | 타입 | UI에서 사용하는 핵심 정보 | 유휴 상태 |
| --- | --- | --- | --- |
| `/surgery/gateway_info` | `GatewayInfo` | schema/interface/catalog 버전, Gateway·run ID, procedure 활성 여부 | heartbeat 유지, run ID 비움 |
| `/surgery/catalog` | `ProcedureCatalog` | 수술·단계·도구의 영문/한글명, 순서, 다음 단계, 예상 도구, alias | 정적 목록 유지 |
| `/surgery/context` | `SurgeryContext` | 수술 종류, 현재 단계, confidence, uncertain, 실행 상태, safety flag | inactive + `UNKNOWN` |
| `/surgery/instruments` | `InstrumentStateArray` | 도구 instance별 의미적 위치·보유자·상태 | 빈 배열 |
| `/surgery/robots` | `RobotStateArray` | 로봇 실행 상태, command ID, progress, reason | 빈 배열 |
| `/surgery/robot_end_effectors` | `RobotEndEffectorStateArray` | humanoid 왼손·오른손의 `empty/holding/unknown`과 보유 도구 | 빈 배열 |
| `/surgery/tool_predictions` | `ToolPredictionArray` | 다음 도구 rank, ID, confidence, stability, source | 빈 배열 |
| `/surgery/speech` | `SpeechRecognitionState` | ASR 상태, 연결, sequence, 응답 latency; 확정 문장은 별도 opt-in | unavailable + 빈 text |
| `/surgery/clinical_observations` | `ClinicalObservationArray` | VLM 관찰 단계·도구·위치·gesture와 불확실성; summary는 별도 opt-in | 빈 배열 |
| `/surgery/health` | `SurgeryHealth` | source별 unavailable/stale/error | 실제 상태 계속 발행 |
| `/surgery/events` | `SurgeryEvent` | 단계/도구 등 상태 변화와 outcome, correlation ID | 발행하지 않음 |

## 대표 메시지 예시

아래 값은 wire 형식을 설명하기 위한 더미 예시이며 실제 수술 데이터가 아니다.

### Gateway heartbeat

```yaml
schema_version: "1.1.0"
interface_version: "0.3.0"
catalog_version: "sha256:<catalog digest>"
gateway_instance_id: "<opaque gateway UUID>"
procedure_run_id: "<opaque run UUID>"
procedure_type: thyroidectomy
procedure_active: true
```

### Procedure catalog

```yaml
procedure_type: thyroidectomy
procedure_display_name: Open Thyroidectomy
procedure_display_name_ko: 갑상선절제술
default_phase_id: P01
phases:
  - ordinal: 1
    phase_id: P01
    display_name: Skin incision
    display_name_ko: 피부 절개
    possible_next_phase_ids: [P02, P06]
    expected_instrument_ids: [T01, T04, T03]
instruments:
  - instrument_id: T04
    display_name: Bovie surgical cautery
    display_name_ko: 보비 전기소작기
    category: hemostasis
    inventory_count: 1
    requestable: true
```

### Context와 다음 도구 예측

```yaml
# /surgery/context
revision: 1042
procedure_type: thyroidectomy
procedure_active: true
current_phase: P04
phase_confidence: 0.92
phase_uncertain: false
execution_state: running
evidence_status: DT_ACCEPTED

# /surgery/tool_predictions
revision: 1042
procedure_run_id: "<same active run UUID>"
procedure_active: true
predictions:
  - rank: 1
    instrument_id: T09
    instance_id: ""
    confidence: 0.87
    stability_sec: 3.4
    source: digital_twin
    evidence_status: DT_ACCEPTED
  - rank: 2
    instrument_id: T04
    instance_id: ""
    confidence: 0.73
    stability_sec: 0.0
    source: digital_twin
    evidence_status: DT_ACCEPTED
  - rank: 3
    instrument_id: T07
    instance_id: ""
    confidence: 0.61
    stability_sec: 0.0
    source: digital_twin
    evidence_status: DT_ACCEPTED
```

예측은 최대 3개이며 confidence 내림차순이다. rank 1만 기존 내부 BT scalar와
동일하고, rank 2·3은 제어에 들어가지 않는다. 이 예측 전체는 UI 표시용 advisory
정보이며 Action 실행 지시나 승인으로 사용하면 안 된다.

### 로봇 손과 도구 상태

```yaml
# /surgery/robot_end_effectors
end_effectors:
  - robot_id: humanoid
    end_effector_id: right_hand
    state: holding
    instrument_id: T04
    instance_id: "T04#1"
    confidence: 1.0
    evidence_status: DT_ACCEPTED
  - robot_id: humanoid
    end_effector_id: left_hand
    state: empty
    instrument_id: ""
    instance_id: ""
```

집도의가 현재 사용하는 도구 목록은 `/surgery/instruments`에서
`holder_role=surgeon`이고 `state`가 `handed_over` 또는 `in_use`인 row를 고른다.
공개 위치는 모두 `location_type=surgeon`, `location_id=surgeon`으로 단순화되며,
내부의 `surgeon_hand`, `surgical_field`, `bed_fixed_tool` 구분은 노출하지 않는다.
도구가 항상 하나라고 가정하지 않는다.

Mayo 위 도구 목록은 `location_type=mayo_stand`인 row를 고른다. 물리 위치는
하나이며, `state=parked_for_reuse`는 재사용 대기,
`state=awaiting_retrieval`은 회수 queue에 등록된 상태다.
`mayo_reuse_zone`과 `mayo_recovery_zone`은 공개 위치값이 아니다.

### 확정 음성 인식 메타데이터와 latency — 기본 redaction

```yaml
available: true
connected: true
state: listening
utterance_sequence: 7
text: ""
latency_available: true
response_latency_ms: 184.6
latency_basis: latest_pcm_send_complete_to_final_receive
source: taskplanner_asr
evidence_status: GATEWAY_OBSERVED_REDACTED
```

위 상태는 확정 문장이 있었지만 공개 자유문장 정책으로 내용이 숨겨졌다는 뜻이다.
`PUBLISH_SHARED_FREE_TEXT=true`를 명시한 배포에서만 `text`에 ASR 확정 문장이
들어오며, 그 경우에도 planner 수락 또는 로봇 실행 결과를 뜻하지 않는다.
`latency_available=false`이면 `response_latency_ms`를 표시하지 않는다. 알 수 없는
자유문장 `latency_basis`는 공개하지 않고 latency 전체를 unavailable 처리한다.

VLM도 기본 정책에서 단계·도구·위치·gesture의 구조화된 값은 유지하고
`summary=""`로 발행한다. 원래 summary가 존재했다면
`evidence_status=MODEL_OBSERVED_REDACTED`이므로 UI가 문장을 추정해서 만들면 안 된다.

### Event outcome

```yaml
sequence: 287
schema_version: "1.1.0"
catalog_version: "sha256:<catalog digest>"
gateway_instance_id: "<opaque gateway UUID>"
procedure_run_id: "<opaque run UUID>"
procedure_type: thyroidectomy
event_type: PhaseTransitionRejected
subject_type: procedure
subject_id: P04
phase: P04
state: rejected
correlation_id: ""
evidence_status: DT_ACCEPTED
```

여기서 `DT_ACCEPTED`는 “거절 이벤트라는 사실을 Gateway가 수용했다”는
의미다. 성공 여부는 반드시 `state`로 판단한다.
첫 Event가 다음 1 Hz heartbeat보다 먼저 도착해도 Event 자체의 run ID로
귀속할 수 있다. `sequence`만으로 다른 run 또는 Gateway 재시작을 합치지 않는다.

모든 공개 confidence·uncertainty는 유한한 `[0,1]` 값이다. 잘못된 숫자 claim은
`UNKNOWN`으로 안전하게 바뀌거나 해당 row가 제외되며, clinical parallel array는
Gateway가 길이를 검증한다. UI도 배열을 결합하기 전에 길이를 다시 확인한다.

## 공개 카메라 alias

| 공개 토픽 | 기본 원본 | 조건 |
| --- | --- | --- |
| `/surgery/images/flir/compressed` | `/synced/flir/color/image_raw/compressed` | fresh active procedure + subscriber |
| `/surgery/images/cam4/compressed` | `/synced/cam_4/color/image_raw/compressed` | fresh active procedure + subscriber |

시나리오 유휴·정지·stale·procedure mismatch이면 공개 토픽은 발견되지만
프레임은 전달되지 않는다. relay는 원본 JPEG를 decode/re-encode하지 않는다.
공개 rosbridge는 카메라 구독을 서버에서 `queue_length=1`, 최대 10 Hz로
강제하고 `compression="cbor"`로 정규화하며 연결별 송신 큐도 최신 4개만
유지한다. PNG/알 수 없는 압축은 카메라 encoder에 들어가지 않는다. 브라우저도
CBOR를 요청하고 화면에는 최신 프레임만 렌더링한다.

## 수신 점검 명령

```bash
ros2 topic echo /surgery/gateway_info \
  surgical_interop_msgs/msg/GatewayInfo \
  --qos-reliability reliable --qos-durability transient_local --once

ros2 topic echo /surgery/catalog \
  surgical_interop_msgs/msg/ProcedureCatalog \
  --qos-reliability reliable --qos-durability transient_local --once

ros2 topic info /surgery/speech --verbose
ros2 topic info /surgery/images/flir/compressed --verbose
```

다른 PC의 native ROS 2 subscriber는 동일 Domain/RMW/discovery와
`surgical_interop_msgs` 0.3.0 설치가 필요하다.

> **Native DDS 보안 경계 주의**: 현재 배포의 DDS subnet은 인증/ACL 경계가
> 아니다. 같은 Domain 참가자는 공개 13개 endpoint뿐 아니라 내부 토픽도
> 탐색·구독할 수 있고, 동일한 공개/내부 토픽 이름으로 위조 또는 충돌 샘플을
> 발행할 수 있다. free-text suppression, 카메라 active gate, 9092 allowlist는
> Taskplanner 소유 출력에만 적용되고 다른 DDS 참가자의 트래픽은 필터링하지
> 않는다. 따라서 UI 전용 PC는 DDS에 합류시키지 말고 9092만 사용한다. DDS
> subnet에는 상호 신뢰된 관리형 제어기 PC만 연결하며 Wi-Fi, Tailscale/VPN,
> 인터넷으로 DDS를 라우팅하지 않는다. 비신뢰 DDS 참가자가 필요하면 ROS 2/DDS
> Security의 identity, governance, permissions를 별도로 구축해야 한다.

브라우저 UI는 `ws://<Taskplanner 유선 IP>:9092`에 연결한다. 9092는 별도
512 MiB 제한 컨테이너이며 위 11개 공개 토픽과 두 카메라 alias에 대한
`subscribe`만 허용한다. publish/service/Action/rosapi는 제공하지 않는다.
정확히는 Subscribe capability의 `subscribe`와 `unsubscribe` 연산만 허용하며,
incoming `fragment`와 그 밖의 알 수 없는 연산은 재조립하지 않고 거부한다.
WebSocket frame 하나에는 완성된 JSON 요청 하나만 허용한다. UTF-8 기준 64 KiB
초과나 malformed/incomplete JSON은 parse buffer를 비우고 연결을 종료한다.
송신 fragmentation도 비활성화되어 전체 직렬화 결과가 4 MiB를 넘으면 어떤
fragment frame도 보내지 않고 해당 sample을 폐기한다.
9090 운영 bridge와 9091 Debug bridge를 UI/UX 팀 주소로 사용하면 안 된다.

9092의 외부 접속은 `TASKPLANNER_DEBUG_NETWORK_INTERFACE`로 지정한 유선 NIC와
그 NIC의 직접 연결 IPv4 subnet으로만 제한된다. 기본 WebSocket Origin 정책도
localhost/private IPv4만 허용한다. 기관 UI가 DNS/HTTPS Origin을 사용하면
`PUBLIC_ROSBRIDGE_ALLOWED_ORIGINS=https://ui.example`처럼 정확한 Origin을
쉼표로 설정한다. Sidecar는 WebSocket upgrade 전에 direct peer가 loopback인지도
검사하므로, Tailscale/VPN이 가상 IP를 loopback으로 DNAT하는 환경에서도 지정
유선 proxy만 통과할 수 있다. Origin은 인증이나 네트워크 경계가 아니므로
인터넷에 직접 노출하지 않는다.
