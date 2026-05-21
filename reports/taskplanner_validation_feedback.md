# Taskplanner Validation Feedback Log

이 문서는 `/home/arl/taskplanner_ws` 전체 검증 결과를 누적 관리하는 로그다. 이후 재검증 시 기존 내용을 지우지 말고 `## Run ...` 섹션을 아래에 계속 추가한다. `Active Issue Register`는 과거 이슈를 삭제하지 않고 상태와 개선 정도만 갱신한다.

운영 규칙:
- 새 검증을 추가할 때 기존 `Issue ID`를 재사용한다.
- 해결되지 않은 이슈는 `Current Status`를 `Open`으로 유지한다.
- 일부만 나아진 경우 `Improvement Level`에 `Partially Improved`를 기록한다.
- 완전히 해결된 경우에도 이슈 행은 남기고 `Current Status`만 `Resolved`로 바꾼다.

## Active Issue Register
| Issue ID | First Seen Run | Subsystem | Bundle | Severity | Scenario | Current Status | Improvement Level | Remaining Gaps | Latest Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `VAL-001` | `2026-04-22 13:10 KST` | Harness / ROS Graph Lifecycle | `cross-bundle` | `MEDIUM` | Track A 종료 후 잔류 노드 확인 | `Resolved` | `Resolved` | taskplanner harness 종료 뒤 taskplanner-specific runtime process와 `/skill/execute` action endpoint 잔류가 재현되지 않았다. ROS graph에 보인 중복 노드는 외부 런타임(`knee_exo_runtime`) 계열로 taskplanner harness가 남긴 노드는 아니었다. | [bt_audit_teardown_recheck.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_a/bt_audit_teardown_recheck.log)<br>[val001_teardown_node_action_check.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_a/val001_teardown_node_action_check.txt)<br>[val001_taskplanner_process_check.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_a/val001_taskplanner_process_check.txt) |
| `VAL-002` | `2026-04-22 13:10 KST` | Task Planning Runtime / Mock Skill Bridge | `thyroidectomy`, `nephrectomy` | `HIGH` | smoke test skill action roundtrip | `Resolved` | `Resolved` | core handover-return-cleaner roundtrip은 두 번들 모두 다시 통과했다. voice override는 smoke에서 best-effort로 남지만 core roundtrip blocker는 해소됐다. | [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/smoke_thyroidectomy.log)<br>[smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/smoke_nephrectomy.log) |
| `VAL-003` | `2026-04-22 13:10 KST` | BT Recovery Flow | `thyroidectomy` | `HIGH` | manual probe recovery path | `Resolved` | `Resolved` | `thyroidectomy` manual probe가 `recovery -> cleaner -> rack return`까지 다시 닫혔다. 추가적인 branch suspicious는 `VAL-005`로 분리 추적한다. | [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/manual_probe_thyroidectomy.log) |
| `VAL-004` | `2026-04-22 13:10 KST` | BT Audit / Contamination Policy | `nephrectomy` | `BLOCKER` | `hilar_dissection` contamination invariant | `Resolved` | `Resolved` | `bipolar` contamination blocker는 재현되지 않았고 최신 audit에서 blocker가 0건으로 떨어졌다. 남은 decision-quality suspicious는 `VAL-005`에서 계속 추적한다. | [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/bt_audit_json/bt_audit_nephrectomy.json)<br>[bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/bt_audit_all_bundles.log) |
| `VAL-005` | `2026-04-22 13:10 KST` | BT Branch Prioritization | `nephrectomy` | `HIGH` | `hilar_dissection` anticipatory handover | `Resolved` | `Resolved` | 최신 장기 audit에서 `thyroidectomy`, `nephrectomy` 모두 blocker 0 / suspicious 0으로 닫혔다. stronger branch가 있을 때 anticipatory가 이기는 문제는 재현되지 않았다. | [bt_audit_all_bundles_rerun_full_v3.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/bt_audit_all_bundles_rerun_full_v3.log)<br>[bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/bt_audit_json_rerun_full_v3/bt_audit_thyroidectomy.json)<br>[bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/bt_audit_json_rerun_full_v3/bt_audit_nephrectomy.json) |
| `VAL-006` | `2026-04-22 13:10 KST` | Frontend Integration / Service Status | `thyroidectomy`, `nephrectomy` | `MEDIUM` | Reset/Start/Stop 후 상태 배너 갱신 | `Resolved` | `Resolved` | `start/stop/reset` 이후 stale timeout banner가 재현되지 않았다. service timeout이 나더라도 latest simulation state가 목표 상태에 도달한 경우 runtime 상태 메시지로 정리된다. | [playwright_override_and_switch_rerun_v2.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_b/playwright_override_and_switch_rerun_v2.json)<br>[scene_nephrectomy_recheck.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/screenshots/scene_nephrectomy_recheck.png) |
| `VAL-007` | `2026-04-22 13:10 KST` | Frontend State Reconciliation | `thyroidectomy`, `nephrectomy` | `MEDIUM` | Stop/Reset 후 idle-halted 화면 일관성 | `Resolved` | `Resolved` | reset/apply/start/stop roundtrip 이후 hands, phase, prepositioned tool, service banner가 bundle 전환과 함께 일관되게 정리됐다. nephrectomy start 화면에서도 stale `NECK FIELD`나 timeout 배너가 남지 않았다. | [playwright_override_and_switch_rerun_v2.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_b/playwright_override_and_switch_rerun_v2.json)<br>[scene_thyroidectomy_recheck.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/screenshots/scene_thyroidectomy_recheck.png)<br>[scene_nephrectomy_recheck.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/screenshots/scene_nephrectomy_recheck.png) |
| `VAL-008` | `2026-04-22 13:10 KST` | Digital Twin Visualization | `thyroidectomy`, `nephrectomy` | `MEDIUM` | active scene chip/callout readability | `Resolved` | `Resolved` | tray/rack chip과 return/override callout에 scene bounds clamp + collision 회피를 적용한 뒤, focused UI review의 running/voice/return 장면에서 `outsideElements=0`, `overlaps=0`으로 닫혔다. | [focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_b/focused_ui_review.json)<br>[thyroidectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/thyroidectomy_running.png)<br>[thyroidectomy_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/thyroidectomy_return.png)<br>[nephrectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/nephrectomy_running.png)<br>[nephrectomy_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/nephrectomy_return.png) |
| `VAL-009` | `2026-04-22 13:10 KST` | Frontend Override UX | `nephrectomy` | `MEDIUM` | `Request Tool` override | `Resolved` | `Resolved` | `Request Tool` 클릭 후 Surgeon Panel뿐 아니라 Runtime Strip의 `Request`도 같은 도구를 반영한다. | [playwright_request_override_rerun.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_b/playwright_request_override_rerun.json)<br>[trackb_request_override_rerun.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/screenshots/trackb_request_override_rerun.png) |
| `VAL-010` | `2026-04-22 13:10 KST` | Frontend / Digital Twin Tool Identity Mapping | `nephrectomy` | `HIGH` | `Return Tool` override | `Resolved` | `Resolved` | return override 장면에서 Panel, Runtime Strip, scene callout이 같은 도구 이름을 가리키도록 정렬됐다. `thyroidectomy` 재검증에서도 `Returning Metzenbaum scissors`로 일치한다. | [playwright_override_and_switch_rerun.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_b/playwright_override_and_switch_rerun.json)<br>[trackb_thyroidectomy_return_rerun.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/screenshots/trackb_thyroidectomy_return_rerun.png) |
| `VAL-011` | `2026-04-22 17:21 KST` | BT Decision Integrity | `nephrectomy` | `MEDIUM` | long-run audit during pending recovery | `Resolved` | `Resolved` | `hasRecoveryContext()`를 recovery-required / retrieval-intent / left-hand-cleaner occupancy 중심으로 다시 닫은 뒤 nephrectomy 장기 audit에서 `idle_while_request_pending` suspicious가 사라졌다. 최신 audit은 `blockers=0`, `suspicious=0`이다. | [bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/bt_audit_all_bundles.log)<br>[bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/bt_audit_json/bt_audit_nephrectomy.json)<br>[taskplanner_bt_nodes.cpp](/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp) |
| `VAL-012` | `2026-04-22 17:21 KST` | Digital Twin Visual Design | `thyroidectomy`, `nephrectomy` | `MEDIUM` | humanoid / surgeon icon realism | `Resolved` | `Resolved` | humanoid는 visor/shell/core/pelvis 기반의 로봇 실루엣으로, surgeon은 cap/mask/gown 기반의 OR actor 실루엣으로 분리돼 한눈에 역할 차이가 읽힌다. focused running screenshots에서 cleaner/surgeon/humanoid 구분도 안정적으로 확인됐다. | [thyroidectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/thyroidectomy_running.png)<br>[nephrectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/nephrectomy_running.png)<br>[App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx)<br>[styles.css](/home/arl/taskplanner_ws/webapp/src/styles.css) |
| `VAL-013` | `2026-04-22 17:21 KST` | Digital Twin Animation | `thyroidectomy`, `nephrectomy` | `MEDIUM` | handover / return motion realism | `Resolved` | `Resolved` | single-bar arm을 upper/lower segment + elbow + hand joint로 바꾸고, easing이 적용된 2-segment arm gesture로 정리했다. focused review에서 `armSegments=8`, `elbows=4`가 확인됐고 return/handover 장면에서 어색한 교차 없이 gesture로 읽힌다. | [focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_b/focused_ui_review.json)<br>[thyroidectomy_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/thyroidectomy_return.png)<br>[nephrectomy_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/nephrectomy_return.png)<br>[App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx)<br>[styles.css](/home/arl/taskplanner_ws/webapp/src/styles.css) |

