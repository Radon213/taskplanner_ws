# 수술보조 Taskplanner 인수인계 문서

기준 버전: `0.1.0` 이후 현재 개발 상태
최종 갱신: 2026-08-12 KST

## 1. 목적

이 문서는 `taskplanner_ws`를 이어서 개발할 사람 또는 에이전트가 현재
시스템의 설계 의도와 구현 상태를 빠르게 파악하도록 작성한 인수인계
문서이다.

현재 프로젝트는 단순 phase classifier가 아니다. 목표는 수술 진행 중
외부로 드러나는 신호를 바탕으로 다음 도구 전달, 예측 대기, Mayo 회수,
정리 행동을 보수적으로 결정하는 것이다.

## 2. 핵심 설계 철학

1. 기본 정책은 fail-closed이다. 검증되지 않은 입력, stale context,
   unhealthy VLM, 물리적으로 불가능한 상태에서는 로봇 행동을 만들지
   않는다.
2. 핵심 시스템은 VLM + OR digital twin + BT이다.
3. LLM 집도의는 검증용 actor이다. 실제 perception truth가 아니며,
   VLM에 hidden actor state를 넘겨서는 안 된다.
4. VLM은 실제 환경에서 관측 가능한 정보만 받아야 한다.
5. 도구 회수의 정상 경로는 집도의 손 직접 회수가 아니라 Mayo stand를
   거치는 회수이다.
6. 수술 지식은 코드가 아니라 procedure YAML에 둔다.
7. 출혈/지혈은 일반 phase 순서가 아니라 interrupt event로 다룬다.
8. BT는 raw VLM이 아니라 digital twin이 정리한 world state를 보고
   행동한다.

## 3. 현재 런타임 구성

기본 실행:

```bash
docker compose up taskplanner-runtime webapp
```

기본값:

- VLM: `real`
- VLM provider: `vllm`
- VLM endpoint: `http://127.0.0.1:8001`
- VLM model: `unsloth/gemma-4-E4B-it-NVFP4`
- LLM surgeon actor: `llm`
- Actor model: `google/gemma-4-12b-qat`
- VLM input image: `no_image_camera`
- Dashboard: `http://127.0.0.1:4173`

현재 no-image camera는 실제 수술 영상이 불투명한 상황에서 VLM 입력 파이프라인을
검증하기 위한 대체 입력이다. 이 화면은 카메라를 대체하는 것이므로 정답을
누설하면 안 된다.

## 4. VLM 입력 원칙

VLM에 줄 수 있는 정보:

- synthetic/public field image
- 집도의 손 내밈 여부
- Mayo stand 위에 실제로 보이는 도구 이름
- 최근 공개 음성 발화
- 공개된 도구 전달/회수/세척 이벤트
- BT decision context
- digital twin의 public event/state 요약

VLM에 주면 안 되는 정보:

- LLM actor 내부 phase ground truth
- LLM actor 내부 next planned tool
- YAML에서 이미 알고 있는 interrupt event tool 정답
- reducer가 아직 확정하지 않은 hidden lifecycle 판단
- 회수/reuse 정답 라벨

즉, actor가 테스트를 위해 알고 있는 내부 정보와 VLM이 실제 환경에서 볼 수
있는 정보는 반드시 분리해야 한다.

## 5. Procedure YAML

현재 수술별 지식은 다음 단일 파일 형식으로 관리한다.

```text
src/procedure_spec/procedure_spec/specs/<procedure>/vlm_procedure_prompt.yaml
```

현재 포함된 수술:

- `thyroidectomy`
- `nephrectomy`
- `inguinal_hernia_repair`

각 YAML은 다음을 포함한다.

- procedure id/name
- normal phase labels
- interrupt phase labels
- phase Korean labels
- tool ids/names
- normal phase transition cues
- interrupt enter/exit cues
- phase별 visual cues
- phase별 expected tool sequence

새 수술을 추가할 때는 새 디렉터리에 `vlm_procedure_prompt.yaml`을 만들고
`display_catalog.yaml`에 등록한다. 정상적인 경우 BT 코드를 수정하지 않아야
한다.

## 6. LLM 집도의 Actor

LLM 집도의는 테스트용 정보 원천이다.

해야 하는 일:

- 수술을 스스로 진행한다.
- 적절한 시점에 도구를 요청한다.
- 음성, 손 내밈, 음성+손 내밈을 섞는다.
- 사용한 도구를 Mayo stand에 내려놓는다.
- 작은 대화와 field interrupt를 자연스럽게 발생시킨다.
- procedure YAML이 달라져도 같은 원칙으로 동작한다.

하지 말아야 하는 일:

- `/twin/world_state`를 보고 자신의 수술 진행을 맞추지 않는다.
- 숨겨진 다음 도구 계획을 VLM에 제공하지 않는다.
- 특정 thyroidectomy 시나리오에만 맞춘 하드코딩을 하지 않는다.

외부 피드백은 robot skill completion만 사용한다. 이는 타이밍 조절을 위한
것이고, humanoid action은 현재 mock server에서 항상 성공하는 것으로 둔다.

## 7. Mayo Stand Recovery

정상 회수 경로:

