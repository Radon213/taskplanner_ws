# Plan2 Alignment Review

- Review basis: [Plan2.md](/mnt/c/Users/skado/Downloads/Plan2.md)
- Workspace: `/home/arl/taskplanner_ws`
- Validation run: `2026-04-22 23:01 KST` / evidence root [20260422_230106](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106)

## Summary

이번 설계 변경은 구조적으로는 타당하고, 핵심 의도도 상당 부분 잘 반영됐다. 특히 `lifecycle_stage`와 `next_required_transition`이 실제 runtime, BT blackboard, message payload, frontend badge에 모두 연결되어 있어, Plan2가 의도한 "lifecycle 중심 단일 truth"는 거의 완성형에 가깝다.

다만 Plan2의 가장 강한 문장 하나는 아직 완전히 닫히지 않았다. twin은 skill event 기반 전이에는 합법성 검사를 적용하지만, observation 기반 전이에는 여전히 direct rebase를 허용한다. 그래서 현재 구현은 "주요 동작은 lifecycle graph를 따른다" 수준에는 도달했지만, "모든 observation/skill event가 lifecycle legality gate를 통과해야 한다" 수준까지는 아니다.

## Validation Result

| Check | Result | Evidence |
| --- | --- | --- |
| `npm run build` | `PASS` | [precheck_webapp_build.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_b/precheck_webapp_build.txt) |
| `smoke_test --spec-name thyroidectomy` | `PASS` | [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/smoke_thyroidectomy.log) |
| `smoke_test --spec-name nephrectomy` | `PASS` | [smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/smoke_nephrectomy.log) |
| `manual_probe --spec-name thyroidectomy` | `PASS` | [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/manual_probe_thyroidectomy.log) |
| `manual_probe --spec-name nephrectomy` | `PASS` | [manual_probe_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/manual_probe_nephrectomy.log) |
| `taskplanner_bt_audit` | `PASS` / `blockers=0 suspicious=0` | [bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/bt_audit_all_bundles.log), [bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/bt_audit_thyroidectomy.json), [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/bt_audit_nephrectomy.json) |
| Playwright regression | `PASS` | [playwright_bundle_verify.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_b/playwright_bundle_verify.json) |
| Focused lifecycle UI review | `PASS` | [focused_lifecycle_review.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_b/focused_lifecycle_review.json), [mayo_recovery_detected.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/screenshots/mayo_recovery_detected.png) |

## Plan2 Verdict

| Review Axis | Verdict | Evidence | Review |
| --- | --- | --- | --- |
| Lifecycle source of truth | `Acceptable Deviation` | [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:35), [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:366), [node.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/node.py:68), [taskplanner_bt_nodes.cpp](/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp:289), [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:200) | `lifecycle_stage`와 `next_required_transition`이 twin에서 계산되고 `WorldState`, `SimulationState`, BT blackboard, frontend badge까지 그대로 전달된다. 다만 observation rebase가 legality gate를 우회하는 점 때문에 "완전한 단일 gate"라고 보기는 어렵다. |
| Allowed / prohibited transitions | `Risky Deviation` | [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:286), [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:319), [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:527) | `ALLOWED_EVENT_TRANSITIONS`와 `_apply_event_transition()`은 잘 구현됐고 illegal skill transition도 violation으로 막는다. 하지만 observation path는 `_apply_observation_rebase()`로 바로 lifecycle을 재설정하므로, Plan2가 의도한 "observation/skill event 모두 legality 검사"에는 아직 못 미친다. |
| Lifecycle-guarded BT decisions | `Aligned` | [surgical_assist_v1.xml](/home/arl/taskplanner_ws/src/taskplanner_bt_trees/behavior/surgical_assist_v1.xml:9), [taskplanner_bt_nodes.cpp](/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp:189), [taskplanner_bt_nodes.cpp](/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp:494), [taskplanner_bt_nodes.cpp](/home/arl/taskplanner_ws/src/taskplanner_bt_nodes/src/taskplanner_bt_nodes.cpp:717), [bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/bt_audit_all_bundles.log) | explicit / anticipatory / recovery / idle이 lifecycle과 `next_required_transition`을 중심으로 고른다. `idle_while_request_pending`도 최신 audit에서 사라졌고, mispredicted preposition return과 contaminated recovery flow도 하네스에서 다시 닫혔다. |
| VLM / mock / frontend 표현 정합성 | `Acceptable Deviation` | [vlm_node.py](/home/arl/taskplanner_ws/src/vlm_node/vlm_node/node.py:87), [mock_surgeon.py](/home/arl/taskplanner_ws/src/simulation_runtime/simulation_runtime/mock_surgeon.py:289), [App.tsx](/home/arl/taskplanner_ws/webapp/src/App.tsx:537), [mayo_recovery_detected.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/screenshots/mayo_recovery_detected.png), [thyroidectomy_scene.png](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/screenshots/thyroidectomy_scene.png) | VLM은 `mayo_reuse_zone`, `mayo_recovery_zone`, recent surgeon-side history를 분리해서 점수화하고, frontend도 `SURGEON-OWNED`, `MAYO REUSE`, `MAYO RECOVERY`, `RECOVER LEFT`를 직접 보여준다. 다만 `mock_surgeon`은 여전히 optional random voice request를 발생시켜, Plan2의 "request/return 생성자 아님" 문장을 조금 느슨하게 구현한다. |
| Validation / audit criteria | `Acceptable Deviation` | [smoke_test.py](/home/arl/taskplanner_ws/src/bringup/bringup/smoke_test.py:190), [bt_audit.py](/home/arl/taskplanner_ws/src/bringup/bringup/bt_audit.py:182), [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/manual_probe_thyroidectomy.log), [manual_probe_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260422_230106/track_a/manual_probe_nephrectomy.log) | 현재 검증 세트는 contaminated tool direct rack return, pending recovery 중 idle, mispredicted preposition return, explicit/recovery/cleaner cycle을 꽤 잘 잡는다. 다만 observation-originated illegal rebase를 별도 audit finding으로 닫는 규칙은 아직 없다. |

