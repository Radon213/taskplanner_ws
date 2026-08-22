# PNU 실시간 인식 preflight

> 이 파일의 초기 부분은 정렬 깊이 활성화 전 역사적 preflight snapshot이다.
> 현재 CAM4 aligned-depth 3D 계약과 로컬/원격 운용값은
> `docs/PNU_LIVE_3D_RUNBOOK_KO.md`를 기준으로 한다. Tool Drive 액세스 요청은
> 이후 승인됐고, 현재 세 모델 모두 설치·실행 검증을 마쳤다. 최신 격리 Debug
> 검증의 기계 판독 기록은 `debug_all_model_live_acceptance.v1.json`이다.

기준 시각은 2026-08-21 01:06 KST, upstream 기준은
`hanwae-py/hand-blood-tools@0f9e93115b8cc1d470398c92e010e3fc6ef1de5d`이다.
VIPLab에는 read-only SSH/ROS 구독과 센서 option 조회만 수행했다. 카메라
parameter, node, service, stream 설정은 변경하지 않았다.

## Tool 승인 및 Debug all-model 갱신 (2026-08-21 11:29 KST)

- 승인된 Google Drive 폴더 `1E42Cpgg8CbFRtnA8DuFbYeBT5IWx_G_h`의 파일
  `13JW_AVPgiJZ_XdWmOReSeSCg2d35wHSC`는 Drive 제목
  `checkpoint_best_regular.pth`로 확인했다. 로컬 운용 파일명은 upstream
  manifest에 맞춘 `cam4_rfdetr_seg_small_regular_resume_e13_best.pth`다.
- 로컬 파일은 133,941,485 bytes, mode `0600`, SHA256
  `253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430`이며
  `unzip -t`를 통과했다. 현재 `tools/pnu_live_preflight.py artifacts`도
  Tool/Blood/Hand 모두 일치해 `accepted=true`를 반환한다.
- worker 전역 health는 Tool/Blood/Hand 세 모델 모두 `ready=true`다. 실제
  VIPLab CAM4 RGB·color CameraInfo·aligned depth·aligned-depth CameraInfo의
  source stamp가 정확히 일치한 quartet 8개에서 세 알고리즘 모두 요청·실행됐다.
- 실제 표본에서 Tool은 `Allis Forceps` 1건(confidence 0.71, z 0.806 m)이었다.
  `position_valid=true`, `orientation_valid=false`이고 검증되지 않은 지지 평면을
  명시하는 `SUPPORT_PLANE_UNVALIDATED`를 유지했다. Hand는 2개, Blood는 실행
  완료 후 0건이었으며 `metric_3d_ready=true`였다.
- Debug overlay는 1280x720 투명 lossless WebP로 발행됐고 alpha가 실제 사용됐다.
  render/encode 12.402 ms, 모델 inference 85.482 ms, source-to-output 109.142 ms는
  이 격리 실행 표본의 측정값이며 성능 보장값이 아니다. worker 자원은 RAM
  2.411 GiB, VRAM 1,604 MiB였다.
- 합성 확인 이미지는 `/tmp/pnu-debug-all-model-live-composite.png`에만 임시
  증거로 남겼고 저장소에 복사하지 않았다. 시험용 서비스는 정리했으며 기존
  Taskplanner runtime은 재시작하거나 변경하지 않았다.
- 이 통과 결과는 전달·모델 실행·3D·Debug overlay 경로의 acceptance다. 모델
  정확도나 Tool/Blood checkpoint 버전 간 output parity는 주장하지 않는다.

과거의 권한 거부와 부분 모델 시험은 감사 추적을 위해 아래에 그대로 구분해
남긴다. 아래 blocker 문구는 해당 관측 시점의 상태이지 현재 상태가 아니다.
특히 `live_rgbd_acceptance.json`의 `access_requested_pending`과
`BLOCKED_CHECKPOINT_ACCESS`는 당시 2-model acceptance의 역사 값이며, 현재 상태는
`debug_all_model_live_acceptance.v1.json`이 대체한다.

## 초기 preflight 판정 (역사 기록)

- VIPLab CAM4의 RGB, native compressedDepth, 두 CameraInfo와 extrinsics가
  실제로 발행 중이다. 같은 capture cycle에서 얻은 RGB/depth source stamp
  차이는 62,989 ns였다.
- 캡처한 RGB/depth는 메모리에서 SHA256과 크기만 계산했고 원본 바이트나
  이미지 파일은 저장하지 않았다. 상세 증거는
  `live_smoke_input_manifest.json`에 있다.
