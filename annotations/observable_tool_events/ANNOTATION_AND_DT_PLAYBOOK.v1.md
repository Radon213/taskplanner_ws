# 실제 수술영상 관측 이벤트 및 DT 검증 참조 생성 Playbook v1

상태: `development_policy`

기준 사례: `0704_6`

적용 대상: `0704_7`–`0704_17`

## 목차

1. 목적과 비협상 원칙
2. 권한과 책임
3. 케이스별 처리 단계
4. 정확 프레임 판정 규칙
5. Marlin Policy 02
6. Codex 5.6-sol 3-pass 검수
7. 명시적 chain 기반 DT projection
8. 음성 문맥 정책
9. provisional Phase 정책
10. 평가 mask 정책
11. provenance와 필수 산출물
12. fail-closed 완료 gate
13. 정보경계

## 1. 목적과 비협상 원칙

이 문서는 실제 수술영상에서 Taskplanner 검증에 필요한 최소 관측
이벤트를 만들고, 그 관측층에서 별도의 DT 검증 참조를 파생하는 표준
절차를 정의한다. `0704_6`에서 발생한 경계 오류, 가림, 음성 정정,
cleanup, 중복 도구 인스턴스, DT projection 오류 가능성을 이후
케이스에서 반복하지 않는 것이 목적이다.

원시 수동 라벨은 다음 세 종류로 제한한다.

- 빈 손바닥을 펼쳐 도구 수령을 기다리는 `implicit_tool_request` 구간
- 도구의 제어 또는 지지가 세 추적 위치 사이에서 바뀌는
  `tool_transfer` 순간
- 임상 장면과 지속적인 도구 패턴 변화로 구분한 `phase_start` 순간

추적 위치는 다음 세 값만 허용한다.

- `mayo_stand`
- `scrub_nurse`
- `surgeon`

영상의 assistant는 시연 계약상 `surgeon`으로 정규화한다. 이 정규화가
쓰인 이벤트에서는 실제 actor identity를 채점하지 않는다.

다음 원칙은 예외 없이 적용한다.

- Ground truth와 평가 reference는 VLM, reducer, DT reducer, BT의 런타임
  입력이 아니다.
- 영상과 공개 음성에서 외부적으로 관측 가능한 사실만 기록한다.
- 집도의의 숨은 의도, 보이지 않는 도구 이동, 가려진 release를
  그럴듯하게 보완하지 않는다.
- 손 자세와 semantic tool request는 분리한다. 손 자세 라벨에는
  요청 도구를 넣지 않는다.
- 음성의 도구명은 검색과 시각적 식별 보조에 쓸 수 있으나 음성만으로
  이동 이벤트를 만들지 않는다.
- Raw observed reference와 DT reference는 항상 별도 파일로 유지한다.
- 불확실성을 억지로 확정하는 대신 `ambiguous`, censoring, mask,
  `not_scorable`을 사용한다.
- 완벽한 결과란 불확실성이 0인 결과가 아니라, 모든 불확실성이
  명시되고 부당한 확정이 없는 결과다.
- 이전 버전과 원본은 덮어쓰지 않는다. 수정은 새 버전으로 발행하고
  이전 버전은 audit intermediate로 보존한다.

`0704_6` 관측을 이용해 현재 정책과 procedure prompt를 개발했으며,
`0704_7`–`0704_17`도 이 정책과 Phase ontology를 반복 개선하는 데
사용한다. 따라서 `0704_6`–`0704_17` 전체는
`development_calibration`이다. 이 사례군으로 held-out 일반화 성능을
주장해서는 안 된다. Phase ontology와 runtime prompt를 동결한 뒤
수집되는 새로운 영상을 held-out 평가에 사용한다.

## 2. 권한과 책임

### 2.1 NemoStation/Marlin-2B

Marlin은 temporal search proposal generator다.

Marlin이 할 수 있는 일:

- transcript anchor와 full-video anchor 주변의 coarse temporal span 제안
- 같은 event에 대한 두 질의의 within-model consensus 계산
- 장면 caption과 raw model response 보존
- 검토 우선순위를 위한 검색 위치 축소

Marlin이 할 수 없는 일:

- `confirmed`, `ambiguous`, `rejected` 최종 판정
- 정확한 source frame 또는 request interval 경계 확정
- Phase 생성 또는 Phase 경계 판정
- 음성만으로 요청·이동 이벤트 생성
- 도구 종류, 이동 방향, actor identity를 최종 확정
- 모델이 찾지 못한 구간을 negative ground truth로 선언
- observability gap 안의 이벤트 추론

Marlin 출력은 항상 `proposal_only_not_ground_truth`이며 모든 후보는
`review_status=proposed`로 시작한다.

### 2.2 Codex 5.6-sol

Codex는 사용자에게 명시적으로 위임된 영상 판독자다.

Codex가 수행할 일:

- Marlin 후보와 독립적으로 전체 영상 검토
- 정확 CAM4 timeline을 기준으로 밀집 프레임 판독
- CAM1, CAM2, CAM3, FLIR 등 alternate view 대조
- 공개 transcript와 영상의 인과적 대조
- 누락 이벤트 추가, 중복 제거, false proposal 기각
- 도구 종류·방향·도착 프레임의 시각적 확정
- 동일 물리 이동 chain의 명시적 연결
- Raw observed reference, DT reference, Phase context, mask, report 생성
- 물리 연속성, evaluator contract, 정보경계 검증

Codex 확정 행은 다음 provenance를 사용한다.

- `label_origin=assistant_video_adjudication`
- `review.reviewer_kind=ai_assistant`
- `review.reviewer_id=codex-gpt-5.6-sol`
- `review.authorized_by`에 실제 사용자 승인 주체
- `review.reviewed_at`에 실제 검수 시각
- `review.notes`에 프레임 기반 관측 근거

Codex 판정을 `human_video_review`로 기록하거나 사람 검토 로그를
위조해서는 안 된다. 사용자 직접 검토가 실제로 수행된 행만
`reviewer_kind=human`과 `human_video_review`를 사용한다.

### 2.3 결정론적 도구

결정론적 코드가 수행할 일:

- frame index에서 canonical `time_sec` 재계산
- schema와 tool catalog 검증
- hash와 create-only publication
- 명시적으로 승인된 chain에 대한 DT projection
- mask에 따른 evaluator target 선택
- source/output mapping 및 report 생성

결정론적 코드가 해서는 안 되는 일:

- time gap과 tool type만으로 동일 물리 인스턴스라고 단정
- 시각적 관측 없이 Phase나 transfer 방향 추론
- 누락된 initial inventory를 보완
- `ambiguous`를 자동으로 `confirmed`로 승격

### 2.4 사용자

사용자의 일상적인 케이스별 판정은 필수 gate가 아니다. Codex는
관측 가능한 범위에서 독립적으로 완료하고, 해결 불가능한 항목은
사용자에게 강제로 선택시키지 않고 `ambiguous` 또는 `not_scorable`로
닫는다.

사용자가 개입할 수 있는 지점은 다음과 같다.

- Phase ontology 또는 DT contract 정책 변경
- 필요 시 최종 read-only GUI 표본 감사
- 새로운 관측 사실이나 기존 판정 오류의 명시적 교정

## 3. 케이스별 처리 단계

각 케이스는 다음 상태를 순서대로 통과한다. 앞 단계 gate가 실패하면
뒤 단계 파일을 final로 발행하지 않는다.

### 3.1 `SOURCE_PREPARED`

필수 작업:

- 원본 MCAP와 metadata의 위치·SHA-256 기록
- CAM4의 모든 source frame에 대한 canonical timeline 생성
- frame count, source FPS, 시작·종료 시각 기록
- timestamp discontinuity를 gap으로 검출
- `/surgery/transcript` 전체를 causal voice v2로 추출
- transcript-guided search anchors 생성
- full-video search anchors 생성

CAM4 timeline은 모든 interaction label의 canonical 좌표다. 다른
카메라는 증거 view이며 그 카메라의 N번째 frame을 CAM4의 N번째
frame이라고 임의로 가정하지 않는다. 다른 view를 사용할 때도
저장되는 `source_frame_idx`와 `time_sec`는 CAM4 timeline에 맞춘다.

### 3.2 `MARLIN_PROPOSED`

두 Marlin 프로세스를 동시에 실행한다. 두 프로세스는 독립된 anchor
범위와 독립된 create-only 출력 경로를 사용한다.

완료 조건:

- transcript pass와 full-scan pass가 모두 완료
- 모든 model revision, query, span, caption, validation error 보존
- gap 내부 anchor는 명시적 skipped record로 남음
- 모든 출력이 proposal-only
- 모델 consensus가 없는 raw response도 삭제하지 않음

### 3.3 `CODEX_VISUALLY_ADJUDICATED`

Codex 3-pass 검수를 수행한다. 모든 Marlin 후보는
`confirmed`, `ambiguous`, `rejected` 중 하나로 판정하고, 후보에서
누락된 실제 이벤트를 별도 assistant annotation으로 추가한다.

완료 조건:

- 미검토 후보 0건
- 전체 영상 coverage 완료
- 모든 음성 이벤트 대조 완료
- 모든 confirmed event에 exact frame 근거 메모 존재
- 동일 request 또는 동일 물리 transfer의 중복 후보 제거

### 3.4 `OBSERVED_FINAL`

물리적으로 관측된 이벤트를 시간순으로 발행한다.

포함 대상:

- 확정된 implicit request interval
- 확정된 세 위치 간 tool transfer
- DT에서 제외될 scrub-only 정리 동작
- DT에서 collapse될 물리적 중간 전이
- completion 이후의 관측 가능한 cleanup 전이

Raw observed reference는 projection 때문에 삭제되거나 edge가 바뀌면
안 된다.

### 3.5 `DT_PROJECTED`

Raw observed reference에 명시적 chain policy를 적용한다.

완료 조건:

- 모든 observed event에 identity, exclude, collapse 중 하나의 mapping
- 모든 projected event에 source event provenance
- compound action의 target과 substep 분리
- unclosed return과 cleanup의 scoring role 명시

### 3.6 `PHASE_PROVISIONAL`

interaction final 이후 Codex가 Phase를 판정한다. 현재 Phase는 모두
provisional context로 발행하며 accuracy ground truth가 아니다.

### 3.7 `MASKED_AND_VALIDATED`

다음 작업을 완료한다.

- evaluation mask와 voice context role 작성
- source/cutoff/instance uncertainty 적용
- manifest의 모든 파일 hash와 count 갱신
- schema 및 artifact consistency test
- evaluator smoke run
- information-boundary scan
- observed/final-DT GUI와 overlay 검증

## 4. 정확 프레임 판정 규칙

### 4.1 공통 규칙

- `source_frame_idx`가 권위 좌표다.
- `time_sec`는 timeline의 해당 frame timestamp에서 다시 계산한다.
- point를 wall-clock 또는 proxy seek time으로 직접 저장하지 않는다.
- 경계 전후의 상태가 각각 명확해질 때까지 frame-by-frame으로 본다.
- `confirmed` 메모에는 경계 이전, 경계 frame, 경계 이후의 차이를
  기록한다.
- frame gap 안이나 gap을 가로질러 point/interval을 만들지 않는다.
- 가림 때문에 경계를 보지 못했다면 가장 그럴듯한 중간 frame을
  선택하지 않는다.

### 4.2 암묵적 손 요청 구간

`implicit_tool_request`는 빈 손을 펼쳐 도구 수령을 기다리는 관측
자세다. semantic tool request나 eventual handover tool을 뜻하지 않는다.

시작 frame:

- 손에 도구가 없다.
- 손바닥 전체가 수령 가능한 방향, 보통 위쪽을 향한다.
- 손가락이 명확하게 펼쳐져 있다.
- 접근, 회전, 손 펴기 전이가 끝난 첫 frame이다.
- 직전 frame은 위 조건 중 하나 이상을 충족하지 않는다.

제외할 frame:

- 이전 도구와 손이 아직 접촉
- 손을 수술야에서 빼는 중
- 손목을 돌리는 중
- 손가락을 펴는 중
- 손바닥이 부분적으로 보여 수령 자세인지 불명확
- 단순 작업 손동작 또는 지지 자세

종료 frame:

- 도구와 아직 접촉하지 않은 마지막 frame이다.
- 다음 frame에서 도구 접촉이 시작되거나 요청 자세가 명확히 끝난다.

연속성:

- 음성 correction이나 cancellation 중에도 동일한 빈 손 자세가
  끊기지 않으면 하나의 interval로 유지한다.
- 같은 자세를 음성 문장별로 중복 분할하지 않는다.
- 반대로 손바닥 자세가 명확히 끝난 뒤 다시 완전히 펼쳐지면 별도
  request interval로 기록할 수 있다. 두 interval이 같은 한 건의
  eventual handover로 이어져도 transfer를 중복 생성하지 않는다.