## Risky Deviation

### `RD-001` Observation rebase가 lifecycle legality gate를 우회함

- Related Plan2 item:
  `디지털 트윈은 observation/skill event를 받을 때 먼저 현재 lifecycle에서 합법 전이인지 검사하고, 불법 전이는 상태를 바꾸지 않고 invariant violation으로 기록한다.`
- Current implementation:
  skill event는 [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:286) 의 `_apply_event_transition()`을 통해 `ALLOWED_EVENT_TRANSITIONS`를 검사한다.
  observation은 [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:319) 의 `_apply_observation_rebase()`로 직접 lifecycle을 재설정하고, [twin.py](/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py:527) 의 `reconcile_observation()`도 `_transition_allowed()`를 호출하지 않는다.
- Why risky:
  perception noise가 있으면 `home_rack -> surgeon_owned`, `mayo_reuse -> mayo_recovery`, `surgeon_owned -> returned_home` 같은 금지 edge가 raw observation만으로 수용될 수 있다.
  현재 smoke/manual/audit가 모두 pass하더라도, 그것은 "현 시나리오에서 문제가 안 났다"는 뜻이지 twin이 설계상 모든 illegal observation transition을 막는다는 뜻은 아니다.
- Fix needed:
  `Yes`

## Acceptable Deviation

### `AD-001` `mock_surgeon`이 random voice request를 optional로 유지함

- Related Plan2 item:
  `mock_surgeon은 request/return 생성자가 아니라, voice + override + VLM stabilization만 담당한다.`
- Current implementation:
  [mock_surgeon.py](/home/arl/taskplanner_ws/src/simulation_runtime/simulation_runtime/mock_surgeon.py:374) 에서 `random_voice_enabled=True` 기본값으로 random voice request를 inject할 수 있다.
- Why acceptable:
  직접 lifecycle을 바꾸는 건 아니고, voice noise를 주는 방식이라 시스템 robustness 확인에는 도움이 된다.
  다만 "Plan2 strict mode"로 볼 때는 기본값을 `false`로 두는 편이 더 문서 의도에 가깝다.
- Fix needed:
  `Optional`

### `AD-002` Audit가 observation illegal rebase를 독립 항목으로는 아직 잡지 않음

- Related Plan2 item:
  `도구별 transition graph 이탈 0건`
- Current implementation:
  [smoke_test.py](/home/arl/taskplanner_ws/src/bringup/bringup/smoke_test.py:246) 와 [bt_audit.py](/home/arl/taskplanner_ws/src/bringup/bringup/bt_audit.py:182) 는 world invariant, decision quality, contamination, idle leakage를 잘 본다.