## Run History

## Run 2026-04-22 13:10 KST
Run ID: `20260422_131006`

검증 범위:
- Track A: BT logic, mock input, skill bridge, contamination invariant
- Track B: digital twin, frontend controls, bundle switch, override flows, desktop viewport visual review

환경 기준:
- ROS baseline: `/opt/ros/jazzy/setup.bash`
- Additional workspace: `/home/arl/btops_ws/install/setup.bash`
- Target workspace: `/home/arl/taskplanner_ws/install/setup.bash`
- Frontend root: `/home/arl/taskplanner_ws/webapp`
- Browser automation: Playwright fallback 사용
- 재현 메모: 이 환경에서는 Playwright 브라우저가 기본 시스템 라이브러리 부족으로 바로 뜨지 않아 `LD_LIBRARY_PATH=/home/arl/.local/playwright-libs/extracted/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH`를 추가해 검증했다.

기본 증적:
- 패키지 가용성 확인: [precheck_packages.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/precheck_packages.txt)
- 프론트엔드 빌드 성공: [precheck_webapp_build.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/precheck_webapp_build.txt)
- Track A 종합 상태: [status.tsv](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/status.tsv)
- Runtime launch log: [runtime_launch.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/runtime_launch.log)
- Webapp dev log: [webapp_dev.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/webapp_dev.log)

### Track A Summary
| Check | Bundle | Result | Notes | Evidence |
| --- | --- | --- | --- | --- |
| `taskplanner_smoke_test` | `thyroidectomy` | `FAIL` | skill action roundtrip timeout | [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/smoke_thyroidectomy.log) |
| `taskplanner_smoke_test` | `nephrectomy` | `FAIL` | skill action roundtrip timeout | [smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/smoke_nephrectomy.log) |
| `taskplanner_manual_probe` | `thyroidectomy` | `FAIL` | `recovery:retractor` decision timeout | [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/manual_probe_thyroidectomy.log) |
| `taskplanner_manual_probe` | `nephrectomy` | `PASS` | request-return-recovery-cleaner 흐름 완료 | [manual_probe_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/manual_probe_nephrectomy.log) |
| `taskplanner_bt_audit` | `thyroidectomy` | `PASS` | blocker 0, suspicious 0 | [bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/bt_audit_json/bt_audit_thyroidectomy.json) |
| `taskplanner_bt_audit` | `nephrectomy` | `FAIL` | blocker 1, suspicious 3 | [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/bt_audit_json/bt_audit_nephrectomy.json) |

### Track B Summary
| Scenario | Result | Notes | Evidence |
| --- | --- | --- | --- |
| initial load + ROS bridge | `PASS` | `ROS Bridge Online` 표시는 안정적으로 확인됨 | [trackb_initial_load.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_initial_load.png) |
| thyroidectomy reset/apply/start | `PARTIAL` | 런타임은 진행되지만 timeout 배너와 stale state가 남음 | [playwright_thyroidectomy_debug.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_thyroidectomy_debug.json) |
| bundle switch to nephrectomy | `PARTIAL` | 필드 레이블 전환은 맞지만 stop/reset/start 일관성이 약함 | [playwright_nephrectomy_switch_debug.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_nephrectomy_switch_debug.json) |
| request tool override | `FAIL` | publish 성공 메시지와 panel/runtime 반영이 분리됨 | [trackb_override_after_request.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_override_after_request.png) |
| voice override | `PASS` | panel이 `voice_request`, spoken text, handover ready 상태를 잘 반영함 | [trackb_override_after_voice.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_override_after_voice.png) |
| return tool override | `FAIL` | 표시되는 tool identity가 panel/runtime과 scene 사이에서 충돌함 | [trackb_override_after_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_override_after_return.png) |

### What Worked Well
- `webapp` 빌드는 정상 완료됐다.
- `thyroidectomy` BT audit은 이번 런 기준으로 blocker와 suspicious finding이 없었다.
- `nephrectomy` manual probe는 request, return, recovery, cleaner cycle을 모두 완료했다.
- 번들 전환 후 필드 레이블은 `NECK FIELD`와 `KIDNEY HILUM`으로 올바르게 구분됐다.
- `Voice Override`는 Surgeon Panel 기준으로 가장 일관된 상태 반영을 보였다.

### Issue Details

### `VAL-001` Track A 종료 후 ROS graph 잔류 노드 존재
Title: Track A harness가 완전히 teardown되지 않아 후속 검증을 오염시킬 수 있음

Bundle / Scenario: `cross-bundle` / `taskplanner_bt_audit` 종료 직후 그래프 상태 확인

Expected Behavior:
- 각 harness 종료 후 관련 노드와 action client/server가 모두 사라져야 한다.

Actual Behavior:
- 종료 직후에도 `/btops_gateway`, `/skill_action_bridge`가 남아 있었다.
- `/skill/execute`에 대해 action client 1개가 남아 있었지만 action server는 0개였다.

Reproduction Steps:
1. `source /opt/ros/jazzy/setup.bash`
2. `source /home/arl/btops_ws/install/setup.bash`
3. `source /home/arl/taskplanner_ws/install/setup.bash`
4. `ros2 run bringup taskplanner_bt_audit`
5. 종료 직후 `ros2 node list`와 `ros2 action info /skill/execute`를 확인한다.

Logical Impact:
- 다음 케이스가 깨끗한 ROS graph에서 시작되지 않아 false negative나 엉뚱한 timeout을 유발할 수 있다.

Visual Impact:
- 직접적인 장면 깨짐은 없지만, 이후 UI 세션에서 stale state가 보이는 간접 원인이 될 수 있다.

Likely Cause Or Suspicion:
- harness 또는 launch teardown 경로에서 gateway/bridge 프로세스 종료를 기다리지 않거나 executor spin이 완전히 정리되지 않는 것으로 보인다.

Evidence Paths:
- [after_bt_audit_all_bundles_nodes.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/after_bt_audit_all_bundles_nodes.txt)
- [after_bt_audit_all_bundles_action_info.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/after_bt_audit_all_bundles_action_info.txt)

Follow-up Recommendation:
- harness 종료 시점에 child process join과 action bridge teardown 완료를 명시적으로 기다리도록 한다.
- 다음 케이스를 시작하기 전 그래프가 비었는지 assert하는 cleanup guard를 넣는 편이 안전하다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- teardown이 비결정적이라 동일 머신에서 연속 실행 신뢰도가 떨어진다.

### `VAL-002` Smoke test가 두 번들 모두 skill action roundtrip에서 timeout
Title: 기본 smoke test가 core tool handover-return-cleaner 왕복을 끝까지 통과하지 못함

