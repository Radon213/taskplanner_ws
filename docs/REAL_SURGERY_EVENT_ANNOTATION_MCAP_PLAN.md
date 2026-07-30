# 실제 수술영상 최소 관측 이벤트 어노테이션 및 MCAP 통합 계획

## 1. 목적

대상 데이터는 다음 경로의 갑상샘 수술 멀티모달 ROS 2 MCAP 패키지다.

```text
/mnt/arl/NAS관리/백업/업무/ARPA-H/SurgeryData/갑상샘/0704_멀티모달_ROS2_MCAP_v1.0.0
```

이 데이터에는 수술 전체가 아닌 일부 구간이 담겨 있고, 영상 속 스크럽 널스의
행동이 Taskplanner의 휴머노이드 정책과 항상 일치하지 않는다. 따라서 실제 영상을
로봇 행동 스키마에 억지로 맞추지 않고, 영상에서 확인할 수 있는 최소한의 물리적
사실을 정답 라벨로 만든다.

핵심 목표는 다음과 같다.

1. 도구의 소유자 또는 위치가 바뀌는 순간만 최소 단위로 기록한다.
2. `누가`, `어떤 도구를`, `어디에서`, `어디로` 옮겼는지를 남긴다.
3. 모델은 후보 구간을 제안하고, 최종 정답은 사람이 확인한다.
4. 확정 JSON 이벤트를 원본 bag과 같은 시간축의 ROS 토픽으로 삽입한다.
5. Taskplanner는 정답 토픽을 입력으로 사용하지 않고, 평가기만 이를 구독한다.
6. 원본 MCAP은 보존하고, 어노테이션이 추가된 파생 MCAP을 별도로 생성한다.

## 2. 범위와 비범위

### 이번 범위

- 도구 전달
- 도구 직접 반환
- Mayo stand에 도구 놓기
- Mayo stand에서 도구 집기
- 부분 영상 시작 시점의 도구 초기 상태
- 도구, 전달 주체, 수령 주체, 손 또는 위치
- 이벤트 발생 시각
- 모델 제안과 사람 확정 상태
- JSONL 저장, 검증, MCAP 토픽 삽입 및 재생

### 이번 범위에서 제외

- 집도의의 세부 손동작 분류
- 절개, 박리, 결찰 등 모든 임상 술기 어노테이션
- 로봇 관절 궤적 또는 스크럽 널스 팔 궤적
- 영상 속 행동을 `direct_handover`, `retrieve_from_mayo` 같은 로봇 skill로
  직접 치환하는 작업
- 보이지 않는 의도나 향후 사용할 도구의 추정값을 정답으로 기록하는 작업
- 모델 제안을 사람 확인 없이 정답으로 사용하는 작업

수술 단계 정확도까지 평가하려면 별도의 희소한 phase interval 라벨이 필요하다.
이는 도구 이벤트 라벨과 분리된 후속 트랙으로 추가한다.

## 3. 핵심 설계 원칙

### 3.1 관측 사실과 로봇 정책 분리

영상의 정답은 로봇 행동명이 아니라 물리적 상태 변화다.

```text
scrub_nurse/right_hand
  -> surgeon/left_hand
```

위 변화는 평가 시 `handover`로 해석할 수 있지만, 원본 라벨의 핵심 정답은
`from`과 `to`다. 같은 방식으로 영상에서 집도의가 스크럽 널스에게 직접 도구를
돌려주더라도 이를 Taskplanner의 정상 회수 정책으로 간주하지 않는다.

Taskplanner의 정상 회수 경로는 계속 다음과 같다.

```text
surgeon -> Mayo stand -> robot left hand -> cleaner -> rack
```

실제 영상의 사람 간 직접 반환은 `observed_direct_return`으로 남기며,
`retrieve_from_hand`의 정답으로 변환하지 않는다.

### 3.2 부분 영상 지원

영상 시작 전의 전달 이벤트를 추론하지 않는다. 첫 프레임에서 이미 집도의가 들고
있거나 Mayo stand에 있는 도구는 `initial_state`로 기록한다. 식별이 어렵다면
`unknown_tool` 또는 `unknown` 위치를 허용한다.

