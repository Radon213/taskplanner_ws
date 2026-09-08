# tool_belief_tracker

Active procedure의 `tool_population`과 `tool_placement`가 정한 타입/capacity를
상한으로만 사용한다. 운영자는 Start 직전 화면에서 확인한 tool-belief의 타입,
수량, 위치를 승인하고, `running` 전이 순간 그 live belief가 이 run의 immutable
population prior가 된다. 따라서 실행 후 CAM3/4 관측은 고정 slot의 **위치확률만**
갱신하며, detector class나 뒤늦게 등장한 type은 audit count에 남기고 run의 도구
목록을 추가·삭제하지 않는다.
Digital Twin, admission, Action goal 또는 물리 제어는 변경하지 않는다.

프로시저가 `exchangeable_population=true`로 선언한 타입에 대해 운영 기본값
`exchangeable_instances=true`는 `T04#1`, `T04#2`를 영구적인 물리 개체 ID가 아니라
scenario inventory 한도 안의 교환 가능한 논리 capacity slot으로 취급한다. 전역 설정은
기능 opt-in일 뿐이므로 legacy fixed population은 동일 타입이 여러 개여도 strict로 남는다.
한 zone에서 같은 타입이 `k`개 검출되면 `n`개 슬롯
모두에 `k/n`을 복제하지 않고, 최대 `k`개의 free slot에 구체적으로 할당한다. Action이
구체 instance ID를 주면 그 ID를 보존하되, 실행 중인 command나 robot/surgeon/cleaner
custody로 잠기지 않은 같은 타입 슬롯끼리 belief label을 교환해 source 위치와 맞춘다.
타입 단위 command/event도 source 위치가 가장 잘 맞는 free slot 하나에 결정적으로
bind한다. 새 타입이나 inventory 한도 밖 슬롯은 만들지 않는다. 한 frame의 known
instrument 검출 수가 capacity를 넘으면 초과분만 `ignored_out_of_inventory_count`에
더하고 `ignored_class_names`에 `<instrument_id>:capacity_overflow`를 남긴다.

교차 zone의 단일 검출은 기존 활성 슬롯을 먼저 이동시킨다. 다른 zone에서
`max(2 * evidence_window_sec, 0.5초)` 이내에 실제 카메라 양성 관측이 있었을 때만 그
슬롯을 동시 존재로 보호하고 휴면 capacity를 활성화한다.

교환 모드의 `existence_probability`는 물리 ID의 존재 확률이 아니라 해당 논리 capacity
slot이 현재 active/present하다는 확률이다. `initial_count`까지만 active/home 위치로
시작하며 `capacity`까지의 나머지 슬롯은 inactive/unknown으로 예약된다. 검출/Action/event는 올리고, 그 슬롯이
있다고 믿던 zone에서의 miss는 `miss_half_life_sec`에 따라 점진적으로 낮춘다. 출력
`status_flags` 계약은 다음과 같다.

- 항상: `logical_instance_exchangeable`, `physical_identity_not_asserted`
- activity 구간: `capacity_slot_active` (`>= confirm_threshold`),
  `capacity_slot_probable` (`>= probable_threshold`), 그 미만은
  `capacity_slot_inactive`
- 할당 이력: `exchangeable_slot_assigned`; Action ID를 source slot으로 permutation한
  경우 `exchangeable_slot_rebound`

순수 Python `TrackerConfig()`는 기존 strict 동작을 보존하기 위해 false가 기본이며,
ROS node와 `config/default.yaml`은 true를 기본으로 사용한다. strict 모드는 기존처럼
모호한 타입 단위 command/event를 거부하고 대칭 identity marginal을 유지한다.
단, `capacity > initial_count`인 프로시저는 observer-only 슬롯을 strict DT identity로
오인하지 않도록 startup/reload/hot parameter에서 `exchangeable_instances=false`를 거부한다.

기본 출력은
`/surgery/perception/tool_beliefs`
(`surgical_perception_msgs/msg/TrackedToolBeliefArray`)이다.

`idle` 상태에서도 health와 scenario identity는 수신한다. 실제 `running` 전이에서는
post-start detector capture를 수행하지 않는다. 대신 이미 표시되어 운영자가 검토한
live belief를 즉시 run prior로 동결한다. `_state_sync_ready`는 node warm restart 뒤
authored baseline이 현재 inventory로 오인되지 않은 상태에서만 이 동결을 허용한다.

CAM3/4 positive evidence는 `positive_gain × elapsed_time × detector_confidence`
로 적분된다. `WorldState.cam4_mayo_hand_present`와 robot motion은 positive/miss
gain을 낮추는 soft context일 뿐 관측이나 commit을 차단하지 않는다. 손·로봇 가림
중 Mayo/Tray에서 보이지 않는 도구는 `unknown` 쪽으로만 이동하며, surgeon/robot
custody는 Action 결과나 명시 semantic event로만 강하게 올라간다.

## 실행 중 켜기/끄기

`enabled`는 기본값 `true`인 runtime-mutable boolean parameter다. UI와 기타 운영
client는 다음 표준 계약을 사용한다.

- Service: `/surgery/perception/tool_beliefs/set_enabled`
  (`std_srvs/srv/SetBool`)
- Latched state: `/surgery/perception/tool_beliefs/enabled`
  (`std_msgs/msg/Bool`, reliable + transient-local, depth 1)

`false`에서는 CAM3/4 및 skill/action evidence 반영과 belief snapshot 발행을 멈춘다.
health와 `SimulationState` subscription은 유지하므로 현재 scenario identity, stopped
reload gate와 active-bundle self-heal은 계속 유효하다. `true`로 바뀔 때에는 disable
전 belief를 재사용하지 않는다. scenario-capacity tracker를 새로 만들고
`state_sync_ready=false`로
돌린 뒤, 다음 complete matching `SimulationState`에서 재수화된 후에만 다시 발행한다.
동일 값 재요청은 idempotent하며 이미 enabled인 tracker를 초기화하지 않는다.

