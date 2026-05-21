# PLAN3 Authoritative Single-Twin V2 Review

## Executive Verdict

초기 검증(`20260423_213701_KST`)에서는 PLAN3의 방향을 상당히 반영했지만, 아직 "Authoritative Single-Twin V2가 안정적으로 동작한다"고 판정하기는 어려웠다.

구조적으로는 `or_digital_twin`이 `/simulation/state`, `/twin/world_state`, `/simulation/event`의 authoritative output을 소유하고, `surgeon_actor`가 `/surgeon/actor_event`와 `/surgeon/request`를 발행하며, VLM 입력용 synthetic renderer가 `render_mode=vlm` clean image를 내보내는 점은 PLAN3 의도와 잘 맞는다.

재검증(`20260423_223113_KST_plan3_recheck`)에서는 핵심 blocker였던 return/recovery latch가 durable recovery transaction으로 닫혔고, legacy VLM observation 경로도 proposal envelope + reducer decision event로 감사 가능해졌다. 두 번들 smoke/manual probe/bt audit는 모두 통과했고, audit 결과는 blockers=0/suspicious=0이다.

따라서 현재 판정은 `Mostly Aligned / Core Runtime Recheck Passed`이다. 남은 편차는 dedicated `RobotSkillEvent`/`EnvironmentTickEvent` 분리와 legacy compatibility path 정리, 그리고 추가 UI polish다.

## Run Metadata

| Field | Value |
|---|---|
| Run ID | `20260423_213701_KST` |
| 기준 계획 | `/mnt/c/Users/skado/Downloads/PLAN3.md` |
| Workspace | `/home/arl/taskplanner_ws` |
| Evidence root | `/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_213701_KST` |
| 검증 일시 | 2026-04-23 KST |
| 범위 | `thyroidectomy`, `nephrectomy`, backend harness, BT audit, focused UI/browser review, synthetic VLM camera capture |

## PLAN3 Verdict Matrix

| PLAN3 Item | Verdict | Evidence | Notes |
|---|---|---|---|
| Single authoritative twin reducer | `Aligned` | `or_digital_twin/node.py:35-46`, `ros_runtime_introspection.txt` | `/or_digital_twin`이 state/event output을 publish하고 actor/skill/VLM/control input을 subscribe한다. |
| Surgeon actor as request/return/phase owner | `Acceptable Deviation` | `surgeon_actor.py:86-95`, `ros_actor_vlm_topics.txt` | launch는 `surgeon_actor`를 사용하고 `/surgeon/actor_event`, `/surgeon/request`의 publisher는 `surgeon_actor`다. 단, legacy `mock_surgeon.py`가 남아 있고 spec도 `mock_surgeon` assets를 계속 참조한다. |
| Actor-less phase change 금지 | `Aligned in audit` | `bt_audit.py`, `bt_audit_*.json` | audit에 phase change origin check가 있고 이번 run에서 `world_invariant_violations=[]`였다. |
| VLM as recognizer + next-state proposer | `Aligned via proposal envelope` | `VLMInferenceProposal.msg`, `VLMReducerDecision.msg`, `bt_audit_*.json` | legacy `ToolObservation` input is wrapped into explicit proposal/reducer decisions; illegal observations do not mutate authoritative state. |
| VLM accepted/rejected 로그 | `Aligned in recheck` | `/twin/events`, `/simulation/event`, `bt_audit_*.json` | `VLMProposalAccepted`, `VLMProposalRejected`, and `VLMProposalQuarantined` are visible with proposal id, source, transition, confidence, and reducer reason. |
| Robot BT lifecycle guarded decision | `Aligned in recheck` | `taskplanner_bt_nodes.cpp`, `manual_probe_*_latest.txt`, `bt_audit_*_final_rerun.txt` | durable recovery transactions are prioritized over anticipatory/idle and both manual probes now complete receive-clean-return. |
| Idle 금지 조건 | `Aligned in audit` | `bt_audit_thyroidectomy.json`, `bt_audit_nephrectomy.json` | final recheck reports blockers=0/suspicious=0, including no pending recovery idle leak. |
| Anticipatory 제한 | `Aligned statically` | `taskplanner_bt_nodes.cpp:212-221`, `798-810` | `home_rack`/`returned_home`만 anticipatory candidate로 허용한다. |
| Recovery 제한 | `Aligned in recheck` | `manual_probe_*_latest.txt`, `bt_audit_*_final_rerun.txt` | active recovery transactions survive request cancellation and close only after terminal returned-home recovery. |
| VLM/debug renderer split | `Aligned` | `synthetic_scene_camera.py:36-74`, `synthetic_vlm_camera.jpg` | `render_mode=vlm` 기본값이며 캡처 이미지에 phase/title/lifecycle badge가 없다. |
| Debug UI lifecycle visibility | `Aligned with polish gaps` | `focused_plan3_ui_review_final.json`, `focused_final_*.png` | lifecycle/pending labels, Action Ack, and Recovery Tx are visible; focused review found no chip overlap/out-of-stage chips. |
| Bundle switch stale state 0건 | `Aligned in focused UI` | `focused_plan3_ui_review_final.json` | nephrectomy 화면에서 `Kidney Hilum`이 보이고 stale `Neck Field`는 탐지되지 않았다. |

