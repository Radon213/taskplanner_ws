# VLM + Phase Estimator + OR Digital Twin + BT 기반 휴머노이드 수술보조 시스템 구현 인수인계 문서

## 1. 문서 목적

이 문서는 **수술 도구 전달을 주 목적으로 하는 휴머노이드 수술보조 시스템**의 구현을 다른 AI 에이전트 또는 개발자가 이어서 진행할 수 있도록 하기 위한 상세 설계 문서이다.

이 문서의 목표는 다음과 같다.

1. 프로젝트의 핵심 목적과 설계 철학을 분명히 정의한다.
2. 현재까지 합의된 시스템 아키텍처를 정리한다.
3. 각 모듈의 책임과 입출력을 명확히 한다.
4. ROS 2 기준 패키지 구조와 런타임 데이터 흐름을 제안한다.
5. 수술별 YAML 기반 설정 구조를 정의한다.
6. 실제 구현 순서와 우선순위를 제시한다.
7. 추후 변경 가능성이 높은 부분과 고정해야 하는 부분을 분리한다.

이 문서는 코드 구현 이전의 **상세한 설계 기준 문서**이며, 구현자는 반드시 이 문서의 구조적 의도를 보존하면서 개발을 진행해야 한다.

---

## 2. 프로젝트 한 줄 요약

**수술 절차 YAML과 도구/장면 명세를 기반으로, VLM의 관측 결과를 Phase Estimator가 안정화하고, OR Digital Twin이 수술실과 도구의 현재 상태를 관리하며, BT가 이 상태를 바탕으로 도구 전달·대기·복구 행동을 결정하는 휴머노이드 수술보조 시스템**.

---

## 3. 프로젝트의 핵심 문제 정의

이 시스템은 단순히 수술 phase를 분류하는 것이 목적이 아니다. 실제 목표는 다음과 같다.

- 현재 수술 맥락을 해석한다.
- 현재 어떤 도구가 필요하거나 곧 필요할 가능성이 높은지 판단한다.
- 로봇이 현재 어떤 도구를 어디서 집을 수 있는지 안다.
- 도구가 현재 어디에 있다고 믿어야 하는지 지속적으로 추적한다.
- 안전한 조건에서만 도구 전달 동작을 수행한다.
- VLM 또는 비전의 불안정성 때문에 잘못된 phase 추론이 발생하더라도 시스템 전체는 보수적으로 동작한다.

즉, 이 프로젝트는 **단순 perception 프로젝트가 아니라, perception + state estimation + decision making + execution**이 결합된 프로젝트이다.

---

## 4. 왜 이 구조가 필요한가

수술방 전체 상황을 비전만으로 안정적으로 추적하는 것은 현실적으로 어렵다. 이유는 다음과 같다.

- 도구가 사람 손이나 몸에 가려질 수 있다.
- 트레이, 메이요 스탠드, 수술 필드, 집도의 손 사이로 도구가 자주 이동한다.
- 일부 도구는 카메라에 잘 보이지 않거나 프레임 밖으로 벗어난다.
- VLM의 phase 추론은 순간 관측에 흔들릴 수 있다.
- 하나의 센서만으로 전체 장면을 완전하게 설명할 수 없다.

따라서 시스템은 다음 두 가지를 모두 가져야 한다.

1. **관측 계층**: 지금 보이는 장면을 해석하는 계층
2. **상태 유지 계층**: 지금까지의 행동과 관측을 바탕으로 현재 상태를 안정적으로 관리하는 계층

이 두 역할 사이의 핵심 연결자가 바로 **Phase Estimator**이고, 전체 상태의 중심 허브가 **OR Digital Twin**이다.

---

## 5. 시스템 핵심 구성요소

본 프로젝트에서 핵심적인 논리 구성요소는 아래와 같다.

### 5.1 VLM Node

역할:
- 수술 장면 이미지/프레임을 입력받아 현재 장면을 해석
- 현재 phase 후보, 도구 단서, 행동 단서 등을 산출
- raw phase evidence를 제공