- 현재 한 프레임 크기 기준 RGB 206,750 bytes + depth 429,257 bytes이다.
  15 Hz에서 payload만 약 76.3 Mbit/s이다. CameraInfo, DDS/HTTP framing,
  재전송을 감안해 한 방향 100 Mbit/s 이상을 예산으로 잡는다. Taskplanner가
  DDS를 받고 다시 원격 worker로 보낼 때도 정상 1 GbE의 범위 안이지만,
  실제 운용 시 링크 이용률과 drop을 측정해야 한다.
- depth scale은 CAM4 serial `146222251000`에서 현재 `0.001 m/unit`이다.
  그러나 extrinsics 배열의 row-major/column-major 의미가 검증되지 않아
  metric 3D는 계속 fail-closed이다. Tool/Blood/Hand 2D 추론은 이 blocker와
  분리해 먼저 시험할 수 있다.
- 당시 Hand `.task`는 공식 pinned URL에서 내려받아 로컬 SHA256을 고정했다.
  Blood checkpoint도 공개 gdown 확인 흐름으로 받아 size/SHA256과 ZIP 구조를
  검증했다. Hand는 통합 이미지의 MediaPipe 0.10.18 CPU constructor load를
  통과했고, Blood는 PyTorch 강제 weights-only 및 CPU RF-DETR constructor load를
  통과했다. 이 최초 snapshot 당시에는 Tool checkpoint를 아래 blocker 때문에
  받지 못해 세 모델 전체 GPU inference를 시작할 수 없었다. 이 blocker는 위의
  2026-08-21 11:29 KST 갱신에서 해소됐다.
- Blood checkpoint metadata는 RF-DETR `1.10.0.dev0`, 통합 이미지는 `1.9.0`이다.
  load 자체는 성공했지만 checkpoint args에 `num_queries`/`group_detr`가 없어
  loader가 flat-slice fallback을 사용했다. grouped query 구조가 달랐을 가능성은
  실제 frame output parity 전까지 미검증 blocker이다.

## 초기 Artifact 판정 (역사 기록)

| 알고리즘 | 상태 | 크기 | SHA256 | 라이선스 판정 |
|---|---|---:|---|---|
| Hand MediaPipe float16 v1 | 다운로드·검증 완료 | 7,819,105 | `fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1` | MediaPipe source/sample은 Apache-2.0이나 `.task` 자체에는 LICENSE가 없어 model asset 재배포 조건은 별도 검토 필요 |
| Tool RF-DETR | 당시 Google Drive 폴더와 로그인 계정이 `액세스 권한 필요`로 거부되어 미다운로드. 현재는 승인·다운로드·무결성·실행 검증 완료 | 133,941,485 | `253617aa5337fec219d694ca50537e4867fb8c403ce60f3a6945bbe15fecf430` | source bundle은 Apache-2.0이나 upstream 문서가 fine-tuned checkpoint 소유권/외부 제공 범위를 pending으로 명시하므로 재배포 승인을 뜻하지 않음 |
| Blood RF-DETR | 공개 gdown 확인 흐름으로 다운로드·ZIP/weights-only/CPU constructor 검사 완료; 1.10-dev → 1.9 output parity 미검증 | 133,788,164 | `f4967b2b8c7ab63921f8aa9b2ea0a4e3324243a9b98253da3ea4b9ecd6df6f75` (첫 로컬 다운로드 기록; provider-published checksum 아님) | 저장소와 viewer 모두 checkpoint 권리를 명시하지 않아 unknown |

최초 확인에서는 Google Drive 로그인·권한을 우회하지 않았고 액세스 요청도 보내지 않았다. 2026-08-21
01:36 KST에 현재 Chrome 로그인 계정으로도 Tool 폴더가 `액세스 권한 필요`를
반환했다. Blood는 gdown의 공개 대용량 파일 확인 흐름을 사용했고 Tool 폴더의
권한 부재를 당시 blocker로 남겼다. 이후 사용자 요청이 승인되어 정상 권한으로
다운로드했으며 우회는 없었다. 상세 기계 판독 정보는
`artifact_manifest.json`에 있으며 로컬 모델 root는
`/home/arl/.local/share/taskplanner/models/pnu_hand_blood`이다. 기존 파일은
덮어쓰지 않는다.

