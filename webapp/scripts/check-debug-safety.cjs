const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const bridge = fs.readFileSync(
  path.join(root, "src", "hooks", "useIntegrationDebugBridge.ts"),
  "utf8",
);
const workspace = fs.readFileSync(
  path.join(root, "src", "components", "debug", "DebugWorkspace.tsx"),
  "utf8",
);
const multicamBridge = fs.readFileSync(
  path.join(root, "src", "hooks", "useMulticamOpsBridge.ts"),
  "utf8",
);
const multicamWorkspace = fs.readFileSync(
  path.join(root, "src", "components", "multicam", "MulticamOpsWorkspace.tsx"),
  "utf8",
);

const violations = [];

if (!bridge.includes('name: "/integration/debug/readiness"')) {
  violations.push("Debug UI must subscribe to the debug-namespaced readiness topic");
}
if (bridge.includes('name: "/integration/readiness"')) {
  violations.push("Debug UI must not subscribe to the live readiness topic");
}
if (bridge.includes('name: "/sensors/surgeon/sentence"')) {
  violations.push("Debug UI must not publish surgeon sentences directly through ROSBridge");
}
if (!workspace.includes('runCommand("publish_voice_command", { text: normalized })')) {
  violations.push("Manual sentences must pass through the backend debug command gate");
}
if (!workspace.includes("!interventionAllowed || !status.session.armed || !sentence.trim() || sentencePending")) {
  violations.push("Manual sentence submission must be disabled while manual control is disarmed");
}
if (!workspace.includes('disabled={!connected || !interventionAllowed || !armed || pending} onClick={() => void invoke("publish_once"')) {
  violations.push("One-shot dummy output must require the backend intervention gate and armed manual control");
}
if (!workspace.includes("(!row.enabled && (!interventionAllowed || !armed || !validRate))")) {
  violations.push("Starting continuous dummy output must require the backend intervention gate and armed manual control");
}
if (!workspace.includes('disabled={!connected || !enabledCount} onClick={() => void runCommand("stop_outputs")}')) {
  violations.push("The emergency stop-all output path must remain available while disarmed");
}
if (!workspace.includes('row.enabled ? "\uc815\uc9c0" : "\uc5f0\uc18d \ubc1c\ud589"')) {
  violations.push("Per-topic stop controls must remain rendered for active outputs");
}
if (!workspace.includes("const networkLocked = network.locked_to_runtime === true")) {
  violations.push("Debug DDS controls must consume the runtime network-lock status");
}
if (!workspace.includes("disabled={!connected || networkLocked}")) {
  violations.push("Debug DDS discovery and Domain controls must be disabled when runtime-locked");
}
if (!workspace.includes("disabled={!connected || networkLocked || !changed")) {
  violations.push("Debug DDS apply must be disabled when runtime-locked");
}
if (!bridge.includes("manual_control_available?: boolean")) {
  violations.push("Debug status must type the backend operational manual-control interlock");
}
if (!bridge.includes("operational_intervention_allowed?: boolean")) {
  violations.push("Debug status must type the authoritative operational intervention gate");
}
if (!bridge.includes("operational_control_window_open?: boolean")) {
  violations.push("Debug status must type the retained control window for an admitted command");
}
if (!bridge.includes("startsOperationalIntervention(operation, payload)")) {
  violations.push("The Debug command client must gate intervention-starting operations before transport");
}
if (!bridge.includes('"vlm_load",')) {
  violations.push("Shared VLM load must use the operational intervention gate");
}
if (!workspace.includes('"운영 시나리오 일시정지 · 신규 개입 가능"')) {
  violations.push("Debug UI must identify a paused operational intervention window");
}
if (!workspace.includes('"운영 시나리오 완전 정지 · 신규 개입 가능"')) {
  violations.push("Debug UI must identify a fully stopped operational intervention window");
}
if (!workspace.includes('"관찰 가능 · 개입 잠김"')) {
  violations.push("Debug UI must keep observation visible while intervention is fail-closed");
}
if (workspace.includes("planner_coexistence_confirmed") || workspace.includes("acknowledged_blocked_nodes")) {
  violations.push("Debug UI must not replace the operational intervention gate with planner acknowledgement");
}
if (!workspace.includes('return "수동 잠금 · Fault"')) {
  violations.push("Debug UI must distinguish a stopped scenario from a Fault-based manual lock");
}
if (!workspace.includes("bridge.status.runtime.manual_control_available !== true")) {
  violations.push("Arming must stay disabled unless the backend interlock explicitly allows it");
}
if (!workspace.includes("bridge.status.session.armed\n          ? !bridge.connected")) {
  violations.push("Disarming must remain available if the operational interlock closes");
}
if (!workspace.includes("readOnlySession={bridge.readOnlySession}")) {
  violations.push("Embedded Multicam must receive the existing Debug read-only ROS session");
}
if (!multicamWorkspace.includes("readOnlySession?: DebugReadOnlyRosSession")) {
  violations.push("Multicam workspace must expose only the bounded read-only session adapter");
}
if (!multicamBridge.includes("if (readOnlySession)")) {
  violations.push("Embedded Multicam must branch onto the shared Debug observer session");
}
if (!multicamBridge.includes("readOnlySession.subscribeTopic")) {
  violations.push("Embedded Multicam must subscribe through the shared read-only adapter");
}
if ((multicamBridge.match(/new ROSLIB\.Ros\(\)/g) || []).length !== 1) {
  violations.push("Multicam may create exactly one ROSLIB session for its standalone path only");
}
if (!bridge.includes('service: "/rosapi/topics"')) {
  violations.push("The Debug read-only adapter must use the allowlisted topic inventory service");
}
if (!multicamBridge.includes("World Anchor 서비스 호출을 전송하지 않았습니다.")) {
  violations.push("Multicam World Anchor mutation must remain unavailable");
}

if (violations.length) {
  console.error("Debug safety guard failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Debug safety guard passed.");