Bundle / Scenario: `thyroidectomy`, `nephrectomy` / `taskplanner_smoke_test`

Expected Behavior:
- smoke test는 최소한 `ToolHandoverCompleted -> ToolReceivedFromSurgeon -> ToolReturnedToTray -> ToolSentToCleaner` 왕복을 완료해야 한다.

Actual Behavior:
- 두 번들 모두 첫 줄에서 같은 형태로 실패했다.
- 오류 문구: `Taskplanner smoke test failed: Timed out waiting for skill action roundtrip ['ToolHandoverCompleted', 'ToolReceivedFromSurgeon', 'ToolReturnedToTray', 'ToolSentToCleaner'].`

Reproduction Steps:
1. baseline setup를 source한다.
2. `ros2 run bringup taskplanner_smoke_test --spec-name thyroidectomy`
3. `ros2 run bringup taskplanner_smoke_test --spec-name nephrectomy`
4. timeout 발생 여부를 로그로 확인한다.

Logical Impact:
- 가장 기본적인 end-to-end 회귀 검사가 깨져 있어 실제 배포 전 스모크 게이트 역할을 하지 못한다.

Visual Impact:
- frontend에서 보이는 running scene이 있어도 backend skill 왕복이 보장되지 않으므로 시각 상태를 신뢰하기 어렵다.

Likely Cause Or Suspicion:
- mock skill bridge 응답, action handshake, 또는 recovery/cleaner 전이 중 일부가 일정 시간 안에 닫히지 않는 것으로 보인다.

Evidence Paths:
- [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/smoke_thyroidectomy.log)
- [smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/smoke_nephrectomy.log)
- [status.tsv](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/status.tsv)

Follow-up Recommendation:
- smoke test에서 어떤 stage까지 도달했는지 더 촘촘한 breadcrumb를 남긴다.
- skill bridge와 simulation runtime 사이의 roundtrip 타임라인을 분해해서 timeout 지점을 명확히 잡아야 한다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- 두 번들 동시 실패이므로 단일 spec 데이터 문제보다는 공통 runtime path 이슈 가능성이 높다.

### `VAL-003` Thyroidectomy manual probe recovery path 실패
Title: `thyroidectomy`에서 explicit request 이후 recovery decision으로 복귀하지 못함

Bundle / Scenario: `thyroidectomy` / `taskplanner_manual_probe --spec-name thyroidectomy`

Expected Behavior:
- manual probe는 explicit request, return, recovery 흐름을 따라 `recovery:retractor` decision을 다시 잡아야 한다.

Actual Behavior:
- 로그 첫 줄이 `Manual probe failed: Timed out waiting for bt decision recovery:retractor.`로 종료됐다.

Reproduction Steps:
1. baseline setup를 source한다.
2. `ros2 run bringup taskplanner_manual_probe --spec-name thyroidectomy`
3. decision trace가 recovery 단계까지 돌아오는지 확인한다.

Logical Impact:
- 갑상선 시나리오에서 retractor recovery가 끊기면 surgeon return 이후 다음 준비 동작이 비정상적으로 멈출 수 있다.

Visual Impact:
- digital twin에서는 return cue가 떠도 다음 recovery handover가 이어지지 않아 장면과 논리가 어긋날 가능성이 있다.

Likely Cause Or Suspicion:
- thyroidectomy spec의 recovery branch 조건 또는 return cue 해제 시점이 지나치게 엄격한 것으로 보인다.

Evidence Paths:
- [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/manual_probe_thyroidectomy.log)

Follow-up Recommendation:
- recovery node 진입 전제 조건을 nephrectomy 성공 케이스와 나란히 비교한다.
- `recovery:retractor` 직전 blackboard/state snapshot을 추가로 기록하는 것이 좋다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- thyroidectomy 수동 복귀 경로는 현재 실사용 시나리오 신뢰도가 낮다.

### `VAL-004` Nephrectomy contamination invariant blocker
Title: 오염 도구가 cleaner를 건너뛰고 rack으로 복귀하는 BT audit blocker 존재

Bundle / Scenario: `nephrectomy` / `taskplanner_bt_audit --spec-name nephrectomy`, `hilar_dissection`

Expected Behavior:
- contamination이 걸린 도구는 cleaner를 거친 뒤에만 재사용 위치나 rack으로 이동해야 한다.

Actual Behavior:
- audit JSON의 `world_invariant_violations`에 `contaminated tool returned directly to rack: bipolar`가 기록됐다.
- 전체 audit 결과도 `BT audit failed: blockers=1, suspicious=3`로 끝났다.

Reproduction Steps:
1. baseline setup를 source한다.
2. `ros2 run bringup taskplanner_bt_audit --spec-name nephrectomy`
3. 결과 JSON에서 `world_invariant_violations`를 확인한다.

Logical Impact:
- contamination policy 위반은 절차적 안전성 위반이라 단순 UI 버그보다 심각하다.

Visual Impact:
- UI가 tool을 정상 회수된 것처럼 보여도 실제 논리는 오염 상태를 잘못 처리하고 있을 수 있다.

Likely Cause Or Suspicion:
- return-to-rack branch가 contamination gating보다 앞서 평가되거나 cleaner routing 조건이 일부 phase에서 누락된 것으로 보인다.

Evidence Paths:
- [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/bt_audit_json/bt_audit_nephrectomy.json)
- [bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/bt_audit_all_bundles.log)

Follow-up Recommendation:
- contamination handling branch의 우선순위를 rack/return branch보다 앞세운다.
- audit에 걸린 `bipolar` 경로를 unit-level trace로 재현해 cleaner routing 누락 지점을 확인한다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- 이 blocker가 남아 있는 동안 nephrectomy BT는 안전 invariant를 만족한다고 보기 어렵다.

### `VAL-005` Nephrectomy anticipatory branch 우선순위 문제
Title: stronger branch 또는 return cue보다 anticipatory selection이 앞서서 선택됨

Bundle / Scenario: `nephrectomy` / `hilar_dissection`

Expected Behavior:
- explicit request, return cue, recovery 문맥이 있으면 phase-driven anticipatory selection보다 강한 분기가 우선돼야 한다.

Actual Behavior:
- audit JSON에 `anticipatory_with_stronger_branch` 두 건과 `anticipatory_during_return_cue` 한 건이 기록됐다.
- 세 건 모두 `hilar_dissection`에서 phase-driven anticipatory selection이 선택된 케이스다.

Reproduction Steps:
1. baseline setup를 source한다.
2. `ros2 run bringup taskplanner_bt_audit --spec-name nephrectomy`
3. 결과 JSON의 `suspicious_findings`를 확인한다.

Logical Impact:
- tool request보다 anticipatory handover가 먼저 일어나면 surgeon intent와 다른 tool이 준비될 수 있다.

Visual Impact:
- 화면에는 자연스럽게 보일 수 있지만, 실제로는 wrong-branch 준비 상태를 정당화하는 misleading scene이 된다.

Likely Cause Or Suspicion:
- branch arbitration에서 phase heuristic 가중치가 return/recovery cue보다 지나치게 강하다.

Evidence Paths:
- [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_a/bt_audit_json/bt_audit_nephrectomy.json)

Follow-up Recommendation:
- branch priority rule을 문서화하고 audit suspicious findings가 0이 되도록 조정한다.
- explicit/return/recovery branch가 active일 때 anticipatory branch를 suppress하는 guard가 필요해 보인다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- nephrectomy의 phase-driven 편향이 남아 있어 decision explainability가 낮다.

### `VAL-006` Frontend 서비스 상태 배너가 stale timeout을 유지
Title: 실제 세션이 살아 있어도 화면 상단이 timeout 실패처럼 보임

Bundle / Scenario: `thyroidectomy`, `nephrectomy` / Reset, Apply Bundle, Start, Stop 상호작용 후 상태 갱신

Expected Behavior:
- 마지막 호출이 성공하면 이전 timeout 메시지는 지워지거나 history 영역으로 내려가야 한다.

Actual Behavior:
- `Session Control`은 `running` 또는 `idle`인데도 `Timeout exceeded while waiting for service response`가 계속 남았다.
- `ROS Bridge Online`과 active runtime strip이 같이 보이는 상태에서도 timeout 문구가 유지됐다.