추가로 VIPLab에서 Tool checkpoint를 read-only로 검색했다. `/home/viplab`,
`/home`, `/opt`, `/srv`, `/mnt`, `/media`와 가상 파일시스템·Docker 저장소를
제외한 root filesystem을 최대 60초로 제한해 정확한 파일명 또는 정확한 기대
크기 133,941,485 bytes를 찾았다. 후보는 0개였다. byte-identical 파일은 이름을
바꿔도 크기가 같으므로 이 검색에 포함된다. SHA256을 계산할 후보가 없었고 로컬
복사도 수행하지 않았다. VIPLab의 파일, 카메라, ROS와 설정은 변경하지 않았다.

## Local/remote 배치 체크리스트

공통:

- Taskplanner-side ROS adapter가 CAM4 네 입력 토픽을 구독하고 binary
  multipart로 worker `/v1/infer`에 전달한다. 카메라 JPEG/depth를 base64 JSON으로
  확대하지 않는다.
- worker는 `GET /v1/health`, `GET /v1/capabilities`, `POST /v1/infer`를
  versioned JSON contract로 제공한다. 자동 local/remote fallback은 두지 않는다.
- `health.ready=true`만으로 시험 성공을 선언하지 않는다. 결과에 source stamp,
  request ID, 각 algorithm의 `ready=true`, `executed=true`, `latency_ms`,
  `detections` 배열이 모두 있어야 한다.
- `detections=[]`는 실제 실행 증거가 있을 때만 정상 0건이다. `executed=false`나
  field 누락은 각각 `MODEL_NOT_EXECUTED` 또는 `INVALID_RESULT`이다.
- input/result source stamp가 다르거나 입력이 stale이면 Taskplanner로 전달하지
  않고 fail-closed한다.

Local worker:

- worker bind는 `127.0.0.1:<port>`, endpoint도 loopback origin으로 둔다.
- inbound firewall rule은 만들지 않는다. `ss -lntp`로 loopback에만 listen하는지
  확인한다.
- 기존 NInfer 모델과 PNU 모델을 동시에 load하지 않는 운영 순서를 유지한다.

Remote worker:

- worker bind는 가능하면 worker의 특정 LAN IP로 제한한다. `0.0.0.0` bind가
  불가피하면 firewall source를 Taskplanner PC의 고정 LAN IP와 worker port로만
  제한한다.
- endpoint에는 credential/query/fragment를 넣지 않는다. 격리된 신뢰 LAN 밖을
  경유하면 HTTPS 또는 상호 인증 proxy를 사용한다.
- 양쪽 NIC에서 `ethtool <iface>`의 `Speed: 1000Mb/s`, `Duplex: Full`,
  `Link detected: yes`를 확인한다. route가 Wi-Fi/Tailscale을 선택하지 않는지도
  `ip route get <peer-ip>`로 확인한다.
- `ss -lntp`, firewall read-only 조회, Taskplanner PC에서 `/v1/health`와
  `/v1/capabilities`를 확인한다. `iperf3` 같은 부하 생성은 별도 승인 후 한다.
- 15 Hz에서 최소 60초 동안 input frame count, algorithm executed count,
  semantics publish count, drop/timeout count를 함께 기록한다. 실행 count가 0인데
  detection 0으로 보이는 상태를 성공으로 처리하지 않는다.
- 원격 worker가 죽거나 timeout이면 local model로 자동 전환하지 않고 해당
  provider를 unavailable로 표시한다.

실제 선택값은 다음처럼 고정한다. Tool checkpoint 설치 전에는
`REQUIRE_PERCEPTION_ON_START=true`가 의도대로 Live 시작을 막았고, 현재는 세
artifact 검사가 모두 통과한다.

```bash
# 같은 PC에서 worker 실행
PERCEPTION_PROVIDER=pnu_hand_blood
PERCEPTION_LOCATION=local
PERCEPTION_ENDPOINT=http://127.0.0.1:8020
PNU_SERVICE_URL=http://127.0.0.1:8020
REQUIRE_PERCEPTION_ON_START=true

# 별도 LAN PC에서 worker 실행. 두 URL은 동일한 remote origin이다.
PERCEPTION_PROVIDER=pnu_hand_blood
PERCEPTION_LOCATION=remote
PERCEPTION_ENDPOINT=http://WORKER_LAN_IP:8020
PNU_SERVICE_URL=http://WORKER_LAN_IP:8020
PNU_CLIENT_API_TOKEN_FILE=/run/taskplanner/perception/token
PNU_ALLOW_UNAUTHENTICATED_REMOTE=false
REQUIRE_PERCEPTION_ON_START=true
```