주의:
- VLM은 최종 truth source가 아니다.
- VLM은 **현재 관측 evidence**를 공급하는 모듈이다.
- VLM 결과는 흔들릴 수 있으므로 그대로 BT에 연결하면 안 된다.

### 5.2 Phase Estimator / Phase Filter

역할:
- VLM의 raw phase evidence를 바로 사용하지 않고 안정화
- 수술 절차 YAML의 phase 전이 제약을 사용
- Digital Twin의 현재 phase belief를 prior로 참고
- temporal smoothing, hysteresis, dwell time, uncertainty 처리를 수행
- 최종 filtered phase를 산출

핵심 개념:
- VLM = observation
- Digital Twin = current belief/prior
- Phase Estimator = observation과 prior를 부드럽게 결합하는 안정화기

### 5.3 OR Digital Twin

역할:
- 시스템이 현재 믿고 있는 수술실 상태를 저장하는 중심 허브
- filtered phase belief 유지
- 도구 위치와 소유 상태 추적
- event history 유지
- uncertainty/confidence 유지
- BT가 읽는 통합 world state 제공

중요:
- Digital Twin은 단순 시각화용 디지털 트윈이 아니라 **stateful runtime belief manager**이다.
- YAML은 정적 명세이고, Digital Twin은 동적 현재 상태이다.

### 5.4 BT Node

역할:
- Digital Twin의 world state를 읽고 행동 결정
- 명시적 요청 처리
- phase 기반 anticipatory handover
- 대기, 복구, 반환 등의 행동 선택

중요:
- BT는 raw VLM 결과를 직접 보지 않는다.
- BT는 **Digital Twin이 관리하는 통합 상태만 신뢰**한다.

### 5.5 Skill / Robot Execution

역할:
- BT가 선택한 행동을 실제 실행 가능한 skill로 변환
- grasp, prepare, handover, return, retract 등을 수행
- 실행 결과를 Digital Twin에 event로 환류

### 5.6 Surgery Spec YAML Bundle

역할:
- 수술별 정적 지식을 외부 파일로 관리
- 수술 종류가 바뀌어도 BT 코드를 최대한 재사용 가능하도록 함

포함 내용:
- 수술 phase 정의
- 허용 전이
- 도구 목록과 alias
- 도구 초기 배치
- semantic location 정의
- phase guard policy
- BT action guard policy

---

## 6. 시스템 설계 철학

### 6.1 절대 원칙

1. **수술 지식은 코드가 아니라 YAML로 분리한다.**
2. **phase 추론은 raw VLM 결과를 직접 쓰지 않고 안정화 과정을 거친다.**
3. **BT는 world state를 읽고 행동을 선택하는 공통 엔진으로 유지한다.**
4. **Digital Twin은 현재 상태를 관리하는 단일 중심 허브가 된다.**
5. **고위험 행동은 phase 하나만으로 허용하지 않는다.**
6. **비전 기반 관측이 없더라도 행동/event 기반 상태 추적으로 시스템이 유지되어야 한다.**
7. **관측(Observation), 믿음(Belief), 의도/명령(Commanded)을 분리한다.**

### 6.2 무엇을 고정하고 무엇을 바꿀 것인가

#### 바뀌는 것
- 수술 종류
- phase 정의
- phase 개수
- phase 전이 구조
- 도구 목록
- 도구 alias
- 장면 semantic location 정의
- handover 정책 threshold

#### 바뀌지 않아야 하는 것
- Digital Twin 중심 구조
- BT의 상위 의사결정 프레임
- observation → phase estimation → twin → BT → execution → twin 흐름
- 안전 우선 정책

---

## 7. 전체 아키텍처

### 7.1 개념 아키텍처

