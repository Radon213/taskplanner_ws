# 음성 명령 계약: legacy migration reference

> **Legacy-only reference.** 이 문서는 이전
> `/surgery/audio/request_text → voice_command_resolver → /surgery/voice/intent`
> proposal 경로를 사용하는 오래된 rosbag·replay를 식별하기 위한 것이다. 그 경로는
> 현재 런타임의 명령 경로가 아니며, 새 기능이나 배포 설정에 복사하면 안 된다.

현재의 규범은 [`taskplanner_principles.toml`](../taskplanner_principles.toml)과
[`VOICE_COMMAND_MODULARIZATION.md`](VOICE_COMMAND_MODULARIZATION.md)다.

## 현재 명령 경로

```text
typed final ASR
  -> speech_input_adapter
  -> /surgery/audio/admitted_utterance
  -> CommandRouter
  -> catalog-selected typed Topic / Service / Action adapter
  -> endpoint server

CommandRouter
  -> /surgery/audio/observed_utterance (read-only observers only)
  -> VLM, UI, logs, TTS presentation
```

`speech_input_adapter`가 final-text, source, freshness, TTS echo, 그리고
`utterance_id` 중복을 한 번만 검사한다. `CommandRouter`만 admitted topic을
소비하고 catalog를 통해 실행을 요청한다. VLM, Digital Twin, BT, UI, logger는
관찰자이거나 별도 producer이며 명령을 재-admit하거나 재-dispatch하지 않는다.

새 명령은 기존 ROS type이면 catalog record를 고쳐 router를 reload하면 된다. 새
custom ROS IDL이면 interface package와 직접 consumer만 build한다. controller와
endpoint server는 type/range 검증, idempotency, E-stop·물리 limit, Action
feedback/result/cancel을 계속 소유한다.

## legacy 데이터의 취급

이전 `request_text` 또는 `VoiceCommandIntent`가 들어 있는 기록은 historical
evidence로만 읽는다. 이를 새 live graph의 topic, launch argument, catalog
binding, 또는 실행 입력으로 되살리지 않는다. 현재 경로에서 관찰이 필요하면
`/surgery/audio/observed_utterance`를 사용한다.

이 문서는 live ROS, controller, 또는 physical execution을 검증했다는 주장이
아니다.