지원 launcher는 local일 때만 `pnu-perception`을 시작하고, remote일 때는 남아
있는 local PNU worker도 중지한다. 위치 변경 시 자동 fallback은 없다. Remote
worker 쪽에도 동일 upstream commit과 세 모델 SHA256을 배치해야 한다.

## 실제 VIPLab live 부분 추론 (2026-08-21)

Tool asset이 없는 상태에서 전체 준비성을 완화하지 않고, 격리된 시험 출력으로
`[blood, hand]`만 요청했다. 현재 VIPLab의 RGB, native compressedDepth, color/depth
CameraInfo가 모두 worker 요청에 포함됐다.

- Blood/Hand 모두 `executed=true`; Blood 0건, Hand 0건
- request `e3250be6-8dc5-4535-b289-163a3f350353`, source stamp
  `1787243479390561768 ns`
- decode 1.939 ms, 두 알고리즘 합계 inference 19.607 ms,
  adapter source-to-output 23.960 ms (warm 실행 한 표본이며 성능 보장값 아님)
- `depth_included=true`, `depth_camera_info_included=true`,
  `depth_scale_m_per_unit=0.001`, `depth_scale_validated=true`,
  `depth_aligned=false`, `metric_3d_ready=false`
- 부분 시험이므로 `semantic_ready=false`, `partial_ready`; Tool semantics/Mayo는
  발행하지 않았다. 0건을 Tool 결과로 위조하지 않았다.
- PNU 프로세스는 `nvidia-smi` 기준 1,080 MiB, 컨테이너 RAM은 1.668 GiB였다.
  전체 GPU는 32,607 MiB 중 5,538 MiB 사용/26,571 MiB 여유였고 NInfer 모델은
  `unloaded`였다.
- PNU image logical size는 5,212,769,754 bytes로 unified base보다 78,822 bytes
  증가했다. 현재 호스트 filesystem 여유는 838 GiB였다.

위 수치는 live 부분 시험에 실제 사용한 image의 기록이다. 이후 remote token
preflight와 authenticated capabilities gate를 보강해 현재 local image를
`sha256:793522642830f000430949c401a76bd7629e2e361bd930e960d95d00e4c7083f`
(5,212,769,742 bytes)로 다시 빌드했다. 모델 실행 코드는 동일하며, 새 image의
API/계약 테스트는 33개 모두 통과했다.

기계 판독 결과는 `live_partial_inference_result.json`에 있다. 이 결과는 실제
입력/전송/실행 증거이지 Blood checkpoint의 RF-DETR 1.10-dev→1.9 정확도 동등성
증거는 아니다.

공유된 librealsense issue의 방법처럼 depth pixel을 meter로 바꾸는 값은 해당
depth sensor의 `get_depth_scale()`/Depth Units에서 읽어야 한다. 따라서 현재
측정한 `0.001 m/unit`은 유효한 단위 근거다. 다만 이 값만으로 native depth가
color-aligned가 되지는 않으므로 extrinsics layout/registration gate는 그대로다.

## Acceptance CLI

표준 라이브러리만 사용하므로 Taskplanner Python 환경에서 그대로 실행한다.

```bash
python3 tools/pnu_live_preflight.py self-test

# 현재 세 artifact가 모두 일치하므로 exit 0과 accepted=true가 정상이다.
python3 tools/pnu_live_preflight.py artifacts

# worker가 준비된 뒤 local 또는 remote origin을 명시한다.
python3 tools/pnu_live_preflight.py worker \
  --location remote \
  --endpoint http://WORKER_LAN_IP:PORT \
  --api-token-file /path/to/token

# /v1/infer JSON 응답을 stdin으로 검증한다.
curl ... | python3 tools/pnu_live_preflight.py accept-result - \
  --algorithms blood,hand \
  --request-id REQUEST_ID \
  --source-stamp-ns SOURCE_STAMP_NS \
  --require-metric-3d
```

exit code는 `0=accepted`, `2=invalid result/input`, `3=model not ready`,
`4=model ready but not executed`, `5=endpoint/network contract failure`이다.

실제 기물을 다시 배치한 뒤에도 검출 개수만 성공 조건으로 삼지 않는다. 먼저 세
알고리즘 모두의 executed counter/source stamp/latency를 확인하고, 그 다음 검출이
있으면 semantics가 Taskplanner 입력 토픽까지 동일 request/source stamp로
전달되는지를 확인한다. 현재 격리 시험은 이 실행·전달 경로를 통과했지만 정확도나
checkpoint parity를 보장하지 않는다.