```text
[Surgery Spec YAML]
   ├─ procedure.yaml
   ├─ instruments.yaml
   ├─ scene_layout.yaml
   └─ policy.yaml

[VLM Node] -> [Phase Estimator] -> [OR Digital Twin] -> [BT Node] -> [Skill/Robot Exec]
                                   ^                       |
                                   |                       |
                                   +----- event/result ----+
```

### 7.2 보다 정확한 관계 설명

- **YAML**은 VLM label normalization, Phase Estimator, Digital Twin, BT 모두에 간접적으로 관여한다.
- **VLM Node**는 현재 프레임 기반 observation evidence를 생성한다.
- **Phase Estimator**는 observation과 prior를 결합해 filtered phase를 만든다.
- **OR Digital Twin**은 filtered phase와 도구 상태를 포함한 통합 world state를 관리한다.
- **BT Node**는 Digital Twin의 상태를 보고 행동을 결정한다.
- **Skill/Robot Exec**은 행동을 실행하고 실행 결과를 event로 Digital Twin에 반영한다.

### 7.3 왜 완전한 삼각형이 아닌가

세 모듈이 모두 동등한 구조는 아니다.

- VLM/Phase Estimator는 관측과 해석 쪽에 가깝다.
- BT는 행동 선택 쪽에 가깝다.
- Digital Twin은 이 둘의 공통 상태 허브이다.

따라서 전체 구조는 **비대칭 삼각형** 또는 **Digital Twin 중심 허브 구조**로 보는 것이 맞다.

---

## 8. YAML Bundle 상세 설계

YAML은 반드시 정적 명세만 담아야 한다. 현재 상태를 저장하면 안 된다.

### 8.1 procedure.yaml

역할:
- 수술 절차 정의
- phase와 전이 규칙 정의
- phase별 expected instruments 정의

예시:

```yaml
procedure_id: thyroidectomy
procedure_display_name: Open Thyroidectomy

phases:
  - id: exposure
    display_name: Exposure
    possible_next:
      - dissection
      - hemostasis
    expected_instruments:
      - retractor
      - cautery
      - metzenbaum
    min_duration_sec: 5

  - id: dissection
    display_name: Dissection
    possible_next:
      - vessel_control
      - hemostasis
    expected_instruments:
      - metzenbaum
      - suction
      - right_angle
    min_duration_sec: 10
```

### 8.2 instruments.yaml

역할:
- 도구 마스터 목록 정의
- alias와 category 정의
- handover/grasp 관련 profile 정의

예시:

```yaml
instruments:
  - id: metzenbaum
    aliases: [metzen, metz, metzenbaum scissors, 메츤]
    category: cutting
    role: dissection
    handover_profile: ring_grasp

  - id: suction
    aliases: [suction, suction tip, 흡입기]
    category: suction
    role: suction
    handover_profile: shaft_grasp
```

### 8.3 scene_layout.yaml

역할:
- 수술실 semantic location 정의
- 도구 초기 배치 정의
- tray, mayo, field, handover zone 정의

예시:

```yaml
locations:
  - id: main_tray_slot_1
    type: tray_slot
  - id: main_tray_slot_2
    type: tray_slot
  - id: mayo_region_a
    type: mayo_stand
  - id: field_region_thyroid
    type: surgical_field
  - id: surgeon_handover_zone
    type: handover_zone

initial_instrument_placement:
  - instrument_id: metzenbaum
    location_id: main_tray_slot_1
  - instrument_id: suction
    location_id: main_tray_slot_2
```

### 8.4 policy.yaml

역할:
- phase estimator와 BT의 정책 파라미터 정의
- smoothing, threshold, guard rules 정의

예시:

```yaml
phase_guard:
  min_confidence_to_keep: 0.55
  min_confidence_to_switch: 0.80
  smoothing_window: 5
  min_dwell_time_sec: 5
  allow_unknown_phase: true

action_guard:
  block_handover_when_phase_uncertain: true
  require_multi_evidence_for_handover: true
  allow_prepositioning_when_uncertain: false
  explicit_request_priority: true
```

---

## 9. Runtime State와 Static Spec의 구분