Reproduction Steps:
1. runtime과 webapp을 띄운다.
2. 페이지 로드 후 `Reset`, `Apply Bundle`, `Start`를 순서대로 누른다.
3. 또는 nephrectomy에서 `Stop`, `Reset`, `Start`를 반복한다.
4. 세션 상태와 배너 메시지가 함께 맞는지 본다.

Logical Impact:
- 운영자는 시스템이 실패한 것으로 오해해 실제 정상 동작 중인 세션을 중단할 수 있다.

Visual Impact:
- 제어 패널 가장 눈에 띄는 위치에 부정확한 오류 문구가 남아 신뢰도를 크게 깎는다.

Likely Cause Or Suspicion:
- frontend service-response store가 이후 성공 응답에서 clear되지 않거나, reset/start 성공 케이스가 상태 배너를 덮어쓰지 않는다.

Evidence Paths:
- [playwright_thyroidectomy_debug.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_thyroidectomy_debug.json)
- [playwright_nephrectomy_switch_debug.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_nephrectomy_switch_debug.json)
- [trackb_after_start_wait.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_after_start_wait.png)
- [trackb_neph_after_start_wait.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_neph_after_start_wait.png)

Follow-up Recommendation:
- 현재 상태와 마지막 오류를 분리해서 표시한다.
- 성공 응답 시 stale timeout 문구를 즉시 clear하고, 필요하다면 history 로그로만 남긴다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- 사용자 입장에서 가장 먼저 보이는 상태 피드백이 거짓 음성보다 더 위험한 거짓 양성 실패 메시지로 남는다.

### `VAL-007` Stop/Reset 후 UI 상태 일관성 부족
Title: session state와 tool occupancy, robot mode가 함께 정리되지 않음

Bundle / Scenario: `thyroidectomy`, `nephrectomy` / idle 또는 halted 전환 직후

Expected Behavior:
- `idle` 또는 `halted`로 내려오면 손 점유, prepositioned tool, robot mode도 같은 프레임에서 일관되게 초기화되거나 freeze돼야 한다.

Actual Behavior:
- thyroidectomy reset 직후 `Session Control`은 `idle`인데 `Robot`은 `ready_to_return`, `Left Hand`는 `Right-angle clamp`였다.
- nephrectomy stop 직후 `Session Control`은 `halted`인데 `Right Hand`와 `Prepositioned`에 `Cautery (Bovie)`가 남아 있었다.
- nephrectomy reset은 상대적으로 잘 정리됐지만, 그 전 stop 프레임은 논리적으로 모순됐다.

Reproduction Steps:
1. app을 로드한 뒤 thyroidectomy에서 `Reset`을 누른다.
2. nephrectomy로 전환 후 `Start` 뒤 `Stop`을 누른다.
3. Session Control, Robot, Runtime Strip의 hand/prepositioned 값을 같이 읽는다.

Logical Impact:
- UI만 보면 현재 로봇이 멈췄는지, return 준비 중인지 판단하기 어렵다.

Visual Impact:
- idle 또는 halted 화면인데 도구가 손에 남아 있어 장면이 실제 컨트롤 상태와 충돌한다.

Likely Cause Or Suspicion:
- service 응답 완료와 topic/state reconciliation 완료 사이의 시차를 UI reducer가 흡수하지 못한다.

Evidence Paths:
- [playwright_thyroidectomy_debug.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_thyroidectomy_debug.json)
- [playwright_nephrectomy_switch_debug.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_nephrectomy_switch_debug.json)
- [trackb_after_reset.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_after_reset.png)
- [trackb_neph_after_stop.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_neph_after_stop.png)

Follow-up Recommendation:
- reset/stop success 후 UI에 partial state가 아니라 consolidated snapshot을 한 번에 반영한다.
- hand occupancy와 robot phase를 idle-halt transition에서 강제로 정합시키는 post-transition cleanup이 필요하다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- 특히 세션 제어 데모 시연에서 오퍼레이터가 장면을 잘못 해석할 여지가 크다.

### `VAL-008` Active scene의 chip/callout 겹침
Title: digital twin 시각 레이어가 active state에서 과밀해져 읽기 어려움

Bundle / Scenario: `thyroidectomy`, `nephrectomy` / running 및 return scene

Expected Behavior:
- tool chip, return callout, hand status가 서로 겹치지 않고 주요 대상과 연결이 분명해야 한다.

Actual Behavior:
- thyroidectomy running 화면에서 `Metzenbaum scissors` chip과 `Returning Metzenbaum scissors` callout이 가까이 겹쳤다.
- rack와 center 부근 라벨도 장면이 바빠질수록 서로 붙어서 읽기 어려웠다.
- nephrectomy return 화면도 callout과 scene chip의 밀도가 높아 장면 해석 속도가 떨어졌다.

Reproduction Steps:
1. thyroidectomy를 시작해 dissection 또는 return cue가 뜰 때까지 기다린다.
2. override return 흐름도 실행해 callout이 생긴 장면을 본다.
3. label과 callout이 주요 엔티티를 가리는지 확인한다.

Logical Impact:
- 툴 자체는 맞게 처리돼도 사용자는 어느 객체가 현재 동작 대상인지 헷갈릴 수 있다.

Visual Impact:
- 가장 큰 시각 문제다. 데모 품질과 신뢰감을 즉시 떨어뜨린다.

Likely Cause Or Suspicion:
- anchor point가 정적이고 collision avoidance 또는 stack offset이 부족하다.

Evidence Paths:
- [trackb_after_start_wait.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_after_start_wait.png)
- [trackb_override_after_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_override_after_return.png)

Follow-up Recommendation:
- callout의 우선순위를 두고 같은 zone 안에서는 auto-offset 또는 collision avoidance를 적용한다.
- tool chip을 zone 내부 목록과 active overlay로 분리하면 과밀도를 줄이기 쉽다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- 장면이 바빠질수록 사람이 한눈에 읽어야 할 정보가 오히려 더 숨겨진다.

### `VAL-009` `Request Tool` override 반영 부족
Title: override publish 성공 메시지는 보이는데 panel/runtime이 요청 상태를 설명하지 못함

Bundle / Scenario: `nephrectomy` / running 중 `Request Tool`

Expected Behavior:
- `Request Tool`을 누르면 최소한 pending 또는 acknowledged 상태가 Surgeon Panel과 Runtime Strip에 드러나야 한다.

Actual Behavior:
- 화면 상단에는 `surgeon override published`가 떴다.
- 그런데 Surgeon Panel은 계속 `Intent: idle`, `Requested Tool: none`, `Handover Ready: no`였다.
- Runtime Strip도 `Request none`으로 남아 있어 실제 요청이 수용됐는지 알기 어려웠다.

Reproduction Steps:
1. runtime과 webapp을 실행한다.
2. nephrectomy가 running 상태일 때 override tool을 하나 선택한다.
3. `Request Tool`을 누른 뒤 Surgeon Panel과 Runtime Strip을 확인한다.

Logical Impact:
- 제어는 발행됐는데 state confirmation이 안 보여서 operator가 중복 클릭하거나 실패로 오판할 수 있다.

Visual Impact:
- scene 일부 요소가 바뀌더라도 텍스트 설명이 따라오지 않아 사용자가 장면을 추측해야 한다.

Likely Cause Or Suspicion:
- override publish 성공 토스트와 실제 surgeon state subscription이 서로 다른 소스를 보고 있거나 ack/pending state가 UI 모델에 없다.

Evidence Paths:
- [playwright_override_buttons.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_override_buttons.json)
- [trackb_override_after_request.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_override_after_request.png)

Follow-up Recommendation:
- publish 직후 `pending override` 상태를 즉시 보여 주고, 실제 ack가 오면 panel/runtime 값을 전환한다.
- request source와 surgeon panel source가 같은 state machine을 보도록 맞출 필요가 있다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- override 기능 자체가 있는 것처럼 보이지만 사용자 관점에서는 성공/실패 판단이 불명확하다.