## Validation Results

| Check | Result | Evidence |
|---|---|---|
| Package availability | `PASS` | `package_availability.txt` |
| `npm run build` | `PASS` | `npm_build.txt` |
| Smoke `thyroidectomy` | `PASS` | `smoke_thyroidectomy.txt` |
| Smoke `nephrectomy` | `PASS` | `smoke_nephrectomy.txt` |
| Manual probe `thyroidectomy` | `FAIL` | `manual_probe_thyroidectomy.txt` |
| Manual probe `nephrectomy` | `FAIL` | `manual_probe_nephrectomy.txt` |
| BT audit both bundles | `FAIL` | `bt_audit_all.txt`, `bt_audit_thyroidectomy.json`, `bt_audit_nephrectomy.json` |
| Existing Playwright bundle verify | `FAIL` | `playwright_bundle_verify.txt` |
| Focused Playwright UI review | `PASS as data collection, findings present` | `focused_plan3_ui_review.json`, `focused_*.png` |
| Synthetic VLM camera capture | `PASS` | `synthetic_vlm_camera.jpg`, `synthetic_vlm_camera_capture.txt` |

## Recheck Run `20260423_223113_KST_plan3_recheck`

| Check | Result | Evidence |
|---|---|---|
| `npm run build` | `PASS` | `/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_223113_KST_plan3_recheck/npm_build_latest.txt` |
| Smoke `thyroidectomy` | `PASS` | `smoke_thyroidectomy_latest.txt` |
| Smoke `nephrectomy` | `PASS` | `smoke_nephrectomy_latest.txt` |
| Manual probe `thyroidectomy` | `PASS` | `manual_probe_thyroidectomy_latest.txt` |
| Manual probe `nephrectomy` | `PASS` | `manual_probe_nephrectomy_latest.txt` |
| BT audit `thyroidectomy` | `PASS`, blockers=0, suspicious=0 | `bt_audit_thyroidectomy_final_rerun.txt`, `bt_audit_thyroidectomy.json` |
| BT audit `nephrectomy` | `PASS`, blockers=0, suspicious=0 | `bt_audit_nephrectomy_final_rerun.txt`, `bt_audit_nephrectomy.json` |
| Existing Playwright bundle verify | `PASS` | `playwright_bundle_verify_latest_clean.txt` |
| Focused browser review | `PASS with residual polish notes` | `focused_plan3_ui_review_final.json`, `focused_final_*.png` |

Notes:

- Early failed files in this recheck directory were produced during overlapping/contaminated ROS graph attempts and are superseded by the `*_latest` / `*_final_rerun` evidence above.
- `playwright_bundle_verify_latest.txt` was invalidated by stale runtime state and is superseded by `playwright_bundle_verify_latest_clean.txt`.
- Focused review reported `chipOverlaps=[]` and `outOfStage=[]` for thyroid started/request/return and nephrectomy started. Nephrectomy retained `Kidney Hilum` and did not show stale `Neck Field`.