### 9.1 Static Spec

YAML에 저장되는 것:
- phase 정의
- allowed transitions
- expected instruments
- instrument ids/aliases/categories
- semantic locations
- initial placements
- threshold/policy

### 9.2 Runtime State

Digital Twin에 저장되는 것:
- current filtered phase
- phase confidence
- phase uncertainty
- 각 도구의 현재 위치 belief
- 각 도구의 ownership/state
- 최근 이벤트 log
- robot hand occupancy
- execution status

이 둘의 구분은 필수이다.

---

## 10. OR Digital Twin 상세 설계

### 10.1 역할 재정의

OR Digital Twin은 본 프로젝트의 중심 허브이며, 다음을 관리한다.

1. 현재 filtered phase belief
2. 도구별 semantic location
3. 도구별 ownership
4. 도구별 status
5. uncertainty/confidence
6. event history
7. robot 상태
8. 추후 surgeon readiness나 safety flags까지 포함 가능

### 10.2 Tool State 예시

```python
class InstrumentState:
    instrument_id: str
    location_type: str
    location_id: str | None
    owner: str | None
    status: str
    confidence: float
    last_update_time: float
```

### 10.3 상태값 후보

#### owner
- none
- robot_left_hand
- robot_right_hand
- surgeon
- assistant

#### status
- available
- prepared
- held
- handed_over
- returned
- missing
- contaminated
- unknown

#### location_type
- tray_slot
- mayo_stand
- surgical_field
- robot_left_hand
- robot_right_hand
- surgeon_hand
- assistant_hand
- handover_zone
- unknown

### 10.4 이벤트 기반 상태 갱신

Digital Twin은 직접 상태를 덮어쓰기보다 이벤트 기반으로 갱신하는 것이 바람직하다.

예시 이벤트:
- ToolObservedOnTray
- RobotGraspedTool
- ToolPrepared
- ToolHandoverAttempted
- ToolHandoverCompleted
- ToolReturnedToTray
- ToolDetectedInField
- PhaseUpdated

이벤트 기반 접근의 장점:
- 왜 상태가 이렇게 되었는지 추적 가능
- 시뮬레이션 replay 쉬움
- evaluation/logging 유리

---

## 11. VLM Node 상세 설계

### 11.1 역할

VLM Node는 멀티모달 또는 이미지 기반 장면 해석기로 동작한다.

입력:
- 수술 장면 프레임 또는 이미지
- 필요 시 tool tray, field, robot hand cam 등 복수 입력

출력 후보:
- raw phase candidates
- scene summary
- visible tools
- action cues
- confidence

예시 출력 구조:

```json
{
  "timestamp": 1710000000.0,
  "phase_candidates": [
    {"phase_id": "dissection", "confidence": 0.63},
    {"phase_id": "hemostasis", "confidence": 0.21}
  ],
  "visible_instruments": [
    {"instrument_id": "metzenbaum", "confidence": 0.81},
    {"instrument_id": "suction", "confidence": 0.74}
  ],
  "scene_summary": "surgeon is dissecting around the thyroid region",
  "uncertainty": 0.28
}
```

### 11.2 주의사항

- VLM은 최종 phase를 확정하지 않는다.
- 도구 alias는 instruments.yaml을 통해 정규화한다.
- 가능하면 structured JSON output을 강제한다.
- raw natural language output은 바로 사용하지 않는다.

---

## 12. Phase Estimator 상세 설계

### 12.1 역할

Phase Estimator는 raw VLM evidence와 Digital Twin의 prior를 결합하여 filtered phase를 생성한다.

### 12.2 입력

1. VLM observation evidence
2. procedure.yaml의 phase/transition 정보
3. policy.yaml의 threshold/smoothing 규칙
4. Digital Twin의 current phase belief와 recent event context

### 12.3 출력

- filtered_phase
- phase_confidence
- phase_uncertain
- allowed_next_phases
- phase_stability score

### 12.4 필요한 기능