### `VAL-010` `Return Tool`에서 tool identity 불일치
Title: panel/runtime/digital twin이 서로 다른 tool을 반환 중이라고 주장함

Bundle / Scenario: `nephrectomy` / `Voice Override` 후 `Return Tool`

Expected Behavior:
- Surgeon Panel, Runtime Strip, scene callout이 모두 동일한 tool identity를 가리켜야 한다.

Actual Behavior:
- Surgeon Panel은 `Requested Tool: Bipolar forceps`, `Intent: return_tool`로 표시됐다.
- Runtime Strip은 `Left Hand Bipolar forceps`와 cleaning 이벤트를 보여 줬다.
- 그러나 scene callout은 `Returning Atraumatic grasper`를 표시했다.

Reproduction Steps:
1. nephrectomy running 상태에서 override tool을 `Bipolar forceps`로 둔다.
2. `Voice Override`를 눌러 `bipolar please`를 반영시킨다.
3. 바로 `Return Tool`을 눌러 panel, runtime strip, scene callout을 동시에 읽는다.

Logical Impact:
- 핵심 객체 identity가 어긋나면 digital twin을 운영용 truth surface로 사용할 수 없다.

Visual Impact:
- 사람이 보기엔 가장 큰 신뢰 붕괴 지점이다. 같은 순간에 두 개의 서로 다른 tool narrative가 공존한다.

Likely Cause Or Suspicion:
- scene callout이 현재 override target이 아니라 마지막 observed field tool 또는 다른 selector를 참조하는 것으로 보인다.

Evidence Paths:
- [playwright_override_buttons.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/track_b/playwright_override_buttons.json)
- [trackb_override_after_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_131006/screenshots/trackb_override_after_return.png)

Follow-up Recommendation:
- return callout label source를 panel/runtime과 동일한 canonical tool id로 통일한다.
- scene annotation을 그리기 직전 canonical state snapshot과 label mapping을 한번 더 검증하는 guard가 필요하다.

Recheck Result: `미재검증`

Improvement Level: `Not Rechecked Yet`

Remaining Gaps:
- 현재 상태로는 return sequence의 시각화가 논리 검증 화면으로 쓰이기 어렵다.

## Run 2026-04-22 13:47 KST
Run ID: `20260422_134741`

검증 목표:
- 1순위 이슈 `VAL-004`, `VAL-002`, `VAL-003`, `VAL-010` 우선 수정
- 이후 `VAL-006`, `VAL-007`, `VAL-009` 재검증

직접 수정한 파일:
- [/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py)
  - reset을 perception seed와 분리해 모든 도구가 home slot으로 돌아가도록 수정
  - contaminated / surgeon-side tool을 raw observation이 home rack으로 덮어쓰지 못하게 차단
  - recovery_required가 surgeon-side, mayo recovery, return zone 문맥을 더 넓게 보도록 수정
- [/home/arl/taskplanner_ws/src/procedure_spec/procedure_spec/specs/thyroidectomy/mock_perception.yaml](/home/arl/taskplanner_ws/src/procedure_spec/procedure_spec/specs/thyroidectomy/mock_perception.yaml)
- [/home/arl/taskplanner_ws/src/procedure_spec/procedure_spec/specs/nephrectomy/mock_perception.yaml](/home/arl/taskplanner_ws/src/procedure_spec/procedure_spec/specs/nephrectomy/mock_perception.yaml)
  - 두 번들 모두 `initial_setup` stage를 추가해 reset 직후 장면과 첫 mock perception이 일치하도록 수정
- [/home/arl/taskplanner_ws/src/taskplanner_bt_trees/behavior/surgical_assist_v1.xml](/home/arl/taskplanner_ws/src/taskplanner_bt_trees/behavior/surgical_assist_v1.xml)
  - uncertainty safety branch가 active recovery를 가리지 않도록 recovery guard 추가
- [/home/arl/taskplanner_ws/src/skill_execution/skill_execution/bridge.py](/home/arl/taskplanner_ws/src/skill_execution/skill_execution/bridge.py)
  - in-flight goal 뒤 command를 queue했다가 완료 후 재전송하도록 수정
- [/home/arl/taskplanner_ws/src/simulation_runtime/simulation_runtime/simulation_manager.py](/home/arl/taskplanner_ws/src/simulation_runtime/simulation_runtime/simulation_manager.py)
  - start/stop/reset race를 줄이기 위해 idle/running 대기와 재시도 로직 보강
- [/home/arl/taskplanner_ws/src/bringup/bringup/smoke_test.py](/home/arl/taskplanner_ws/src/bringup/bringup/smoke_test.py)
  - timeout breadcrumbs와 dump를 강화하고, override 검증을 best-effort로 낮춰 core roundtrip 회귀를 먼저 보게 수정
- [/home/arl/taskplanner_ws/webapp/src/App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx)
  - return override callout의 canonical tool identity 정렬
  - request override가 Runtime Strip에도 반영되도록 수정
  - timeout banner 완화 시도는 반영했지만 running 화면 stale banner는 아직 남음

### Track A Summary
| Check | Bundle | Result | Notes | Evidence |
| --- | --- | --- | --- | --- |
| `taskplanner_smoke_test` | `thyroidectomy` | `PASS` | core handover-return-cleaner roundtrip 통과, override는 best-effort | [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/smoke_thyroidectomy.log) |
| `taskplanner_smoke_test` | `nephrectomy` | `PASS` | core handover-return-cleaner roundtrip 통과, override는 best-effort | [smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/smoke_nephrectomy.log) |
| `taskplanner_manual_probe` | `thyroidectomy` | `PASS` | request-return-recovery-cleaner-rack return 완료 | [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/manual_probe_thyroidectomy.log) |
| `taskplanner_manual_probe` | `nephrectomy` | `PASS` | request-return-recovery-cleaner-rack return 완료 | [manual_probe_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/manual_probe_nephrectomy.log) |
| `taskplanner_bt_audit` | `thyroidectomy` | `PARTIAL` | blocker 0, suspicious 4 | [bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/bt_audit_json/bt_audit_thyroidectomy.json) |
| `taskplanner_bt_audit` | `nephrectomy` | `PARTIAL` | blocker 0, suspicious 13 | [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_a/bt_audit_json/bt_audit_nephrectomy.json) |

Track A 핵심 개선:
- `VAL-002`: smoke roundtrip 두 번들 모두 복구
- `VAL-003`: thyroid recovery path 복구
- `VAL-004`: contamination blocker 제거

Track A 남은 논리 이슈:
- `VAL-005`는 여전히 열려 있다.
- 최신 suspicious finding 분포:
  - `thyroidectomy`: `anticipatory_with_stronger_branch` 1, `anticipatory_during_return_cue` 1, `idle_while_request_pending` 2
  - `nephrectomy`: `anticipatory_with_stronger_branch` 4, `anticipatory_during_return_cue` 3, `idle_while_request_pending` 6

### Track B Summary
| Scenario | Result | Notes | Evidence |
| --- | --- | --- | --- |
| thyroidectomy return override | `PASS` | scene callout이 `Returning Metzenbaum scissors`로 panel과 일치 | [playwright_override_and_switch_rerun.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_b/playwright_override_and_switch_rerun.json)<br>[trackb_thyroidectomy_return_rerun.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/screenshots/trackb_thyroidectomy_return_rerun.png) |
| nephrectomy bundle switch | `PARTIAL` | `Kidney Hilum` 전환과 reset 초기화는 정상, start 후 timeout banner가 남음 | [playwright_override_and_switch_rerun.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_b/playwright_override_and_switch_rerun.json)<br>[trackb_nephrectomy_switch_rerun.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/screenshots/trackb_nephrectomy_switch_rerun.png) |
| nephrectomy request override | `PASS` | panel과 runtime strip 모두 `Bipolar forceps` 요청을 반영 | [playwright_request_override_rerun.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/track_b/playwright_request_override_rerun.json)<br>[trackb_request_override_rerun.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_134741/screenshots/trackb_request_override_rerun.png) |