## Active Issue Register

| Issue ID | Subsystem | Severity | Current Status | Improvement Level | Remaining Gaps | Latest Evidence |
|---|---|---|---|---|---|---|
| `P3-BT-001` | BT / Surgeon Actor / Twin | `Critical` | `Resolved in recheck` | `Full for tested scenarios` | Durable recovery transaction now survives cancel/transient ready clearing and closes only after terminal recovery. Continue monitoring longer stochastic runs. | `20260423_223113_KST_plan3_recheck/manual_probe_*_latest.txt`, `bt_audit_*_final_rerun.txt` |
| `P3-VLM-001` | VLM / Twin reducer | `Major` | `Resolved in recheck` | `Full for legacy observation path` | Legacy `ToolObservation` is wrapped into proposal/reducer events. Future real VLM should publish native `VLMInferenceProposal` directly. | `20260423_223113_KST_plan3_recheck/bt_audit_*.json`, `/twin/events` proposal samples |
| `P3-ARCH-001` | Public interfaces | `Major` | `Partially resolved` | `Major improvement` | `VLMInferenceProposal` and `VLMReducerDecision` are explicit. Dedicated `RobotSkillEvent`, `EnvironmentTickEvent`, and optional `VLMTransitionProposal` remain future cleanup. | `surgical_msgs/msg/VLMInferenceProposal.msg`, `surgical_msgs/msg/VLMReducerDecision.msg` |
| `P3-UI-001` | Frontend controls / integration | `Major` | `Resolved in recheck` | `Full for Playwright path` | UI now shows stable Action Ack and readiness even when surgeon booleans clear quickly. Need occasional manual review for edge timing under very slow ROS service calls. | `playwright_bundle_verify_latest_clean.txt`, `focused_plan3_ui_review_final.json` |
| `P3-UI-002` | Digital twin visualization | `Minor` | `Partially resolved` | `Major improvement` | Focused review reports 0 chip overlaps and 0 out-of-stage chips. Actor realism and advanced route polish are still aesthetic follow-ups. | `focused_final_*.png`, `focused_plan3_ui_review_final.json` |
| `P3-ARCH-002` | Runtime hygiene | `Minor` | `Open` | `Partially improved` | `mock_surgeon.py` remains as executable legacy path; accidental launch would reintroduce old scripted request behavior. | `mock_surgeon.py:37-75`, `setup.py` |
| `P3-ENV-001` | Validation environment | `Non-product` | `Observed` | `N/A` | Old runtime/webapp processes can contaminate UI validation with duplicate ROS graph and port conflicts. | `process_cleanup.txt`, `interactive_runtime.log` |

## Detailed Findings

### `P3-BT-001` Return/recovery chain loses latch after strong return cue

| Field | Value |
|---|---|
| Related PLAN3 item | `idle 금지`, `recovery는 mayo_recovery 또는 strong return cue surgeon_owned에서만 시작`, `Robot BT and Surgeon BT emit events only` |
| Verdict | `Risky Deviation` |
| Severity | `Critical` |
| Expected behavior | Strong `return_tool` cue should keep the selected tool in recovery until `ToolReceivedFromSurgeon -> ToolSentToCleaner -> ToolCleaningCompleted -> ToolReturnedToTray` completes. Anticipatory handover should not resume mid-chain. |
| Actual behavior | `thyroidectomy` timed out waiting for `ToolReceivedFromSurgeon:cautery`. `nephrectomy` timed out waiting for `ToolSentToCleaner:bipolar`. Both logs show return/cancel churn and anticipatory decisions after recovery began. |
| Evidence | `/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_213701_KST/manual_probe_thyroidectomy.txt` |
| Evidence | `/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_213701_KST/manual_probe_nephrectomy.txt` |
| Likely cause | Surgeon actor clears request signatures too aggressively via `cancel_request`, while the twin/BT recovery latch depends on transient request/ready flags. Once those flags clear, `NeedsRecovery` fails and anticipatory opens again. |
| Code references | `surgeon_actor.py:364-390`, `twin.py:715-725`, `taskplanner_bt_nodes.cpp:224-260`, `taskplanner_bt_nodes.cpp:568-584` |
| Fix recommendation | Add a durable return/recovery transaction state in authoritative twin, keyed by tool id, that survives request cancellation until the reducer observes terminal recovery events. BT should read that durable state instead of only transient surgeon request flags. |
| Recheck Result | `PASS` |
| Improvement Level | `Full for smoke/manual/audit scope` |
| Remaining Gaps | Durable transaction is validated in both bundles; continue long-run stochastic monitoring for unusual actor/VLM timing. |