### 3.3 정보 누설 방지

정답 토픽은 다음 namespace를 사용한다.

```text
/evaluation/ground_truth/annotation_manifest
/evaluation/ground_truth/tool_events
```

VLM, 디지털 트윈 reducer, BT는 이 토픽을 구독하지 않는다. 정답 토픽은 평가기와
선택적인 oracle baseline 노드만 구독할 수 있다. 실제 end-to-end 성능을 측정할
때 oracle baseline은 실행하지 않는다.

### 3.4 원본 보존

기존 21개 토픽과 원본 MCAP 파일은 수정하지 않는다. 어노테이션 삽입기는 원본을
읽고 모든 메시지와 새 정답 이벤트를 시간순으로 병합하여 별도 파생 bag을 만든다.

## 4. 최소 이벤트 온톨로지

### 4.1 필수 이벤트

| 이벤트 | 완료 시각 정의 |
|---|---|
| `initial_state` | clip의 `t=0` |
| `tool_transfer` | 수령자가 도구를 단독으로 제어하고 전달자가 놓은 순간 |
| `place_on_mayo` | 도구가 Mayo stand에 놓이고 손에서 완전히 떨어진 순간 |
| `pickup_from_mayo` | 도구가 Mayo stand를 떠나 집는 사람의 손에 들어간 순간 |

`tool_transfer`는 전달과 반환을 모두 포함하는 중립 이벤트다. 아래 의미는
`from`과 `to`를 이용해 자동 파생한다.

| From | To | 파생 의미 |
|---|---|---|
| 스크럽 널스 손 | 집도의 손 | `handover` |
| 집도의 손 | 스크럽 널스 손 | `observed_direct_return` |
| 사람의 손 | Mayo stand | `place_on_mayo` |
| Mayo stand | 사람의 손 | `pickup_from_mayo` |
| 그 외 | 그 외 | `relocate` |

### 4.2 주체와 위치의 제한 어휘

`holder`:

```text
surgeon
scrub_nurse
assistant
circulating_nurse
none
unknown
```

`location`:

```text
left_hand
right_hand
both_hands
mayo_stand
instrument_table
surgical_field
off_screen
unknown
```

필요한 어휘는 데이터에서 실제로 확인된 경우에만 추가한다. 사람 또는 위치가
명확하지 않으면 잘못 추정하지 않고 `unknown`을 사용한다.

### 4.3 이벤트 경계

이벤트의 대표 시각 `time_sec`은 동작 시작이 아니라 소유권 또는 위치 변화가
완료된 시각이다. temporal grounding 모델이 제시한 시작·종료 구간은 선택적으로
보존할 수 있다.

```text
candidate_start_sec  선택
candidate_end_sec    선택
time_sec             필수, 최종 상태 변화 시각
```

이 정의를 사용하면 영상 속 동작 속도와 관계없이 디지털 트윈 상태 전이 시점을
일관되게 비교할 수 있다.

## 5. JSON 스키마

### 5.1 확정 이벤트 예시

```json
{
  "schema": "taskplanner.observable_tool_event.v1",
  "case_id": "0704_5",
  "event_id": "0704_5-E0042",
  "event_type": "tool_transfer",
  "time_sec": 124.73,
  "tool": {
    "id": "army_navy_retractor",
    "name": "Army-Navy retractor"
  },
  "from": {
    "holder": "scrub_nurse",
    "location": "right_hand"
  },
  "to": {
    "holder": "surgeon",
    "location": "left_hand"
  },
  "derived_action": "handover",
  "source_views": ["cam2", "cam4"],
  "visibility": "clear",
  "review_status": "confirmed"
}
```

### 5.2 부분 영상 초기 상태 예시

초기 상태도 도구 하나당 한 이벤트를 사용해 이후 상태 전이와 같은 구조로
처리한다.