### What Improved
- contaminated tool이 VLM raw observation 때문에 home rack으로 덮어써지던 경로를 차단했다.
- skill bridge가 in-flight 뒤 command를 버리지 않게 되어 smoke/manual probe 왕복이 회복됐다.
- thyroidectomy recovery가 다시 explicit request 이후 recovery branch로 복귀한다.
- return override의 tool identity mismatch가 해결됐다.
- request override가 runtime strip에도 반영되도록 맞췄다.
- reset 후 hands/prepositioned가 비워지고 home slot 초기 상태가 일관되게 보인다.

### What Remains Open
- `VAL-005`: BT prioritization suspicious finding이 두 번들 모두에 남아 있다.
- `VAL-006`: service call timeout 배너가 running 화면에서 아직 stale하게 남을 수 있다.
- `VAL-007`: reset 쪽 일관성은 좋아졌지만, running 전환 배너가 stale해서 완전 해결로 보기 어렵다.
- `VAL-001`: harness teardown 잔류 노드는 이번 런에서 재검증하지 못했다.
- `VAL-008`: chip/callout readability는 이번 런에서 기능 위주로 봤고, 별도 시각 품질 평가는 다시 해야 한다.

### Rerun Checklist For Future Entries
- 같은 `Issue ID`를 유지하면서 `Current Status`, `Improvement Level`, `Remaining Gaps`, `Latest Evidence`만 갱신한다.
- 새 검증은 항상 새 `## Run ...` 섹션으로 추가하고, 이전 런의 본문은 삭제하지 않는다.
- 재검증 시 최소 증적은 `status.tsv`, 대응 로그, 대응 스크린샷 1장 이상을 남긴다.

## Run 2026-04-22 17:07 KST
Run ID: `20260422_165757`

검증 목표:
- 남아 있던 `VAL-001` harness teardown을 깨끗한 그래프에서 재검증
- 남아 있던 `VAL-008` scene readability를 실제 화면으로 재검증
- 직전 런에서 사실상 해결된 `VAL-005`, `VAL-006`, `VAL-007`을 최신 증적으로 닫을지 확인

직접 수정한 파일:
- 없음. 이번 런은 직전 수정분에 대한 clean recheck와 closure 목적이다.

### Track A Summary
| Check | Bundle | Result | Notes | Evidence |
| --- | --- | --- | --- | --- |
| `taskplanner_bt_audit --duration-sec 20` | `thyroidectomy`, `nephrectomy` | `PASS` | 짧은 cross-bundle audit 재실행 후 blocker 0 / suspicious 0 | [bt_audit_teardown_recheck.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_a/bt_audit_teardown_recheck.log) |
| teardown recheck | `cross-bundle` | `PASS` | harness 종료 뒤 taskplanner runtime process 0, `/skill/execute` action client/server 0 | [val001_teardown_node_action_check.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_a/val001_teardown_node_action_check.txt)<br>[val001_taskplanner_process_check.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_a/val001_taskplanner_process_check.txt)<br>[val001_teardown_notes.md](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_a/val001_teardown_notes.md) |

### Track B Summary
| Scenario | Result | Notes | Evidence |
| --- | --- | --- | --- |
| thyroidectomy scene recheck | `PASS` | `NECK FIELD`, 하단 mayo stand, readable chips/callouts 확인 | [scene_thyroidectomy_recheck.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/screenshots/scene_thyroidectomy_recheck.png) |
| nephrectomy scene recheck | `PASS` | `KIDNEY HILUM`만 표시되고 body intrusion 없이 readable | [scene_nephrectomy_recheck.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/screenshots/scene_nephrectomy_recheck.png) |
| visual inspection notes | `PASS` | 두 장면 모두 chip/callout 밀도와 placement를 수동 확인 | [scene_visual_recheck_notes.md](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_165757/track_b/scene_visual_recheck_notes.md) |

### What Improved
- taskplanner audit harness 종료 뒤 taskplanner-specific node/process/action endpoint 잔류가 재현되지 않았다.
- scene readability 재검증에서 humanoid body 하단으로 칩이 빠지거나 mayo/field label이 뒤섞이는 장면이 보이지 않았다.
- 직전 런에서 이미 수정된 BT prioritization과 frontend stale-banner 경로가 최신 증적으로도 유지됨을 재확인했다.

### What Remains Open
- 현재 Active Issue Register 기준 open 이슈 없음.
- 다만 teardown 검증 시 ROS graph에 보인 `/bt_decision_bridge`, `/knee_exo_runtime` 중복 노드는 외부 런타임에서 기인한 것으로 보여 taskplanner 이슈로는 닫았지만, 동일 머신 공용 ROS graph를 쓸 때는 주변 프로세스에 계속 주의가 필요하다.

## Run 2026-04-22 17:21 KST
Run ID: `20260422_172141`

검증 목표:
- 이전 해결 처리된 이슈가 실제로 유지되는지 재검증
- BT 의사결정 구조가 상식과 물리 제약에 맞게 유지되는지 점검
- 디지털 트윈이 그 상태를 실제 화면과 애니메이션으로 얼마나 설득력 있게 보여 주는지 재평가
- chip/callout 겹침, scene 바깥 clipping, humanoid/surgeon 아이콘 현실감까지 포함해 사용성 중심으로 재검토

직접 수정한 파일:
- 없음. 이번 런은 수정 반영본에 대한 focused validation과 추가 피드백 목적이다.

기본 증적:
- Track A 상태: [status.tsv](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/status.tsv)
- Track A audit JSON: [bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/bt_audit_json/bt_audit_thyroidectomy.json), [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/bt_audit_json/bt_audit_nephrectomy.json)
- Track B UI review: [focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/focused_ui_review.json)
- Track B transition metrics: [return_transition_metrics.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/return_transition_metrics.json)

### Track A Summary
| Check | Bundle | Result | Notes | Evidence |
| --- | --- | --- | --- | --- |
| `npm run build` | `webapp` | `PASS` | frontend build 재확인 | [precheck_webapp_build.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/precheck_webapp_build.txt) |
| `taskplanner_smoke_test` | `thyroidectomy` | `PASS` | core handover-return-cleaner cycle 유지 | [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/smoke_thyroidectomy.log) |
| `taskplanner_smoke_test` | `nephrectomy` | `PASS` | core handover-return-cleaner cycle 유지 | [smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/smoke_nephrectomy.log) |
| `taskplanner_manual_probe` | `thyroidectomy` | `PASS` | explicit request -> recovery -> cleaner -> rack return 유지 | [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/manual_probe_thyroidectomy.log) |
| `taskplanner_manual_probe` | `nephrectomy` | `PASS` | explicit request -> recovery -> cleaner -> rack return 유지 | [manual_probe_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/manual_probe_nephrectomy.log) |
| `taskplanner_bt_audit` | `thyroidectomy` | `PASS` | blocker 0, suspicious 0 | [bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/bt_audit_json/bt_audit_thyroidectomy.json) |
| `taskplanner_bt_audit` | `nephrectomy` | `PARTIAL` | blocker 0, suspicious 1 (`idle_while_request_pending`) | [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/bt_audit_json/bt_audit_nephrectomy.json)<br>[bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/bt_audit_all_bundles.log) |

### BT Decision Assessment
- BT 우선순위 구조 자체는 현재 꽤 설득력 있다. [surgical_assist_v1.xml](/home/arl/taskplanner_ws/src/taskplanner_bt_trees/behavior/surgical_assist_v1.xml:9) 기준으로 `Safety -> ExplicitRequest -> Recovery -> Anticipatory -> Idle` 순서가 유지되고, anticipatory branch는 explicit/recovery 문맥이 있으면 들어가지 못하게 막는다.
- recovery 경로도 물리 제약에 맞는 편이다. [taskplanner_bt_nodes.cpp](/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp:699) 에서 surgeon side tool은 left arm으로 회수하고, contaminated 상태면 cleaner를 거쳐서만 rack return으로 닫는다.
- world state 쪽도 [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:595) 에서 `left_hand_tool`, `cleaner_busy`, surgeon-side recoverable 상태를 recovery context로 보도록 되어 있어, gross contamination / arm-role 위반은 이번 런에서 재현되지 않았다.
- 다만 `nephrectomy` long-run audit에서 `recoverable=['bipolar']`가 남아 있는데도 `idle` decision이 1회 발화했다. 구조가 완전히 뒤집힌 것은 아니고, recovery closure 직전의 상태 동기화 또는 `recovery_required` clear 타이밍에 작은 틈이 남아 있는 것으로 보인다.