### `P3-VLM-001` VLM/mock inputs are still observations, not explicit proposals with clean accept/reject lifecycle

| Field | Value |
|---|---|
| Related PLAN3 item | `VLM 역할을 recognizer + next-state proposer로 제한`, `VLM proposal rejected/accepted 로그 남김` |
| Verdict | `Risky Deviation` |
| Severity | `Major` |
| Expected behavior | VLM should publish compact proposal semantics, and the twin reducer should record each proposal as accepted/rejected with reason. Illegal visual guesses should not appear as ordinary observations that repeatedly hit lifecycle guards. |
| Actual behavior | `ToolObservation` is still the reducer input. The reducer blocks direct illegal rebases, but both audit reports set `observation_violation_detected=true`. |
| Evidence | `bt_audit_thyroidectomy.json`: `observation_direct_rebase_forbidden` samples |
| Evidence | `bt_audit_nephrectomy.json`: `illegal_observation_transition` samples |
| Code references | `or_digital_twin/node.py:41-45`, `twin.py:810-876`, `surgical_msgs/msg/ToolObservation.msg` |
| Why this matters | The authoritative state is protected, but the upstream contract is still noisy and ambiguous. A future agent cannot tell whether a bad VLM output was intentionally rejected, quarantined, or merely ignored without reverse-reading safety flags. |
| Fix recommendation | Add explicit proposal message or at least a proposal envelope around `ToolObservation`, with `proposal_id`, `source`, `proposed_transition`, `confidence`, and reducer-published `accepted/rejected/reason` event. |
| Recheck Result | `PASS` |
| Improvement Level | `Full for legacy observation path` |
| Remaining Gaps | Real VLM should publish native `VLMInferenceProposal` directly instead of relying on the compatibility wrapper around `ToolObservation`. |

### `P3-ARCH-001` PLAN3 public event split is only partially implemented

| Field | Value |
|---|---|
| Related PLAN3 item | `Public Interfaces / Types` |
| Verdict | `Acceptable Deviation -> Risky if left undocumented` |
| Severity | `Major` |
| Expected behavior | Clear input event separation: `SurgeonActorEvent`, `RobotSkillEvent`, `VLMInferenceProposal`, `EnvironmentTickEvent`, optional `VLMTransitionProposal`. |
| Actual behavior | `SurgeonActorEvent` exists. Robot skill events still use generic `TwinEvent`. VLM observations use `ToolObservation`, `PhaseEvidence`, `SurgeonGestureEvidence`. Cleaner countdown is encoded as skill/twin events, not `EnvironmentTickEvent`. |
| Evidence | `/home/arl/taskplanner_ws/src/surgical_msgs/msg` contents |
| Code references | `surgical_msgs/msg/SurgeonActorEvent.msg`, `surgical_msgs/msg/TwinEvent.msg`, `surgical_msgs/msg/ToolObservation.msg` |
| Why this matters | The implementation can work, but agent-facing architecture no longer matches PLAN3 names. Future changes may accidentally treat observations as truth writes because the interface names do not express proposal/reducer semantics. |
| Fix recommendation | Either implement the planned messages or document the exact semantic mapping in code and reports. Preferred fix is to add explicit proposal/result messages for VLM first, then consider aliasing robot/environment events. |
| Recheck Result | `PARTIAL PASS` |
| Improvement Level | `Major` |
| Remaining Gaps | `VLMInferenceProposal`/`VLMReducerDecision` exist. Dedicated robot/environment event types are still represented by existing generic event paths. |

