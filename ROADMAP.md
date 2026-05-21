# Taskplanner Priorities

## Current Phase

The workspace is now in the simulation-polish and interaction-reliability phase.
The main runtime path is expected to look like this:

- a selected surgery bundle drives perception, surgeon scripting, scene layout, and policy
- the BT continues to publish `/bt/skill_command`
- the action bridge converts those commands into ROS 2 action goals on `/skill/execute`
- the mock action server publishes `/skill/status` and `/skill/events`
- the digital twin publishes `/simulation/state` and `/simulation/event`
- the standalone webapp is the primary demo surface, with btops and Foxglove as supporting observability tools

## Priority Order

1. Interaction reliability
Keep `Start`, `Stop`, `Reset`, bundle switching, and surgeon override controls stable under repeated use from the webapp.

2. Physical coherence
Preserve one-tool-per-hand, cleaner hold-time, contaminated-tool return, and rack/home-slot invariants while the BT runs continuously.

3. Demo readability
Make surgeon requests, voice overrides, cleaner countdown, phase monitoring, and arm motion immediately legible in the UI.

4. Adapter boundary preservation
Treat `/skill/execute` as the handoff point for future robot integration, while keeping mock execution available for repeatable demos.

## Completed In This Round

- headless Chromium verification of the real webapp buttons
- generic field naming so `thyroidectomy` and `nephrectomy` no longer share a hardcoded label
- visible arm motion and cleaner countdown in the scene canvas
- persistent override state so `Request Tool`, `Voice Override`, and `Return Tool` are actually visible in the UI
- action/status reconciliation so the session panel no longer shows stale reset text after the simulation is running
- human-readable runtime event cards instead of placeholder-heavy `? -> ?` entries
- repeated invariants checks confirming no multi-tool hand occupancy and no contaminated tool directly returning to the rack

## Next Remaining Work

1. Motion polish
Upgrade the current anchor-to-anchor arm movement into smoother multi-segment reach arcs and clearer surgeon hand gestures.

2. Visual density reduction
Tighten scene chip collision handling and trim low-value labels so busy moments stay readable without zooming.

3. Regression automation
Promote the current browser and invariant checks into a repeatable automated verification script for demo readiness.

4. Demo narration polish
Add a slightly richer scene/event narration layer so key state changes are understandable without reading the runtime strip.