```bash
ros2 service call /surgery/perception/tool_beliefs/set_enabled \
  std_srvs/srv/SetBool '{data: false}'
ros2 topic echo /surgery/perception/tool_beliefs/enabled \
  std_msgs/msg/Bool --once
ros2 service call /surgery/perception/tool_beliefs/set_enabled \
  std_srvs/srv/SetBool '{data: true}'
```

## 실행 중 튜닝

Input/output topic topology는 node 시작 시 고정되어 restart-required다. 반면 stopped
bundle switch의 `spec_dir`은 새 bundle과 scenario-capacity tracker를 먼저 완전히
구성한 뒤 원자적으로 교체한다. 로드 실패 시 기존 spec, tracker, procedure run을
그대로 유지한다. 추론/시간 파라미터도 원자적으로 검증한 뒤 즉시 교체하므로 전체
Taskplanner restart가 필요 없다.

`spec_root`는 restart-fixed다. Optional tracker가 bundle transaction 중 내려가 있었던
경우 fresh `SimulationState.active_bundle`을 받아 `<spec_root>/<active_bundle>`을 검증한
뒤 self-heal한다. 첫 complete `SimulationState.instrument_states`와 새 run-id에서는
scenario-capacity tracker를 초기화한 뒤 현재 Digital Twin 위치를 read-only로
재수화한다. 노드
단독 warm restart 직후 authored initial placement가 잠시 현재값처럼 보이지 않도록,
complete matching state로 재수화되기 전에는 belief snapshot을 발행하지 않는다.

동적으로 바꿀 수 있는 항목은 다음뿐이다.

- boolean: `enabled`, `exchangeable_instances`
- 양수: `evidence_window_sec`, `max_camera_evidence_dt_sec`,
  `miss_half_life_sec`, `robot_motion_grace_sec`, `mayo_hand_grace_sec`,
  `positive_gain`, `commit_dwell_sec`, `uv_memory_sec`,
  `uv_match_radius_px`, `uv_ambiguity_margin_px`
- 0~1: `robot_motion_positive_scale`, `robot_motion_negative_scale`,
  `mayo_hand_positive_scale`, `mayo_hand_negative_scale`, `confirm_threshold`,
  `probable_threshold`, `commit_release_threshold`, `commit_switch_margin`,
  `identity_assignment_margin`, `uv_relabel_scale`,
  `uv_ambiguous_evidence_scale`
- `publish_period_sec`: 0.02~10초
- `health_freshness_sec`: 0.05~30초

`probable_threshold <= confirm_threshold`를 포함한 상호 제약도 한 batch 전체에
적용된다. 하나라도 잘못되면 model config, timer, health freshness를 모두 기존값으로
유지한다. topic 이름과 `spec_root`는 restart-required다. `spec_dir`은 fresh stopped
`SimulationState`가 확인된 때에만 직접 변경할 수 있고, 정상 운영에서는 Simulation
Manager의 stopped bundle transaction을 사용한다. scenario capacity 변경은 numeric
tuning이 아니라 이 검증된 spec transaction으로만 수행한다.

```bash
ros2 param list /tool_belief_tracker
ros2 param describe /tool_belief_tracker miss_half_life_sec
ros2 param set /tool_belief_tracker miss_half_life_sec 4.0
ros2 param set /tool_belief_tracker robot_motion_negative_scale 0.05
ros2 run tool_belief_tracker tool_belief_tune --file \
  "$(ros2 pkg prefix tool_belief_tracker)/share/tool_belief_tracker/config/tuning.yaml"
```

`ros2 param load`는 Jazzy에서 여러 parameter의 원자 적용을 보장하지 않으므로 tuning
bundle에는 사용하지 않는다. `tool_belief_tune`은
`SetParametersAtomically`를 한 번 호출한다. `config/tuning.yaml`에는 dynamic 항목만
있고, 한 batch에 invalid 값이 있으면 전체를 거부해 기존 설정을 유지한다. 발행
메시지의 `tracker_revision`에는 dynamic 설정과 실제 spec/inventory digest가 포함된다.
`exchangeable_instances`를 바꾸면 기존 marginal을 새 identity 의미로 재해석하지 않고
tracker만 다시 만든 뒤 fresh `SimulationState` 재수화를 기다린다.

카메라의 `observation_point_uv_px`는 같은 view에서만 1초 동안 기억한다. 한 검출과
한 기존 도구 anchor가 서로 유일하게 가까울 때만 일시적인 클래스 반전 증거를 낮은
가중치로 원래 도구 타입에 반영한다. 두 검출이 겹치거나 복수 anchor와 비슷하게
가까우면 identity를 다시 배정하지 않고 관련 camera evidence를 낮춰 분리될 때까지
확률 상태를 유지한다. UV는 새 도구 생성이나 위치 확정 조건으로 쓰이지 않는다.

Python tracker 구현만 수정했다면 scoped build 뒤 이 독립 node만 재시작하면 된다.
launch는 tracker에 `respawn=True`를 적용하므로 Taskplanner 전체를 내릴 필요가 없다.

```bash
cd /workspaces/taskplanner_ws
source /opt/ros/jazzy/setup.bash
source install/docker/setup.bash
colcon build --build-base build/docker --install-base install/docker \
  --symlink-install --packages-select tool_belief_tracker
```

반면 `.msg` ABI를 수정한 경우에는 `surgical_perception_msgs`와 consumer를 scoped
rebuild하고 stopped-state coordinated restart가 필요하다. 이를 node-only Python hot
restart와 같은 것으로 취급하지 않는다.