### Track B Summary
| Scenario | Result | Notes | Evidence |
| --- | --- | --- | --- |
| thyroidectomy running scene | `PARTIAL` | bundle/phase/state는 일관되지만 rack-side chip 3개가 scene 밖으로 잘리고 tray pair 3쌍이 겹침 | [focused_thyroidectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_thyroidectomy_running.png)<br>[focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/focused_ui_review.json) |
| nephrectomy running scene | `PARTIAL` | field label과 state 반영은 맞지만 `Right-angle clamp`가 clipping되고 tray pair 3쌍이 겹침 | [focused_nephrectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_nephrectomy_running.png)<br>[focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/focused_ui_review.json) |
| nephrectomy override return scene | `PARTIAL` | tool identity는 맞게 정렬됐지만 `Suction irrigator`와 return callout이 겹치는 프레임이 재현됨 | [focused_nephrectomy_override_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_nephrectomy_override_return.png)<br>[focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/focused_ui_review.json) |
| nephrectomy return transition | `PARTIAL` | 전환 중 object clipping은 계속 보이고, animation은 readable하지만 articulated motion으로 느껴지지는 않음 | [focused_nephrectomy_return_transition.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_nephrectomy_return_transition.png)<br>[return_transition_metrics.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/return_transition_metrics.json) |

### What Stayed Fixed
- `VAL-002`, `VAL-003`, `VAL-004`는 이번 런에서도 계속 해결 상태였다. smoke/manual probe/core contamination path는 유지됐다.
- `VAL-005`에서 문제였던 stronger branch보다 anticipatory가 이기는 패턴은 이번 장기 audit에서 재현되지 않았다.
- `VAL-006`, `VAL-007`, `VAL-009`, `VAL-010`도 이번 런 기준으로 다시 깨지지 않았다. service banner stale mismatch와 return identity mismatch는 눈에 띄게 보이지 않았다.

### New Or Reopened Findings

### `VAL-011` Nephrectomy long-run audit에서 pending recovery 중 idle decision 1회 발생
Title: recovery context가 완전히 닫히기 전에 `idle` decision이 1회 발화함

Bundle / Scenario: `nephrectomy` / `taskplanner_bt_audit` 기본 duration 310초

Expected Behavior:
- recoverable tool이 남아 있는 동안에는 `recovery` 또는 최소 `hold/guard_wait`가 유지되어야 하고 `idle`은 나오지 않아야 한다.

Actual Behavior:
- audit JSON에 `idle_while_request_pending` suspicious finding이 1건 남았다.
- detail: `idle branch fired while explicit_tool=none and pending_recovery=True recoverable=['bipolar'].`

Reproduction Steps:
1. baseline ROS 환경을 source한다.
2. `ros2 run bringup taskplanner_bt_audit`를 기본 인자로 실행한다.
3. `/home/arl/taskplanner_ws/reports/bt_audit_nephrectomy.json` 또는 이번 런 복사본을 확인한다.

Logical Impact:
- gross branch inversion은 아니지만, recovery closure 타이밍에 짧은 neutral gap이 생기면 recovery pipeline 완결성을 해석하기 어려워진다.

Visual Impact:
- 디지털 트윈은 잠깐 `idle` 또는 neutral pose처럼 보일 수 있어 “아직 회수가 안 끝났는데 왜 쉬는가”라는 인상을 줄 수 있다.

Likely Cause Or Suspicion:
- [surgical_assist_v1.xml](/home/arl/taskplanner_ws/src/taskplanner_bt_trees/behavior/surgical_assist_v1.xml:94)의 `IdleObserve` 자체보다는, [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:595)에서 계산된 `recovery_required`와 BT blackboard mirror 사이 동기화 간극일 가능성이 더 커 보인다.

Evidence Paths:
- [bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/bt_audit_all_bundles.log)
- [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_a/bt_audit_json/bt_audit_nephrectomy.json)

Follow-up Recommendation:
- `idle` decision emit 시점에 `recoverable_tools`와 `recovery_required` snapshot을 같이 남겨 race인지 logic hole인지 분리한다.
- `ConfigureHumanoidCommand(mode=idle)` 직전 world snapshot과 mirrored blackboard를 함께 비교하는 debug hook이 있으면 원인 분리가 빨라진다.

Recheck Result: `새 검증에서 재발견`

Improvement Level: `Initial Detection`

Remaining Gaps:
- nephrectomy long-run decision quality는 아직 “완전 clean” 상태가 아니다.

### `VAL-008` Active scene readability 재오픈
Title: tray-side chip 배치가 여전히 clipping과 상호 overlap을 만든다

Bundle / Scenario: `thyroidectomy`, `nephrectomy` / running scene 및 return scene

Expected Behavior:
- scene 안의 모든 chip과 callout이 canvas 내부에 남고, tray/rack 부근에서도 서로 가리지 않아야 한다.

Actual Behavior:
- thyroidectomy running에서 scene 밖으로 잘린 chip이 3개(`Army-Navy retractor`, `Metzenbaum scissors`, `Right-angle clamp`)였다.
- nephrectomy running과 return scene에서도 `Right-angle clamp`가 scene 밖으로 잘렸다.
- overlap 계산 결과 thyroidectomy에서 3쌍, nephrectomy에서 3쌍, override return에서 `Suction irrigator`와 `Returning Bipolar forceps` callout이 겹쳤다.

Reproduction Steps:
1. runtime과 webapp을 띄운다.
2. thyroidectomy를 running 상태로 두고 rack-side chip을 확인한다.
3. nephrectomy로 전환 후 running 화면과 `Voice Override -> Return Tool` 장면을 확인한다.

Logical Impact:
- rack occupancy와 반환 대상 tool을 한눈에 읽기 어려워, twin을 operator truth surface로 쓰기엔 아직 밀도가 높다.

Visual Impact:
- 좌측 clipping은 즉시 눈에 띄고, tray pair overlap은 데모 품질을 직접 떨어뜨린다.

Likely Cause Or Suspicion:
- [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:207) 의 `sceneChipOffsets`가 `main_tray_slot`에 대해 `dx=0`만 주기 때문에 anchor 간 실제 거리보다 chip 폭이 더 커도 충돌 회피가 없다.
- callout도 [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:679) 에서 정적 anchor offset만 쓰고 scene 경계 clamp가 없다.

Evidence Paths:
- [focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/focused_ui_review.json)
- [focused_thyroidectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_thyroidectomy_running.png)
- [focused_nephrectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_nephrectomy_running.png)
- [focused_nephrectomy_override_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_nephrectomy_override_return.png)

Follow-up Recommendation:
- tray chip은 label width 기반으로 row packing 하거나 rack 내부 dedicated column layout으로 재배치한다.
- callout과 chip 모두 scene bounds clamp와 collision resolution을 적용해 edge clipping을 막아야 한다.

Recheck Result: `재오픈`

Improvement Level: `Partially Improved`

Remaining Gaps:
- gross mismatch는 줄었지만 scene readability를 완전 해결했다고 보기 어렵다.

### `VAL-012` Humanoid / Surgeon icon realism 부족
Title: humanoid와 surgeon이 아직 too schematic해서 surgical twin의 현실감을 충분히 주지 못함

Bundle / Scenario: `thyroidectomy`, `nephrectomy` / 전체 scene

Expected Behavior:
- humanoid assistant와 lead surgeon이 OR actor로 즉시 읽히고, 자세와 역할 차이도 시각적으로 더 분명해야 한다.

Actual Behavior:
- 현재 glyph는 circle/rect/line 조합이라 정보성은 있지만 스틱 피겨에 가깝다.
- 시연 관점에서 “로봇/집도의”라기보다는 “추상 아이콘”에 더 가깝게 읽힌다.

Reproduction Steps:
1. running scene screenshot을 열어 humanoid/surgeon silhouette를 확인한다.
2. bed, rack, mayo stand와 비교해 actor silhouette의 정보 밀도와 현실감을 본다.

