# 자연어 음성 명령 계약

## 상태

이 문서는 Taskplanner의 **규범적(normative) 음성 명령 계약**이다. 구현,
프롬프트, procedure bundle, launch 설정을 바꿀 때 이 문서의 불변식을 깨면
안 된다. 예외가 필요하면 위험 분석, shadow replay, 회귀 시험과 함께 이
문서와 관련 테스트를 같은 변경에서 갱신한다.

## 제품 철학: 자연어 입력, 폐쇄된 실행 의미

집도의는 짧고 자연스러운 한국어로 말할 수 있다. 예를 들어 `보비 줘`,
`보비 내놔`, `보비 빨리`, `교시 시작`, `자 이제 교시를 시작해보자`는
표현이 달라도 같은 의도로 해석할 수 있어야 한다. 표준 발화는 지연과
오인을 줄이는 권장 표현이지, 자연어 이해를 포기하기 위한 입력 문법이
아니다.

반대로 로봇 실행으로 가는 의미는 제한된 typed contract여야 한다. LLM/VLM은
자연어와 ASR 오류를 해석해 **제안(proposal)** 할 수 있지만 ROS Action,
Service, 속도, 힘, 좌표를 직접 만들거나 실행할 권한은 없다.

## 변경 불가 불변식

1. `/surgery/audio/request_text`는 최종 STT 증거이며 명령 자체가 아니다.
   명령으로 변환하는 유일한 정상 경로는 `voice_command_resolver`이다.
2. Resolver의 출력은 `/surgery/voice/intent`
   (`surgical_msgs/msg/VoiceCommandIntent`)뿐이다. 이 메시지는 의미 제안이며
   ROS Action/Service payload나 물리 완료 주장으로 사용하면 안 된다.
3. Digital Twin과 BT/controller guard가 상태, inventory, 권한, 중복, 위험도와
   endpoint 계약을 검증한 뒤에만 실행을 결정한다. `request_accepted`는
   admission일 뿐 물리 동작 완료가 아니다.
4. 상태와 시각 문맥은 후보를 **제한**할 수는 있어도, 발화에 없는 필수 슬롯
   (도구, 측면, 거리, 방향, 부호)를 채워 실행하면 안 된다.
5. `도구 줘`처럼 필수 도구가 없으면 `clarify`다. VLM의 다음 도구 예측이나
   tray 이미지가 있어도 자동 전달로 승격하지 않는다. `빨리`는 우선순위
   힌트일 뿐 물리 속도, 힘, 거리 제한을 바꾸지 않는다.
6. 부정, 질문, 일반 대화, 다중/상충 명령, 지원되지 않는 물리 의미는
   `no_command`, `clarify`, 또는 `reject`로 끝난다. 모호함을 임의 실행으로
   해소하지 않는다.
7. 모델이 만든 JSON/함수 호출은 schema-valid일 수 있어도 의미적으로
   정답이라는 증거가 아니다. `strict` schema 또는 candidate ID는 출력 형식을
   고정하는 수단이며, local grounding과 validator를 대체하지 않는다.
8. 입력을 두 명령 소비자가 별도로 해석하면 안 된다. handover와 retraction은
   같은 typed intent를 각각 자기 namespace에서만 소비한다. 이렇게 해야 한
   발화가 두 번 실행되거나 `아미`가 도구 전달/견인 도구 교체로 충돌하지 않는다.
9. tool handover proposal은 resolver가 사용한 `procedure_id`와 `catalog_id`를
   포함하고, Digital Twin은 활성 bundle의 값과 일치할 때만 수용한다. bundle이
   바뀌었거나 catalog를 읽지 못했으면 명령을 추측하지 않고 거절한다.
10. 기존 raw-text parser는 migration/replay용 명시적 compatibility switch가
    있을 때만 켤 수 있다. 기본 launch에서 그 switch는 모두 `false`이며, 새
    기능이 raw String을 바로 실행 경로로 구독하는 것은 금지한다.

## 경로와 책임

```text
final STT text
  -> speech_input_adapter
  -> /surgery/audio/request_text
  -> voice_command_resolver
  -> /surgery/voice/intent (proposal only)
  -> Digital Twin handover guard | BT retraction guard
  -> reviewed Action/Service admission
  -> controller-owned execution and completion feedback
```

현재 normal path에서 구현·검증된 intent family는 `tool_handover`와 direct-teach
lifecycle의 `retractor_command`다. 리트랙터의 방향·거리·부호 조절처럼 controller
의미를 더 표현해야 하는 family는 해당 slots와 endpoint 계약, shadow replay를 먼저
추가하기 전에는 `reject`한다. 이 범위를 넓힐 때 raw parser를 되살리는 것은 허용되지
않는다.