### `P3-UI-001` Frontend override controls do not reliably create visible request/return readiness

| Field | Value |
|---|---|
| Related PLAN3 item | `bundle switch, start/reset, surgeon action, robot action에 따라 authoritative twin 기준으로만 바뀌게 한다` |
| Verdict | `Risky Deviation` |
| Severity | `Major` |
| Expected behavior | `Request Tool` should result in visible request intent and `Handover Ready: yes` long enough for a human/operator to confirm. `Return Tool` should similarly show retrieval readiness while recovery is pending. |
| Actual behavior | Existing Playwright regression failed at `Request Tool did not set handover readiness`. Focused review snapshots after request/voice/return all had `handoverReadyYes=false` and `retrievalReadyYes=false`. |
| Evidence | `playwright_bundle_verify.txt` |
| Evidence | `focused_plan3_ui_review.json` |
| Evidence | `focused_thyroid_after_request.png`, `focused_thyroid_after_return.png` |
| Likely cause | UI override goes through `surgeon_actor`, but actor policy/twin clearing can immediately cancel or satisfy the transient request before the panel can display a stable ready state. This is likely the same root as `P3-BT-001`. |
| Fix recommendation | Add visible override transaction state or event acknowledgement. UI should show `pending override accepted`, `actor event emitted`, and `twin reducer accepted/rejected` instead of only transient surgeon-ready booleans. |
| Recheck Result | `PASS` |
| Improvement Level | `Full for Playwright path` |
| Remaining Gaps | UI now shows `Action Ack`, stable handover/retrieval readiness, and recovery transaction visibility. Manual review should continue around slow ROS service responses. |

### `P3-UI-002` Scene is usable but still visually crowded during animated tool/arm states

| Field | Value |
|---|---|
| Related PLAN3 item | `디지털 트윈 시각화/animation usability`, `humanoid and surgeon icons more realistic` |
| Verdict | `Acceptable Deviation` |
| Severity | `Minor` |
| Expected behavior | Animated handover/recovery state should be readable without tool chips covering anatomy, route, or actor intent. Humanoid and surgeon icons should be recognizable enough to support physical reasoning. |
| Actual behavior | DOM chip-overlap detector found `0` hard chip-chip overlaps and no out-of-stage chips. Human visual review still shows tool chips and route lines covering the humanoid/field area, especially `focused_thyroid_after_request.png` and `focused_thyroid_after_voice.png`. Icons are improved from plain boxes but still diagrammatic rather than realistic. |
| Evidence | `focused_plan3_ui_review.json` |
| Evidence | `focused_thyroid_after_request.png` |
| Evidence | `focused_thyroid_after_voice.png` |
| Fix recommendation | Reserve collision-aware lanes for tool chips, separate arm routes from labels, shrink or fade non-active chips near dense zones, and make surgeon/humanoid silhouettes more anatomically legible. |
| Recheck Result | `PASS with polish remaining` |
| Improvement Level | `Major` |
| Remaining Gaps | Focused review reports 0 chip overlaps/out-of-stage chips. Actor realism and advanced route animation remain aesthetic follow-ups. |

### `P3-ARCH-002` Legacy `mock_surgeon` path remains