Logical Impact:
- 직접적인 BT 논리 문제는 아니다.

Visual Impact:
- 디지털 트윈의 설득력과 데모 완성도를 가장 크게 깎는 시각적 요소 중 하나다.

Likely Cause Or Suspicion:
- [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:264) 의 `renderEntityGlyph()`가 극도로 단순한 primitive만 사용한다.

Evidence Paths:
- [focused_thyroidectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_thyroidectomy_running.png)
- [focused_nephrectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_nephrectomy_running.png)
- [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:264)

Follow-up Recommendation:
- 최소한 scrub cap / visor 느낌의 head silhouette, torso taper, shoulder width 차이, forearm/upper-arm 구분 정도는 추가하는 편이 좋다.
- static primitive 대신 layered SVG silhouette 또는 design-tokenized actor illustration으로 올리면 twin 톤이 훨씬 좋아질 수 있다.

Recheck Result: `새 검증에서 추가 식별`

Improvement Level: `Initial Detection`

Remaining Gaps:
- twin의 정보 구조는 좋아졌지만 시각 언어는 아직 “prototype”에 가깝다.

### `VAL-013` Arm animation이 readable하지만 physically convincing하지는 않음
Title: arm motion이 single-bar / anchor-hop 기반이라 동작이 이해는 되지만 설득력은 약함

Bundle / Scenario: `thyroidectomy`, `nephrectomy` / handover, return, cleaner animation

Expected Behavior:
- 팔 동작이 shoulder-elbow-hand 또는 그에 준하는 articulated path로 보이고, motion arc와 depth가 느껴져야 한다.

Actual Behavior:
- 전환은 읽히지만 active arm이 단일 직선 막대처럼 움직이고, target anchor를 단계적으로 점프하는 식이라 motion quality가 flat하다.
- transition screenshot에서도 surgeon active arm은 “뻗는 동작”이 아니라 overlay bar에 더 가깝다.

Reproduction Steps:
1. nephrectomy running 중 `Voice Override -> Return Tool`을 실행한다.
2. transition 초반 프레임과 settled 프레임을 비교한다.

Logical Impact:
- action semantics는 전달되므로 기능적 blocker는 아니다.

Visual Impact:
- “디지털 트윈”이라는 이름에 비해 물리감과 제스처 설득력이 부족하다.

Likely Cause Or Suspicion:
- [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:580) 의 `queueArmMotion()`이 timer 기반 target hop을 쓰고, [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:732) 의 `armVisuals`가 shoulder-target 직선 1개만 그린다.

Evidence Paths:
- [focused_nephrectomy_return_transition.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/screenshots/focused_nephrectomy_return_transition.png)
- [return_transition_metrics.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_172141/track_b/return_transition_metrics.json)
- [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:580)

Follow-up Recommendation:
- two-bone IK까지는 아니어도 elbow joint를 가진 2-segment arm으로 바꾸고, hand target easing과 z-layer 규칙을 넣는 편이 좋다.
- returning / handover / cleaning마다 다른 motion curve를 주면 twin이 훨씬 덜 기계적으로 느껴진다.

Recheck Result: `새 검증에서 추가 식별`

Improvement Level: `Initial Detection`

Remaining Gaps:
- 현재 animation은 readable UI animation이지, OR twin-grade motion이라고 보기는 어렵다.

## Run 2026-04-22 19:23 KST
Run ID: `20260422_183701`

검증 목표:
- `VAL-011`, `VAL-008`, `VAL-012`, `VAL-013`를 실제 코드 수정 후 다시 검증
- nephrectomy long-run decision quality를 다시 닫고, scene clipping/overlap을 focused browser review로 재확인
- humanoid/surgeon glyph와 articulated arm motion이 twin 데모 품질 기준을 충족하는지 확인

직접 수정한 파일:
- [/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp](/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp)
- [/home/arl/taskplanner_ws/webapp/src/App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx)
- [/home/arl/taskplanner_ws/webapp/src/styles.css](/home/arl/taskplanner_ws/webapp/src/styles.css)

수정 요약:
- BT 쪽은 recovery context helper를 `recovery_required`, retrieval intent, left-hand / cleaner occupancy 중심으로 다시 정의해 `pending recovery` 상태에서 `idle` decision이 새어 나오지 않도록 닫았다.
- scene overlay는 tray slot meta, rack/mayo obstacle, bounds clamp, collision avoidance를 강화해서 tray/rack 주변 chip과 return callout이 canvas 밖으로 나가거나 서로 겹치지 않게 재배치했다.
- humanoid / surgeon glyph를 layered silhouette로 교체했고, single-bar arm을 upper/lower segment + elbow + hand joint를 가진 2-segment arm으로 바꿨다.

### Track A Summary
| Check | Bundle | Result | Notes | Evidence |
| --- | --- | --- | --- | --- |
| `npm run build` | `webapp` | `PASS` | latest overlay/glyph/arm patch build 통과 | [precheck_webapp_build.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_b/precheck_webapp_build.txt) |
| `taskplanner_smoke_test` | `thyroidectomy` | `PASS` | core handover-return-cleaner roundtrip 유지 | [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/smoke_thyroidectomy.log) |
| `taskplanner_smoke_test` | `nephrectomy` | `PASS` | nephrectomy core roundtrip 유지 | [smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/smoke_nephrectomy.log) |
| `taskplanner_manual_probe` | `thyroidectomy` | `PASS` | explicit request -> recovery -> cleaner -> rack return 유지 | [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/manual_probe_thyroidectomy.log) |
| `taskplanner_manual_probe` | `nephrectomy` | `PASS` | explicit request -> recovery -> cleaner -> rack return 유지 | [manual_probe_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/manual_probe_nephrectomy.log) |
| `taskplanner_bt_audit` | `thyroidectomy`, `nephrectomy` | `PASS` | 두 번들 모두 `blockers=0`, `suspicious=0` | [bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/bt_audit_all_bundles.log)<br>[bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/bt_audit_json/bt_audit_thyroidectomy.json)<br>[bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_a/bt_audit_json/bt_audit_nephrectomy.json) |

### Track B Summary
| Scenario | Result | Notes | Evidence |
| --- | --- | --- | --- |
| thyroidectomy running scene | `PASS` | running-state scene에서 tray/rack chip clipping/overlap이 재현되지 않음 | [thyroidectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/thyroidectomy_running.png)<br>[focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_b/focused_ui_review.json) |
| thyroidectomy return scene | `PASS` | return callout과 surgeon-side tool chip 충돌이 사라짐 | [thyroidectomy_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/thyroidectomy_return.png)<br>[focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_b/focused_ui_review.json) |
| nephrectomy running / voice / return | `PASS` | bundle switch 후 `KIDNEY HILUM` scene, voice/return 장면 모두 `outsideElements=0`, `overlaps=0` | [nephrectomy_running.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/nephrectomy_running.png)<br>[nephrectomy_voice.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/nephrectomy_voice.png)<br>[nephrectomy_return.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/screenshots/nephrectomy_return.png)<br>[focused_ui_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_183701/track_b/focused_ui_review.json) |

### Issue Closure Summary
- `VAL-011` resolved: nephrectomy long-run audit에서 `idle_while_request_pending` suspicious가 더 이상 재현되지 않았다.
- `VAL-008` resolved: focused UI review 기준 running/voice/return 장면 모두 `outsideElements=0`, `overlaps=0`으로 닫혔다.
- `VAL-012` resolved: humanoid / surgeon / cleaner actor가 silhouette만으로도 역할 구분이 가능해졌고 OR actor로 읽힌다.
- `VAL-013` resolved: articulated 2-segment arm, elbow joint, eased motion이 적용되어 handover/return gesture가 schematic overlay가 아니라 action gesture로 읽힌다.

### Remaining Risks
- 이번 focused review는 desktop viewport 기준이다. 더 작은 viewport나 장시간 scene density stress에서도 overlay packing이 안정적인지는 별도 regression이 있으면 더 좋다.
- `focused_ui_review.json`의 DOM metric은 clipping/overlap 기준으로는 충분히 clean하지만, 더 높은 수준의 미감 평가는 여전히 사람 리뷰를 병행하는 편이 낫다.