- confidence threshold
- temporal smoothing
- hysteresis
- min dwell time
- allowed transition check
- unknown phase fallback

### 12.5 매우 중요한 설계 원칙

Digital Twin은 **prior**로만 사용해야 한다.

잘못된 사용:
- Digital Twin이 현재 exposure를 믿고 있으니 무조건 exposure 유지

올바른 사용:
- VLM evidence가 약하면 prior 비중을 조금 높인다.
- VLM evidence가 충분히 강하고 consistent하면 prior를 뒤집을 수 있다.

즉, 자기강화 루프를 피해야 한다.

---

## 13. BT Node 상세 설계

### 13.1 상위 역할

BT는 현재 world state를 보고 다음 행동을 선택한다.

### 13.2 상위 구조 제안

```text
Fallback
├── Safety subtree
├── Explicit request subtree
├── Recovery subtree
├── Anticipatory handover subtree
└── Idle / Observe subtree
```

### 13.3 각 subtree 의미

#### Safety
- unsafe면 hold/retract
- high-risk 행동 중단

#### Explicit request
- 명시적 음성 요청이 있으면 우선 처리
- phase보다 우선순위 높음

#### Recovery
- 잘못 잡음
- 전달 실패
- 반환 필요
- 상태 불일치 복구

#### Anticipatory handover
- filtered phase 기반으로 다음 도구 후보 선택
- 단, commit guard 통과 시에만 실제 handover 수행

#### Idle / Observe
- 준비 자세 유지
- 재관측
- 아무 것도 확실하지 않을 때 보수적 대기

### 13.4 BT가 절대 직접 보지 말아야 할 것

- raw VLM natural language output
- unfiltered phase estimate
- 비정규화된 도구명

### 13.5 BT가 봐야 하는 것

- filtered phase
- phase uncertainty
- tool availability/location belief
- explicit request
- robot current state
- action guard result

---

## 14. Skill / Robot Execution 상세 설계

### 14.1 역할

BT의 추상 명령을 실제 skill 수준 명령으로 실행한다.

예시 skill:
- prepare_tool(tool_id)
- grasp_tool(tool_id)
- move_to_handover_pose(tool_id)
- handover_tool(tool_id)
- receive_back_tool(tool_id)
- return_tool_to_location(tool_id)
- go_idle_pose()
- retract_arm()

### 14.2 주의점

고위험 행동은 skill executor에서 한 번 더 guard를 적용하는 것이 바람직하다.

예:
- handover zone 확보 여부
- 실제 tool availability
- robot gripper state
- safe motion path

즉, BT의 결정이 곧바로 최종 실행 허가가 되어서는 안 된다.

---

## 15. ROS 2 기준 추천 패키지 구조

```text
surgical_assist_ws/
└── src/
    ├── surgical_msgs/
    ├── procedure_spec/
    ├── vlm_node/
    ├── phase_estimator/
    ├── or_digital_twin/
    ├── bt_orchestrator/
    ├── skill_execution/
    ├── robot_bridge/
    ├── sim_bridge/
    ├── logging_and_eval/
    └── bringup/
```

### 15.1 surgical_msgs

정의할 메시지 후보:
- PhaseEvidence.msg
- FilteredPhase.msg
- ToolObservation.msg
- ToolState.msg
- WorldState.msg
- BTDecision.msg
- SkillCommand.msg
- SkillStatus.msg
- TwinEvent.msg

### 15.2 procedure_spec

역할:
- YAML loader
- schema validator
- query API 제공

내부 예시:

```text
procedure_spec/
├── procedure_spec/
│   ├── loader.py
│   ├── models.py
│   ├── validator.py
│   ├── query_api.py
│   └── specs/
│       └── thyroidectomy/
│           ├── procedure.yaml
│           ├── instruments.yaml
│           ├── scene_layout.yaml
│           └── policy.yaml
```

### 15.3 vlm_node

역할:
- image topic subscribe
- local/remote VLM inference
- structured PhaseEvidence / ToolObservation publish