| Field | Value |
|---|---|
| Related PLAN3 item | `mock_surgeon free-running scripted request generation 제거` |
| Verdict | `Acceptable Deviation with risk` |
| Severity | `Minor` |
| Expected behavior | Default runtime should not use old free-running `mock_surgeon`. Any remaining mock path should be clearly marked as deprecated/manual test hook. |
| Actual behavior | launch uses `surgeon_actor`, which is good. But `mock_surgeon.py` and console entrypoint still exist and can publish `/surgeon/state`, `/surgeon/request`, `/surgery/audio/request_text`. Procedure specs still expose `mock_surgeon` stages used as voice templates. |
| Evidence | `taskplanner_mock.launch.py:106`, `mock_surgeon.py:37-75`, `simulation_runtime/setup.py` |
| Fix recommendation | Rename/deprecate old executable, block it from default launch paths, or convert it into a test-only hook with namespace isolation so it cannot collide with `surgeon_actor`. |
| Recheck Result | `UNCHANGED` |
| Improvement Level | `None this run` |
| Remaining Gaps | Legacy `mock_surgeon` executable remains available outside default launch path. It should be deprecated or namespace-isolated in a cleanup pass. |

### `P3-ENV-001` Validation can be contaminated by old runtime processes

| Field | Value |
|---|---|
| Related PLAN3 item | `Interactive runtime + frontend verification` |
| Verdict | `Non-product finding` |
| Severity | `Non-product` |
| Expected behavior | UI verification should run against exactly one ROS graph and one Vite server. |
| Actual behavior | Before cleanup, old runtime/webapp processes occupied `9090` and `4173`, and logs showed interleaved phase states from duplicate graph activity. |
| Evidence | `process_cleanup.txt`, `interactive_runtime.log`, `webapp_dev_server.log` |
| Fix recommendation | Add a preflight validation script that checks for taskplanner ROS/Vite process groups and port occupancy before browser tests. Do not run UI validation until graph uniqueness is confirmed. |
| Recheck Result | `MITIGATED DURING RECHECK` |
| Improvement Level | `Operational` |
| Remaining Gaps | Clean runtime was used for final Playwright/focused review; a reusable preflight script is still recommended to prevent future contaminated runs. |

## Positive Findings

| Area | Evidence | Assessment |
|---|---|---|
| Smoke behavior | `smoke_thyroidectomy.txt`, `smoke_nephrectomy.txt` | Both bundles complete core handover/recovery/override paths in the broad smoke harness. |
| Renderer split | `synthetic_vlm_camera.jpg`, `ros_runtime_introspection.txt` | VLM camera is clean and default `render_mode=vlm`, with no debug title, lifecycle badge, or phase overlay. |
| Bundle switch UI | `focused_plan3_ui_review.json` | Nephrectomy screen showed `Kidney Hilum`; stale `Neck Field` was not detected. |
| Authoritative output ownership | `ros_runtime_introspection.txt` | `/simulation/state` has one publisher: `or_digital_twin`. |
| Surgeon actor producer | `ros_actor_vlm_topics.txt` | `/surgeon/actor_event` and `/surgeon/request` each have one publisher: `surgeon_actor`. |
| Debug UI information | focused screenshots | Lifecycle and next transition labels are visible on tool chips, making state explainable even where layout needs polish. |

## Required Fix Prompts For Implementation Agent

Historical note: the prompts below were the implementation targets after the initial failing run. The `20260423_223113_KST_plan3_recheck` section above records the code changes and verification results after applying them.

### Prompt 1: Fix return/recovery latch

```text
You are modifying /home/arl/taskplanner_ws according to PLAN3 Authoritative Single-Twin V2. Focus on P3-BT-001.

Problem: manual_probe fails for both thyroidectomy and nephrectomy. After a strong return cue, BT emits recovery but the return/recovery chain loses its latch before ToolReceivedFromSurgeon/ToolSentToCleaner/ToolCleaningCompleted/ToolReturnedToTray completes. Evidence is in /home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_213701_KST/manual_probe_thyroidectomy.txt and manual_probe_nephrectomy.txt.

Implement a durable authoritative twin return/recovery transaction keyed by tool_id. It should be created by surgeon actor return_tool or place_on_mayo_recovery, survive cancel_request and transient surgeon_ready flag clearing, and close only after terminal recovery events. Robot BT must treat that transaction as stronger than anticipatory/idle. Update audit/manual probe expectations if needed, but do not weaken the scenarios. Re-run smoke, manual_probe, and bt_audit for both bundles.
```