```json
{
  "schema": "taskplanner.observable_tool_event.v1",
  "case_id": "0704_5",
  "event_id": "0704_5-I0001",
  "event_type": "initial_state",
  "time_sec": 0.0,
  "tool": {
    "id": "bovie_cautery",
    "name": "Bovie surgical cautery"
  },
  "from": null,
  "to": {
    "holder": "surgeon",
    "location": "right_hand"
  },
  "derived_action": "initial_state",
  "source_views": ["cam1", "cam4"],
  "visibility": "partial",
  "review_status": "confirmed"
}
```

### 5.3 필수 필드

| 필드 | 규칙 |
|---|---|
| `schema` | 고정값 `taskplanner.observable_tool_event.v1` |
| `case_id` | MCAP case ID와 일치 |
| `event_id` | case 내 유일 |
| `event_type` | 제한 어휘 사용 |
| `time_sec` | bag의 `t=0` 기준 초, bag duration 내부 |
| `tool.id` | canonical ID 또는 `unknown_tool_NN` |
| `tool.name` | 사람이 읽을 수 있는 실제 도구명 |
| `from` | `initial_state`를 제외하고 필수 |
| `to` | 항상 필수 |
| `visibility` | `clear`, `partial`, `occluded` |
| `review_status` | `proposed`, `confirmed`, `ambiguous`, `rejected` |

`derived_action`은 사람이 직접 입력하지 않고 validator가 `from`과 `to`로부터
계산한다.

## 6. 파일 구조

어노테이션 소스는 MCAP과 분리된 JSONL로 관리한다.

```text
annotations/
  schema/
    observable_tool_event.v1.schema.json
  catalogs/
    tools.yaml
  cases/
    0704_5/
      tool_events.v1.jsonl
      annotation_manifest.json
    ...
    0704_17/
      tool_events.v1.jsonl
      annotation_manifest.json
  review/
    adjudication_log.jsonl
  reports/
    annotation_validation_summary.json
```

`tools.yaml`은 procedure YAML의 도구를 시작점으로 사용하되, 영상에 실제로 등장한
도구가 procedure 목록에 없으면 `dataset_only` 상태로 추가한다. 도구를 억지로
기존 procedure 도구에 매핑하지 않는다.

## 7. 반자동 어노테이션 흐름

### 7.1 1단계: 수동 기준 세트

먼저 시간 특성과 장면 구성이 다른 3개 case를 선택해 완전 수동으로 라벨링한다.
이 기준 세트는 다음 용도로 사용한다.

- 이벤트 정의와 단축키 검증
- 모델 query 문장 작성
- 모델 후보 재현율 측정
- 사람 간 시각 일치 허용 범위 결정

### 7.2 2단계: 모델 기반 후보 구간 생성

TimeLens2 같은 텍스트 기반 temporal grounding 모델은 정답 생성기가 아니라
고재현율 후보 탐색기로 사용한다. 예시는 다음과 같다.

```text
The scrub nurse hands a surgical instrument to the surgeon.
The surgeon returns a surgical instrument to the scrub nurse.
A person places a surgical instrument on the Mayo stand.
A person picks up a surgical instrument from the Mayo stand.
```

후보 생성기는 다음 정보를 결합한다.

- 여러 카메라의 저해상도 proxy video
- 도구 bbox, segmentation, track의 출현·소실·이동
- transcript의 도구명 또는 요청 표현
- 손과 Mayo stand 주변의 시각적 변화

모델 결과에는 `candidate_start_sec`, `candidate_end_sec`, query, confidence를
저장한다. 이 값은 `review_status=proposed`이며 최종 정답으로 간주하지 않는다.

### 7.3 3단계: 사람이 정확한 전이 시각 확정

기존 동기화 GUI에 annotation mode를 추가한다.

- 다중 카메라 동시 재생
- pause, frame step, ±0.1초·±1초 이동
- 후보 구간 바로가기
- 도구 선택
- `from.holder/location`, `to.holder/location` 선택
- source view와 visibility 표시
- 저장, 수정, reject, ambiguous 처리
- 인접 이벤트와 현재 도구 소유 상태 표시

권장 단축키:

```text
H  handover 후보
R  direct return 후보
M  place on Mayo
P  pickup from Mayo
I  initial state
Enter  confirm
Backspace  reject
```