Censoring:

- 영상 첫 frame에서 이미 자세가 유지 중이면 left-censored다.
- gap 뒤 첫 frame에서 이미 자세가 유지 중이면 left-censored다.
- 이때 `gesture_presence=true`, `gesture_onset=false`로 mask한다.
- 화면 밖 이동이나 가림으로 종료를 볼 수 없으면 offset은
  ambiguous 또는 mask한다.

손 요청 record에는 `tool`, `from`, `to`, `phase_id`를 넣지 않는다.

권장 검수 메모 형식:

> fA–fB는 손 펴기 전이이고 fC에서 빈 손바닥과 펼친 손가락이 처음
> 명확하다. fD까지 무접촉 자세가 유지되고 fD+1에서 도구 접촉이
> 시작된다.

### 4.3 사람 사이 도구 전달

사람→사람 transfer point는 다음이 처음 함께 성립한 frame이다.

- 수령자가 도구를 안정적으로 제어
- 제공자의 제어가 해제
- 이후 frame에서도 수령자 소유 상태가 유지

양쪽이 동시에 잡은 frame은 transfer 완료가 아니다. 제공자 손이
분리되지 않거나 수령자 제어가 불명확하면 confirmed point를 만들지
않는다.

### 4.4 Mayo에서 pickup

`mayo_stand → scrub_nurse` point는 다음이 처음 성립한 frame이다.

- 도구가 Mayo의 지지만 받는 상태에서 벗어남
- scrub이 안정적으로 제어
- 이후 surgeon handover 또는 Mayo replacement로 이어지는 동일한
  도구 이동이 확인됨

단순 접촉이나 집으려는 동작만으로 pickup을 확정하지 않는다.

### 4.5 Mayo에 placement

`scrub_nurse/surgeon → mayo_stand` point는 첫 tray 접촉 frame이 아니다.
다음이 처음 성립한 frame이다.

- Mayo가 도구를 지지
- 이전 holder의 손이 도구에서 분리
- 놓기가 완료됨

도구가 tray에 닿았지만 손이 계속 잡고 있다면 아직 placement가 아니다.

### 4.6 위치와 actor 정규화

- Mayo 밖의 공급 위치는 추적하지 않는다.
- 외부에서 가져온 도구는 최초로 관측되는
  `scrub_nurse → surgeon` 전달부터 기록한다.
- 영상 assistant 수령자는 demo contract에 따라 `surgeon`으로
  정규화한다.
- 정규화된 이벤트에서는 actor identity metric을 비활성화한다.
- 집도의가 도구를 어디에 내려놓았는지 추적할 필요가 없더라도,
  Mayo 도착이 명확하고 DT 상태 commit에 필요한 경우
  `surgeon → mayo_stand`를 기록한다.

### 4.7 도구 종류

- canonical tool catalog ID만 사용한다.
- 영상 형태와 공개 음성의 시간적 일치를 함께 사용할 수 있다.
- 음성 도구명이 cancellation되거나 correction되면 최종 발화와 실제
  이동을 다시 대조한다.
- 음성에 도구명이 있지만 이동이 보이지 않으면 transfer를 만들지 않는다.
- 전달은 보이지만 도구 종류를 확정할 수 없고 schema가 generic tool을
  허용하지 않으면 ambiguous로 남긴다.
- 여러 도구가 한 묶음으로 이동해 개별 release와 종류가 분리되지
  않으면 여러 point를 지어내지 않는다.

### 4.8 같은 시각의 독립 관측

하나의 frame에 서로 다른 관측 사실이 함께 성립할 수 있다. timestamp가
같다는 이유만으로 하나를 삭제하거나 두 사실을 하나의 이벤트로 합치지
않는다.

- surgeon이 도구를 Mayo에 완전히 놓는 순간 새 빈 손바닥 요청이
  시작되면 direct return point와 request interval onset을 각각 기록한다.
- 그 옆에서 scrub이 Mayo 도구를 정리하더라도 surgeon handover가 없으면
  그 정리 동작은 transfer가 아니다.
- 동시 관측은 각 대상 물체, holder release, recipient control을 독립적으로
  확인한다.
- 동일 timestamp는 동일 event 또는 동일 물리 chain이라는 근거가 아니다.

## 5. Marlin Policy 02

Policy ID 권장값:
`taskplanner.marlin_interaction_proposal.policy02`

### 5.1 모델 고정과 provenance

각 run은 다음을 기록한다.

- model ID: `NemoStation/Marlin-2B`
- 실제 model revision
- 실제 local model path
- PyTorch, CUDA, GPU
- video, timeline, anchor SHA-256
- query 문장과 event type
- clip window와 anchor 범위
- raw response, normalized span, validation error
- 실행 시간과 output SHA-256

revision은 기존 report에서 복사하지 말고 실제 로드한 checkpoint에서
검증한다.

### 5.2 동시 실행 2

동시에 로드하는 Marlin 인스턴스는 최대 2개다.

안전한 실행 방식:

- worker A와 B가 서로 다른 case 또는 서로 다른 anchor batch를 처리
- output/report path를 worker별로 분리
- 동일 create-only target에 두 worker가 쓰지 않음
- 각 run 완료 후 GPU 메모리와 process 종료 상태 확인
- 완료된 케이스는 Codex 검수 queue로 넘기고 두 worker는 다음 케이스
  처리

기존 `.initial.v1` evidence는 삭제하거나 덮어쓰지 않는다. 새 정책
결과는 새 run suffix와 새 candidate version으로 발행한다.

### 5.3 transcript-guided pass

transcript의 역할:

- tool mention 주변 검색 위치 제공
- `tool_hint` 제공
- request, correction, cancellation, maintenance, return 문맥 분류

transcript의 비권한:

- 발화가 visible request 또는 transfer의 존재를 보장하지 않음
- 발화 tool을 실제 전달 tool로 자동 확정하지 않음
- 발화 start에 complete 문장을 미리 공개하지 않음

긴 transcript는 word timing을 가장하지 않고 여러 search point로
분할한다.

### 5.4 full-video pass

full-video pass는 transcript-independent false negative를 찾는다.

coverage 규칙:

- 모든 observable video frame이 최소 한 clip에 포함되어야 함
- clip 사이에는 사각지대가 없어야 함
- 권장 예시는 center 12초 간격, before/after 각각 7초의 14초 창
- 실제 orchestration은 clip 길이를 명시적으로 넘겨야 함
- 코드 기본값이 1.25초+4.25초인 상태에서 12초 anchor 간격을 쓰면
  약 6.5초씩 보지 않는 구간이 생기므로 금지
- gap에서 clip을 분할하고 한 clip이 두 observability segment를
  가로지르지 않게 함

full-video 질의 대상:

- implicit open-palm request
- scrub→surgeon handover
- Mayo→scrub pickup
- surgeon→scrub return
- scrub→Mayo placement
- 직접 surgeon→Mayo placement 후보

도구 요청이 거의 항상 음성과 함께 발생하더라도 full scan에서
request와 handover를 빼면 안 된다.

#### 5.4.1 실제 coverage 계산과 보충 실행

anchor 개수나 설정값만 보고 전체 영상이 덮였다고 간주하지 않는다. 각
완료 run의 실제 `clip.start/end`를 corrected bag time으로 합친 뒤,
observability segment별 합집합과 차집합을 계산한다.

- gap은 coverage 분모에서 제외하되 별도 unobservable interval로 보존
- observable 차집합이 1 frame보다 작아도 0으로 반올림하지 않음
- 누락 구간마다 create-only supplemental anchor 파일을 생성
- supplemental clip도 gap과 영상 끝에서 잘라 같은 segment에만 유지
- 보충 run, proposal, report는 기존 파일을 덮어쓰지 않고 새 suffix 사용
- 보충 결과까지 합친 observable coverage가 100%가 되어야 Codex
  adjudication 완료 gate를 통과

실제 clip 합집합은 설정값이나 anchor 수로 대체하지 않고 다음
read-only 감사로 다시 계산한다.

```bash
PYTHONPATH=. python3 -m \
  tools.real_surgery_annotation.audit_marlin_policy02_coverage \
  --output \
  annotations/observable_tool_events/reports/\
marlin2_policy02_coverage_audit.v1.json
```

잘린 실제 클립보다 긴 span을 모델이 반환하면 그 query는 invalid다.
이때 span을 클립 끝으로 억지 보정하거나 candidate로 승격하지 않는다.
실제 클립 길이를 고려해 더 짧은 `clip-after`로 새 보충 run을 실행하고,
최초 실패도 기술적 audit evidence로 보존한다.

### 5.5 consensus와 span

한 event type에 대해 최소 두 개의 다른 paraphrase를 사용한다.

consensus 조건:

- 두 response 모두 format-valid span
- span endpoint가 finite
- start ≤ end
- clip 범위 안
- 같은 observability segment
- corrected bag time midpoint 차이가 정책 한계 이하

consensus는 confidence score가 아니다. 모델이 반대 방향 질의에 같은
span을 반환할 수 있으므로 direction과 tool identity는 Codex가 다시
판정한다.

Marlin midpoint를 final timestamp로 복사하지 않는다. Codex는 두
질의 span의 합집합과 주변 frame을 exact-frame으로 검토한다.

서로 배타적인 여러 event type이 두 paraphrase 모두에서 동일한 전체
클립 span과 동일 midpoint를 반환하면 `generic full-span saturation`
패턴으로 기록한다. 이는 다수 이벤트가 동시에 일어났다는 뜻이 아니다.
각 event type을 exact-frame으로 독립 검수하고, 관측 조건이 성립하지
않는 후보는 모두 기각한다.

### 5.6 후보 병합

- raw evidence 링크를 유지
- event ID 충돌을 거부
- 동일 시각의 상반된 방향 후보를 임의로 하나 선택하지 않음
- temporal overlap과 event type이 같은 후보는 review cluster로 묶을
  수 있으나 raw response는 보존
- 병합 결과의 모든 행은 `proposed`
- 병합 단계에서 ground truth count는 0이어야 함

## 6. Codex 5.6-sol 3-pass 검수

Marlin 후보만 검토하면 model false negative가 그대로 남는다. 각
케이스에서 다음 세 pass를 독립적으로 수행한다.

### 6.1 Pass 1: 후보 중심 exact-frame 검수

각 Marlin cluster에 대해:

1. coarse span 전부터 재생
2. 상태가 명확히 달라지는 frame까지 전후 확장
3. point 또는 interval 시작·종료를 한 frame씩 결정
4. alternate view로 control/release/tool identity 대조
5. confirmed/ambiguous/rejected 판정
6. 이전·경계·이후 frame을 메모

반대 방향 후보가 같은 span에 있어도 하나를 자동 선택하지 않고
실제 holder 변화를 본다.

### 6.2 Pass 2: 음성 중심 검수

모든 voice event를 순서대로 확인한다.

- tool request 뒤 실제 open palm 또는 handover가 있는지
- 이미 사용 중인 도구에 대한 명령인지
- cancellation되었는지
- within-utterance correction의 최종 도구가 무엇인지
- utterance가 영상 gap 안인지
- alternate view에만 handover가 보이는지
- procedure completion 이후 cleanup인지

음성은 누락 탐색에 적극 사용하되 visible event가 없으면 transfer를
생성하지 않는다.

### 6.3 Pass 3: 전체 영상과 물리 연속성 검수

후보와 음성 anchor를 보지 않는 독립적인 연속 재생 검수를 수행한다.

확인 항목:

- 무음 request와 transfer
- 요청 뒤 실제 handover 또는 cancellation
- 이전 도구 반환과 새 요청이 겹치는 구간
- scrub의 Mayo 정리 roundtrip
- surgeon→scrub→Mayo 연속 return
- Mayo pickup→scrub→surgeon compound handover
- 영상 종료 전 unclosed return
- 여러 같은 종류 도구가 동시에 존재하는지
- cleanup 시작과 procedure completion

물리 continuity audit는 tool type별 단일 상태 머신을 맹신하지 않는다.
같은 type의 source가 이전 belief와 맞지 않으면 다음 중 하나다.

- 여러 실제 인스턴스
- 관측하지 못한 전이
- 잘못된 tool identity
- 잘못된 transfer 방향

영상을 다시 보고도 구분할 수 없으면 instance-level state, physical
feasibility, reuse/recover를 mask한다.

## 7. 명시적 chain 기반 DT projection

### 7.1 Raw observed 보존

DT projection은 Raw observed record를 수정하지 않는다. projection
report에는 각 raw event가 어떻게 처리됐는지 남긴다.

필수 operation:

- `identity`
- `exclude_scrub_only_roundtrip`
- `collapse_surgeon_scrub_mayo`
- `exclude_unclosed_direct_return`

### 7.2 명시적 chain

현재 0704_6 projector는 동일 tool type의 인접 두 이동이 3초 안에
있고 edge 패턴이 맞으면 자동으로 같은 chain이라고 간주한다. 여러
물리 인스턴스가 있으면 서로 다른 도구의 이동을 잘못 collapse할 수
있다.

새 정책에서는 다음 근거를 모두 가진 pair/episode만 같은 chain으로
확정한다.

- Codex가 영상에서 같은 연속 물체 이동으로 확인
- 중간에 다른 동일 type 도구와 혼동될 접촉이 없음
- source event ID 목록이 명시됨
- chain 또는 episode ID가 명시됨
- 경계 frame과 도구 종류가 모두 확인됨

`max_continuous_chain_gap_sec`는 search heuristic 또는 sanity limit으로만
사용한다. time+tool type만으로 final collapse하지 않는다.

### 7.3 scrub-only Mayo roundtrip

다음 chain은 raw에는 남기고 DT에서 제외한다.

`mayo_stand → scrub_nurse → mayo_stand`

적용 조건:

- 동일 물리 도구
- surgeon handover가 없음
- 정리, 혼동, 잘못 집은 도구의 replacement

### 7.4 surgeon return collapse

다음 chain은 Mayo 도착 시각에 한 건으로 collapse한다.

`surgeon → scrub_nurse → mayo_stand`

DT 출력:

- `from=surgeon`
- `to=mayo_stand`
- timestamp=Mayo release 완료 frame
- direct observation이 아니라 derived projection임을 명시
- 두 source event ID 보존

중간 scrub handover가 보여도 Taskplanner의 의미 있는 최종 state commit은
Mayo 도착이다.

같은 frame에 surgeon의 새 request pose가 시작하더라도 return chain의
release/placement 근거와 request pose 근거를 각각 만족하면 두 이벤트는
공존할 수 있다. DT collapse는 return edge만 변환하며 request interval을
흡수하거나 이동시키지 않는다.

### 7.5 compound handover

다음 chain은 두 물리 전이를 DT에 유지한다.

`mayo_stand → scrub_nurse → surgeon`

평가 role:

- Mayo pickup: `compound_action_substep`, action/latency false
- surgeon arrival: `action_target`, action/latency true
- BT handover action은 한 번만 채점

### 7.6 unclosed direct return

`surgeon → scrub_nurse`가 보이지만 영상 끝까지 Mayo placement를
확인할 수 없으면:

- raw observed에 유지
- 현재 DT/BT action target에서 제외
- 이후 해당 도구의 state/physical/reuse를 mask
- Mayo 도착을 추측하지 않음

### 7.7 completion 이후 cleanup

procedure completion voice가 causal하게 이용 가능한 시각 이후의
return은 다음처럼 처리한다.

- raw observed에 보존 가능
- DT state observation only
- next-tool target 아님
- BT action target 아님
- cleanup interval mask 적용

### 7.8 duplicate tool instance

같은 canonical type이 물리적으로 여러 개 필요한 sequence라면:

- type-level handover action은 관측 가능하면 유지
- canonical singleton warning 생성
- 최소 필요 instance 수를 report
- instance ID가 구분되지 않으면 state/physical/reuse false
- singleton DT belief와 영상 사실의 불일치를 숨기지 않음

## 8. 음성 문맥 정책

### 8.1 Source voice track

voice source JSONL은 공개 transcript의 충실한 causal context다.

필수 필드:

- `time_sec`: 원 발화 시작
- `end_sec`: 원 발화 종료
- `available_sec`: 완성 text가 실제 이용 가능한 earliest time
- `text`: 원문
- source topic, source record timestamp, source message index, source wav
- `source_authority=public_runtime_transcript`
- `scoring_role=context_only_not_ground_truth`
- `availability_policy=not_before_utterance_end`

규칙:

- `available_sec >= end_sec`
- `available_sec`는 필요하면 MCAP record time까지 늦춤
- source voice record에 frame index를 넣지 않음
- source voice record에 inferred tool, semantic class, handover association을
  넣지 않음
- complete utterance를 발화 시작 시점에 runtime에 미리 공개하지 않음

### 8.2 Evaluation-side voice roles

semantic role은 source record를 수정하지 않고 evaluation mask 또는
별도 sidecar에 둔다.

허용 role 예:

- `procedure_context`
- `tool_request_context`
- `use_existing_tool_context`
- `correction_context`
- `gap_context`
- `procedure_completion_context`
- `offscreen_context`

`handover_target=true`는 실제 visible handover와 연결된 request에만
부여한다.

`handover_target`은 음성 자체를 정답으로 승격하는 표시가 아니라,
해당 public utterance 뒤/주변에 시각적으로 확인된 전달이 있었다는
evaluation-side 연결 표시다. visible transfer와 연결된 tool request 또는
최종 correction은 true, 취소된 최초 도구·기존 도구 사용 명령·전달 없는
발화는 false로 둔다.

예외:

- suction이 이미 surgeon 손에 있으면 새 handover target이 아님
- gap 안의 tool utterance는 false-negative target이 아님
- cancellation된 최초 도구 요청은 target이 아님
- `Bovie 아니 bipolar` 같은 문장은 `available_sec` 이후 최종 correction만
  runtime-visible

### 8.3 Cutoff

procedure completion cutoff는 발화 시작이 아니라 completion 문장의
`available_sec`다.

- action/next-tool scoring end = completion `available_sec`
- visual state audit end = 마지막 observable camera frame
- voice context end = 마지막 유효 transcript end/available time

completion 발화가 전혀 없는 예외 케이스에서는 마지막 명확한
task-relevant frame을 action cutoff로 두고, 바로 다음 source frame부터
cleanup mask를 연다. 이때 report와 mask reason에 visual-frame 근거를
명시하며 음성 completion이 있었다고 가장하지 않는다. 인접 두 frame의
timestamp 차이는 관측 누락 interval로 해석하지 않는다.

## 9. provisional Phase 정책

### 9.1 생성 시점과 권한

Phase는 Marlin에 맡기지 않는다. interaction observed/DT sequence를
최종화한 뒤 Codex가 영상 전체를 다시 검토해 생성한다.

Phase record는 현재 다음 상태다.