### 15.4 phase_estimator

역할:
- VLM evidence subscribe
- Digital Twin prior query/subscribe
- FilteredPhase publish

### 15.5 or_digital_twin

역할:
- initial spec load
- world state store
- event application
- world state publish

### 15.6 bt_orchestrator

역할:
- world state subscribe
- BT tick
- skill command publish

### 15.7 skill_execution

역할:
- skill command subscribe
- robot bridge 호출
- result/status/event publish

### 15.8 sim_bridge

역할:
- Isaac Sim과 state/event 연동
- scene sync

### 15.9 logging_and_eval

역할:
- 모든 핵심 토픽 기록
- offline replay 및 evaluation 지원

---

## 16. 추천 토픽 흐름 예시

이는 예시이며, 최종 naming은 구현 시 통일성 있게 조정한다.

### 입력 관련
- `/surgery/images/field`
- `/surgery/images/tray`
- `/surgery/audio/request_text`

### VLM 출력
- `/vlm/phase_evidence`
- `/vlm/tool_observations`

### Phase Estimator 출력
- `/phase/filtered`

### Digital Twin 출력
- `/twin/world_state`
- `/twin/tool_states`
- `/twin/events`

### BT 출력
- `/bt/decision`
- `/bt/skill_command`

### Skill/Execution 출력
- `/skill/status`
- `/skill/events`

---

## 17. 데이터 흐름 요약

### 정적 데이터 흐름

```text
YAML -> VLM label normalization
YAML -> Phase Estimator rules
YAML -> Digital Twin initialization
YAML -> BT policy and expected tool mapping
```

### 동적 데이터 흐름

```text
VLM -> Phase Estimator -> Digital Twin -> BT -> Skill/Exec -> Digital Twin
```

### 보조 데이터 흐름

```text
Digital Twin -> Phase Estimator (prior/context)
Voice request -> Digital Twin or BT input
Tool observation -> Digital Twin reconcile
```

---

## 18. 안전 설계 규칙

반드시 구현에 반영할 것.

### 18.1 Phase uncertainty 처리

- filtered phase가 uncertain이면 high-risk handover 금지
- explicit request만 제한적으로 처리 가능
- 기본 행동은 wait/observe

### 18.2 High-risk action guard

handover 수행 조건은 phase 하나만으로 결정하지 않는다.

최소 조건 예시:
- filtered phase sufficiently stable
- requested or expected tool is available
- robot state valid
- safety flag clear
- handover zone ready or equivalent evidence

### 18.3 Unknown 상태 허용

모든 도구 상태와 phase는 unknown/uncertain을 허용해야 한다.
억지로 확정하지 말 것.

### 18.4 Observation-Belief mismatch 처리

예:
- twin은 surgeon hand라고 믿는데 tray에서 다시 검출됨
- twin은 tray라고 믿는데 handover completed event가 들어옴

이 경우 mismatch flag 또는 confidence reduction을 두고 reconcile해야 한다.

---

## 19. 구현 우선순위 및 단계별 계획

### Phase 1: Spec Loader와 Twin 기본 뼈대

목표:
- YAML bundle 로딩
- validation
- Digital Twin 초기 상태 생성

산출물:
- `procedure_spec` 패키지
- `or_digital_twin` 기본 state store

### Phase 2: VLM Node와 Phase Estimator 최소 버전

목표:
- mock 또는 실제 VLM output 구조화
- raw phase evidence publish
- filtered phase 생성

산출물:
- `vlm_node`
- `phase_estimator`

### Phase 3: BT 최소 버전

목표:
- Safety, Explicit Request, Idle subtree 동작
- world_state 기반 기본 결정

산출물:
- `bt_orchestrator`

### Phase 4: Tool Tracking과 Event Loop

목표:
- tool state event 처리
- grasp/handover/return state update
- Digital Twin ↔ BT ↔ skill loop 형성

산출물:
- event-driven twin
- skill command/status interface