### 7.4 4단계: 검수

- 전체 이벤트는 1차 검수자가 확인한다.
- `partial`, `occluded`, `unknown_tool` 이벤트는 2차 검수한다.
- 전체의 최소 10%는 두 명이 독립 검수해 시간·도구·주체 일치도를 측정한다.
- 의견이 다르면 `adjudication_log.jsonl`에 원안, 수정안, 최종안을 남긴다.

## 8. Annotation Validator

`validate_annotations.py`는 다음을 검사한다.

1. JSON Schema 준수
2. case ID와 디렉터리 일치
3. event ID 중복 없음
4. `time_sec` 단조 증가 및 bag duration 내부
5. 제한 어휘 준수
6. `derived_action` 재계산 결과 일치
7. 각 도구의 이전 `to`와 다음 이벤트의 `from` 일치
8. 한 도구가 동시에 두 holder에 존재하지 않음
9. Mayo에서 집는 이벤트 전에 해당 도구가 Mayo에 존재함
10. 초기 상태 또는 최초 관측 이전에는 상태를 임의로 추정하지 않음

`ambiguous` 이벤트는 데이터에는 보존하되 기본 정확도 계산에서 제외한다.

## 9. MCAP 삽입 방식

### 9.1 토픽

```text
/evaluation/ground_truth/annotation_manifest
  std_msgs/msg/String JSON
  t=0에 1회

/evaluation/ground_truth/tool_events
  std_msgs/msg/String JSON
  각 이벤트의 time_sec에 1회
```

`std_msgs/msg/String`을 사용하면 Taskplanner custom message 설치 여부와 관계없이
Foxglove, rosbag2, 일반 ROS 2 도구에서 JSON을 바로 확인할 수 있다. 정확한 발행
시각은 MCAP record timestamp가 담당하며 JSON 안에도 `time_sec`을 중복 보존한다.

### 9.2 삽입기

`inject_annotations.py`는 다음 순서로 동작한다.

1. 원본 bag과 확정 JSONL을 연다.
2. 원본의 모든 topic metadata를 복사한다.
3. 두 ground-truth topic metadata를 추가한다.
4. `stamp_ns = round(time_sec * 1_000_000_000)`으로 변환한다.
5. 원본 메시지와 JSON 이벤트를 timestamp 기준으로 안정 정렬한다.
6. staging 디렉터리에 새 MCAP을 작성한다.
7. 검증이 통과한 경우에만 파생 출력 디렉터리로 이동한다.

동일 timestamp에서는 manifest, initial state, 원본 메시지, 일반 이벤트 순서를
고정해 빌드 결과가 매번 동일하도록 한다.

### 9.3 파생 bag

```text
annotated_bags/
  0704_5/
    0704_5_annotated.mcap
    metadata.yaml
  ...
```

파생 schema version 예시:

```text
arpa_h_0704_multimodal_bag_v6_observable_gt
```

기존 checksum 파일을 덮어쓰지 않고, 파생 bag 전용 checksum과 build report를
새로 만든다.

### 9.4 재생

```bash
ros2 bag play annotated_bags/0704_5 --clock --delay 1
```

재생 중에는 이벤트가 영상·음성·transcript와 같은 bag clock에서 발행되어야 한다.

```bash
ros2 topic echo /evaluation/ground_truth/tool_events
```

## 10. Taskplanner 적용 방식

### 10.1 Shadow end-to-end 모드

가장 중요한 실험 모드다.

```text
공개 영상·음성·인식 데이터 -> VLM -> reducer -> BT -> shadow skill decision
정답 tool event -------------------------------> evaluator only
```

- Taskplanner 행동은 실제 녹화 영상을 바꾸지 않는다.
- 영상은 원래 타임라인대로 끝까지 진행한다.
- BT 명령은 실행 결과 대신 shadow decision으로 기록한다.
- evaluator는 각 실제 이벤트 전까지 나온 VLM 예측과 BT 결정을 비교한다.
- ground-truth topic은 VLM, reducer, BT 입력에 연결하지 않는다.

이 방식이면 Taskplanner가 영상 속 스크럽 널스와 다른 판단을 해도 재생이 막히지
않는다.