- `review_status=ambiguous`
- `status=provisional_ambiguous`
- `scoring_role=context_only_not_ground_truth`
- `phase_accuracy=false`
- `label_origin=assistant_video_adjudication`
- `review.reviewer_kind=ai_assistant`
- `review.authorized_by`에 사용자의 위임 근거 기록

Phase는 raw observed와 final DT GUI에 같은 별도 context track으로
포함한다. interaction confirmed count에는 포함하지 않는다.

0704_7 이후에는 사람이 직접 판정하지 않은 Phase에 human provenance를
가공하지 않는다. manifest의 Phase authority는
`user_authorized_ai_assistant_video_adjudication_provisional_context_not_scoring_ground_truth`
로 선언하고, `provisional_reference_file`, hash, event count, assistant
reviewer IDs, `authorized_by`를 직접 검증한다. 따라서
`human_decision_file`이나 가짜 human action log는 만들지 않는다.

### 9.2 경계 근거

Phase transition에는 다음 두 축이 함께 필요하다.

- observable clinical/operative-field configuration change
- 단일 교환이 아닌 지속적인 surgical-tool/action pattern change

경계는 새 상태가 처음 성립한 frame으로 두되, 그 뒤 약 1–2초의
multiview를 보고 같은 field/tool pattern이 실제로 지속되는지
후향적으로 확인한다. 이 confirmation horizon은 경계의 근거이지
타임스탬프 자체가 아니다.

단독으로 불충분한 근거:

- spoken phase 또는 tool name
- 한 번의 도구 handover
- open-palm request 시작 또는 음성 correction 종료 frame
- gap 내부 추정
- 임상 명칭만으로 가려진 해부학 구조 추정
- 원본 procedure 문서의 순서

### 9.3 Censoring과 미등장 Phase

- clip 시작 전에 Phase가 이미 진행 중이면 frame 0에
  `clip_initial_state`
- 실제 transition을 봤다고 주장하지 않음
- gap 주변이면 `uncertain_transition` 또는 경계 미생성
- procedure catalog에 존재하지만 영상에 나오지 않은 Phase는 authored
  placeholder로 둘 수 있음
- 미등장 Phase에는 해당 케이스 timestamp를 생성하지 않음

### 9.4 Cross-case optimization

`0704_7`–`0704_17`을 검토하며 다음 기준으로 Phase catalog를 반복
개선한다.

- 여러 사례에서 반복되는 observable cue
- VLM이 영상으로 구분할 수 있는 시각적 안정성
- reducer가 사용할 수 있는 단조롭고 일관된 순서
- Phase 내부 next-tool pattern의 응집도
- 경계 전후 tool/action distribution의 유의미한 변화
- 임상적으로 설명 가능한 이름
- 지나치게 많은 Phase를 만들지 않는 parsimonious 구조

ontology를 바꾸면 새 catalog version을 만들고 이전 catalog를
덮어쓰지 않는다. 현재 사례군을 이용해 ontology를 바꾸는 동안 모두
development/calibration 상태다.

### 9.5 Cleanup Phase

procedure completion과 instrument clearance는 demo end state일 수 있으며
임상 thyroidectomy Phase라고 단정할 수 없다.

- 반복 사례에서 독립적인 임상/도구 패턴 근거가 생기기 전까지 Phase로
  강제하지 않음
- completion voice와 cleanup mask로 표현 가능
- 0704_6에서 기각된 P10을 이후 케이스에 자동 복제하지 않음

### 9.6 현재 policy field 정합성

0704_6 `dt_projection_policy.v1.json`에는
`phase_reference_included=false`가 남아 있지만 v5 manifest와 final
review layer는 Phase context를 포함한다. 이 필드는 현재 projector에서
실질적으로 사용되지 않아 결과는 포함됐지만 audit 관점에서
모순이다.

`0704_7`–`0704_17` 템플릿에서는 다음 중 하나로 정합화한다.

- field를 제거하고 manifest의 별도 Phase descriptor만 권위로 사용
- 또는 `phase_reference_included=true`와
  `context_only_not_ground_truth`를 명시

어느 경우에도 Phase를 interaction/BT scoring target으로 합치지 않는다.

## 10. 평가 mask 정책

### 10.1 Default deny

mask가 존재하는 케이스는 모든 metric을 기본 false로 시작한다. 각
event와 interval에 대해 검증 가능한 metric만 명시적으로 true로 연다.

metric 예:

- `action`
- `latency`
- `state`
- `physical`
- `reuse`
- `gesture_presence`
- `gesture_onset`
- `phase_accuracy`
- `actor_identity`

알 수 없는 role 또는 instance resolution은 fail-closed
`not_scorable`로 처리한다.

### 10.2 Event role

요청:

- `gesture_target`
- presence는 자세가 보이면 true
- onset은 시작 경계를 실제로 봤을 때만 true

handover:

- completion/cleanup 전 완료된 `scrub_nurse → surgeon`은
  `action_target`
- Mayo pickup은 `compound_action_substep`
- return과 cleanup은 `state_observation_only`
- direct projection output은 direct observation이 아님을 별도 provenance

`action_target`은 영상에서 실제로 일어난 다음 도구 도착 정답이다.
완성 transcript의 `available_sec`가 도착보다 약간 늦다는 이유만으로
실제 전달을 삭제하지 않는다. 대신 latency report를 다음 두 층으로
분리해 같은 수치로 섞지 않는다.

- `visual_anticipatory`: 영상/Phase/tool pattern만으로 얼마나 먼저
  예측했는지; 모든 정상 action target에 적용 가능
- `voice_causal`: tool-identifying 최종 발화가 causally available한
  뒤의 예측·행동만 별도 집계

correction의 최초 오도구, completion 이후 cleanup, 관측되지 않은
handover는 두 층 모두에서 action target이 아니다. 이 구분은 원시
라벨을 중복 생성하지 않고 voice `available_sec`, action target, correction
mask로 evaluator가 파생한다.

Phase:

- `context_only_not_ground_truth`
- 모든 scoring metric false

### 10.3 Interval mask

최소 검토 대상:

- visual gap
- left/right-censored interval
- spoken cancellation/correction episode
- procedure-completion 이후 cleanup
- camera offscreen tail
- actor normalization 구간
- tool identity 또는 bundle ambiguity

correction 동안 open palm이 계속 보이는 경우 gesture presence/onset은
유지할 수 있지만 BT action/latency는 별도로 false로 mask할 수 있다.

