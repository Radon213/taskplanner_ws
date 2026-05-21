# Digital Twin Operational Rules

## Purpose

This document defines the minimum physical and state-consistency rules that the
taskplanner digital twin, mock simulation, and behavior tree must satisfy.
These rules are the source of truth for debugging runtime behavior.

## Core Modeling Rules

1. Every instrument has exactly one current owner and one current location.
2. Every instrument must appear in exactly one of these mutually exclusive places:
   - rack slot
   - mayo reuse zone
   - mayo recovery zone
   - robot right hand
   - robot left hand
   - surgeon hand
   - surgical field
   - cleaner
3. A single humanoid hand can hold at most one instrument at a time.
4. A single surgeon hand can hold at most one instrument at a time.
5. The same instrument must never be rendered or tracked in two places at once.

## Humanoid Arm Rules

1. The right arm is the default handover/preposition arm.
2. The left arm is the default recovery/cleaning/return arm.
3. If the left arm is recovering a tool, it is occupied until the tool is either:
   - inserted into the cleaner and held there, or
   - returned to its home rack slot.
4. If the left arm is holding a tool in the cleaner, that arm is unavailable for any
   other recovery or cleaning action.
5. If the right arm is already holding a tool, no second tool may be picked or
   prepositioned into that same hand before the current tool is either handed over
   or returned.

## Surgeon Interaction Rules

1. Empty-hand extension toward the receive zone is produced by VLM hand-cue evidence
   and becomes handover readiness only after stabilization in `mock_surgeon`.
2. Extension with a used tool toward the return zone is produced by VLM hand-cue
   evidence and becomes recovery readiness only after stabilization in `mock_surgeon`.
3. `request_tool` and `return_tool` intents must not be independently scripted outside
   the VLM cue path; `mock_surgeon` only owns voice, override, and cue fusion.
4. Voice requests may override an anticipatory/prepositioned tool.
5. A voice request must be visible in the UI immediately as active spoken intent.
6. A returned tool is assumed used/contaminated unless explicitly modeled otherwise.

## VLM Retrieval Inference Rules

1. `mock_vlm` is the single raw producer of `SurgeonGestureEvidence`.
2. Return-cue confidence is probabilistic and must combine:
   - current phase context
   - whether the tool is still expected in that phase
   - the tool's observed location (`mayo_recovery_zone`, `return_zone`, field, reuse zone)
   - recent tool history on surgeon-side locations
   - observed hand pose
   - phase uncertainty
3. A tool in `mayo_recovery_zone` should increase recovery probability.
4. A tool in `mayo_reuse_zone` should decrease recovery probability unless another
   cue strongly indicates return.
5. Context-only inference may synthesize a `return_tool` cue when a tool appears in a
   strong recovery location even if a perfect hand cue is not visible.
6. Stabilization must suppress one-frame noise; transient raw cues must not directly
   become BT-visible intent.

## Contamination and Cleaning Rules

1. Any instrument that reaches the surgeon is considered used.
2. A used instrument is contaminated and must not be returned directly to the rack.
3. The only valid recovery chain for a used tool is:
   - surgeon hand or return zone
   - robot left hand
   - cleaner hold
   - robot left hand
   - home rack slot
4. Cleaning takes time and is not instantaneous.
5. During cleaning, the left arm remains occupied and the cleaner timer must count down.
6. After cleaning completes, the instrument may be returned to the rack.
7. The robot may only enter `retracted` when both hands are empty and the cleaner is idle.

## Mayo Stand Semantics

1. The mayo stand is split into two semantic sub-zones:
   - humanoid-adjacent `mayo_recovery_zone`
   - surgeon-adjacent `mayo_reuse_zone`
2. `mayo_recovery_zone` means the tool is waiting for robot pickup.
3. `mayo_reuse_zone` means the tool is temporarily parked for likely continued surgeon-side use.
4. VLM must report which mayo sub-zone a tool occupies, not just "mayo stand".
5. A tool in `mayo_recovery_zone` is a valid recovery candidate.
6. A tool in `mayo_reuse_zone` is not automatically a recovery candidate unless a separate return cue or event reclassifies it.

## Rack and Home-Slot Rules

1. Every instrument has exactly one configured home slot in the procedure bundle.
2. When a clean tool is returned to storage, it must go to its configured home slot.
3. An unused prepositioned tool may be returned directly to its home slot without cleaning.
4. A contaminated tool must never be shown on the rack.

## BT and Runtime Consistency Rules

1. BT decisions must respect hand occupancy before publishing a skill command.
2. Recovery must not select a second tool while the left arm is already occupied.
3. Handover must not select a second right-hand tool while the right hand is occupied.
4. `explicit_request` may only fire when a stabilized VLM request or voice override
   is active for a valid tool.
5. `recovery` may only fire when a surgeon-presented tool or active left-arm cleaning
   pipeline exists.
6. `anticipatory_handover` may only fire when explicit and pending recovery conditions
   are absent and the selected tool is phase-appropriate.
7. Reset returns the simulation to idle and clears transient execution state.
8. Reset must not auto-start the BT.
9. Switching bundles while stopped must not require relaunching the workspace.

## UI Consistency Rules

1. The scene canvas should show one tool chip per physical tool.
2. The scene canvas should not show multiple tool chips on one hand unless that is an
   intentionally supported physical behavior, which is currently not allowed.
3. The current phase must always be visible in the UI.
4. The active spoken request must be visible in the UI when present.
5. The cleaner countdown must be visible whenever cleaning is in progress.

## Debugging Acceptance Checks

When validating a run, the following must remain true:

- no hand contains more than one tool
- `robot_state=retracted` never appears while a hand or cleaner still carries a tool
- no contaminated tool appears on the rack
- every used tool eventually reaches cleaner and then home rack slot
- reset returns to idle without auto-restarting
- bundle switching works without relaunch
- the UI scene matches the digital twin ownership/location state
