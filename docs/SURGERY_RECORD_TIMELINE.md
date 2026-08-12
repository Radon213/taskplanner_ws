# 리플레이 기반 수술기록 생성 AI 입력 타임라인

## 목적

이 기능은 완성된 임상 수술기록지를 직접 작성하지 않는다. 검증이 끝난 Shadow
Replay의 전체 trace를 수술 종료 후 후처리하여, 별도의 수술기록 생성 AI가 초안을
만드는 데 사용할 `surgery_record_input.txt`를 생성한다.

실행 기준은 릴리스 검증을 통과한 Shadow Replay 흐름과 같다.

- 시작: `scripts/taskplanner up replay --no-build`
- Replay: `tools.real_surgery_annotation.run_shadow_replay`
- 모드: strict + interactive `elastic_demo`
- VLM: NInfer `http://127.0.0.1:8080`, `qwen3.6-35b-a3b`
- 집도의 음성: source bag의 `/surgery/transcript`; 집도의 LLM은 사용하지 않음

## TXT에 수록하는 근거

모든 항목은 replay source time을 기준으로 하나의 시간순 로그에 배치한다.

1. **집도의 원본 발화**
   - `/surgery/transcript`만 사용한다.
   - 이 발화를 재발행한 `/surgery/audio/request_text`는 중복이므로 제외한다.
   - transcript JSON 안의 `start_sec`를 타임스탬프로 사용한다.
2. **VLM 임상 관찰**
   - `/vlm/result`의 `vlm_raw` layer만 사용한다.
   - `schema_version == "4"`인 `summary`만 수록한다.
   - `T04`, `P03` 같은 내부 코드는 procedure spec의 영어 수술도구명과 영어 수술 단계명으로 치환한다.
   - 이 영어 정규화는 VLM 관찰 문장에만 적용하며 집도의 한국어 원본 발화는 변경하지 않는다.
   - schema v4 wire key `sum`은 ROS 메시지에서 `VLMResult.summary`가 된다.
   - 완전히 같은 문장이 30초 이내 반복될 때만 보수적으로 중복 제거한다.
3. **Phase 상태 변화**
   - `reducer_fused.filtered_phase`가 바뀔 때만 구조적 북마크로 수록한다.
   - `P03` 같은 내부 Phase ID는 표시하지 않고 단계명 간 전환으로 표현한다.
   - `시스템 추정·의료진 미확정`으로 표시하며 임상 확정 사실로 쓰지 않는다.
4. **중요 보조 시스템 이벤트**
   - 도구 준비·파지·전달·반환은 `스크럽 널스`의 행동으로 표현한다.
   - Retraction Tool Change·미세 조정 명령/결과는 `어시스턴트`의 행동으로 표현한다.
   - 임상 석션 도구 요청·사용은 도구/발화 사건으로 표현하며, bed-mounted
     suction robot-arm 또는 흡입 장치 제어 사건으로 만들지 않는다.
   - 로봇 손·수신 구역·트레이 슬롯 같은 내부 위치 코드는 사람 중심 위치명으로 치환한다.
   - 모든 항목을 `Shadow 가상 실행(물리 실행 아님)`으로 표시한다.

## 의도적으로 제외하는 정보

- VLM `raw_json`
- VLM Phase 후보와 confidence
- VLM 도구/위치 후보와 confidence
- gesture, intent, uncertainty
- `vlm_model_raw`
- 평가용 `evaluation_ground_truth` 및 annotation reference
- 반복되는 reducer 전체 상태와 로봇 running/health heartbeat

이 경계는 VLM의 행동 계획용 구조화 필드나 평가 정답이 수술기록 생성 AI에게
임상 사실처럼 전달되는 것을 막는다.

## 산출 시점과 위치

`run_shadow_replay.py`가 trace 계약과 정보 경계를 검증하고 offline evaluation을
완료하여 manifest가 `complete`가 된 뒤 TXT renderer를 실행한다. 실패하거나
중단된 replay에는 수술기록 입력 TXT를 생성하지 않는다.

정상 완료 시 다음 파일이 같은 run 디렉터리에 생긴다.

```text
output/shadow_runs/<run-id>/surgery_record_input.txt
```

`run_manifest.json`의 `artifacts.surgery_record_input`에도 경로와 SHA-256이
기록된다.

TXT 본문은 읽기 쉽게 `집도의`, `VLM`, `Phase`, `스크럽 널스`, `어시스턴트`
구분만 표시한다. 사실성 경계와 세부 provenance는 TXT에 반복하지 않고
`run_manifest.json`, trace 및 평가 산출물에 보존한다.

## 준비 점검과 추후 실행

다음 명령은 dataset, procedure prompt, exporter, NInfer 모델의 실제 load 및
vision capability만 읽기 전용으로 점검한다. Replay는 시작하지 않는다.

```bash
scripts/run_thyroidectomy_surgery_record.sh --check
```

실제 전체 cycle을 수행할 때만 다음 명령을 사용한다.

```bash
scripts/run_thyroidectomy_surgery_record.sh --execute
```

`--execute`는 기본 case `0704_6`, ROS domain `193`, rosbridge port `9293`,
VLM timeout/drain `130초` 설정을 사용한다. 데이터셋 위치와 case는
`SHADOW_DATASET_ROOT`, `SHADOW_CASE_ID` 환경변수로 덮어쓸 수 있다.

## 해석 제한

이 파일은 수술기록 초안의 근거 입력이다. VLM 관찰과 시스템 Phase는 의료진이
확인한 사실이 아니며, Shadow counterfactual 이벤트는 실제 물리 실행이 아니다.
생성 AI의 결과는 반드시 집도의 검토를 거쳐야 한다.
