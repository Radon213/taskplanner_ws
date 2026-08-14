# 멀티캠 관제 콘솔

Taskplanner 웹앱의 `/?workspace=multicam` 화면은 현재 ARPA 멀티카메라 ROS 2
그래프를 한 개의 operator ROSBridge 세션으로 관제한다. 기본 endpoint는
`ws://<현재 웹앱 호스트>:9091`이며, 이는 통합 디버그 ROSBridge다. 공개용
`9092` bridge는 읽기 전용·allowlist 전용이라 이 화면의 ROS graph 탐색과
World Anchor 제어에 사용할 수 없다.

## 화면과 데이터 경로

| 화면 | ROS 2 데이터 | 동작 경계 |
| --- | --- | --- |
| 동기화 Color | `/synced/cam_[1-4]/color/image_raw/compressed`, `/synced/flir/color/image_raw/compressed` | CBOR, 별도 throttle 없음. synchronizer가 제공하는 15 Hz 프레임을 그대로 표시 |
| 동기화 Depth | `/synced/cam_[1-4]/depth/image_rect_raw/compressedDepth` | CBOR, 별도 throttle 없음. 기본 `가시화`는 원본 거리값을 카드 대비로만 매핑하며, `원본 PNG` 토글로 transport header만 제거한 payload를 그대로 확인 가능 |
| Capture/카메라 상태 | `/multicam_node/capture_status` | multicam 노드가 인식한 온라인 카메라, 동기화 skew, 세션, calibration readiness |
| 좌표계 | `/tf_static` | reliable/transient-local로 받은 고정 변환만 3D로 표시 |
| 토픽 검사 | `/rosapi/topics` + 사용자가 고른 토픽 1개 | 허용된 멀티캠 그래프를 목록화하고, payload는 선택한 한 토픽만 bounded sample으로 표시 |
| World Anchor | `/world_anchor_node/status`, `begin`, `stop`, `solve`, `publish` Trigger 서비스 | `solve`와 `publish`만 명시 확인 후 호출 |

카메라 표의 serial은 현재 rig의 `cameras.yaml` inventory와 동일하다. 각
카메라가 `capture_status`에서 online이고 해당 synced 프리뷰가 갱신되면,
launch inventory의 ID를 가진 카메라가 드라이버 및 동기화 경로를 통과하고
있음을 표시한다. 이것은 브라우저가 직접 읽은 USB descriptor/링크 속도는
아니다. 케이블·전원·포트 속도·hotplug flap의 물리 USB 판정은 원격 PC의
안전한 read-only 도구인 아래 명령으로 보완한다.

```bash
ros2 run arpa_multicam cam_watch
```

`preflight.sh`는 librealsense 장치를 직접 열 수 있으므로 드라이버가 이미
스트리밍 중일 때는 실행하지 않는다.

## World Console 매핑

기존 `world_console`의 키 입력은 GUI의 같은 service call로 매핑된다.

| GUI | world_console | 서비스 | 효과 |
| --- | --- | --- | --- |
| 샘플 수집 시작 | `b` | `/world_anchor_node/begin` | 기존 sample을 비우고 tag 관측을 시작 |
| 수집 중지 | `x` | `/world_anchor_node/stop` | sample은 보존한 채 수집만 멈춤 |
| Solve · 저장 · TF 발행 | `w` | `/world_anchor_node/solve` | 새 static anchor를 파일에 저장하고 발행 |
| 저장된 Anchor 다시 발행 | `p` | `/world_anchor_node/publish` | 기존 anchor JSON을 다시 static TF로 발행 |

`solve`와 `publish`는 로봇을 직접 구동하지는 않지만 `world → camera/tag`
static TF를 바꾼다. 따라서 태그가 이동하는 추적 대상이 아니라 고정 기준점인지
확인하는 체크박스를 통과해야 활성화된다.

## 로컬 개발/검증

```bash
cd /home/arl/Documents/ARPA-H/taskplanner_ws/webapp
npm run build
```

현재 실행 중인 개발 웹앱에서는 다음 주소를 연다.

```text
http://127.0.0.1:4173/?workspace=multicam
```

브라우저에서 `9091` endpoint가 연결된 뒤 다음을 확인한다.

1. Color 탭에서 CAM 1–4와 FLIR가 LIVE이고, Depth 탭에서 CAM 1–4가 갱신된다.
2. Capture 카드에서 5/5 online과 sync skew가 수신된다.
3. TF 카드에서 `/tf_static` frame tree와 3D axes가 표시된다.
4. Topic inspector에서 `/multicam_node/capture_status`, `/world_anchor_node/status`를 각각 선택해 최신 payload를 확인한다.
5. World Anchor는 상태 확인만으로 시작하고, 실제 solve/reload는 고정 기준 태그가 준비되었을 때에만 확인 체크 후 실행한다.