음성 `available_sec` interval은 `voice_causal` 계산 경계이지
`visual_anticipatory` target을 자동으로 닫는 경계가 아니다. 이미 시각적으로
확정된 정상 `scrub_nurse → surgeon` 도착과 겹치는 경우 interval의
`action`/`latency`는 true로 두고, 음성 인과 지연은 `available_sec`에서
별도로 파생한다. Event role만 보고 target 수를 세지 말고 evaluator의
event-role × interval × cutoff precedence를 적용한 실효 target 수도
동일한지 검증한다.

### 10.4 Tool metric scope

각 canonical tool에 대해 다음을 기록한다.

- instance resolution 상태
- state/physical/reuse eligibility
- 필요 시 `mask_after_sec`
- 불가 사유

허용 instance resolution 예:

- `resolved`
- `unresolved_multiple_instances`
- `initial_inventory_unavailable`

initial inventory와 physical instance가 분리되지 않으면 다음을
채점하지 않는다.

- 실제 위치와 DT 위치 정합성
- 물리적 행동 가능성
- Mayo reuse/recover

이 metric을 false로 두는 것은 실패가 아니라 현재 reference의 정직한
적용 범위다.

### 10.5 Mask completeness

release gate에서 다음 집합의 모든 ID에 role이 있는지 검사한다.

- raw observed events
- DT reference events
- provisional Phase events
- voice context events

DT에 없는 raw-only event도 이후 state uncertainty를 만들 수 있으므로
role에서 누락하면 안 된다.

## 11. provenance와 필수 산출물

### 11.1 불변성과 version

- 원본 MCAP와 source metadata는 수정하지 않음
- candidate, review action, assistant adjudication은 append-only 또는
  create-only
- final observed/DT/Phase/report는 create-only
- 수정 시 `v2`, `v3`처럼 새 파일 발행
- manifest에 superseded reference와 사유·hash 보존

### 11.2 Assistant authority

Codex가 확정한 결과는 사람 판정이 아니다.

금지:

- 빈 human decision을 만들어 finalizer gate 통과
- Codex 행을 `human_video_review`로 기록
- 전체 reference를 human-confirmed라고 표현

필수:

- 실제 reviewer kind와 model ID
- 사용자 authorization
- 정확한 검수 시각
- frame 기반 notes
- source action 또는 candidate hash anchoring

현재 finalizer는 사람 review state의 `remaining_count==0`을 먼저
요구하고 assistant correction을 그 위에 overlay한다. 사용자가
직접 검토하지 않는 새 케이스에 이 구조를 그대로 적용하면 fake human
authority가 생길 수 있다.

새 파이프라인은 다음 중 하나를 지원해야 한다.

- neutral `review_actions`에 `reviewer_kind=ai_assistant`
- 별도 `assistant_annotation_decisions`를 base adjudication으로 사용

어느 방식이든 기존 human log와 명확히 분리한다.

### 11.3 케이스별 필수 파일

권장 파일 집합:

- `annotation_manifest.json`
- `cam4_frame_timeline.v1.json`
- `transcript_tool_anchors.policy02.v1.json`
- `marlin_full_scan_anchors.policy02.v1.json`
- raw Marlin evidence JSONL과 run report
- `interaction_candidates.ai_review.v2.jsonl`
- assistant/human review action audit log
- `interaction_events.observed.final.vN.jsonl`
- `interaction_events.dt_reference.final.vN.jsonl`
- `phase_events.provisional.final.vN.jsonl`
- `voice_events.source.v2.jsonl`
- `evaluation_masks.vN.json`
- `dt_projection.final.vN.json`
- `final_adjudication.vN.md`
- `information_boundary.final.vN.json`

파일명 version은 실제 파이프라인 버전에 맞추되 기존 파일을 덮어쓰지
않는다.

### 11.4 Manifest 필수 선언

- case ID와 source bag hash
- canonical timeline hash
- Marlin model ID/revision과 raw evidence hash
- observed/DT/Phase/voice/mask 파일·count·hash
- projection policy와 report hash
- 실제 authority와 label-origin count
- `complete` 상태
- Phase의 provisional/context-only 상태
- development/calibration 분류와 held-out false
- information-boundary report
- 이전 superseded reference
- runtime input 금지

legacy tool-event injection gate는 별도이며, minimal interaction final이
완료됐다는 이유로 자동으로 열지 않는다.

### 11.5 Projection report

필수 내용:

- 모든 input hash
- observed와 DT count
- event-type 및 label-origin count
- excluded roundtrip
- collapsed return
- unclosed direct return
- compound action episode
- output별 source mapping
- direct/non-direct projection provenance
- singleton continuity warning
- assistant correction/adjudication provenance
- output hash

## 12. fail-closed 완료 gate

다음 gate를 모두 통과해야 케이스를 final complete로 선언한다.

### 12.1 Source와 timeline

- source MCAP와 metadata hash 일치
- frame count와 timestamp array 길이 일치
- timestamps finite 및 strictly increasing
- 선언된 gap과 실제 discontinuity 일치
- visual end와 voice end를 분리

### 12.2 Marlin

- model revision 기록
- transcript/full-scan 모두 완료
- full-scan observable frame coverage 100%
- gap 내부 anchor는 skipped
- clip이 gap을 가로지르지 않음
- invalid span이 candidate로 승격되지 않음
- 모든 결과 proposal-only
- output/report path 서로 다름
- create-only publication

### 12.3 Codex adjudication

- 미검토 후보 0
- 3-pass 완료
- 후보 외 누락 탐색 완료
- every confirmed event에 source view와 frame 근거
- duplicate request/transfer 없음
- confirmed assistant event의 authority가 실제와 일치
- `reviewed_at`은 timezone이 있는 실제 검수 시각이며 미래 시각이 아님
- ambiguous/rejected 행은 final confirmed reference에 없음

### 12.4 Event schema와 timing

- event ID 유일
- canonical tool ID
- allowed location만 사용
- `from != to`
- point `time_sec`가 timeline frame과 exact match
- interval start/end가 frame과 exact match
- start ≤ end
- interval이 gap을 가로지르지 않음
- request에 tool/from/to 없음
- transfer에 tool/from/to 존재

### 12.5 Physical consistency

- transfer 전후 control/release 근거
- placement가 first contact가 아니라 first release
- same-chain pair가 명시적으로 영상 확인됨
- time+type만으로 자동 collapse하지 않음
- duplicate instance warning 검토
- unclosed transition이 숨겨지지 않음
- 물리 instance 미확정 metric은 not_scorable

