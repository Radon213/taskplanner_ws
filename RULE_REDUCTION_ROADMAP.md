# Rule Reduction Roadmap

## Goal

The system should keep hard rules only for physical safety and state
consistency. Procedure-specific judgment should move out of C++/Python control
flow and into procedure specs, VLM evidence, or lightweight policy/ranking
models.

## Rule Categories

### Safety Invariants

Keep these in the Digital Twin and BT guards:

- One physical tool has one owner and one location.
- One robot hand holds at most one tool.
- Contaminated tools never return directly to a rack.
- Unknown tools and paused-state overrides are rejected.
- VLM observations cannot directly rewrite impossible world state.
- Floor-dropped tools stop robot dispatch until a human recovery event arrives.

### Procedure Policies

Move or keep these in procedure bundle assets instead of hard-coded runtime
branches:

- Phase-specific expected tool order.
- Emergency priority tools such as suction during bleeding.
- Mayo reuse/recovery promotion thresholds.
- Procedure-specific completion cleanup priorities.
- Soft limits such as Mayo reuse capacity.

### Experiment Shortcuts

Keep these clearly named as tests or remove them from normal runtime:

- Directly injecting a tray/home tool into `mayo_recovery_zone`.
- Fixed sleep windows as pass/fail evidence.
- Manual phase transition cues used only because a source topic has no active
  subscriber.
- State-backed VLM observations that expose hidden Digital Twin state.

## Near-Term Test Split

`fallingtool` is now reserved for a true dropped-tool safety scenario:

1. A tool is requested and handed over.
2. VLM observes that tool in `floor_zone`.
3. Digital Twin marks the tool `dropped_floor`.
4. `dropped_tool_requires_human` blocks robot skill dispatch.
5. A human recovery actor event removes the contaminated tool from the field and
   records a sterile replacement or later cleaning path.

`mayo_recovery_after_handover` verifies the normal Mayo recovery chain:

1. A tool is requested and handed over.
2. The surgeon places the used tool on Mayo recovery.
3. Digital Twin marks `mayo_recovery`.
4. BT selects `retrieve_from_mayo`.
5. The tool proceeds through cleaner and returns home.

## Isaac Sim Role

Use Isaac Sim first as a perception and safety-test generator:

- Render VLM input images for normal handover, Mayo reuse, Mayo recovery, and
  floor drop cases.
- Export ground-truth tool pose/location labels for reducer evaluation.
- Generate confusing visual cases such as occlusion, wrong tool on Mayo, and
  repeated false recovery suggestions.
- Replay human intervention and resume scenarios.

This keeps the runtime lightweight while still using simulation to broaden
visual coverage beyond one surgery.
