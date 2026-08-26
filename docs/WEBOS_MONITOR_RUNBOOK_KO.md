# webOS TV 수술 관제 화면

LG StanbyME 2 Max 같은 webOS TV에서는 HDMI 대신 TV 브라우저로 아래 주소를 엽니다.

```text
http://192.168.1.4:4173/tv
```

`/tv`는 `/monitor/index.html?profile=tv`로 이동합니다. webOS 사용자 에이전트로 `/`에 접속해도 같은 TV 전용 화면으로 자동 이동합니다. 화면 구성과 1920×1080 디자인 좌표는 기존 SurgiMate UI를 그대로 사용하며, 브라우저 터치 이벤트도 유지됩니다.

## 데이터 경로

- 상태 데이터: 기존 subscribe-only ROSBridge(`ws://192.168.1.4:9092`)
- 수술 영상: 동일 호스트의 H.264 Main/yuv420p HLS(`/media/flir.m3u8`)
- 재생 순서: MSE가 있으면 지연 로드한 HLS.js 경량 엔진, 아니면 webOS 네이티브 HLS, 모두 불가하면 ROS 영상
- HLS가 준비되지 않거나 6초 이상 멈춤: latest-only ROS 압축 영상으로 자동 폴백
- `?camera=ros`: TV에서도 HLS를 사용하지 않고 ROS 영상 경로를 강제
- `?mode=dummy`: ROS 연결 없이 로컬 검증 화면 사용

HLS 게이트웨이는 이미 검토된 `/surgery/images/flir/compressed` 공개 별칭만 구독합니다. ROS publish/service/action을 만들지 않으며 로봇 제어 권한이 없습니다. 입력은 BEST_EFFORT/VOLATILE/depth 1, 인코더 대기열은 최신 프레임 한 장으로 제한됩니다.

## 상태 확인

```bash
docker compose ps webapp monitor-media-gateway
curl -fsS http://127.0.0.1:4173/healthz
curl -fsS http://127.0.0.1:4173/media/health.json
```

`media/health.json`의 `state`가 `streaming`, `playlist_ready`와 `encoder_running`이 `true`이면 TV 영상 경로가 준비된 상태입니다. 카메라가 아직 공개되지 않은 경우 `waiting_for_camera`는 정상적인 fail-closed 대기 상태입니다.

소스 또는 설정을 변경한 뒤에는 운영 ROS 런타임 전체가 아니라 다음 두 서비스만 재생성합니다.

```bash
docker compose \
  --env-file .env.example \
  --env-file .env \
  --env-file docker/orchestration/live.env \
  up -d --force-recreate monitor-media-gateway webapp
```

`docker/monitor-media/Dockerfile` 또는 기반 이미지를 바꾼 경우에만 위 명령에 `--build`를 추가합니다. `webapp`은 `package-lock.json` 해시가 바뀐 경우에만 격리된 의존성 볼륨을 자동 동기화하므로, 일반 재시작 때마다 패키지를 다시 설치하지 않습니다.