### 12.6 DT projection

- raw observed 불변
- 모든 raw event에 operation mapping
- 모든 DT output에 source mapping
- 모든 collapse에 non-direct provenance
- compound substep 이중 채점 없음
- cleanup이 action target에 없음
- unclosed direct return이 DT target에 없음

### 12.7 Voice와 Phase

- 모든 transcript record 추출
- `available_sec >= end_sec`
- causal replay smoke test
- source voice는 context-only
- Phase는 Marlin 미사용
- Phase는 provisional/ambiguous/context-only
- Phase accuracy false
- frame-0 Phase는 left-censored initial state
- gap 내부 Phase transition 없음

### 12.8 Mask와 evaluator

- mask schema 통과
- default deny
- 모든 event/Phase/voice ID role coverage
- cutoffs와 interval mask 유효
- evaluator가 manifest의 최신 DT reference를 선택
- evaluator가 mask와 tool catalog hash 검증
- action target count가 mask와 일치
- raw action target과 interval/cutoff precedence 이후 실효 action·latency
  target이 일치
- physical/state/reuse 범위가 report와 일치
- reference 0건 run을 성능 점수로 보고하지 않음

### 12.9 Cross-layer 산술 reconciliation

최종 발행 전에는 사람이 세어 본 인상 대신 canonical 파일을 다시 읽어
다음 산술을 자동 검증한다.

- `observed = confirmed request interval + raw observed transfer`
- `DT = confirmed request interval + explicit projection 결과 transfer`
- observed와 DT의 차이는 projection operation의 exclude/collapse 순감소와
  정확히 일치
- gesture target 수는 gesture mask가 열린 request interval 수와 일치
- raw action target과 interval/cutoff 적용 후 effective action·latency
  target 수가 정책상 의도한 값과 일치
- voice `handover_target` 수와 action-arrival link 수는 각각 집계하되
  일대일이라고 가정하지 않음
- 여러 request interval이 한 arrival로 이어지거나 한 utterance가 여러
  arrival를 가리킬 수 있음을 허용
- report, manifest, observed, DT, Phase, voice, mask의 count와 SHA-256이
  모두 일치

이 gate는 f501 같은 scrub-only 정리가 실수로 transfer가 되거나, 두 단계
return collapse에서 raw 한 건만 지워지는 문제, 동일한 eventual handover를
위해 transfer를 중복 생성하는 문제를 잡아야 한다.

### 12.10 Artifact와 GUI

- manifest의 모든 hash 일치
- report count와 실제 JSONL count 일치
- observed final mode와 observed file 일치
- final-DT mode와 DT file 일치
- Phase와 voice context track 표시
- request interval과 transfer point 위치 일치
- video overlay의 Phase, voice, request, transfer 표시 위치와 내용 검증
- adjudication, explicit projection, evaluation mask, reconciliation을
  manifest hash로 결박하고 감사 시작·종료 시 다시 확인
- versioned 산출물 전체를 create-only batch로 먼저 발행하고 검증한 뒤
  canonical manifest를 동일 디렉터리 원자 교체로 전환
- canonical 전환 실패 시 이전 manifest를 원자 복구하고 새 batch를 롤백

전 케이스 manifest가 발행된 뒤 다음 read-only batch audit를 실행한다.

```bash
PYTHONPATH=. python3 -m \
  tools.real_surgery_annotation.audit_policy02_final_batch \
  --output \
  annotations/observable_tool_events/reports/policy02_final_batch_audit.v1.json
```

이 검사는 manifest hash와 `FinalReviewBundle`, 최소 event type, 허용 DT
edge, scrub-only roundtrip 배제, compound Mayo pickup closure,
Phase/voice context-only, default-deny mask, 후보·voice 전수 검수,
물리 continuity 및 report count를 함께 fail-closed로 확인한다.

### 12.11 실패 시 동작

- 기존 final을 덮어쓰지 않음
- 새 final version을 발행하지 않음
- 실패한 gate와 관련 ID를 report
- 관측 불가 문제는 ambiguous/mask로 닫음
- 정책 또는 코드 문제는 수정 후 새 version으로 재생성

## 13. 정보경계

### 13.1 허용되는 runtime 입력

strict runtime이 받을 수 있는 정보:

- 공개 camera stream
- 해당 시각에 causally available한 transcript
- 미리 동결된 case-agnostic procedure specification
- production configuration과 공개 tool catalog

### 13.2 금지되는 runtime 입력

- observed final JSONL
- DT reference JSONL
- Phase timestamp reference
- evaluation mask
- projection report
- assistant/human review log
- Marlin raw evidence와 후보
- per-case start phase bootstrap
- 다음 도구 정답
- expected BT action
- hidden actor intent

평가 reference는 replay 후 offline evaluator만 읽는다. VLM, reducer, DT
reducer, BT, skill layer는 reference를 구독하거나 file path로 읽지
않는다.

### 13.3 Procedure specification

case-agnostic procedure specification은 runtime에 사용할 수 있지만 다음
조건을 충족해야 한다.

- per-case timestamp 없음
- observed-in-case flag 없음
- review decision 없음
- ground-truth event ID 없음
- ontology version 고정

현재 사례에서 procedure prompt를 개발했다면 해당 사례는
development/calibration이며 held-out이 아니다.

### 13.4 정보경계 검사 보강

현재 정보경계 도구가 `/evaluation/ground_truth` 문자열만 찾는다면 flat
evaluation file의 직접 참조를 놓칠 수 있다. 다음 참조도 runtime roots,
config, launch, production UI에서 금지하고 검사한다.

- `annotations/observable_tool_events/cases`
- `interaction_events.observed.final`
- `interaction_events.dt_reference.final`
- `phase_events.provisional.final`
- `evaluation_masks`
- projection report 경로
- manifest의 evaluation-only 파일 hash 또는 경로

검사 대상 runtime roots에는 최소한 다음을 포함한다.

- `src`
- `config`
- `docker`
- production `webapp`
- launch 및 deployment 파일

검사 결과는 create-only report로 발행하고 violation 0건일 때만 release
gate를 통과한다.

### 13.5 최종 원칙

평가용 reference가 정교해질수록 정보경계는 더 엄격해야 한다. 좋은
정답은 런타임 성능을 돕는 입력이 아니라, 런타임이 독립적으로 낸
결정을 사후에 정확히 측정하는 기준이다.