```text
surgeon hand -> Mayo stand -> robot left hand -> cleaner -> rack
```

내부 lifecycle:

- `mayo_reuse`: Mayo 위에 있지만 아직 회수 확정 전
- `mayo_recovery`: VLM/reducer가 회수 대상으로 승격
- `recovering_left`: robot left hand가 Mayo에서 집는 중
- `cleaning_left`: cleaner 처리 중
- `returned_home`: rack 복귀 완료

VLM 출력에서 `mayo_retrieve`가 같은 도구에 대해 안정적으로 유지될 때만
reducer가 회수를 승격한다. 정상 BT recovery branch는 `retrieve_from_mayo`만
사용한다.

`retrieve_from_hand`는 legacy/manual-only이다.

메이요 위 도구가 다시 필요해진 경우에는 별도 회수/재사용 구역을 옮기는
것이 아니라, 집도의의 명시적 음성 요청 또는 암묵적 손 내밈 요청을
그대로 처리한다.

```text
Mayo stand -> robot right hand -> surgeon receive zone
```

이때 BT는 `pick_up_from_mayo_and_handover`를 발행한다. GUI는 내부
`mayo_reuse`/`mayo_recovery` lifecycle을 두 칸으로 나누지 않고 하나의
Mayo stand에 합쳐 표시하며, 각 도구 태그에는 VLM의 최신 판단을
`재사용 확률 NN%`로 표시한다.

## 8. BT 의사결정

BT의 우선순위는 대략 다음과 같다.

```text
safety / invariant guard
-> cleanup / recovery
-> explicit request
-> anticipatory handover
-> idle / hold
```

중요 guard:

- 도구가 현재 bundle에 속해야 한다.
- 도구가 active여야 한다.
- 이미 다른 holder에 있는 도구를 중복 선택하지 않는다.
- robot right hand에는 하나의 handover/preposition tool만 허용한다.
- robot left hand에는 하나의 recovery/cleaning tool만 허용한다.
- contaminated tool은 rack으로 직접 복귀하지 않는다.
- procedure completion 중 오른손에 예측 대기 도구가 남아 있으면
  `return_unused_preposition`으로 정리한다.

### Bed-mounted retraction arm

- bed-mounted robot-arm 연동은 retraction 역할만 대상으로 한다.
- 갑상샘 절제술 Tool Change는 완료 대기형
  `/surgery/tool_change/request` Service를 사용한다.
- 신장 절제술 Malleable 미세 조정은 취소 가능한
  `/surgery/retraction/adjust` Action을 사용한다.
- 로봇팔 상태는 `/external/bed_robot_arms/status`의 문서화된 필드만
  수용한다. 자세, 궤적, 힘, 충돌, 상세 안전 상태는 제어기 소유다.
- bed-mounted suction arm 제어·상태 경로는 없다. 다만 임상 석션 도구와
  집도의의 석션 발화는 일반 도구/공개 증거로 계속 처리한다.

## 9. Dashboard

대시보드는 operator/debug view이다.

주요 기능:

- procedure 선택
- 시작 phase 선택
- VLM 모델 선택
- LLM 집도의 on/off 및 모델 선택
- VLM 입력 이미지 preview
- digital twin scene rendering
- Mayo stand 상태 표시
- interrupt event popup
- BT/VLM/reducer 판단 layer
- phase/tool 정답률 표시

점수 표기는 다음 의미다.

```text
correct / proposed / evaluable
```

- `correct`: 맞힌 수
- `proposed`: 실제로 제안한 수
- `evaluable`: 평가 가능한 전체 샘플 수

## 10. 검증 명령

ROS build:

```bash
source /opt/ros/jazzy/setup.bash
source /opt/btops_ws/install/setup.bash
colcon build --symlink-install --cmake-args -DBUILD_TESTING=OFF
source install/setup.bash
```

Web build:

```bash
cd webapp
npm run build
```

Core probes:

```bash
ros2 run bringup taskplanner_edge_probe
ros2 run bringup taskplanner_multi_bundle_runtime_probe --duration-sec 60
ros2 run bringup taskplanner_smoke_test --spec-name thyroidectomy
ros2 run bringup taskplanner_smoke_test --spec-name nephrectomy
ros2 run bringup taskplanner_smoke_test --spec-name inguinal_hernia_repair
```

## 11. 다음 개발 시 주의점

1. 실제 영상 입력이 들어오면 no-image camera를 단순히 제거하지 말고 같은
   topic/preview/VLM 소비 경로가 유지되는지 확인한다.
2. VLM 정확도 개선은 특정 actor trajectory에 과적합하면 안 된다.
3. actor 내부 ground truth는 평가에는 써도 VLM 입력에는 쓰지 않는다.
4. procedure YAML을 바꿔도 actor, dashboard, BT가 일반적으로 동작해야 한다.
5. 휴머노이드 도구 전달은 내부 `/skill/execute` 경계를 유지하되 외부
   retraction arm은 `docs/EXTERNAL_INPUT_CONTRACT.md`의 Service/Action/Topic만
   사용한다.
6. 외부 retraction 계약에 문서에 없는 자세·진행률·세부 상태를 추가하지
   않는다.
7. mock skill server는 회귀 테스트용으로 계속 보존한다.
