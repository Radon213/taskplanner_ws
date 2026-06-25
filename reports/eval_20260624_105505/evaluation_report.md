# Taskplanner 0.1.0 Prediction and BT Decision Evaluation

- Generated: 2026-06-24 11:14:28
- Source VLM/reducer run: `multi_bundle_runtime_probe.json`; 3 procedures x 60 seconds.
- Source BT audit run: `bt_decision_audit_after_policy_fix.json`; 3 procedures x 45 seconds, attached to the same Docker runtime.
- Score format: `correct / proposed / evaluable`. Accuracy in the table is `correct / evaluable`.
- Ground truth: hidden phase and future tool requests from the LLM surgeon actor, used only by the evaluator, not by VLM input.
- BT correctness rule: a decision passes when it respects explicit request priority, recovery context, lifecycle/contamination guards, phase certainty, handover readiness, and current policy that the surgeon may hold up to two tools.

## Executive Summary

| Metric | Aggregate result |
|---|---:|
| VLM raw phase | 112 / 114 / 114 (98.2%) |
| System fused phase | 114 / 114 / 114 (100.0%) |
| VLM raw next-tool | 18 / 18 / 18 (100.0%) |
| System fused next-tool | 16 / 16 / 18 (88.9%) |
| BT decision audit | 65 / 65 passed; blockers=0, suspicious=0 |

## Per-Procedure Accuracy

| Procedure | VLM raw phase | System fused phase | VLM raw tool | System fused tool | VLM avg/p95 latency | VLM samples |
|---|---:|---:|---:|---:|---:|---:|
| Thyroidectomy (갑상선절제술) | 31 / 33 / 33 (93.9%) | 33 / 33 / 33 (100.0%) | 7 / 7 / 7 (100.0%) | 5 / 5 / 7 (71.4%) | 1.479s / 2.343s | 42 |
| Nephrectomy (신장절제술) | 40 / 40 / 40 (100.0%) | 40 / 40 / 40 (100.0%) | 5 / 5 / 5 (100.0%) | 5 / 5 / 5 (100.0%) | 1.465s / 1.875s | 40 |
| Inguinal hernia repair (서혜부 탈장술) | 41 / 41 / 41 (100.0%) | 41 / 41 / 41 (100.0%) | 6 / 6 / 6 (100.0%) | 6 / 6 / 6 (100.0%) | 1.428s / 1.753s | 41 |

## BT Decision Audit

| Procedure | Audited decisions | Passed | Blockers | Suspicious | Decision counts | Skill actions |
|---|---:|---:|---:|---:|---|---|
| Thyroidectomy | 23 | 23 | 0 | 0 | anticipatory_handover:1, explicit_request:4, hold:2, idle:16 | direct_handover:3, pick_up_and_handover:1, predict_tool:3, retrieve_from_mayo:2 |
| Nephrectomy | 21 | 21 | 0 | 0 | anticipatory_handover:3, explicit_request:3, hold:2, idle:12, recovery:1 | direct_handover:2, pick_up_and_handover:1, predict_tool:3, put_down_and_handover:1, retrieve_from_mayo:2 |
| Inguinal hernia repair | 21 | 21 | 0 | 0 | anticipatory_handover:2, explicit_request:3, hold:1, idle:14, recovery:1 | direct_handover:2, pick_up_and_handover:2, predict_tool:2, retrieve_from_mayo:2 |

## Interpretation

- Phase 판단은 reducer/system fusion이 raw VLM의 transient mismatch를 흡수했다. Thyroidectomy에서 raw VLM은 phase transition cue 직후 P02/P03을 두 번 앞당겨 예측했지만, system fused phase는 전 샘플 정답이었다.
- 다음 도구 예측은 VLM raw가 이번 run의 평가 가능 샘플에서 모두 맞았다. System fused tool은 thyroidectomy에서 2개 샘플을 의도적으로 제안하지 않아 overall accuracy가 낮아졌지만, 제안한 16개는 모두 정답이었다. 즉 문제는 wrong prediction보다 coverage/latency 쪽이다.
- BT decision audit는 최종 정책 기준에서 모든 audited decision을 통과했다. 회수 action은 정상 flow에서 `retrieve_from_mayo`로만 나타났고, explicit/anticipatory/recovery branch에서 lifecycle/guard 위반은 발견되지 않았다.
- 입력 영상 stream은 약 30 Hz로 유지되었고, VLM 평균 latency는 약 1.43-1.48초, p95는 약 1.75-2.34초였다.

## Evaluator Notes

- `SmokeHarness`의 오래된 “집도의 도구 1개 초과 보유 금지” 기준은 현재 정책인 “최대 2개 보유 가능”과 맞지 않아 `>2` 기준으로 수정했다.
- `bt_audit.report_for()`가 `decision_counts`를 참조로 반환하던 버그를 수정해 bundle별 decision count가 독립적으로 기록되도록 했다.

## Figures

![Prediction accuracy](/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/prediction_accuracy_by_procedure.png)

![Tool precision and coverage](/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/tool_prediction_precision_coverage.png)

![BT decision audit](/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/bt_decision_audit_result.png)

![Runtime observability](/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/runtime_observability.png)

## Generated Files

- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/multi_bundle_runtime_probe.json`
- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/bt_decision_audit_after_policy_fix.json`
- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/accuracy_and_bt_summary.csv`
- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/bt_decision_counts.csv`
- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/prediction_accuracy_by_procedure.png`
- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/tool_prediction_precision_coverage.png`
- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/bt_decision_audit_result.png`
- `/home/arl/Documents/ARPA-H/taskplanner_ws/reports/eval_20260624_105505/runtime_observability.png`

## Caveats

- 이 평가는 LLM surgeon actor 기반 검증이다. 실제 수술 영상 기반 일반화 성능은 별도 데이터셋 평가가 필요하다.
- Tool prediction의 evaluable sample은 “현재 요청 이후, 다음 요청까지 충분한 lead time이 있고 interrupt/phase transition/mayo-visible ambiguity가 없는 구간”만 포함한다.
- BT decision correctness는 런타임 world state와 정책 guard에 대한 rule audit 결과다. 사람 평가가 필요한 임상적 적합성까지 검증한 것은 아니다.
