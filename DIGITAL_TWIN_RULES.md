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

1. Empty-hand extension toward the receive zone is a handover cue. It can be
   produced by VLM evidence, manual override, or the LLM surgeon actor.
2. The normal used-tool return path is not direct hand retrieval. The surgeon
   places used or reusable tools on the Mayo stand.
3. `request_tool` may be explicit voice, implicit hand extension, or voice+hand.
4. Voice requests may override an anticipatory/prepositioned tool.
5. A voice request must be visible in the UI immediately as active spoken intent.
6. A Mayo-placed tool is assumed used/contaminated unless explicitly modeled otherwise.
7. `retrieve_from_hand` is legacy/manual-only and must not be dispatched by the
   normal BT recovery branch.
8. A floor-dropped tool is not a robot recovery target. Robot dispatch must hold
   until a human recovery event removes the contaminated tool from the field and
   either starts cleaning or provides a sterile replacement.
9. A valid explicit voice request or implicit handover cue may request a tool
   already on Mayo. The right arm then executes
   `pick_up_from_mayo_and_handover` instead of treating the tool as unavailable.

## VLM Retrieval Inference Rules

1. VLM may run in real, mock, or dual mode, but the default integration path is
   real VLM mode.
2. Real VLM mode uses the selected procedure bundle's
   `vlm_procedure_prompt.yaml` as the procedure prompt asset. The asset may use
   compact Pxx/Txx ids, but runtime JSON, reducer decisions, and BT decisions
   must use the active bundle's canonical phase/tool ids.
3. Return-cue confidence is probabilistic and must combine:
   - current phase context
   - whether the tool is still expected in that phase
   - the tool's observed location (`mayo_recovery_zone`, `return_zone`, field, reuse zone)
   - recent tool history on surgeon-side locations
   - observed hand pose
   - phase uncertainty
4. The VLM context must contain only public evidence that could exist in the
   real system: image/overlay cues, voice transcript, visible Mayo tools,
   digital-twin public events, skill status, and BT context. It must not contain
   hidden LLM actor ground truth or YAML-derived answer hints.
5. VLM reports a single visible Mayo stand plus `mayo` reuse/recover
   assessments and a top `mayo_retrieve` candidate.
6. `mayo_retrieve` requires confidence >= 0.5 for at least 5 seconds before the
   reducer may promote a tool to recovery.
7. A same-tool `reuse` assessment with confidence >= 0.5 for at least 5 seconds
   suppresses recovery promotion.
8. VLM `tool` prediction requires confidence >= 0.8 for at least 3 seconds before
   BT may dispatch `predict_tool`.
9. Stabilization must suppress one-frame noise; transient raw cues must not directly
   become BT-visible intent.
10. Schema v4 keeps semantic `intent` separate from visual-only `gesture`.
    `gesture=["request_tool", tool_id, "open_receive", confidence]` is valid only
    when the current raw CAM4 pixels clearly show an empty open palm extended
    toward the assistant. Speech, procedure order, detector text, prior output,
    and next-tool candidates must not fabricate this field.
11. RF-DETR remains advisory. With object recognition disabled, raw CAM4 pixels
    may still establish visual gesture evidence, but the reducer accepts one
    request per gesture episode only after confidence and temporal stability
    checks and agreement with an independently stabilized or prepositioned next
    tool. Ambiguous tool identity remains observation-only.

## Sentence-only Degraded Operation

1. `/surgery/audio/request_text` is admitted public sentence evidence and may
   directly create a canonical explicit tool request when the sentence contains an active
   procedure instrument and command intent.
2. Procedure-defined suction/retraction utterances are reserved for their bed
   robot-arm group and must not also become tool-handover requests.
3. A sentence-backed explicit request may bypass only `vlm_unhealthy` and phase
   uncertainty. Every physical-state, contamination, ownership, capacity,
   readiness, and active-task guard remains mandatory.
4. VLM absence must disable autonomous phase inference, next-tool prediction,
   and probabilistic Mayo recovery. It must not stop explicit voice handover.
5. Duplicate transcript and structured request messages for the same pending
   tool are coalesced into one request.

## Contamination and Cleaning Rules

1. Any instrument that reaches the surgeon is considered used.
2. A used instrument is contaminated and must not be returned directly to the rack.
3. The normal recovery chain for a used tool is:
   - surgeon hand
   - Mayo stand
   - robot left hand
   - cleaner hold
   - robot left hand
   - home rack slot
4. The floor-drop chain is:
   - surgeon/robot/Mayo side
   - floor zone
   - human recovery
   - cleaner, removed-from-field, or sterile replacement flow
5. Cleaning takes time and is not instantaneous.
6. During cleaning, the left arm remains occupied and the cleaner timer must count down.
7. After cleaning completes, the instrument may be returned to the rack.
8. The robot may only enter `retracted` when both hands are empty and the cleaner is idle.

## Mayo Stand Semantics

1. The VLM-facing scene presents a single Mayo stand and a visible list of tools.
2. Internally, `mayo_reuse_zone` means the tool is parked on Mayo but not yet
   confirmed for robot pickup.
3. Internally, `mayo_recovery_zone` means the reducer has promoted the tool for
   robot pickup.
4. The overlay must not expose `reuse` or `recover` labels to VLM; those are VLM
   judgments, not visual ground truth.
5. A tool on Mayo becomes a valid recovery candidate only after stable VLM
   `mayo_retrieve` evidence or procedure-completion cleanup.
6. The internal reuse/recovery distinction does not create separate physical or
   GUI zones. The dashboard renders one Mayo stand and shows the latest VLM-derived
   reuse probability on each tool tag.
7. A handover request for a Mayo tool takes priority over a pending recovery
   candidate when no recovery action has started. Grasping the tool with the right
   hand closes its pending recovery transaction.

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
5. `recovery` in normal flow dispatches `retrieve_from_mayo` only. Direct hand
   retrieval is reserved for legacy/manual test paths.
6. A requested Mayo tool dispatches `pick_up_from_mayo_and_handover` with the
   Mayo location as source, the right arm, and the surgeon receive zone as target.
7. `anticipatory_handover` may only fire when explicit and pending recovery conditions
   are absent and the selected tool comes from stable VLM next-tool prediction
   (`confidence >= 0.8` for at least 3 seconds).
8. Reset returns the simulation to idle and clears transient execution state.
9. Reset must not auto-start the BT.
10. Switching bundles while stopped must not require relaunching the workspace.

## UI Consistency Rules

1. The scene canvas should show one tool chip per physical tool.
2. The scene canvas should not show multiple tool chips on one hand unless that is an
   intentionally supported physical behavior, which is currently not allowed.
3. The current phase must always be visible in the UI.
4. The active spoken request must be visible in the UI when present.
5. The cleaner countdown must be visible whenever cleaning is in progress.
6. Mayo tools must share one panel; recovery/reuse columns must not be rendered.
7. Each Mayo tool tag must show its latest VLM-derived reuse probability, or `--`
   when no current Mayo assessment exists.

## Debugging Acceptance Checks

When validating a run, the following must remain true:

- no hand contains more than one tool
- `robot_state=retracted` never appears while a hand or cleaner still carries a tool
- no contaminated tool appears on the rack
- every used tool eventually reaches cleaner and then home rack slot
- reset returns to idle without auto-restarting
- bundle switching works without relaunch
- the UI scene matches the digital twin ownership/location state
