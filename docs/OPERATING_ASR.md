# 운영 USB 마이크 ASR

`taskplanner-asr`는 Taskplanner의 **live 프로파일에서만** 실행되는 운영 음성
입력 sidecar다. 시나리오를 생성하는 LLM surgeon이 아니며,
`llm-surgeon`, `replay`, standalone `debug` 프로파일에는 포함되지 않는다.
운영/Debug 화면은 계속 하나의 Web UI(`http://127.0.0.1:4173`)를 사용한다.

## 실행 수명주기

정상 실행은 Compose를 직접 호출하지 않고 launcher를 사용한다.

```bash
scripts/taskplanner up live --build  # 새 checkout/인터페이스 변경 후 최초 1회
scripts/taskplanner up live          # install overlay가 최신일 때
```

launcher는 다음 순서를 보장한다.

1. 이전 `taskplanner-runtime`, `taskplanner-asr`, perception, 통합 Debug
   sidecar를 정지하고 제거한다.
2. 공용 모델 제어면과 Web UI를 준비한다.
3. `taskplanner-asr`를 시작하고 `/input/asr/runtime_status`에
   `/taskplanner_asr` publisher가 나타날 때까지 기다린다.
4. 그 뒤 `taskplanner-runtime`을 시작한다.
5. 마지막으로 같은 4173 UI에서 사용할 통합 Debug sidecar를 시작한다.

ASR 프로세스가 올라왔다는 것만으로 수술 시나리오가 준비된 것은 아니다.
운영 ASR은 Puzzle AI 전송이 실제로 연결된 세션에서만 최종 문장 publisher를
유지하며, live 런타임의 `/integration/readiness`는
`/sensors/surgeon/sentence` publisher가 없으면 fail-closed 상태를 유지한다.
같이 실행되는 통합 Debug sidecar의 `asr_start`는 명시적으로 거부된다. Debug
ASR이 이 publisher 조건을 대신 만족한 뒤 시나리오 시작과 함께 중지되는 경로를
차단하기 위한 것으로, 운영 화면의 **수술실 음성 입력**에서 장치를 새로고침하고
ASR을 시작해야 한다. Debug의 장치 새로고침과 중지는 계속 사용할 수 있다.

`scripts/taskplanner up llm-surgeon`은 `taskplanner-asr`를 시작하지 않는다.
다른 모드로 전환하거나 `scripts/taskplanner down`을 실행하면 이전 운영 ASR도
함께 정지·제거된다.

## ROS 인터페이스

| 방향 | 이름 | 타입 | 의미 |
|---|---|---|---|
| ASR → ROS | `/sensors/surgeon/sentence` | `std_msgs/msg/String` | 서버가 확정한 비어 있지 않은 최종 문장만 발행 |
| ASR → ROS | `/input/asr/runtime_status` | `std_msgs/msg/String` | `taskplanner.asr.status.v1` JSON 상태, transient-local |
| UI → ASR | `/input/asr/control` | `surgical_msgs/srv/AsrControl` | 장치 새로고침과 캡처 시작·정지 |

부분 인식, 토큰 스트림, 빈 문장은 ROS 문장 입력으로 발행하지 않는다. 상태
publisher healthcheck와 최종 문장 publisher readiness는 서로 다른 검증이다.

## 컨테이너 경계

sidecar는 `taskplanner-runtime`과 같은 `taskplanner-ws:dev` 이미지, host
network, IPC, `ROS_DOMAIN_ID`, discovery 범위, Cyclone DDS RMW/profile을 쓴다.
호스트 사용자와 동일한 `TASKPLANNER_UID:TASKPLANNER_GID`로 실행하고 다음만
추가로 공유한다.

- 소스 트리: `/workspaces/taskplanner_ws`
- 실행 산출물: `${TASKPLANNER_RUN_ROOT}` → `/taskplanner-runs`
- Ubuntu의 현재 PipeWire 소켓 하나 →
  `/run/taskplanner-pipewire/pipewire-0`
- 컨테이너 `XDG_RUNTIME_DIR=/run/taskplanner-pipewire`

마이크는 Ubuntu 설정에서 선택한 논리적 기본 입력을 따른다. raw ALSA 장치
목록을 별도 운영 선택지로 노출하지 않는다. 운영 기본값은 캡처 PCM·전사
artifact를 저장하지 않으며 상태와 확정 문장만 ROS로 전달한다.

운영과 standalone Debug ASR은 `/taskplanner-runs/asr/microphone.lock`을
공유한다. 한 쪽이 캡처 중이면 다른 쪽은 마이크를 열 수 없으며, 강제로 lock
파일을 지워 동시 캡처를 우회해서는 안 된다. 소유 프로세스가 종료되면 advisory
lock이 해제된다. live에 포함된 통합 Debug sidecar는 이 lock 충돌에 의존하지
않고 Debug `asr_start` 자체를 거부한다.

## 환경 계약

| 변수 | 운영 기본값 | 설명 |
|---|---|---|
| `PUZZLE_ASR_URL` | `wss://arpa.worker-02.puzzle-ai.com` | Puzzle AI WebSocket endpoint |
| `SENTENCE_INPUT_TOPIC` | `/sensors/surgeon/sentence` | 최종 문장 출력 토픽 |
| `TASKPLANNER_ASR_CAPTURE_LOCK` | `/taskplanner-runs/asr/microphone.lock` | 컨테이너 내부 공유 캡처 lock |
| `TASKPLANNER_PIPEWIRE_SOCKET` | `/run/user/<uid>/pipewire-0` | 호스트 PipeWire 소켓 |
| `TASKPLANNER_RUN_ROOT` | `${HOME}/.local/share/taskplanner/runs` | 호스트 산출물 root |
| `TASKPLANNER_UID`, `TASKPLANNER_GID` | `1000`, `1000` | 컨테이너 프로세스 사용자 |

`PUZZLE_ASR_URL`에는 사용자명, 비밀번호, query 또는 fragment를 넣을 수 없다.
이 정책은 shell override에도 동일하며 node 시작·제어 경계에서 검증한다.

## 정적 점검과 장애 확인

현재 운영 컨테이너를 바꾸지 않고 설정만 검증하려면 다음을 사용한다.

```bash
scripts/taskplanner config live
scripts/taskplanner config llm-surgeon
bash tests/test_taskplanner_launcher.sh
```

`live` 출력에는 `taskplanner-asr`가 있어야 하고 `llm-surgeon` 출력에는 없어야
한다. 실제 실행 후 상태 확인은 다음 명령을 사용한다.

```bash
scripts/taskplanner status
docker compose logs --tail 200 taskplanner-asr
ros2 topic info --verbose /input/asr/runtime_status
ros2 topic info --verbose /sensors/surgeon/sentence
```

- `taskplanner-asr`가 unhealthy면 설치 overlay, ROS Domain/RMW, 상태 publisher와
  컨테이너 로그를 확인한다.
- 상태 토픽은 보이지만 문장 publisher가 없으면 ASR 캡처/서버 연결이 아직
  열리지 않은 정상적인 fail-closed 상태일 수 있다.
- `HOST_AUDIO_UNAVAILABLE`은 PipeWire 소켓 또는 사용자 UID/GID를,
  `NO_INPUT`은 Ubuntu에서 선택된 입력 장치와 USB 연결을 확인한다.
- 캡처 충돌은 운영/Debug 중 실제 마이크 소유자를 먼저 정지한다. lock 파일
  삭제는 복구 절차가 아니다.