### 10.2 Post-event reconciliation 모드

긴 부분 영상에서 Taskplanner 내부 도구 상태가 실제 영상과 계속 벌어지는 문제를
분리 진단하기 위한 모드다.

1. 실제 이벤트가 발생하기 전에는 정답을 공개하지 않는다.
2. 이벤트 timestamp가 지난 뒤에만 confirmed event를 관측 결과로 변환한다.
3. 디지털 트윈은 이를 `external_observation`으로 받아 현재 물리 상태를 맞춘다.
4. 다음 이벤트 구간의 판단을 계속한다.

이는 자연스러운 연속 재생을 위한 평가 보조 모드이며, VLM end-to-end 성능 결과와
섞지 않고 `oracle_post_event_reconciliation=true`로 명시한다.

### 10.3 Oracle observation baseline

정답 event를 event timestamp에 `ToolObservation` 또는 별도 외부 관측 메시지로
변환해 reducer와 BT만 시험한다. 이 결과는 인식 오차를 제거했을 때 downstream
정책의 상한선이다. 일반 성능으로 보고하지 않는다.

## 11. 평가 기준

영상 속 스크럽 널스의 행동은 유일한 최적해가 아니라 실제로 관측된 기준
trajectory다. 따라서 BT 결과를 단순 exact match만으로 평가하지 않는다.

| 판정 | 의미 |
|---|---|
| `exact_match` | 실제 다음 이벤트와 도구·행동이 일치 |
| `needs_human_adjudication` | 공개 상태만으로 잘못 또는 임상적으로 허용 가능한 대안을 확정할 수 없음 |
| `unsafe_or_impossible` | 오염, 중복 소유, 없는 도구, 잘못된 위치 등으로 실행 불가능 |
| `missed_opportunity` | 필요한 시점까지 결정이 없음 |
| `not_evaluable` | 가림, 도구 불명, clip 경계로 판단 불가 |

다음 도구 예측은 `place_on_mayo`나 반환 이벤트가 아니라 이후의
`scrub_nurse -> surgeon` handover를 target으로 삼는다. lead-time window는
고정 상수가 아니라 보고서에 함께 기록하는 설정값으로 둔다.
`retrieve_from_mayo`는 handover false positive에 포함하지 않고 별도 회수
감사에서 관측 상태, 이후 재사용 간격, blocker/suspicious 여부를 보고한다.

도구 이벤트 라벨만으로 가능한 평가는 다음과 같다.

- 다음 실제 handover 도구 top-1 정확도
- handover 이전 최초 정답 예측 lead time
- Mayo 배치·회수 상태 추적 정확도
- BT action의 exact/acceptable/unsafe 분포
- digital twin 도구 소유 상태의 시간 일치율

Phase 정확도 평가는 별도 phase interval ground truth가 생긴 뒤 활성화한다.

## 12. 검증 계획

### 12.1 어노테이션

- 모든 JSONL이 schema validator를 통과한다.
- confirmed 이벤트의 도구 상태 invariant 위반이 0건이다.
- 2인 검수 subset에서 도구와 from/to exact agreement를 보고한다.
- 대표 시각 차이는 median, p95, 최대값으로 보고한다.

### 12.2 MCAP

- `ros2 bag info -s mcap`에서 원래 21개 토픽과 2개 정답 토픽이 보인다.
- 원본 21개 토픽의 message count가 원본 bag과 동일하다.
- 원본 각 메시지의 timestamp와 serialized payload hash가 동일하다.
- ground-truth event message 수가 confirmed·ambiguous JSONL 행 수와 일치한다.
- 각 ROS event timestamp가 `round(time_sec * 1e9)`와 정확히 일치한다.
- 모든 이벤트가 bag duration 내부에 있고 timestamp 순서가 단조 증가한다.
- 재생 시 GUI 영상, transcript, 정답 이벤트가 같은 시각에 나타난다.

### 12.3 정보 경계

- VLM, digital twin, BT node의 subscription 목록에
  `/evaluation/ground_truth/*`가 없어야 한다.