### Phase 5: Isaac Sim / 실제 로봇 연동

목표:
- sim_bridge 또는 robot_bridge 통해 scene/action 연동
- end-to-end validation

### Phase 6: Logging, Replay, Evaluation

목표:
- rosbag/replay test
- phase stability, handover success, state mismatch 분석

---

## 20. 구현 시 강하게 권장하는 개발 순서

1. YAML schema와 spec loader부터 확정한다.
2. Digital Twin의 internal data model을 먼저 설계한다.
3. VLM output을 structured JSON/ROS msg로 강제한다.
4. Phase Estimator를 독립 클래스 형태로 구현한다.
5. BT를 world_state 기반으로 최소 기능부터 붙인다.
6. Skill/Execution을 event-driven으로 연결한다.
7. 마지막에 sim/robot bridge를 연결한다.

이 순서를 지키는 이유는, perception보다 state와 interface가 먼저 안정되어야 전체 시스템이 무너지지 않기 때문이다.

---

## 21. 테스트 전략

### 21.1 Unit Test

대상:
- YAML validation
- instrument alias resolution
- allowed transition check
- smoothing/hysteresis logic
- event application in Digital Twin

### 21.2 Replay Test

대상:
- recorded VLM evidence를 넣었을 때 filtered phase가 얼마나 안정적인지
- event sequence에 따라 twin state가 일관되게 갱신되는지
- mismatch 발생 시 confidence/uncertainty 처리가 되는지

### 21.3 BT Logic Test

대상:
- explicit request가 phase보다 우선하는지
- uncertain phase에서 wait로 가는지
- expected tool selection이 procedure spec을 따르는지

### 21.4 End-to-End Simulation Test

대상:
- tray initial state에서 특정 도구를 집어 handover하고 twin state가 업데이트되는지
- return event 후 상태 복귀가 되는지
- wrong detection이 있을 때 system이 안전하게 보수적으로 머무는지

---

## 22. 최소 구현 범위(MVP) 제안

초기 MVP는 아래 기능만 있어도 충분하다.

1. 한 가지 수술 procedure spec 로드
2. 3~4개 phase만 정의
3. 5개 내외 도구 정의
4. tray slot과 robot hand, surgeon hand만 semantic location으로 사용
5. VLM output은 mock 데이터여도 됨
6. Phase Estimator는 smoothing + transition check 정도만 구현
7. BT는 explicit request, anticipatory select, idle 정도만 구현
8. skill executor는 실제 motion 대신 상태 변화 시뮬레이션으로 대체 가능

---

## 23. 추후 확장 방향

추후에는 아래 항목으로 확장 가능하다.

- surgeon hand readiness detection
- voice request integration
- multi-camera fusion
- instrument contamination state
- surgical field region tracking
- procedure-specific preference profiles
- uncertainty-aware BT decorators
- simulation-grounded digital twin visualization
- world model JSON topic publishing

---

## 24. 구현자에게 중요한 주의사항

### 24.1 BT 안에 수술명을 하드코딩하지 말 것

수술마다 바뀌는 내용은 spec에서 읽어야 한다.

### 24.2 phase를 raw classifier output처럼 다루지 말 것

phase는 시계열적 맥락이 있는 상태이며, filter를 거친 belief여야 한다.

### 24.3 Digital Twin을 단순 캐시처럼 구현하지 말 것

Digital Twin은 event-driven state manager여야 한다.

### 24.4 YAML에 runtime state를 저장하지 말 것

YAML은 정적 명세 전용이다.

### 24.5 VLM output을 문자열 그대로 쓰지 말 것

structured schema를 통해 phase/tool/action cue를 정규화해야 한다.

### 24.6 고위험 행동은 BT 결정 하나로 실행하지 말 것

Skill/Execution 단계에서 재검증이 필요하다.

---

## 25. 추천 내부 클래스 예시