- Why acceptable:
  현재 검증은 운영상 중요한 오류는 충분히 잘 잡는다.
  하지만 transition graph strictness를 Plan2 문구 그대로 주장하려면, observation-originated forbidden edge도 별도 finding으로 표면화하는 편이 더 정확하다.
- Fix needed:
  `Optional`

## Design Feedback

- Plan2 자체는 타당하다.
  특히 `mayo_reuse`와 `mayo_recovery`를 의미적으로 분리한 결정은 BT explainability와 twin readability를 동시에 개선한다.
- 다만 실제 perception이 noisy하다면, observation legality를 완전 hard-block만 할지, 아니면 `quarantine + invariant violation + confidence hysteresis`로 처리할지 설계에서 한 번 더 명시하는 편이 좋다.
  현재 코드가 observation rebase를 느슨하게 둔 것도 이 tradeoff 때문으로 보인다.

## Reflection Quality Feedback

- 반영이 잘 된 부분:
  `BTDecision` payload에 lifecycle / next transition / decision reason / blocking guard가 모두 들어간다.
  frontend가 위치만이 아니라 lifecycle 의미를 직접 렌더링한다.
  `MAYO REUSE`와 `MAYO RECOVERY`가 실제 화면에서 분리돼 보인다.
  `manual_probe`에서 mispredicted preposition tool이 두 번들 모두 rack 복귀로 닫혔다.
  `bt_audit`가 두 번들 모두 `blockers=0 suspicious=0`으로 닫혔다.
- 아직 덜 닫힌 부분:
  twin의 observation path만은 Plan2의 strict lifecycle gate 설계와 완전히 같지 않다.

## Priority

1. `RD-001` 수정
   twin observation path에도 lifecycle legality gate를 넣고, illegal observation transition은 state update 없이 violation으로 기록하도록 닫기
2. audit 보강
   observation-originated illegal transition이 있으면 `bt_audit` 또는 별도 twin audit에서 명시 finding으로 남기기
3. strict Plan2 mode 정리
   `mock_surgeon.random_voice_enabled` 기본값을 `false`로 두는 strict mode를 추가하거나, 현재 default가 의도된 deviation임을 문서화하기

## Update 2026-04-23 00:11 KST

- Evidence root: [/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103)
- Scope: `RD-001` observation-path strict lifecycle gate

### RD-001 Status

`RD-001`은 **핵심 구현 기준으로는 해결**됐다.

이제 twin의 observation 경로도 더 이상 direct rebase를 허용하지 않는다. observation은 먼저 `observed lifecycle -> legality check -> staged accept or ignore` 경로를 타고, 금지 edge는 state truth를 덮어쓰지 않은 채 `ObservationIllegalTransitionIgnored` / `InvariantViolationIgnored`로 기록된다. 즉 Plan2가 요구한 가장 중요한 조건인 **"금지 observation transition을 truth로 승격하지 않는다"** 는 충족한다.

다만 최신 장기 audit에서도 두 번들 모두 `observation_violation_detected=true`는 남아 있다. 이는 strict gate가 실패했다는 뜻이 아니라, nominal mock/VLM 시나리오 안에 아직도 일부 forbidden observation이 섞여 들어온다는 뜻이다. 현재는 그 관측이 twin truth를 오염시키지 않고 안전하게 무시되므로, 운영상 blocker는 아니지만 mock/perception realism 측면의 residual risk는 남아 있다.

### What Changed

- `/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/twin.py`
  - observation 처리 경로를 legality-aware helper로 분리했다.
  - `observed_stage == current_stage`는 즉시 수용한다.
  - 금지 edge는 `ObservationIllegalTransitionIgnored` + `InvariantViolationIgnored`로 기록하고 state update를 막는다.
  - `mayo_recovery` observation은 강한 return context가 있을 때만 수용한다.
  - 일부 stage-changing observation은 candidate streak를 요구해, 1-frame noise가 바로 truth가 되지 않게 했다.
  - event transition이 성공하면 stale observation candidate를 지우게 했다.
- `/home/arl/taskplanner_ws/src/or_digital_twin/or_digital_twin/node.py`
  - `start` 시점에 reset용 `initial_setup`이 아니라 실제 bootstrap scene을 seed하게 바꿨다.
  - reset은 여전히 home slot 초기화를 유지한다.
