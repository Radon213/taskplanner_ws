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
if (!workspace.includes("!status.session.armed || !sentence.trim() || sentencePending")) {
  violations.push("Manual sentence submission must be disabled while manual control is disarmed");
}
if (!workspace.includes('disabled={!connected || !armed || pending} onClick={() => void invoke("publish_once"')) {
  violations.push("One-shot dummy output must be disabled while manual control is disarmed");
}
if (!workspace.includes("(!row.enabled && (!armed || !validRate))")) {
  violations.push("Starting continuous dummy output must require armed manual control");
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
if (!workspace.includes('"운영 시나리오 정지 확인"')) {
  violations.push("Debug UI must identify a confirmed stopped operational scenario");
}
if (!workspace.includes('"운영 시나리오 실행/상태 불명"')) {
  violations.push("Debug UI must visibly fail closed for a running or unknown scenario");
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

if (violations.length) {
  console.error("Debug safety guard failed:");
  for (const violation of violations) console.error(`- ${violation}`);
  process.exit(1);
}

console.log("Debug safety guard passed.");