Resolver는 procedure별 tool catalog, 허용 intent, transcript의 명시 근거와
관측된 ASR 혼동 후보만 사용한다. 현재 inventory/phase/endpoint 상태는 뒤의
Digital Twin·BT guard가 검증한다. 즉 이 상태들은 후보를 좁히는 근거일 수는
있어도 resolver가 생략된 도구나 물리 슬롯을 발명하는 입력이 아니다. native
function calling을 지원하는 모델에서는 strict proposal tool을, 지원하지 않는
runtime에서는 같은 schema의 candidate-ID JSON selector를 사용한다. 둘 중 어느
경우도 모델이 직접 실행 함수를 호출하지 않는다.

Resolver의 `procedure_bundle`은 시작 시 `procedure_spec`에서 requestable tool
별칭을 읽고, 중복 별칭은 제거한 뒤 catalog fingerprint를 만든다. 런타임 bundle
교체로 resolver와 Twin의 fingerprint가 달라지면 handover는 fail-closed다. 두
노드를 같은 bundle로 재설정하거나 재시작한 뒤 shadow replay로 검증해야 한다.
선택적 VLM 자연어 variant는 모델 응답 실패 시 절대 결정론 실행으로 폴백하지
않으며, 현재 confirmation/ack flow가 구현되기 전에는 `requires_confirmation`으로
관찰만 된다.

짧고 완전히 grounded된 발화는 selector/VLM 왕복을 거치지 않는 local fast path로
처리한다. 따라서 `보비 줘`, `교시 시작` 같은 canonical 자연 발화의 지연을 모델
호출 때문에 늘리지 않는다. 모델 selector가 필요한 변형은 별도 opt-in이며, timeout은
실행이 아니라 `reject`다.

VLM 영상은 `저것`, `왼쪽 tray의 것`처럼 지시 대상 해석을 보조할 수 있지만,
텍스트에 없는 critical slot을 자동 실행으로 채우는 근거는 아니다. 텍스트만으로
충분한 명령에는 영상 입력을 생략해 지연과 불필요한 불확실성을 줄인다.

## intent 처리 원칙

| 발화 | 의미 제안 | 정책 |
| --- | --- | --- |
| `보비 줘`, `보비 주세요`, `보비 내놔` | `tool_handover(T04)` | inventory와 procedure guard 뒤 실행 가능 |
| `보비 빨리` | `tool_handover(T04, urgency=urgent)` | urgency는 audit/scheduling hint일 뿐 현재 물리 제어값을 바꾸지 않음 |
| `도구 줘` | `clarify(tool_id)` | 짧게 도구명을 재질문 |
| `교시 시작`, `자 이제 교시를 시작해보자` | `retractor_command(start_direct_teach)` | 허용 lifecycle 상태에서만 진행 |
| `교시 시작하지 마`, `교시 시작할까?` | `no_command` / `clarify` | 부정·질문은 실행하지 않음 |
| `오른쪽 5 cm 덜 당겨` | `reject` / `clarify` | endpoint가 polarity/vector를 표현할 때까지 추정 금지 |

`직접`은 direct-teach intent의 선택적 자연어 별칭이다. 반면 도구명, 좌/우,
거리, 방향과 부호처럼 controller 의미를 바꾸는 슬롯은 명시적으로 grounding
되어야 한다.

## 확인과 재발화

`requires_confirmation=true`인 proposal은 Action/Service로 전달하지 않는다.
사용자에게 필요한 한 가지 slot 또는 제안을 짧게 확인한 뒤, 해당 pending request에
연결된 별도 최종 intent만 실행 경로로 보낼 수 있다. 이 정책은 낮은 confidence
자체가 아니라 missing/repair/context-completed/high-risk slot에 적용한다.

## 검증과 변경 규칙

음성 명령 변경에는 최소한 다음 회귀 시험을 추가한다.

- 같은 의도의 존대·반말·filler·생략 표현
- 실제/예상 ASR 혼동 및 repair provenance
- 부정, 질문, 배경 대화, 발화 끝의 명령어, 중복
- 누락 critical slot의 clarify와 context-completion 금지
- wrong-tool / out-of-state / duplicate가 Action·Service로 도달하지 않는지
- proposal, admission, physical completion을 분리한 trace

운영 배포는 offline replay -> shadow publication -> record-only/canary 순서로
진행한다. 실제 controller·하드웨어에서의 물리 명령은 별도 권한과 검증 없이는
이 문서의 범위에 포함되지 않는다.