- `/home/arl/taskplanner_ws/src/vlm_node/vlm_node/node.py`
  - `start` 시 mock VLM이 bootstrap tick에서 바로 시작하게 바꿨다.
- `/home/arl/taskplanner_ws/src/procedure_spec/procedure_spec/query_api.py`
  - reset/home-only stage와 실제 bootstrap stage를 구분하는 helper를 추가했다.
- `/home/arl/taskplanner_ws/src/procedure_spec/procedure_spec/specs/thyroidectomy/mock_perception.yaml`
- `/home/arl/taskplanner_ws/src/procedure_spec/procedure_spec/specs/nephrectomy/mock_perception.yaml`
  - `mayo_reuse`가 surgeon-side temporary park라는 의미에 맞도록, never-used tool이 바로 `mayo_reuse_zone`에 나타나는 stage를 정리했다.
- `/home/arl/taskplanner_ws/src/bringup/bringup/bt_audit.py`
  - observation illegal transition을 decision-quality suspicious로 직접 fail시키는 대신, 별도 audit signal (`observation_violation_detected`, `observation_violation_samples`)로 보고하도록 바꿨다.
  - 따라서 `blockers/suspicious`는 여전히 순수 BT decision quality를 반영하고, observation strictness signal은 별도 field로 남는다.

### Strictness Outcome

| Check | Result | Evidence |
| --- | --- | --- |
| `npm run build` | `PASS` | [webapp_build.txt](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_b/webapp_build.txt) |
| `smoke_test --spec-name thyroidectomy` | `PASS` | [smoke_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/smoke_thyroidectomy.log) |
| `smoke_test --spec-name nephrectomy` | `PASS` | [smoke_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/smoke_nephrectomy.log) |
| `manual_probe --spec-name thyroidectomy` | `PASS` | [manual_probe_thyroidectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/manual_probe_thyroidectomy.log) |
| `manual_probe --spec-name nephrectomy` | `PASS` | [manual_probe_nephrectomy.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/manual_probe_nephrectomy.log) |
| `taskplanner_bt_audit` | `PASS` / `blockers=0 suspicious=0` | [bt_audit_all_bundles.log](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/bt_audit_all_bundles.log), [bt_audit_thyroidectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/bt_audit_thyroidectomy.json), [bt_audit_nephrectomy.json](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/bt_audit_nephrectomy.json) |
| Observation gate signal summary | `signal preserved` | [observation_gate_summary.md](/home/arl/taskplanner_ws/reports/taskplanner_validation_assets/20260423_001103/track_a/observation_gate_summary.md) |

### Review Conclusion Update

- `RD-001`: **Implementation-resolved / runtime-partial**
  - resolved:
    - observation path no longer bypasses lifecycle legality gate
    - illegal observation transition no longer overwrites truth
    - audit keeps an explicit signal for ignored illegal observations
    - smoke/manual/bt_audit remain green
  - remaining gap:
    - nominal mock/VLM still emits a small number of forbidden observations, now safely ignored
    - therefore strict lifecycle preservation is achieved, but perception/mock realism is not yet perfectly aligned with the new semantics

### Remaining Risk

1. `observation_violation_detected=true` still appears in final audit JSON for both bundles.
   - 이 값은 blocker가 아니고, twin이 금지 관측을 안전하게 무시했다는 의미다.
   - 하지만 장기적으로는 mock/VLM scene 자체가 lifecycle semantics와 더 잘 맞도록 다듬는 편이 좋다.
2. bootstrap 이후 일부 anticipatory selection이 여전히 공격적으로 보일 수 있다.
   - 현재는 illegal transition을 막는 것이 우선이며, anticipatory aggressiveness는 별도 tuning 항목으로 남길 수 있다.

### Next Suggested Priority

1. nominal mock perception에서 `ObservationIllegalTransitionIgnored`가 발생하는 stage를 줄이기
2. observation violation을 bundle/stage 단위로 더 정밀하게 집계하는 별도 twin audit 추가
3. lifecycle UI에 `ignored observation` 디버그 뱃지를 optional debug mode로 노출해, scene와 strictness signal을 한 화면에서 같이 볼 수 있게 하기