```python
class ProcedureSpec:
    def get_expected_instruments(self, phase_id: str) -> list[str]: ...
    def get_allowed_next_phases(self, phase_id: str) -> list[str]: ...
    def resolve_instrument_alias(self, raw_name: str) -> str | None: ...


class PhaseGuardPolicy:
    min_confidence_to_keep: float
    min_confidence_to_switch: float
    smoothing_window: int
    min_dwell_time_sec: float
    allow_unknown_phase: bool


class PhaseEstimator:
    def update(self, evidence, twin_prior, timestamp): ...


class InstrumentState:
    instrument_id: str
    location_type: str
    location_id: str | None
    owner: str | None
    status: str
    confidence: float
    last_update_time: float


class ORDigitalTwin:
    def apply_event(self, event): ...
    def reconcile_observation(self, obs): ...
    def get_world_state(self): ...
    def get_tool_state(self, instrument_id: str): ...


class ActionGuard:
    def can_prepare(self, world_state, tool_id: str) -> bool: ...
    def can_handover(self, world_state, tool_id: str) -> bool: ...


class ToolSelector:
    def select_from_explicit_request(self, world_state, spec): ...
    def select_from_phase_expectation(self, world_state, spec): ...
```

---

## 26. AI 에이전트에게 바로 맡길 작업 목록

다음 순서대로 구현을 진행하라.

### Task 1. YAML schema 확정 및 validator 구현
- procedure.yaml
- instruments.yaml
- scene_layout.yaml
- policy.yaml

### Task 2. ProcedureSpec Python 라이브러리 구현
- loader
- pydantic/dataclass models
- query API

### Task 3. OR Digital Twin 기본 구현
- instrument registry
- initial placement load
- world state model
- event application

### Task 4. VLM output schema와 mock publisher 구현
- raw phase evidence
- tool observations

### Task 5. Phase Estimator 구현
- smoothing
- hysteresis
- transition constraint
- uncertainty output

### Task 6. BT 최소 버전 구현
- explicit request subtree
- anticipatory selection subtree
- idle subtree

### Task 7. Skill executor mock 구현
- command -> result/event 흐름 연결

### Task 8. End-to-end replay demo 구축
- mock VLM evidence
- filtered phase update
- twin state update
- BT decision output

---

## 27. 현재 설계의 핵심 문장 요약

1. **이 프로젝트의 중심은 phase classifier가 아니라 OR Digital Twin이다.**
2. **Phase Estimator는 VLM 관측과 Digital Twin의 belief를 부드럽게 연결하는 완충기/해석기이다.**
3. **BT는 raw perception을 직접 보지 않고 Digital Twin의 통합 state를 기반으로 의사결정해야 한다.**
4. **수술별 차이는 YAML bundle로 외부화하고, BT는 공통 엔진으로 유지해야 한다.**
5. **도구 전달 시스템이므로 phase 자체보다 도구 위치/소유/상태 추적이 매우 중요하다.**
6. **비전이 불완전하므로 행동 기반 event tracking + observation reconciliation 구조가 필수이다.**

---

## 28. 최종 인수인계 요약

구현자는 이 시스템을 다음과 같이 이해해야 한다.

- YAML은 수술에 대한 정적 지식이다.
- VLM은 현재 장면에 대한 관측 evidence를 제공한다.
- Phase Estimator는 관측 evidence를 곧바로 믿지 않고 안정화한다.
- OR Digital Twin은 현재 상태를 누적적으로 관리하는 stateful hub이다.
- BT는 Digital Twin의 world state를 읽고 행동을 선택한다.
- Skill/Execution은 그 행동을 실제 실행하고 결과를 다시 Twin에 반영한다.

즉, 전체 구조는 다음 한 문장으로 정리할 수 있다.

**수술 지식은 YAML로 외부화하고, 관측은 VLM으로 얻고, phase는 Estimator로 안정화하고, 현재 상태는 Digital Twin이 관리하며, BT는 그 상태를 바탕으로 도구 전달 행동을 결정한다.**