### Prompt 2: Make VLM proposal/reducer semantics explicit

```text
Focus on P3-VLM-001 and P3-ARCH-001. VLM/mock outputs are still ordinary ToolObservation messages and bt_audit shows observation_violation_detected=true for both bundles. Add an explicit VLM proposal contract or proposal envelope with accepted/rejected reducer events. At minimum include source/proposal_id/proposed_transition/confidence and reducer decision reason. Preserve current safety behavior: illegal observations must not mutate authoritative state. Re-run bt_audit and verify each rejected proposal is visible in /twin/events or /simulation/event with a stable reason.
```

### Prompt 3: Fix frontend override acknowledgement and visual clarity

```text
Focus on P3-UI-001 and P3-UI-002. Existing Playwright bundle verify fails because Request Tool does not surface Handover Ready: yes, and focused screenshots show dense arm/tool overlays. Add UI-visible acknowledgement for override accepted/emitted/reducer accepted-rejected so buttons are not judged only by transient surgeon booleans. Improve scene chip placement and arm route layering so active tool labels do not obscure humanoid/field/surgeon anchors. Re-run playwright_bundle_verify.cjs and focused UI review.
```

## Recheck Template

| Issue ID | Recheck Date | Recheck Result | Improvement Level | Remaining Gaps | Evidence |
|---|---|---|---|---|---|
| `P3-BT-001` | `2026-04-23 KST` | `PASS` | `Full for tested scenarios` | Long-run stochastic monitoring still recommended. | `20260423_223113_KST_plan3_recheck/manual_probe_*_latest.txt`, `bt_audit_*_final_rerun.txt` |
| `P3-VLM-001` | `2026-04-23 KST` | `PASS` | `Full for legacy observation path` | Real VLM should publish native proposal messages directly. | `bt_audit_*.json`, `VLMProposalAccepted/Rejected/Quarantined` events |
| `P3-ARCH-001` | `2026-04-23 KST` | `PARTIAL PASS` | `Major` | Dedicated robot/environment event split remains future cleanup. | `surgical_msgs/msg/VLMInferenceProposal.msg`, `surgical_msgs/msg/VLMReducerDecision.msg` |
| `P3-UI-001` | `2026-04-23 KST` | `PASS` | `Full for Playwright path` | Continue manual review under slow ROS service timing. | `playwright_bundle_verify_latest_clean.txt`, `focused_plan3_ui_review_final.json` |
| `P3-UI-002` | `2026-04-23 KST` | `PASS with polish remaining` | `Major` | Actor realism and arm-route polish remain aesthetic follow-ups. | `focused_final_*.png`, `focused_plan3_ui_review_final.json` |
| `P3-ARCH-002` | `2026-04-23 KST` | `UNCHANGED` | `None this run` | Legacy `mock_surgeon` executable remains available outside default launch path. | `mock_surgeon.py`, `setup.py` |

## Re-run Checklist

| Command | Required Outcome |
|---|---|
| `cd /home/arl/taskplanner_ws/webapp && npm run build` | PASS |
| `ros2 run bringup taskplanner_smoke_test --spec-name thyroidectomy` | PASS |
| `ros2 run bringup taskplanner_smoke_test --spec-name nephrectomy` | PASS |
| `ros2 run bringup taskplanner_manual_probe --spec-name thyroidectomy` | PASS, includes receive-clean-return chain |
| `ros2 run bringup taskplanner_manual_probe --spec-name nephrectomy` | PASS, includes receive-clean-return chain |
| `ros2 run bringup taskplanner_bt_audit --spec-name thyroidectomy` or equivalent focused run | PASS, no suspicious/blocker and no unexplained observation violations |
| `ros2 run bringup taskplanner_bt_audit --spec-name nephrectomy` or equivalent focused run | PASS, no suspicious/blocker and no unexplained observation violations |
| `node /home/arl/taskplanner_ws/webapp/playwright_bundle_verify.cjs` | PASS |
| Focused UI review | No stale bundle labels, no misleading override readiness, no visually obstructive active animation state |