- end-to-end launch에는 reconciliation/oracle node가 기본 비활성이다.
- evaluator를 종료해도 Taskplanner의 동작이 달라지지 않아야 한다.
- oracle 결과에는 별도 run mode와 watermark를 기록한다.

## 13. 구현 순서

### Milestone 1: 계약 고정

- JSON Schema 작성
- actor/location/tool vocabulary 작성
- 이벤트 완료 시각 지침 작성
- validator 단위 테스트 작성

완료 조건: 예제 JSONL의 정상·오류 case가 모두 기대대로 판정된다.

### Milestone 2: 3개 case 수동 pilot

- 기존 동기화 GUI에 annotation mode 추가
- 3개 case의 initial state와 모든 tool transition 작성
- 2인 검수 subset으로 정의 모호성 수정

완료 조건: 한 도구의 상태 timeline을 처음부터 끝까지 재구성할 수 있다.

### Milestone 3: 반자동 후보 생성

- TimeLens2 temporal query runner 추가
- detector track·transcript hint 결합
- 후보를 GUI timeline에 표시
- 수동 pilot 대비 candidate recall 측정

완료 조건: 후보 누락률과 사람당 어노테이션 시간이 정량화된다.

### Milestone 4: 전체 case 라벨링

- `0704_5`부터 `0704_17`까지 JSONL 작성
- ambiguous·unknown 집중 2차 검수
- validation summary 생성

완료 조건: 13개 case 모두 schema와 state invariant를 통과한다.

### Milestone 5: MCAP 삽입

- annotation injector 작성
- 파생 MCAP 13개 생성
- 원본 메시지 보존, 새 토픽 count·timestamp 검증
- checksum과 build report 생성

완료 조건: 모든 파생 bag을 `ros2 bag info`와 실제 replay로 검증한다.

### Milestone 6: Taskplanner shadow 평가

- 실제 cam4 영상 입력 remap
- 실제 transcript/speech adapter 연결
- decision recorder와 ground-truth evaluator 추가
- shadow, post-event reconciliation, oracle baseline을 분리 실행

완료 조건: 영상이 끝까지 중단 없이 재생되고, 모드별 결과가 구분된 표와
timeline으로 생성된다.

현재 상태: `0704_5-strict-live-015`에서 strict 전체 재생, 정보경계 감사,
4계층 평가, 회수 감사, Markdown/CSV/SVG/PNG 보고서 생성을 완료했다. 합성
fixture `synthetic-strict-023/024`는 공개 입력부터 shadow sink까지 동일한
semantic digest와 일치하는 artifact hash를 재현했다. 파생 MCAP도 ROS 2
Jazzy에서 원본 30,999개 메시지의 payload, timestamp, replay order가 동일함을
재검증했다. 실제 0704_5에는 phase interval 정답이 없으므로 phase 평가는
완료 구현 상태이지만 실제 점수는 `not_available`이다.

## 14. 최종 산출물

1. `observable_tool_event.v1` JSON Schema
2. 데이터셋 도구 catalog
3. 13개 case의 `tool_events.v1.jsonl`
4. multi-view annotation GUI
5. TimeLens2 기반 후보 생성기
6. annotation validator와 검수 report
7. 원본 보존형 MCAP annotation injector
8. ground-truth JSON 토픽이 포함된 13개 파생 MCAP
9. Taskplanner shadow replay launch
10. exact/acceptable/unsafe를 구분한 평가 report

## 15. 첫 구현 단위

전체 자동화를 한 번에 시작하지 않고 다음 세 항목을 첫 작업 단위로 삼는다.

1. JSON Schema와 validator를 먼저 작성한다.
2. `0704_5` 한 case를 사람 손으로 어노테이션한다.
3. 해당 JSONL을 `/evaluation/ground_truth/tool_events`로 삽입한 파생 MCAP을
   만들고 영상·음성·이벤트 동기 재생을 검증한다.

이 pilot이 통과한 뒤 annotation GUI와 TimeLens2 후보 생성기를 붙이면, 스키마나
시간축을 뒤늦게 다시 바꾸는 비용을 줄일 수 있다.
